import asyncio
import logging
import re
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict
from functools import partial
from typing import Optional, Callable, AsyncGenerator, Any, Iterable, Union, Dict, List, Tuple

from arango.collection import VertexCollection, StandardCollection, EdgeCollection
from arango.graph import Graph
from arango.typings import Json
from networkx import MultiDiGraph, DiGraph

from core import feature
from core.constants import less_greater_then_operations as lgt_ops, arangodb_matches_null_ops
from core.db.arangodb_functions import as_arangodb_function
from core.db.async_arangodb import AsyncArangoDB, AsyncArangoTransactionDB, AsyncArangoDBBase
from core.db.model import GraphUpdate, QueryModel
from core.error import InvalidBatchUpdate, ConflictingChangeInProgress, NoSuchChangeError, OptimisticLockingFailed
from core.message_bus import MessageBus, CoreEvent
from core.model.adjust_node import AdjustNode
from core.model.graph_access import GraphAccess, GraphBuilder, EdgeType, Section
from core.model.model import Model, ComplexKind, SyntheticProperty, TransformKind
from core.model.resolve_in_graph import NodePath, GraphResolver
from core.query.model import (
    Predicate,
    IsTerm,
    Part,
    Term,
    CombinedTerm,
    FunctionTerm,
    Navigation,
    IdTerm,
    Aggregate,
    AllTerm,
    AggregateFunction,
    Sort,
    WithClause,
    AggregateVariableName,
    AggregateVariableCombined,
)
from core.query.query_parser import merge_ancestors_parser
from core.util import first, value_in_path_get, utc_str, uuid_str, value_in_path, freeze

log = logging.getLogger(__name__)


class GraphDB(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    async def get_node(self, model: Model, node_id: str) -> Optional[Json]:
        pass

    @abstractmethod
    async def create_node(self, model: Model, node_id: str, data: Json, under_node_id: str) -> Json:
        pass

    @abstractmethod
    async def update_node(self, model: Model, node_id: str, patch: Json, section: Optional[str]) -> Json:
        pass

    @abstractmethod
    def update_nodes(self, model: Model, patches_by_id: Dict[str, Json], **kwargs: Any) -> AsyncGenerator[Json, None]:
        pass

    @abstractmethod
    def update_nodes_desired(
        self, model: Model, patch: Json, node_ids: List[str], **kwargs: Any
    ) -> AsyncGenerator[Json, None]:
        pass

    @abstractmethod
    def update_nodes_metadata(
        self, model: Model, patch: Json, node_ids: List[str], **kwargs: Any
    ) -> AsyncGenerator[Json, None]:
        pass

    @abstractmethod
    async def delete_node(self, node_id: str) -> None:
        pass

    @abstractmethod
    async def search(self, model: Model, tokens: str, limit: int) -> AsyncGenerator[Json, None]:
        yield {}  # only here for mypy type check (detects coroutine otherwise)

    @abstractmethod
    async def merge_graph(
        self, graph_to_merge: MultiDiGraph, model: Model, maybe_change_id: Optional[str] = None, is_batch: bool = False
    ) -> Tuple[List[str], GraphUpdate]:
        pass

    @abstractmethod
    async def list_in_progress_updates(self) -> List[Json]:
        pass

    @abstractmethod
    async def commit_batch_update(self, batch_id: str) -> None:
        pass

    @abstractmethod
    async def abort_update(self, batch_id: str) -> None:
        pass

    @abstractmethod
    def query_list(self, query: QueryModel, **kwargs: Any) -> AsyncGenerator[Json, None]:
        pass

    @abstractmethod
    def query_graph_gen(self, query: QueryModel) -> AsyncGenerator[Tuple[str, Json], None]:
        pass

    @abstractmethod
    async def query_graph(self, query: QueryModel) -> DiGraph:
        pass

    @abstractmethod
    def query_aggregation(self, query: QueryModel) -> AsyncGenerator[Json, None]:
        pass

    @abstractmethod
    async def explain(self, query: QueryModel) -> Json:
        pass

    @abstractmethod
    async def wipe(self) -> None:
        pass

    @abstractmethod
    def to_query(self, query_model: QueryModel, all_edges: bool = False) -> Tuple[str, Json]:
        pass

    @abstractmethod
    async def create_update_schema(self) -> None:
        pass


class ArangoGraphDB(GraphDB):
    def __init__(self, db: AsyncArangoDB, name: str, adjust_node: AdjustNode) -> None:
        super().__init__()
        self._name = name
        self.node_adjuster = adjust_node
        self.vertex_name = name
        self.in_progress = f"{name}_in_progress"
        self.db = db

    @property
    def name(self) -> str:
        return self._name

    def edge_collection(self, edge_type: str) -> str:
        return f"{self.name}_{edge_type}"

    async def search(  # noqa: E501 pylint: disable=invalid-overridden-method
        self, model: Model, tokens: str, limit: int
    ) -> AsyncGenerator[Json, None]:
        bind = {"tokens": tokens, "limit": limit}
        trafo = self.document_to_instance_fn(model)
        with await self.db.aql(query=self.query_search_token(), bind_vars=bind) as cursor:
            for element in cursor:
                yield trafo(element)

    async def get_node(self, model: Model, node_id: str) -> Optional[Json]:
        node = await self.by_id(node_id)
        return self.document_to_instance_fn(model)(node) if node is not None else None

    async def create_node(self, model: Model, node_id: str, data: Json, under_node_id: str) -> Json:
        graph = GraphBuilder(model)
        graph.add_node(node_id, data)
        graph.add_edge(under_node_id, node_id, EdgeType.default)
        access = GraphAccess(graph.graph, node_id, {under_node_id})
        _, node_inserts, _, _ = self.prepare_nodes(access, [], model)
        _, edge_inserts, _ = self.prepare_edges(access, [], EdgeType.default)
        assert len(node_inserts) == 1
        assert len(edge_inserts) == 1
        edge_collection = self.edge_collection(EdgeType.default)
        async with self.db.begin_transaction(write=[self.vertex_name, edge_collection]) as tx:
            result: Json = await tx.insert(self.vertex_name, node_inserts[0], return_new=True)
            await tx.insert(edge_collection, edge_inserts[0])
            trafo = self.document_to_instance_fn(model)
            return trafo(result["new"])

    async def update_node(self, model: Model, node_id: str, patch: Json, section: Optional[str]) -> Json:
        return await self.update_node_with(self.db, model, node_id, patch, section)

    async def update_node_with(
        self, db: AsyncArangoDBBase, model: Model, node_id: str, patch: Json, section: Optional[str]
    ) -> Json:
        node = await self.by_id_with(db, node_id)
        if node is None:
            raise AttributeError(f"No document found with this id: {node_id}")
        if "revision" in patch and patch["revision"] != node["_rev"]:
            raise OptimisticLockingFailed(node_id, node["_rev"], patch["revision"])

        updated = node.copy()
        if section:
            existing_section = node.get(section)
            existing_section = existing_section if existing_section else {}
            updated[section] = {**existing_section, **patch}
        else:
            for sect in Section.all_ordered:
                if sect in patch:
                    existing_section = node.get(sect)
                    existing_section = existing_section if existing_section else {}
                    updated[sect] = {**existing_section, **patch[sect]}

        # Only the reported section is defined by the model and can be coerced
        kind = model[updated[Section.reported]]
        coerced = kind.check_valid(updated[Section.reported])
        updated[Section.reported] = coerced if coerced is not None else updated[Section.reported]

        # call adjuster on resulting node
        ctime = value_in_path_get(node, NodePath.reported_ctime, utc_str())
        adjusted = self.adjust_node(model, GraphAccess.dump_direct(node_id, updated), ctime)
        update = {
            "_key": node["_key"],
            "hash": adjusted["hash"],
            "kinds": adjusted["kinds"],
            "flat": adjusted["flat"],
        }
        # copy relevant sections into update node
        for sec in [section] if section else Section.all:
            if sec in adjusted:
                update[sec] = adjusted[sec]

        result = await db.update(self.vertex_name, update, return_new=True)
        trafo = self.document_to_instance_fn(model)
        return trafo(result["new"])

    async def update_nodes(
        self, model: Model, patches_by_id: Dict[str, Json], **kwargs: Any
    ) -> AsyncGenerator[Json, None]:
        log.info(f"Update nodes called with {len(patches_by_id)} updates.")
        # collect all sections to be deleted
        deletes: Dict[str, List[str]] = defaultdict(list)
        delete_sections = [Section.desired, Section.metadata]
        # group patches by changes: single desired or metadata changes can be executed via special purpose methods.
        updates: Dict[Json, List[str]] = defaultdict(list)

        for uid, patch_js in patches_by_id.items():
            # filter out delete operation
            for section in delete_sections:
                if section in patch_js and patch_js[section] is None:
                    deletes[section].append(uid)
                    del patch_js[section]
            # filter out empty changes (== noop patches)
            for section in Section.all:
                if section in patch_js and patch_js[section] == {}:
                    del patch_js[section]
            # all remaining changes are updates
            if patch_js:
                updates[freeze(patch_js)].append(uid)

        # all changes are executed inside a transaction: either all changes are successful or none
        async with self.db.begin_transaction(read=[self.vertex_name], write=[self.vertex_name]) as tx:

            async def update_node_multi(js: Json, node_ids: List[str]) -> AsyncGenerator[Json, None]:
                for node_id in node_ids:
                    log.debug(f"Update node: change={js} on {node_id}")
                    single_update = await self.update_node_with(tx, model, node_id, js, None)
                    yield single_update

            for section, ids in deletes.items():
                log.debug(f"Delete section {section} for ids: {ids}")
                async for res in self.delete_nodes_section_with(tx, model, section, ids):
                    yield res

            for change, items in updates.items():
                if len(change) == 1 and Section.desired in change:
                    log.debug(f"Update desired many: change={change} on {items}")
                    patch = change[Section.desired]
                    result = self.update_nodes_section_with(tx, model, Section.desired, patch, items)
                elif len(change) == 1 and Section.metadata in change:
                    log.debug(f"Update metadata many: change={change} on {items}")
                    patch = change[Section.metadata]
                    result = self.update_nodes_section_with(tx, model, Section.metadata, patch, items)
                else:
                    result = update_node_multi(change, items)
                async for res in result:
                    yield res

    def update_nodes_desired(
        self, model: Model, patch: Json, node_ids: List[str], **kwargs: Any
    ) -> AsyncGenerator[Json, None]:
        return self.update_nodes_section_with(self.db, model, Section.desired, patch, node_ids)

    def update_nodes_metadata(
        self, model: Model, patch: Json, node_ids: List[str], **kwargs: Any
    ) -> AsyncGenerator[Json, None]:
        return self.update_nodes_section_with(self.db, model, Section.metadata, patch, node_ids)

    async def delete_nodes_section_with(
        self, db: AsyncArangoDBBase, model: Model, section: str, node_ids: List[str]
    ) -> AsyncGenerator[Json, None]:
        bind_var = {"node_ids": node_ids}
        trafo = self.document_to_instance_fn(model)
        with await db.aql(query=self.query_delete_desired_metadata_many(section), bind_vars=bind_var) as cursor:
            for element in cursor:
                yield trafo(element)

    async def update_nodes_section_with(
        self, db: AsyncArangoDBBase, model: Model, section: str, patch: Json, node_ids: List[str]
    ) -> AsyncGenerator[Json, None]:
        bind_var = {"patch": patch, "node_ids": node_ids}
        trafo = self.document_to_instance_fn(model)
        with await db.aql(query=self.query_update_desired_metadata_many(section), bind_vars=bind_var) as cursor:
            for element in cursor:
                yield trafo(element)

    async def delete_node(self, node_id: str) -> None:
        with await self.db.aql(query=self.query_count_direct_children(), bind_vars={"rid": node_id}) as cursor:
            count = cursor.next()
            if count > 0:
                raise AttributeError(f"Can not delete node, since it has {count} child(ren)!")

        with await self.db.aql(query=self.query_node_by_id(), bind_vars={"rid": node_id}) as cursor:
            if not cursor.empty():
                await self.db.delete_vertex(self.name, cursor.next())
            else:
                return None

    async def by_id(self, node_id: str) -> Optional[Json]:
        return await self.by_id_with(self.db, node_id)

    async def by_id_with(self, db: AsyncArangoDBBase, node_id: str) -> Optional[Json]:
        with await db.aql(query=self.query_node_by_id(), bind_vars={"rid": node_id}) as cursor:
            return cursor.next() if not cursor.empty() else None

    async def query_list(  # noqa: E501 pylint: disable=invalid-overridden-method
        self, query: QueryModel, **kwargs: Any
    ) -> AsyncGenerator[Json, None]:
        assert query.query.aggregate is None, "Given query is an aggregation function. Use the appropriate endpoint!"
        q_string, bind = self.to_query(query)
        trafo = self.document_to_instance_fn(query.model)
        visited = set()
        with await self.db.aql(query=q_string, bind_vars=bind) as cursor:
            for element in cursor:
                _id = element["_id"]
                if element is not None and _id not in visited:
                    visited.add(_id)
                    json = trafo(element)
                    if json:
                        yield json

    async def query_graph_gen(  # noqa: E501 pylint: disable=invalid-overridden-method
        self, query: QueryModel
    ) -> AsyncGenerator[Tuple[str, Json], None]:
        assert query.query.aggregate is None, "Given query is an aggregation function. Use the appropriate endpoint!"
        query_string, bind = self.to_query(query, all_edges=True)
        trafo = self.document_to_instance_fn(query.model)
        visited_node = {}
        visited_edge = set()
        with await self.db.aql(query=query_string, bind_vars=bind, batch_size=10000) as cursor:
            for element in cursor:
                try:
                    _id = element["_id"]
                    if _id not in visited_node:
                        json = trafo(element)
                        if json:
                            yield "node", json
                        visited_node[_id] = element["_key"]
                    from_id = element.get("_from")
                    to_id = element.get("_to")
                    if from_id in visited_node and to_id in visited_node:
                        edge_key = from_id + to_id
                        if edge_key not in visited_edge:
                            yield "edge", {"type": "edge", "from": visited_node[from_id], "to": visited_node[to_id]}
                            visited_edge.add(edge_key)
                except Exception as ex:
                    log.warning(f"Could not read element {element}: {ex}. Ignore.")

    async def query_graph(self, query: QueryModel) -> DiGraph:
        result = self.query_graph_gen(query)
        graph = DiGraph()
        async for kind, item in result:
            if kind == "node":
                graph.add_node(item["id"], **item)
            elif kind == "edge":
                graph.add_edge(item["from"], item["to"])
        return graph

    async def query_aggregation(  # noqa: E501 pylint: disable=invalid-overridden-method
        self, query: QueryModel
    ) -> AsyncGenerator[Json, None]:
        q_string, bind = self.to_query(query)
        assert query.query.aggregate is not None, "Given query has no aggregation section"
        with await self.db.aql(query=q_string, bind_vars=bind) as cursor:
            for element in cursor:
                yield element

    async def explain(self, query: QueryModel) -> Json:
        q_string, bind = self.to_query(query, all_edges=True)
        return await self.db.explain(query=q_string, bind_vars=bind)

    async def wipe(self) -> None:
        await self.db.truncate(self.vertex_name)
        for edge_type in EdgeType.all:
            await self.db.truncate(self.edge_collection(edge_type))
        await self.insert_genesis_data()

    @staticmethod
    def document_to_instance_fn(model: Model) -> Callable[[Json], Optional[Json]]:
        def props(doc: Json, result: Json) -> None:
            for prop in Section.all_ordered:
                if prop in doc and doc[prop]:
                    result[prop] = doc[prop]

        def synth_props(doc: Json, result: Json) -> None:
            reported_in = doc[Section.reported]
            kind = model.get(reported_in)
            if isinstance(kind, ComplexKind):
                reported_out = result[Section.reported]
                for synth in kind.synthetic_props():
                    if isinstance(synth.kind, TransformKind) and synth.prop.synthetic:
                        source_value = value_in_path(reported_in, synth.prop.synthetic.path)
                        if source_value:
                            reported_out[synth.prop.name] = synth.kind.transform(source_value)

        def render_prop(doc: Json) -> Optional[Json]:
            if Section.reported in doc or Section.desired in doc or Section.metadata in doc:
                # side note: the dictionary remembers insertion order
                # this order is also used to render the output (e.g. yaml property order)
                result = {"id": doc["_key"], "type": "node"}
                if "_rev" in doc:
                    result["revision"] = doc["_rev"]
                props(doc, result)
                synth_props(doc, result)
                if "kinds" in doc:
                    result["kinds"] = doc["kinds"]
                return result
            else:
                return None

        return render_prop

    async def list_in_progress_updates(self) -> List[Json]:
        with await self.db.aql(self.query_active_updates()) as cursor:
            return list(cursor)

    async def get_tmp_collection(self, change_id: str, create: bool = True) -> StandardCollection:
        id_part = str(uuid.uuid5(uuid.NAMESPACE_DNS, change_id)).replace("-", "")
        temp_name = f"{self.vertex_name}_temp_{id_part}"
        if await self.db.has_collection(temp_name):
            return self.db.collection(temp_name)
        elif create:
            temp = await self.db.create_collection(temp_name, replication_factor=1)
            temp.add_persistent_index(["action"])
            return temp
        else:
            raise NoSuchChangeError(change_id)

    async def move_temp_to_proper(self, change_id: str, temp_name: str) -> None:
        change_key = str(uuid.uuid5(uuid.NAMESPACE_DNS, change_id))
        log.info(f"Move to temp: change_id={change_id}, change_key={change_key}, temp_name={temp_name}")
        edge_inserts = [
            f'for e in {temp_name} filter e.action=="edge_insert" and e.edge_type=="{a}" '
            f"insert e.data in {self.edge_collection(a)}"
            for a in EdgeType.all
        ]
        edge_deletes = [
            f'for e in {temp_name} filter e.action=="edge_delete" and e.edge_type=="{a}" '
            f"remove e.data in {self.edge_collection(a)}"
            for a in EdgeType.all
        ]
        updates = "\n".join(
            map(
                lambda aql: f"db._createStatement({{ query: `{aql}` }}).execute();",
                [
                    f'for e in {temp_name} filter e.action=="node_insert" insert e.data in {self.vertex_name}',
                    f'for e in {temp_name} filter e.action=="node_update" update e.data in {self.vertex_name}'
                    + " OPTIONS {mergeObjects: false}",
                    f'for e in {temp_name} filter e.action=="node_delete" remove e.data in {self.vertex_name}',
                ]
                + edge_inserts
                + edge_deletes
                + [
                    f'remove {{_key: "{change_key}"}} in {self.in_progress}',
                ],
            )
        )
        await self.db.execute_transaction(
            f'function () {{\nvar db=require("@arangodb").db;\n{updates}\n}}',
            read=[temp_name],
            write=[self.edge_collection(a) for a in EdgeType.all] + [self.vertex_name, self.in_progress],
        )

    async def mark_update(
        self, root_node_ids: List[str], parent_node_ids: List[str], change_id: str, is_batch: bool
    ) -> None:
        async with self.db.begin_transaction(read=[self.in_progress], write=[self.in_progress]) as tx:
            existing = next(await tx.aql(self.query_active_change(), bind_vars={"root_node_ids": root_node_ids}), None)
            if existing is not None:
                other = existing["change"]
                raise InvalidBatchUpdate() if change_id == other else ConflictingChangeInProgress(other)
            await tx.insert(
                self.in_progress,
                {
                    "_key": str(uuid.uuid5(uuid.NAMESPACE_DNS, change_id)),
                    "root_node_ids": list(root_node_ids),
                    "parent_node_ids": list(parent_node_ids),
                    "change": change_id,
                    "created": utc_str(),
                    "is_batch": is_batch,
                },
            )

    async def refresh_marked_update(self, change_id: str) -> None:
        with await self.db.aql(self.update_active_change(), bind_vars={"change": change_id}):
            return None

    async def delete_marked_update(self, change_id: str, tx: Optional[AsyncArangoTransactionDB] = None) -> None:
        db = tx if tx else self.db
        doc = {"_key": str(uuid.uuid5(uuid.NAMESPACE_DNS, change_id))}
        await db.delete(self.in_progress, doc, ignore_missing=True)

    def adjust_node(self, model: Model, json: Json, created_at: Any) -> Json:
        reported = json[Section.reported]
        # preserve ctime in reported: if it is not set, use the creation time of the object
        if not reported.get("ctime", None):
            kind = model[reported]
            if isinstance(kind, ComplexKind) and "ctime" in kind:
                reported["ctime"] = created_at

        # adjuster has the option to manipulate the resulting json
        return self.node_adjuster.adjust(json)

    def prepare_nodes(
        self, access: GraphAccess, node_cursor: Iterable, model: Model  # type: ignore # pypy
    ) -> Tuple[GraphUpdate, List[Json], List[Json], List[Json]]:
        sub_root_id = access.root()
        log.info(f"Prepare nodes for subgraph {access.root()}")
        info = GraphUpdate()
        resource_inserts: List[Json] = []
        resource_updates: List[Json] = []
        resource_deletes: List[Json] = []

        optional_properties = [*Section.all, "refs", "kinds", "flat", "hash"]

        def insert_node(node: Json) -> None:
            elem = self.adjust_node(model, node, access.at_json)
            js_doc: Json = {
                "_key": elem["id"],
                "update_id": sub_root_id,
                "created": access.at_json,
                "updated": access.at_json,
            }
            for prop in optional_properties:
                value = node.get(prop, None)
                if value:
                    js_doc[prop] = value
            resource_inserts.append(js_doc)
            info.nodes_created += 1

        def update_or_delete_node(node: Json) -> None:
            key = node["_key"]
            hash_string = node["hash"]
            elem = access.node(key)
            if elem is None:
                # node is in db, but not in the graph any longer: delete node
                resource_deletes.append({"_key": key})
                info.nodes_deleted += 1
            elif elem["hash"] != hash_string:
                # node is in db and in the graph, content is different
                adjusted: Json = self.adjust_node(model, elem, node["created"])
                js = {"_key": key, "update_id": sub_root_id, "updated": access.at_json}
                for prop in optional_properties:
                    value = adjusted.get(prop, None)
                    if value:
                        js[prop] = value
                resource_updates.append(js)
                info.nodes_updated += 1

        for doc in node_cursor:
            update_or_delete_node(doc)

        for not_visited in access.not_visited_nodes():
            insert_node(not_visited)
        return info, resource_inserts, resource_updates, resource_deletes

    def prepare_edges(
        self, access: GraphAccess, edge_cursor: Iterable, edge_type: str  # type: ignore # pypy
    ) -> Tuple[GraphUpdate, List[Json], List[Json]]:
        sub_root_id = access.root()
        log.info(f"Prepare edges of type {edge_type} for subgraph {access.root()}")
        info = GraphUpdate()
        edges_inserts: List[Json] = []
        edges_deletes: List[Json] = []

        def insert_edge(from_node: str, to_node: str) -> None:
            key = self.db_edge_key(from_node, to_node)

            js = {
                "_key": key,
                "_from": f"{self.vertex_name}/{from_node}",
                "_to": f"{self.vertex_name}/{to_node}",
                "update_id": sub_root_id,
            }
            edges_inserts.append(js)
            info.edges_created += 1

        def update_edge(edge: Json) -> None:
            from_node = edge["_from"].split("/")[1]  # vertex/id
            to_node = edge["_to"].split("/")[1]  # vertex/id
            if not access.has_edge(from_node, to_node, edge_type):
                edges_deletes.append(edge)
                info.edges_deleted += 1

        for doc in edge_cursor:
            update_edge(doc)

        for edge_from, edge_to in access.not_visited_edges(edge_type):
            insert_edge(edge_from, edge_to)

        return info, edges_inserts, edges_deletes

    async def merge_graph(
        self, graph_to_merge: MultiDiGraph, model: Model, maybe_change_id: Optional[str] = None, is_batch: bool = False
    ) -> Tuple[List[str], GraphUpdate]:
        change_id = maybe_change_id if maybe_change_id else uuid_str()

        async def prepare_graph(
            sub: GraphAccess, node_query: Tuple[str, Json], edge_query: Callable[[str], Tuple[str, Json]]
        ) -> Tuple[GraphUpdate, List[Json], List[Json], List[Json], Dict[str, List[Json]], Dict[str, List[Json]]]:
            graph_info = GraphUpdate()
            # check all nodes for this subgraph
            query, bind = node_query
            log.debug(f"Query for nodes: {sub.root()}")
            with await self.db.aql(query, bind_vars=bind, batch_size=50000) as node_cursor:
                node_info, ni, nu, nd = self.prepare_nodes(sub, node_cursor, model)
                graph_info += node_info

            # check all edges in all relevant edge-collections
            edge_inserts = defaultdict(list)
            edge_deletes = defaultdict(list)
            for edge_type in EdgeType.all:
                query, bind = edge_query(edge_type)
                log.debug(f"Query for edges of type {edge_type}: {sub.root()}")
                with await self.db.aql(query, bind_vars=bind, batch_size=50000) as ec:
                    edge_info, gei, ged = self.prepare_edges(sub, ec, edge_type)
                    graph_info += edge_info
                    edge_inserts[edge_type] = gei
                    edge_deletes[edge_type] = ged
            return graph_info, ni, nu, nd, edge_inserts, edge_deletes

        roots, parent, graphs = GraphAccess.merge_graphs(graph_to_merge)
        logging.info(f"merge_graph {len(roots)} merge nodes found. change_id={change_id}, is_batch={is_batch}.")

        def parent_edges(edge_type: str) -> Tuple[str, Json]:
            edge_ids = [self.db_edge_key(f, t) for f, t, et in parent.g.edges(data="edge_type") if et == edge_type]
            return self.query_update_edges_by_ids(edge_type), {"ids": edge_ids}

        def merge_edges(merge_node: str, edge_type: str) -> Tuple[str, Json]:
            return self.query_update_edges(edge_type), {"update_id": merge_node}

        def combine_dict(left: Dict[str, List[Any]], right: Dict[str, List[Any]]) -> Dict[str, List[Any]]:
            result = dict(left)
            for key, right_values in right.items():
                left_values = left.get(key)
                result[key] = left_values + right_values if left_values else right_values
            return result

        # this will throw an exception, in case of a conflicting update (--> outside try block)
        log.debug("Mark all parent nodes for this update to avoid conflicting changes")
        await self.mark_update(roots, list(parent.nodes), change_id, is_batch)
        try:
            parents_nodes = self.query_update_nodes_by_ids(), {"ids": list(parent.g.nodes)}
            info, nis, nus, nds, eis, eds = await prepare_graph(parent, parents_nodes, parent_edges)
            for num, (root, graph) in enumerate(graphs):
                log.debug(f"Update subgraph: root={root} ({num+1} of {len(roots)})")
                node_query = self.query_update_nodes(), {"update_id": root}
                edge_query = partial(merge_edges, root)

                i, ni, nu, nd, ei, ed = await prepare_graph(graph, node_query, edge_query)
                info += i
                nis += ni
                nus += nu
                nds += nd
                eis = combine_dict(eis, ei)
                eds = combine_dict(eds, ed)

            log.debug(f"Update prepared: {info}. Going to persist the changes.")
            await self.refresh_marked_update(change_id)
            await self.persist_update(change_id, is_batch, info, nis, nus, nds, eis, eds)
            return roots, info
        except Exception as ex:
            await self.delete_marked_update(change_id)
            raise ex

    async def persist_update(
        self,
        change_id: str,
        is_batch: bool,
        info: GraphUpdate,
        resource_inserts: List[Json],
        resource_updates: List[Json],
        resource_deletes: List[Json],
        edge_inserts: Dict[str, List[Json]],
        edge_deletes: Dict[str, List[Json]],
    ) -> None:
        async def execute_many_async(async_fn: Callable[[str, List[Json]], Any], name: str, array: List[Json]) -> None:
            if array:
                result = await async_fn(name, array)
                ex: Optional[Exception] = first(lambda x: isinstance(x, Exception), result)
                if ex:
                    raise ex  # pylint: disable=raising-bad-type

        async def trafo_many(
            async_fn: Callable[[str, List[Json]], Any], name: str, array: List[Json], template: Json
        ) -> None:
            # update the array in place to not create another intermediate array
            for idx, item in enumerate(array):
                entry = template.copy()
                entry["data"] = item
                array[idx] = entry
            await execute_many_async(async_fn, name, array)

        async def update_directly() -> None:
            log.debug("Persist the changes directly.")
            edge_collections = [self.edge_collection(a) for a in EdgeType.all]
            update_many_no_merge = partial(self.db.update_many, merge=False)
            async with self.db.begin_transaction(write=edge_collections + [self.vertex_name, self.in_progress]) as tx:
                # note: all requests are done sequentially on purpose
                # https://www.arangodb.com/docs/stable/http/transaction-stream-transaction.html#concurrent-requests
                await execute_many_async(self.db.insert_many, self.vertex_name, resource_inserts)
                # noinspection PyTypeChecker
                await execute_many_async(update_many_no_merge, self.vertex_name, resource_updates)
                await execute_many_async(self.db.delete_many, self.vertex_name, resource_deletes)
                for ed_i_type, ed_insert in edge_inserts.items():
                    await execute_many_async(self.db.insert_many, self.edge_collection(ed_i_type), ed_insert)
                for ed_d_type, ed_delete in edge_deletes.items():
                    await execute_many_async(self.db.delete_many, self.edge_collection(ed_d_type), ed_delete)
                await self.delete_marked_update(change_id, tx)

        async def store_to_tmp_collection(temp: StandardCollection) -> None:
            tmp = temp.name
            ri = trafo_many(self.db.insert_many, tmp, resource_inserts, {"action": "node_insert"})
            ru = trafo_many(self.db.insert_many, tmp, resource_updates, {"action": "node_update"})
            rd = trafo_many(self.db.insert_many, tmp, resource_deletes, {"action": "node_delete"})
            edge_i = [
                trafo_many(self.db.insert_many, tmp, inserts, {"action": "edge_insert", "edge_type": tpe})
                for tpe, inserts in edge_inserts.items()
            ]
            edge_u = [
                trafo_many(self.db.insert_many, tmp, deletes, {"action": "edge_delete", "edge_type": tpe})
                for tpe, deletes in edge_deletes.items()
            ]
            await asyncio.gather(*([ri, ru, rd] + edge_i + edge_u))

        async def update_via_temp_collection() -> None:
            temp = await self.get_tmp_collection(change_id)
            log.info(f"Update is too big for tx size: use temp collection {temp.name}")
            try:
                await store_to_tmp_collection(temp)
                await self.move_temp_to_proper(change_id, temp.name)
            finally:
                log.debug(f"Delete temp collection {temp.name}")
                await self.db.delete_collection(temp.name)

        async def update_batch() -> None:
            temp = await self.get_tmp_collection(change_id)
            log.info(f"Batch update: use temp collection {temp.name}")
            await store_to_tmp_collection(temp)

        if is_batch:
            await update_batch()
            await self.refresh_marked_update(change_id)
        elif info.all_changes() < 100000:  # work around to not run into the 128MB tx limit
            await update_directly()
        else:
            await update_via_temp_collection()
        log.debug("Persist update done.")

    async def commit_batch_update(self, batch_id: str) -> None:
        temp_table = await self.get_tmp_collection(batch_id, False)
        await self.move_temp_to_proper(batch_id, temp_table.name)
        await self.db.delete_collection(temp_table.name)

    async def abort_update(self, batch_id: str) -> None:
        try:
            temp_table = await self.get_tmp_collection(batch_id, False)
            await self.db.delete_collection(temp_table.name)
        except NoSuchChangeError:
            pass
        await self.delete_marked_update(batch_id)

    def to_query(self, query_model: QueryModel, all_edges: bool = False) -> Tuple[str, Json]:
        query = query_model.query
        model = query_model.model
        section_dot = f"{query_model.query_section}." if query_model.query_section else ""
        mw = query.preamble.get("merge_with_ancestors")
        merge_with: List[str] = re.split("\\s*,\\s*", str(mw)) if mw else []
        bind_vars: Json = {}

        def aggregate(in_cursor: str, a: Aggregate) -> Tuple[str, str]:
            cursor = "rg"

            def var_name(n: Union[AggregateVariableName, AggregateVariableCombined]) -> str:
                def comb_name(cb: Union[str, AggregateVariableName]) -> str:
                    return f'"{cb}"' if isinstance(cb, str) else f"{cursor}.{section_dot}{cb.name}"

                return (
                    f"{cursor}.{section_dot}{n.name}"
                    if isinstance(n, AggregateVariableName)
                    else f'CONCAT({",".join(comb_name(cp) for cp in n.parts)})'
                )

            def func_term(fn: AggregateFunction) -> str:
                name = f"{cursor}.{section_dot}{fn.name}" if isinstance(fn.name, str) else str(fn.name)
                return f"{name} {fn.combined_ops()}" if fn.ops else name

            vs = {str(v.name): f"var_{num}" for num, v in enumerate(a.group_by)}
            fs = {v.name: f"fn_{num}" for num, v in enumerate(a.group_func)}
            variables = ", ".join(f"{vs[str(v.name)]}={var_name(v.name)}" for v in a.group_by)
            funcs = ", ".join(f"{fs[v.name]}={v.function}({func_term(v)})" for v in a.group_func)
            agg_vars = ", ".join(f'"{v.get_as_name()}": {vs[str(v.name)]}' for v in a.group_by)
            agg_funcs = ", ".join(f'"{f.get_as_name()}": {fs[f.name]}' for f in a.group_func)
            group_result = f'"group":{{{agg_vars}}},' if a.group_by else ""
            aggregate_term = f"collect {variables} aggregate {funcs}"
            return_result = f"{{{group_result} {agg_funcs}}}"
            return (
                "aggregated",
                f"LET aggregated = (for {cursor} in {in_cursor} {aggregate_term} RETURN {return_result})",
            )

        def predicate(cursor: str, p: Predicate) -> str:
            extra = ""
            path = p.name

            # handle that property is an array
            if "array" in p.args:
                arr_filter = p.args["filter"] if "filter" in p.args else "any"
                extra = f" {arr_filter} "
                path = f"{p.name}[]"
            elif "[*]" in p.name:
                extra = " any " if "[*]" in p.name else " "
                path = p.name.replace("[*]", "[]")

            # key of the predicate is the len of the dict as string
            length = str(len(bind_vars))
            # if no section is given, the path is prefixed by the section: remove the section
            lookup = path if query_model.query_section else Section.without_section(path)
            prop = model.property_by_path(lookup)

            def synthetic_path(synth: SyntheticProperty) -> str:
                before, after = p.name.rsplit(prop.prop.name, 1)
                return f'{before}{".".join(synth.path)}{after}'

            op = lgt_ops[p.op] if prop.kind.reverse_order and p.op in lgt_ops else p.op
            if op in ["in", "not in"] and isinstance(p.value, list):
                bind_vars[length] = [prop.kind.coerce(a) for a in p.value]
            else:
                bind_vars[length] = prop.kind.coerce(p.value)
            prop_name = synthetic_path(prop.prop.synthetic) if prop.prop.synthetic else p.name
            var_name = f"{cursor}.{section_dot}{prop_name}"
            p_term = f"{var_name}{extra} {op} @{length}"
            # null check is required, since x<anything evaluates to true if x is null!
            return f"({var_name}!=null and {p_term})" if op in arangodb_matches_null_ops else p_term

        def with_id(cursor: str, t: IdTerm) -> str:
            length = str(len(bind_vars))
            bind_vars[length] = t.id
            return f"{cursor}._key == @{length}"

        def is_instance(cursor: str, t: IsTerm) -> str:
            if t.kind not in model:
                raise AttributeError(f"Given kind does not exist: {t.kind}")
            length = str(len(bind_vars))
            bind_vars[length] = t.kind
            return f"@{length} IN {cursor}.kinds"

        def term(cursor: str, ab_term: Term) -> str:
            if isinstance(ab_term, AllTerm):
                return "true"
            if isinstance(ab_term, Predicate):
                return predicate(cursor, ab_term)
            elif isinstance(ab_term, FunctionTerm):
                return as_arangodb_function(cursor, bind_vars, ab_term, query_model)
            elif isinstance(ab_term, IdTerm):
                return with_id(cursor, ab_term)
            elif isinstance(ab_term, IsTerm):
                return is_instance(cursor, ab_term)
            elif isinstance(ab_term, CombinedTerm):
                left = term(cursor, ab_term.left)
                right = term(cursor, ab_term.right)
                return f"({left}) {ab_term.op} ({right})"
            else:
                raise AttributeError(f"Do not understand: {ab_term}")

        def part(p: Part, idx: int, in_cursor: str) -> Tuple[Part, str, str, str]:
            query_part = ""
            filtered_out = f"step{idx}_filter"

            def filter_statement() -> str:
                nonlocal query_part
                crsr = f"f{idx}"
                out = filtered_out
                md = f"NOT_NULL({crsr}.metadata, {{}})"
                f_res = f'MERGE({crsr}, {{metadata:MERGE({md}, {{"query_tag": "{p.tag}"}})}})' if p.tag else crsr
                query_part += f"LET {out} = (FOR {crsr} in {in_cursor} FILTER {term(crsr, p.term)} RETURN {f_res})"
                return out

            def with_clause(in_crsr: str, clause: WithClause) -> str:
                nonlocal query_part
                # this is the general structure of the with_clause that is created
                #
                # FOR cloud in foo FILTER @0 in cloud.kinds
                #    FOR account IN 0..1 OUTBOUND cloud foo_dependency
                #    OPTIONS { bfs: true, uniqueVertices: 'global' }
                #    FILTER (cloud._key==account._key) or (@1 in account.kinds)
                #        FOR region in 0..1 OUTBOUND account foo_dependency
                #        OPTIONS { bfs: true, uniqueVertices: 'global' }
                #         FILTER (cloud._key==region._key) or (@2 in region.kinds)
                #             FOR zone in 0..1 OUTBOUND region foo_dependency
                #             OPTIONS { bfs: true, uniqueVertices: 'global' }
                #             FILTER (cloud._key==zone._key) or (@3 in zone.kinds)
                #         COLLECT l4_cloud = cloud, l4_account=account, l4_region=region WITH COUNT INTO counter3
                #         FILTER (l4_cloud._key==l4_region._key) or (counter3>=0)
                #     COLLECT l3_cloud = l4_cloud, l3_account=l4_account WITH COUNT INTO counter2
                #     FILTER (l3_cloud._key==l3_account._key) or (counter2>=0) // ==2 regions
                # COLLECT l2_cloud = l3_cloud WITH COUNT INTO counter1
                # FILTER (counter1>=0) //counter is +1 since the node itself is always bypassed
                # RETURN ({cloud: l2_cloud._key, count:counter1})

                def traversal_filter(cl: WithClause, in_crs: str, depth: int) -> str:
                    nav = cl.navigation
                    crsr = f"l{depth}crsr"
                    direction = "OUTBOUND" if nav.direction == "out" else "INBOUND"
                    unique = "uniqueEdges: 'path'" if all_edges else "uniqueVertices: 'global'"
                    filter_clause = f"({term(crsr, cl.term)})" if cl.term else "true"
                    inner = traversal_filter(cl.with_clause, crsr, depth + 1) if cl.with_clause else ""
                    filter_root = f"(l0crsr._key=={crsr}._key) or " if depth > 0 else ""
                    return (
                        f"FOR {crsr} IN 0..{nav.until} {direction} {in_crs} "
                        f"{self.edge_collection(nav.edge_type)} OPTIONS {{ bfs: true, {unique} }} "
                        f"FILTER {filter_root}{filter_clause} "
                    ) + inner

                def collect_filter(cl: WithClause, depth: int) -> str:
                    fltr = cl.with_filter
                    if cl.with_clause:
                        collects = ", ".join(f"l{depth-1}_l{i}_res=l{depth}_l{i}_res" for i in range(0, depth))
                    else:
                        collects = ", ".join(f"l{depth-1}_l{i}_res=l{i}crsr" for i in range(0, depth))

                    if depth == 1:
                        # note: the traversal starts from 0 (only 0 and 1 is allowed)
                        # when we start from 1: increase the count by one to not count the start node
                        # when we start from 0: the start node is expected in the count already
                        filter_term = f"FILTER counter1{fltr.op}{fltr.num + cl.navigation.start}"
                    else:
                        root_key = f"l{depth-1}_l0_res._key==l{depth-1}_l{depth-1}_res._key"
                        filter_term = f"FILTER ({root_key}) or (counter{depth}{fltr.op}{fltr.num})"

                    inner = collect_filter(cl.with_clause, depth + 1) if cl.with_clause else ""
                    return inner + f"COLLECT {collects} WITH COUNT INTO counter{depth} {filter_term} "

                out = f"step{idx}_with"
                out_crsr = "l0crsr"

                query_part += (
                    f"LET {out} =( FOR {out_crsr} in {in_crsr} "
                    + traversal_filter(clause, out_crsr, 1)
                    + collect_filter(clause, 1)
                    + "RETURN l0_l0_res) "
                )
                return out

            def inout(in_crsr: str, start: int, until: int, edge_type: str, direction: str) -> str:
                nonlocal query_part
                out = f"step{idx}_navigation_{direction}"
                out_crsr = f"n{idx}"
                link = f"link{idx}"
                unique = "uniqueEdges: 'path'" if all_edges else "uniqueVertices: 'global'"
                dir_bound = "OUTBOUND" if direction == "out" else "INBOUND"
                query_part += (
                    f"LET {out} =( FOR in{idx} in {in_crsr} "
                    f"FOR {out_crsr}, {link} IN {start}..{until} {dir_bound} in{idx} "
                    f"{self.edge_collection(edge_type)} OPTIONS {{ bfs: true, {unique} }} "
                    f"RETURN MERGE({out_crsr}, {{_from:{link}._from, _to:{link}._to}})) "
                )
                return out

            def navigation(in_crsr: str, nav: Navigation) -> str:
                nonlocal query_part
                if nav.direction == "inout":
                    # traverse to root
                    to_in = inout(in_crsr, nav.start, nav.until, nav.edge_type, "in")
                    # traverse to leaf (in case of 0: use 1 to not have the current element twice)
                    to_out = inout(in_crsr, max(1, nav.start), nav.until, nav.edge_type, "out")
                    nav_crsr = f"step{idx}_navigation"
                    query_part += f"LET {nav_crsr} = UNION({to_in}, {to_out})"
                    return nav_crsr
                else:
                    return inout(in_crsr, nav.start, nav.until, nav.edge_type, nav.direction)

            cursor = filter_statement()
            cursor = with_clause(cursor, p.with_clause) if p.with_clause else cursor
            cursor = navigation(cursor, p.navigation) if p.navigation else cursor
            return p, cursor, filtered_out, query_part

        def merge_ancestors(cursor: str, part_str: str, ancestor_names: List[str]) -> Tuple[str, str]:
            ancestors: List[Tuple[str, str]] = [merge_ancestors_parser.parse(p) for p in ancestor_names]
            m_parts = [f"FOR node in {cursor} "]

            # filter out resolved ancestors: all remaining ancestors need to be looked up in hierarchy
            to_resolve = [(nr, p_as) for nr, p_as in ancestors if nr not in GraphResolver.resolved_ancestors]
            if to_resolve:
                bind_vars["merge_stop_at"] = to_resolve[0][0]
                bind_vars["merge_ancestor_nodes"] = [tr[0] for tr in to_resolve]
                m_parts.append(
                    "LET ancestor_nodes = ("
                    + f"FOR p IN 1..1000 INBOUND node {self.edge_collection(EdgeType.default)} "
                    + "PRUNE @merge_stop_at in p.kinds "
                    + "OPTIONS {order: 'bfs', uniqueVertices: 'global'} "
                    + "FILTER p.kinds any in @merge_ancestor_nodes RETURN p)"
                )
                for tr, _ in to_resolve:
                    bv = f"merge_node_{tr}"
                    bind_vars[bv] = tr
                    m_parts.append(f"""LET {tr} = FIRST(FOR p IN ancestor_nodes FILTER @{bv} IN p.kinds RETURN p)""")

            # all resolved ancestors can be looked up directly
            for tr, _ in ancestors:
                if tr in GraphResolver.resolved_ancestors:
                    m_parts.append(f'LET {tr} = DOCUMENT("{self.vertex_name}", node.refs.{tr}_id)')

            result_parts = []
            for section in Section.all:
                ancestor_result = "{" + ",".join([f"{p[1]}: {p[0]}.{section}" for p in ancestors]) + "}"
                result_parts.append(f"{section}: MERGE(NOT_NULL(node.{section},{{}}), {ancestor_result})")

            m_parts.append("RETURN MERGE(node, {" + ", ".join(result_parts) + "})")
            return "merge_with_ancestor", part_str + f' LET merge_with_ancestor = ({" ".join(m_parts)})'

        def sort(cursor: str, so: List[Sort], sect_dot: str) -> str:
            sorts = ", ".join(f"{cursor}.{sect_dot}{s.name} {s.order}" for s in so)
            return f" sort {sorts} "

        parts = []
        crsr = self.vertex_name
        for idx, p in enumerate(reversed(query.parts)):
            part_tuple = part(p, idx, crsr)
            parts.append(part_tuple)
            crsr = part_tuple[1]

        all_parts = " ".join(p[3] for p in parts)
        resulting_cursor, query_str = merge_ancestors(crsr, all_parts, merge_with) if merge_with else (crsr, all_parts)
        limited = f" LIMIT {query.limit} " if query.limit else ""
        if query.aggregate:  # return aggregate
            resulting_cursor, aggregation = aggregate(resulting_cursor, query.aggregate)
            sort_by = sort("r", query.sort, "") if query.sort else ""
            return (
                f"""{query_str} {aggregation} FOR r in {resulting_cursor}{sort_by}{limited} RETURN r""",
                bind_vars,
            )
        else:  # return results
            # return all tagged parts (last result is "tagged" automatically)
            tagged = {out for part, _, out, _ in parts if part.tag}
            result = f'UNION({",".join(tagged)},{resulting_cursor})' if tagged else resulting_cursor
            sort_by = sort("r", query.sort, section_dot) if query.sort else ""
            return f"""{query_str} FOR r in {result}{sort_by}{limited} RETURN r""", bind_vars

    async def insert_genesis_data(self) -> None:
        root_data = {"kind": "graph_root", "name": "root"}
        sha = GraphBuilder.content_hash(root_data)
        root_node = {"_key": "root", "id": "root", Section.reported: root_data, "kinds": ["graph_root"], "hash": sha}
        try:
            await self.db.insert(self.vertex_name, root_node)
        except Exception:
            # ignore if the root not is already created
            return None

    async def create_update_schema(self) -> None:
        db = self.db

        async def create_update_graph(
            graph_name: str, vertex_name: str, edge_name: str
        ) -> Tuple[Graph, VertexCollection, EdgeCollection]:
            graph = db.graph(graph_name) if await db.has_graph(graph_name) else await db.create_graph(graph_name)
            vertex_collection = (
                graph.vertex_collection(vertex_name)
                if await db.has_vertex_collection(graph_name, vertex_name)
                else await db.create_vertex_collection(graph_name, vertex_name)
            )
            edge_collection = (
                graph.edge_collection(edge_name)
                if await db.has_edge_definition(graph_name, edge_name)
                else await db.create_edge_definition(graph_name, edge_name, [vertex_name], [vertex_name])
            )
            return graph, vertex_collection, edge_collection

        def create_update_indexes(nodes: VertexCollection, progress: StandardCollection) -> None:
            node_idxes = {idx["name"]: idx for idx in nodes.indexes()}
            # this index will hold all the necessary data to query for an update (index only query)
            if "update_id" not in node_idxes:
                nodes.add_persistent_index(["_key", "update_id", "hash", "created"], sparse=False, name="update_value")
            progress_idxes = {idx["name"]: idx for idx in progress.indexes()}
            if "parent_nodes" not in progress_idxes:
                progress.add_persistent_index(["parent_nodes[*]"], name="parent_nodes")
            if "root_nodes" not in progress_idxes:
                progress.add_persistent_index(["root_nodes[*]"], name="root_nodes")

        async def create_collection(name: str) -> StandardCollection:
            return db.collection(name) if await db.has_collection(name) else await db.create_collection(name)

        async def create_update_views(nodes: VertexCollection) -> None:
            name = f"search_{nodes.name}"
            views = {view["name"]: view for view in await db.views()}
            if name not in views:
                await db.create_view(
                    name,
                    "arangosearch",
                    {
                        "links": {
                            nodes.name: {"analyzers": ["identity"], "fields": {"flat": {"analyzers": ["text_en"]}}}
                        },
                    },
                )

        for edge_type in EdgeType.all:
            edge_type_name = self.edge_collection(edge_type)
            await create_update_graph(self.name, self.vertex_name, edge_type_name)

        vertex = db.graph(self.name).vertex_collection(self.vertex_name)
        in_progress = await create_collection(self.in_progress)
        create_update_indexes(vertex, in_progress)
        if feature.DB_SEARCH:
            await create_update_views(vertex)
        await self.insert_genesis_data()

    @staticmethod
    def db_edge_key(from_node: str, to_node: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{from_node}:{to_node}"))

    def query_search_token(self) -> str:
        return f"""
        FOR doc IN search_{self.vertex_name}
        SEARCH ANALYZER(doc.flat IN TOKENS(@tokens, 'text_en'), 'text_en')
        SORT BM25(doc) DESC
        LIMIT @limit
        RETURN doc
        """

    # parameter: rid
    # return: the complete document
    def query_node_by_id(self) -> str:
        return f"""
      FOR resource in {self.vertex_name}
      FILTER resource._key==@rid
      LIMIT 1
      RETURN resource
      """

    def query_update_nodes(self) -> str:
        return f"""
        FOR a IN {self.vertex_name}
        FILTER a.update_id==@update_id
        RETURN {{_key: a._key, hash:a.hash, created:a.created}}
        """

    def query_update_edges(self, edge_type: str) -> str:
        collection = self.edge_collection(edge_type)
        return f"""
        FOR a IN {collection}
        FILTER a.update_id==@update_id
        RETURN {{_key: a._key, _from: a._from, _to: a._to}}
        """

    def query_update_nodes_by_ids(self) -> str:
        return f"""
        FOR a IN {self.vertex_name}
        FILTER a._key IN @ids
        RETURN {{_key: a._key, hash:a.hash, created:a.created}}
        """

    def query_update_edges_by_ids(self, edge_type: str) -> str:
        collection = self.edge_collection(edge_type)
        return f"""
        FOR a IN {collection}
        FILTER a._key in @ids
        RETURN {{_key: a._key, _from: a._from, _to: a._to}}
        """

    def query_update_parent_linked(self) -> str:
        return f"""
        FOR a IN {self.edge_collection(EdgeType.default)}
        FILTER a._from==@from and a._to==@to
        RETURN true
        """

    def query_update_desired_metadata_many(self, section: str) -> str:
        return f"""
        FOR a IN {self.vertex_name}
        FILTER a._key in @node_ids
        UPDATE a with {{ "{section}": @patch }} IN {self.vertex_name}
        RETURN NEW
        """

    def query_delete_desired_metadata_many(self, section: str) -> str:
        return f"""
        FOR a IN {self.vertex_name}
        FILTER a._key in @node_ids
        REPLACE a with UNSET(a, "{section}") IN {self.vertex_name}
        RETURN NEW
        """

    def query_count_direct_children(self) -> str:
        return f"""
        FOR pn in {self.vertex_name} FILTER pn._key==@rid LIMIT 1
        FOR c IN 1..1 OUTBOUND pn {self.edge_collection(EdgeType.default)} COLLECT WITH COUNT INTO length
        RETURN length
        """

    def query_active_updates(self) -> str:
        return f"""
        FOR c IN {self.in_progress}
        RETURN {{id: c.change, created: c.created, affected_nodes: c.root_nodes, is_batch: c.is_batch}}
        """

    def query_active_change(self) -> str:
        return f"""
        FOR change IN {self.in_progress}
        FILTER @root_node_ids any in change.parent_node_ids OR @root_node_ids any in change.root_node_ids
        RETURN change
        """

    def update_active_change(self) -> str:
        return f"""
        FOR d in {self.in_progress}
        FILTER d.change == @change
        UPDATE d WITH {{created: DATE_ISO8601(DATE_NOW())}} in {self.in_progress}
        """


class EventGraphDB(GraphDB):
    def __init__(self, real: ArangoGraphDB, message_bus: MessageBus):
        self.real = real
        self.message_bus = message_bus
        self.graph_name = real.name

    @property
    def name(self) -> str:
        return self.real.name

    async def get_node(self, model: Model, node_id: str) -> Optional[Json]:
        return await self.real.get_node(model, node_id)

    async def create_node(self, model: Model, node_id: str, data: Json, under_node_id: str) -> Json:
        result = await self.real.create_node(model, node_id, data, under_node_id)
        await self.message_bus.emit_event(
            CoreEvent.NodeCreated, {"graph": self.graph_name, "id": node_id, "parent": under_node_id}
        )
        return result

    async def update_node(self, model: Model, node_id: str, patch: Json, section: Optional[str]) -> Json:
        result = await self.real.update_node(model, node_id, patch, section)
        await self.message_bus.emit_event(
            CoreEvent.NodeUpdated, {"graph": self.graph_name, "id": node_id, "section": section}
        )
        return result

    async def delete_node(self, node_id: str) -> None:
        result = await self.real.delete_node(node_id)
        await self.message_bus.emit_event(CoreEvent.NodeDeleted, {"graph": self.graph_name, "id": node_id})
        return result

    def update_nodes(self, model: Model, patches_by_id: Dict[str, Json], **kwargs: Any) -> AsyncGenerator[Json, None]:
        return self.real.update_nodes(model, patches_by_id, **kwargs)

    async def update_nodes_desired(
        self, model: Model, patch: Json, node_ids: List[str], **kwargs: Any
    ) -> AsyncGenerator[Json, None]:
        result = self.real.update_nodes_desired(model, patch, node_ids, **kwargs)
        await self.message_bus.emit_event(
            CoreEvent.NodesDesiredUpdated, {"graph": self.graph_name, "ids": node_ids, "patch": patch}
        )
        async for a in result:
            yield a

    async def update_nodes_metadata(
        self, model: Model, patch: Json, node_ids: List[str], **kwargs: Any
    ) -> AsyncGenerator[Json, None]:
        result = self.real.update_nodes_metadata(model, patch, node_ids, **kwargs)
        await self.message_bus.emit_event(
            CoreEvent.NodesMetadataUpdated, {"graph": self.graph_name, "ids": node_ids, "patch": patch}
        )
        async for a in result:
            yield a

    def search(self, model: Model, tokens: str, limit: int) -> AsyncGenerator[Json, None]:
        return self.real.search(model, tokens, limit)

    async def merge_graph(
        self, graph_to_merge: MultiDiGraph, model: Model, maybe_change_id: Optional[str] = None, is_batch: bool = False
    ) -> Tuple[List[str], GraphUpdate]:
        roots, info = await self.real.merge_graph(graph_to_merge, model, maybe_change_id, is_batch)
        even_data = {"graph": self.graph_name, "root_ids": roots}
        if info.all_changes():  # do not emit an event in case nothing has changed
            if is_batch:
                await self.message_bus.emit_event(CoreEvent.BatchUpdateGraphMerged, even_data)
            else:
                await self.message_bus.emit_event(CoreEvent.GraphMerged, even_data)
        return roots, info

    async def list_in_progress_updates(self) -> List[Json]:
        return await self.real.list_in_progress_updates()

    async def commit_batch_update(self, batch_id: str) -> None:
        info = first(lambda x: x["id"] == batch_id, await self.real.list_in_progress_updates())  # type: ignore
        await self.real.commit_batch_update(batch_id)
        await self.message_bus.emit_event(CoreEvent.BatchUpdateCommitted, {"graph": self.graph_name, "batch": info})

    async def abort_update(self, batch_id: str) -> None:
        info = first(lambda x: x["id"] == batch_id, await self.real.list_in_progress_updates())  # type: ignore
        await self.real.abort_update(batch_id)
        await self.message_bus.emit_event(CoreEvent.BatchUpdateAborted, {"graph": self.graph_name, "batch": info})

    def query_list(self, query: QueryModel, **kwargs: Any) -> AsyncGenerator[Json, None]:
        return self.real.query_list(query, **kwargs)

    def query_graph_gen(self, query: QueryModel) -> AsyncGenerator[Tuple[str, Json], None]:
        return self.real.query_graph_gen(query)

    def query_aggregation(self, query: QueryModel) -> AsyncGenerator[Json, None]:
        return self.real.query_aggregation(query)

    async def query_graph(self, query: QueryModel) -> DiGraph:
        return await self.real.query_graph(query)

    async def explain(self, query: QueryModel) -> Json:
        return await self.real.explain(query)

    async def wipe(self) -> None:
        result = await self.real.wipe()
        await self.message_bus.emit_event(CoreEvent.GraphDBWiped, {"graph": self.graph_name})
        return result

    def to_query(self, query_model: QueryModel, all_edges: bool = False) -> Tuple[str, Json]:
        return self.real.to_query(query_model, all_edges)

    async def create_update_schema(self) -> None:
        await self.real.create_update_schema()
