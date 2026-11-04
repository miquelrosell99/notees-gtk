"""The v28 edge-index backfill — pre-v25 databases created the derived
``edge`` + ``node_link`` tables additively (the v25 migration is DDL-only for
them) and re-derived a node's rows only when a later op rewrote that node, so
untouched nodes kept an empty index. v28 derives every stored node's rows in
place through the applier-time rebuild (the v27 ``search_plain`` backfill
precedent): the derive is a pure function of the committed mirror, so any
pre-v28 database converges to the same index.

Pins: structural convergence with a live-built index (every identity column),
the backfill's ``created_at`` stamp (the node row's own ``updated_at`` — the
order-invariance law), the property family's causal-value stamp surviving the
backfill, idempotence (a second run changes nothing), and the end-to-end
migration path (a database rewound to user_version 27 rebuilds its index when
a store opens it).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from notees_gtk.core.protocol.clock import Hlc
from notees_gtk.core.protocol.ids import new_uuid7
from notees_gtk.core.protocol.models import PROTOCOL_VERSION, RelayEnvelope
from notees_gtk.data.store import LocalStore

WS_A = "ws-a"
BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)


def uid(label: str) -> str:
    """Deterministic test UUID for an id label (test_store.py parity)."""
    return str(uuid5(NAMESPACE_URL, f"notees-gtk/test/{label}"))


def make_env(
    op_type: str,
    payload: dict[str, Any],
    *,
    hlc: tuple[int, int] = (1, 0),
    env_id: str | None = None,
    workspace_id: str = WS_A,
    actor_id: str = "actor-1",
    affected: tuple[str, ...] = (),
) -> RelayEnvelope:
    """Build a minimal valid envelope for store tests (test_edges.py parity)."""
    return RelayEnvelope(
        id=env_id or new_uuid7(),
        protocolVersion=PROTOCOL_VERSION,
        workspaceId=workspace_id,
        actorId=actor_id,
        deviceId="backfill-test-device",
        hlc=Hlc(physical=hlc[0], logical=hlc[1]),
        affectedNodeIds=list(affected),
        opType=op_type,
        payload=payload,
        timestamp=BASE_TS,
    )


def create_env(
    node_id: str,
    *,
    parent_id: str | None = None,
    content: Any = None,
    class_ids: tuple[str, ...] = (),
    tag_ids: tuple[str, ...] = (),
    hlc: tuple[int, int] = (1, 0),
) -> RelayEnvelope:
    """An ``object.create`` envelope; ``content`` is a raw token list."""
    payload: dict[str, Any] = {"objectId": node_id, "parentId": parent_id}
    if class_ids:
        payload["classIds"] = list(class_ids)
    if tag_ids:
        payload["tagIds"] = list(tag_ids)
    if content is not None:
        payload["contentAst"] = content
    return make_env("object.create", payload, hlc=hlc, affected=(node_id,))


def property_set_env(
    node_id: str, schema_id: str, *, value: Any, idx: int = 0, hlc: tuple[int, int]
) -> RelayEnvelope:
    return make_env(
        "property.set",
        {"objectId": node_id, "propertySchemaId": schema_id, "value": value, "idx": idx},
        hlc=hlc,
        affected=(node_id,),
    )


def mention(target: str, *, label: str = "m", link_id: str | None = None) -> dict[str, Any]:
    token: dict[str, Any] = {"type": "mention", "targetNodeId": target, "customLabel": label}
    if link_id is not None:
        token["linkId"] = link_id
    return token


def edges_of(store: LocalStore, source_id: str) -> list[tuple[Any, ...]]:
    """The source's edge rows in id order (test_edges.py parity)."""
    with sqlite3.connect(store._conn.execute("PRAGMA database_list").fetchone()[2]) as raw:  # noqa: SLF001
        return raw.execute(
            "SELECT type, target_id, resolved_target_id, verb, metadata, is_embedded, created_at"
            " FROM edge WHERE source_id = ? ORDER BY id",
            (source_id,),
        ).fetchall()


def make_embedded_source(
    store: LocalStore, source_id: str, *, content: list[Any], hlc: tuple[int, int] = (1, 0)
) -> str:
    """A parent page + an embedded child block carrying ``content``; the
    parent id is returned (test_edges.py parity)."""
    parent = uid(f"{source_id}-parent")
    store.apply_remote(create_env(parent, content=[{"type": "text", "text": "Page"}], hlc=hlc))
    store.apply_remote(create_env(source_id, parent_id=parent, content=content, hlc=(hlc[0] + 1, 0)))
    return parent


@pytest.fixture
def store(tmp_path: Path) -> Iterator[LocalStore]:
    instance = LocalStore(tmp_path / "store.db")
    try:
        yield instance
    finally:
        instance.close()


def all_edges(store: LocalStore) -> list[tuple[Any, ...]]:
    """Every edge row's identity columns in a stable order (created_at
    excluded — the live path stamps the applying envelope, the backfill the
    node row's ``updated_at``; the stamps are pinned separately). Reads on
    the store's own connection: backfill tests DELETE inside an open
    transaction before asserting."""
    return store._conn.execute(  # noqa: SLF001
        "SELECT id, workspace_id, source_id, target_id, resolved_target_id,"
        " type, verb, metadata, is_embedded FROM edge ORDER BY id"
    ).fetchall()


def build_world(store: LocalStore) -> None:
    """A small world exercising every edge family + the link assertions."""
    page = uid("page")
    other = uid("other")
    block = uid("block")
    tag = uid("tag")
    store.apply_remote(create_env(page, content=[{"type": "text", "text": "Page"}]))
    store.apply_remote(create_env(other, content=[{"type": "text", "text": "Other"}]))
    make_embedded_source(store, block, content=[mention(other, label="m", link_id=uid("link"))])
    store.apply_remote(create_env(uid("tagged"), content=[{"type": "text", "text": "Tagged"}], tag_ids=(tag,)))
    klass = uid("class")
    member = uid("member")
    store.apply_remote(create_env(klass, content=[{"type": "text", "text": "Class"}]))
    store.apply_remote(
        create_env(member, content=[{"type": "text", "text": "Member"}], class_ids=(klass,), hlc=(4, 0))
    )
    schema = uid("schema")
    store.apply_remote(
        make_env(
            "propertySchema.create",
            {"propertySchemaId": schema, "name": "Ref", "type": "object"},
            hlc=(6, 0),
        )
    )
    store.apply_remote(property_set_env(page, schema, value={"nodeId": other}, hlc=(7, 0)))


class TestBackfill:
    def test_backfill_converges_with_the_live_index(self, store: LocalStore) -> None:
        live = LocalStore(store._conn.execute("PRAGMA database_list").fetchone()[2] + "-live")  # noqa: SLF001
        try:
            build_world(live)
            build_world(store)
            # Simulate the pre-v25 emptiness: the tables exist, no rows.
            store._conn.execute("DELETE FROM edge")  # noqa: SLF001
            store._conn.execute("DELETE FROM node_link")  # noqa: SLF001
            assert all_edges(store) == []

            store._backfill_edge_index()  # noqa: SLF001

            assert all_edges(store) == all_edges(live)
            # The link-assertion rows come back too.
            backfilled = store._conn.execute(  # noqa: SLF001
                "SELECT id, source_id, target_id FROM node_link ORDER BY id"
            ).fetchall()
            live_links = live._conn.execute(  # noqa: SLF001
                "SELECT id, source_id, target_id FROM node_link ORDER BY id"
            ).fetchall()
            assert backfilled == live_links
        finally:
            live.close()

    def test_backfill_created_at_stamps_the_nodes_updated_at(self, store: LocalStore) -> None:
        build_world(store)
        store._conn.execute("DELETE FROM edge")  # noqa: SLF001
        store._backfill_edge_index()  # noqa: SLF001
        rows = store._conn.execute(  # noqa: SLF001
            "SELECT e.created_at, n.updated_at FROM edge e"
            " JOIN nodes n ON n.id = e.source_id"
            " WHERE e.type != 'property'"
        ).fetchall()
        assert rows
        for created_at, updated_at in rows:
            assert created_at == updated_at

    def test_property_edges_keep_their_causal_value_stamp(self, store: LocalStore) -> None:
        page = uid("page")
        other = uid("other")
        store.apply_remote(create_env(page, content=[{"type": "text", "text": "Page"}]))
        store.apply_remote(create_env(other, content=[{"type": "text", "text": "Other"}]))
        schema = uid("schema")
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {"propertySchemaId": schema, "name": "Ref", "type": "object"},
                hlc=(2, 0),
            )
        )
        store.apply_remote(property_set_env(page, schema, value={"nodeId": other}, hlc=(3, 0)))
        expected = edges_of(store, page)[0][-1]
        store._conn.execute("DELETE FROM edge")  # noqa: SLF001
        store._backfill_edge_index()  # noqa: SLF001
        assert edges_of(store, page)[0][-1] == expected
        # The causal stamp is the value row's hlc in ISO, NOT the node stamp.
        assert expected != BASE_TS.isoformat()

    def test_backfill_is_idempotent(self, store: LocalStore) -> None:
        build_world(store)
        store._conn.execute("DELETE FROM edge")  # noqa: SLF001
        store._backfill_edge_index()  # noqa: SLF001
        first = all_edges(store)
        store._backfill_edge_index()  # noqa: SLF001
        assert all_edges(store) == first


class TestMigrationPath:
    def test_reopening_a_v27_database_backfills_the_index(self, tmp_path: Path) -> None:
        path = tmp_path / "store.db"
        store = LocalStore(path)
        build_world(store)
        expected = all_edges(store)
        # Rewind to the pre-backfill world: empty index, version 27.
        store._conn.execute("DELETE FROM edge")  # noqa: SLF001
        store._conn.execute("DELETE FROM node_link")  # noqa: SLF001
        store._conn.execute("PRAGMA user_version = 27")  # noqa: SLF001
        store._conn.commit()  # noqa: SLF001
        store.close()

        reopened = LocalStore(path)
        try:
            assert reopened._conn.execute("PRAGMA user_version").fetchone()[0] == 28  # noqa: SLF001
            assert all_edges(reopened) == expected
        finally:
            reopened.close()

    def test_fresh_database_stays_empty_until_content_arrives(self, tmp_path: Path) -> None:
        path = tmp_path / "store.db"
        store = LocalStore(path)
        try:
            assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 28  # noqa: SLF001
            assert all_edges(store) == []
        finally:
            store.close()
