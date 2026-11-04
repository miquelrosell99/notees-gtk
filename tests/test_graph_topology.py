"""The graph topology projection — the ``packages/store/src/graph.ts``
reduced port over the live mirror (the store's ``graph_topology`` seam).

Pins: the node-set law (classes + standalone pages only; aliases never
vertices; trashed nodes out; parentlessRole-"block" members out), the
block/alias endpoint rollup, the five structural edge families (mention /
property / tag from the derived index, parent main-node parentage, class
OR-Set membership), parallel merging with the earliest stamp, self-loop and
out-of-set drops, per-node degree, and the deterministic output ordering.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from notees_gtk.core.graph.model import GraphEdge, GraphTopology
from notees_gtk.core.protocol.clock import Hlc
from notees_gtk.core.protocol.ids import new_uuid7
from notees_gtk.core.protocol.models import PROTOCOL_VERSION, RelayEnvelope
from notees_gtk.data.store import LocalStore

WS_A = "ws-a"
BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)


def uid(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"notees-gtk/test/{label}"))


def make_env(
    op_type: str,
    payload: dict[str, Any],
    *,
    hlc: tuple[int, int] = (1, 0),
    timestamp: datetime = BASE_TS,
    affected: tuple[str, ...] = (),
) -> RelayEnvelope:
    return RelayEnvelope(
        id=new_uuid7(),
        protocolVersion=PROTOCOL_VERSION,
        workspaceId=WS_A,
        actorId="graph-test",
        deviceId="graph-test-device",
        hlc=Hlc(physical=hlc[0], logical=hlc[1]),
        affectedNodeIds=list(affected),
        opType=op_type,
        payload=payload,
        timestamp=timestamp,
    )


def text(text_value: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text_value}]


def mention(target: str, label: str = "m") -> dict[str, Any]:
    return {"type": "mention", "targetNodeId": target, "customLabel": label}


def create_env(
    node_id: str,
    *,
    parent_id: str | None = None,
    content: Any = None,
    class_ids: tuple[str, ...] = (),
    tag_ids: tuple[str, ...] = (),
    embedded: bool | None = None,
    hlc: tuple[int, int] = (1, 0),
    timestamp: datetime = BASE_TS,
) -> RelayEnvelope:
    payload: dict[str, Any] = {"objectId": node_id, "parentId": parent_id}
    if class_ids:
        payload["classIds"] = list(class_ids)
    if tag_ids:
        payload["tagIds"] = list(tag_ids)
    if embedded is not None:
        payload["isEmbedded"] = embedded
    if content is not None:
        payload["contentAst"] = content
    return make_env("object.create", payload, hlc=hlc, timestamp=timestamp, affected=(node_id,))


def update_env(node_id: str, *, hlc: tuple[int, int], **fields: Any) -> RelayEnvelope:
    payload: dict[str, Any] = {"objectId": node_id}
    payload.update(fields)
    return make_env("object.update", payload, hlc=hlc, affected=(node_id,))


@pytest.fixture
def store(tmp_path: Path) -> Iterator[LocalStore]:
    instance = LocalStore(tmp_path / "store.db")
    try:
        yield instance
    finally:
        instance.close()


def edge_map(topology: GraphTopology) -> dict[tuple[str, str, str], GraphEdge]:
    return {(edge.kind, edge.source, edge.target): edge for edge in topology.edges}


class TestNodeSet:
    def test_pages_and_classes_are_vertices_blocks_are_not(self, store: LocalStore) -> None:
        page = uid("page")
        klass = uid("class")
        block = uid("block")
        store.apply_remote(create_env(page, content=text("Page")))
        store.apply_remote(create_env(klass, content=text("Class")))
        store.apply_remote(create_env(block, parent_id=page, content=text("block")))
        topology = store.graph_topology(WS_A)
        assert {node.id for node in topology.nodes} == {page, klass}
        assert all(node.degree == 0 for node in topology.nodes)

    def test_trashed_nodes_leave_the_topology(self, store: LocalStore) -> None:
        page = uid("page")
        trashed = uid("trashed")
        store.apply_remote(create_env(page, content=text("Page")))
        store.apply_remote(create_env(trashed, content=text("Trashed")))
        store.apply_remote(make_env("object.delete", {"objectId": trashed}, hlc=(5, 0)))
        topology = store.graph_topology(WS_A)
        assert [node.id for node in topology.nodes] == [page]

    def test_aliases_are_not_vertices(self, store: LocalStore) -> None:
        page = uid("page")
        alias = uid("alias")
        store.apply_remote(create_env(page, content=text("Page")))
        store.apply_remote(create_env(alias, content=text("Alias")))
        store.apply_remote(update_env(alias, hlc=(5, 0), aliasedNodeId=page))
        topology = store.graph_topology(WS_A)
        assert [node.id for node in topology.nodes] == [page]

    def test_parentless_block_role_members_leave_the_topology(self, store: LocalStore) -> None:
        klass = uid("klass")
        member = uid("member")
        store.apply_remote(
            make_env("class.create", {"classId": klass, "contentAst": text("Snippet")}, hlc=(1, 0), affected=(klass,))
        )
        store.apply_remote(
            make_env(
                "class.update",
                {"classId": klass, "parentlessRole": "block"},
                hlc=(2, 0),
                affected=(klass,),
            )
        )
        store.apply_remote(create_env(member, content=text("Snippet member"), class_ids=(klass,), hlc=(3, 0)))
        store.apply_remote(create_env(uid("page"), content=text("Page"), hlc=(4, 0)))
        topology = store.graph_topology(WS_A)
        assert klass in {node.id for node in topology.nodes}
        assert member not in {node.id for node in topology.nodes}


class TestRollup:
    def test_mention_on_a_block_lands_between_the_pages(self, store: LocalStore) -> None:
        source_page = uid("source-page")
        block = uid("block")
        target = uid("target")
        store.apply_remote(create_env(source_page, content=text("Source")))
        store.apply_remote(create_env(block, parent_id=source_page, content=[mention(target)]))
        store.apply_remote(create_env(target, content=text("Target")))
        topology = store.graph_topology(WS_A)
        edges = edge_map(topology)
        assert ("mention", source_page, target) in edges
        assert all(block not in (edge.source, edge.target) for edge in topology.edges)

    def test_block_target_rolls_up_to_its_page(self, store: LocalStore) -> None:
        source = uid("source")
        target_page = uid("target-page")
        target_block = uid("target-block")
        store.apply_remote(create_env(source, content=text("Source")))
        store.apply_remote(create_env(target_page, content=text("Target")))
        store.apply_remote(create_env(target_block, parent_id=target_page, content=text("block")))
        # The mention predates nothing here: authored after every node
        # exists, aimed at the embedded block — the endpoint rolls up.
        store.apply_remote(update_env(source, hlc=(5, 0), contentAst=[mention(target_block)]))
        topology = store.graph_topology(WS_A)
        assert ("mention", source, target_page) in edge_map(topology)

    def test_mention_of_a_parentless_block_drops(self, store: LocalStore) -> None:
        klass = uid("klass")
        block = uid("block")
        source = uid("source")
        store.apply_remote(
            make_env("class.create", {"classId": klass, "contentAst": text("Snippet")}, hlc=(1, 0), affected=(klass,))
        )
        store.apply_remote(
            make_env("class.update", {"classId": klass, "parentlessRole": "block"}, hlc=(2, 0), affected=(klass,))
        )
        store.apply_remote(create_env(block, content=text("loose"), class_ids=(klass,), hlc=(3, 0)))
        store.apply_remote(create_env(source, content=text("Source"), hlc=(4, 0)))
        store.apply_remote(update_env(source, hlc=(5, 0), contentAst=[mention(block)]))
        topology = store.graph_topology(WS_A)
        assert all(edge.kind != "mention" for edge in topology.edges)

    def test_alias_endpoint_resolves_to_the_terminal(self, store: LocalStore) -> None:
        page = uid("page")
        alias = uid("alias")
        source = uid("source")
        store.apply_remote(create_env(page, content=text("Page")))
        store.apply_remote(create_env(alias, content=text("Alias")))
        store.apply_remote(update_env(alias, hlc=(5, 0), aliasedNodeId=page))
        store.apply_remote(create_env(source, content=text("Source")))
        store.apply_remote(update_env(source, hlc=(6, 0), contentAst=[mention(alias)]))
        topology = store.graph_topology(WS_A)
        assert ("mention", source, page) in edge_map(topology)


class TestFamilies:
    def test_tag_property_parent_and_class_edges(self, store: LocalStore) -> None:
        parent = uid("parent")
        subpage = uid("subpage")
        tag = uid("tag")
        klass = uid("klass")
        member = uid("member")
        schema = uid("schema")
        tagged = uid("tagged")
        store.apply_remote(create_env(parent, content=text("Parent")))
        store.apply_remote(create_env(subpage, parent_id=parent, embedded=False, content=text("Sub"), hlc=(2, 0)))
        store.apply_remote(create_env(tag, content=text("Tag"), hlc=(3, 0)))
        store.apply_remote(create_env(tagged, content=text("Tagged"), tag_ids=(tag,), hlc=(4, 0)))
        store.apply_remote(create_env(klass, content=text("Class"), hlc=(5, 0)))
        store.apply_remote(create_env(member, content=text("Member"), class_ids=(klass,), hlc=(6, 0)))
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {"propertySchemaId": schema, "name": "Ref", "type": "object"},
                hlc=(7, 0),
            )
        )
        store.apply_remote(
            make_env(
                "property.set",
                {"objectId": parent, "propertySchemaId": schema, "value": {"nodeId": tag}, "idx": 0},
                hlc=(8, 0),
                affected=(parent,),
            )
        )
        edges = edge_map(store.graph_topology(WS_A))
        assert ("tag", tagged, tag) in edges
        assert ("property", parent, tag) in edges
        assert ("parent", parent, subpage) in edges
        assert ("class", member, klass) in edges

    def test_self_mention_drops(self, store: LocalStore) -> None:
        page = uid("page")
        store.apply_remote(create_env(page, content=text("Page")))
        store.apply_remote(update_env(page, hlc=(5, 0), contentAst=[mention(page)]))
        topology = store.graph_topology(WS_A)
        assert topology.edges == ()

    def test_parallel_property_edges_merge_with_the_earliest_stamp(self, store: LocalStore) -> None:
        # Two visible-set value rows of the same schema pointing at the same
        # target collapse into one weighted edge whose stamp is the EARLIEST
        # contributing row's causal stamp (property edges stamp their value
        # row, so parallel rows genuinely carry different stamps — the
        # content families always share one op stamp).
        page = uid("page")
        target = uid("target")
        schema = uid("schema")
        store.apply_remote(create_env(page, content=text("Page")))
        store.apply_remote(create_env(target, content=text("Target")))
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {"propertySchemaId": schema, "name": "Ref", "type": "object", "multi": True},
                hlc=(2, 0),
            )
        )
        store.apply_remote(
            make_env(
                "property.set",
                {"objectId": page, "propertySchemaId": schema, "value": {"nodeId": target}, "idx": 0},
                hlc=(5, 0),
                affected=(page,),
            )
        )
        store.apply_remote(
            make_env(
                "property.set",
                {"objectId": page, "propertySchemaId": schema, "value": {"nodeId": target}, "idx": 1},
                hlc=(6, 0),
                affected=(page,),
            )
        )
        topology = store.graph_topology(WS_A)
        edges = edge_map(topology)
        assert ("property", page, target) in edges
        assert edges[("property", page, target)].weight == 2
        assert edges[("property", page, target)].created_at == datetime.fromtimestamp(5 / 1000, tz=UTC).isoformat()

    def test_degree_counts_both_endpoints(self, store: LocalStore) -> None:
        hub = uid("hub")
        a = uid("a")
        b = uid("b")
        store.apply_remote(create_env(hub, content=text("Hub")))
        store.apply_remote(create_env(a, content=text("A")))
        store.apply_remote(create_env(b, content=text("B")))
        store.apply_remote(update_env(hub, hlc=(5, 0), contentAst=[mention(a), mention(b)]))
        topology = store.graph_topology(WS_A)
        degrees = {node.id: node.degree for node in topology.nodes}
        assert degrees == {hub: 2, a: 1, b: 1}


class TestShape:
    def test_output_ordering_is_deterministic(self, store: LocalStore) -> None:
        z_page = uid("z-page")
        a_page = uid("a-page")
        m_page = uid("m-page")
        store.apply_remote(create_env(z_page, content=text("Z")))
        store.apply_remote(create_env(a_page, content=text("A")))
        store.apply_remote(create_env(m_page, content=text("M")))
        first = store.graph_topology(WS_A)
        second = store.graph_topology(WS_A)
        assert first == second
        assert [node.id for node in first.nodes] == sorted(node.id for node in first.nodes)
        edge_keys = [(edge.kind, edge.source, edge.target) for edge in first.edges]
        assert edge_keys == sorted(edge_keys)

    def test_empty_workspace_projects_empty(self, store: LocalStore) -> None:
        topology = store.graph_topology(WS_A)
        assert topology.nodes == ()
        assert topology.edges == ()
