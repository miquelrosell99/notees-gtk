"""Tests for the local SQLite store: outbox, op-id dedupe, v2 appliers, migrations, snapshots.

The applier semantics asserted here are the v2 convergence rules ported from
``v2/packages/store/src/appliers.ts``: row-level LWW by (hlc, actor), OR-Set
class/collection membership, m2m class extends with a maintained closure
(cycles fail loud), fractional child-order positions, property tombstones,
and soft/permanent deletes with trash retention.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import make_server_snapshot

from notees_gtk.core.protocol.clock import Hlc
from notees_gtk.core.protocol.ids import new_uuid7
from notees_gtk.core.protocol.models import PROTOCOL_VERSION, RelayEnvelope
from notees_gtk.data.errors import CycleError, MoveGuardError, NotFoundError, PlacementError, UnsupportedCarrierError
from notees_gtk.data.store import LocalStore, NodeRow

WS_A = "ws-a"
WS_B = "ws-b"
ACTOR = "actor-1"
ACTOR_OTHER = "actor-2"
NODE = "node-1"
BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)

#: Sentinel: "field not carried" (distinct from an explicit None/null value).
UNSET: Any = object()

V2_TABLES = {
    "node_child_order",
    "class_property",
    "class_member_set",
    "class_extends",
    "class_hierarchy",
    "class",
    "property_schema",
    "property_value",
    "property_value_tombstone",
    "node_asset",
    "collection_member",
    "trash",
}


def make_env(
    op_type: str,
    payload: dict[str, Any],
    *,
    hlc: tuple[int, int] = (1, 0),
    env_id: str | None = None,
    workspace_id: str = WS_A,
    actor_id: str = ACTOR,
    affected: tuple[str, ...] = (),
) -> RelayEnvelope:
    """Build a minimal valid v2 envelope for store tests."""
    return RelayEnvelope(
        id=env_id or new_uuid7(),
        protocolVersion=PROTOCOL_VERSION,
        workspaceId=workspace_id,
        actorId=actor_id,
        deviceId="store-test-device",
        hlc=Hlc(physical=hlc[0], logical=hlc[1]),
        affectedNodeIds=list(affected),
        opType=op_type,
        payload=payload,
        timestamp=BASE_TS,
    )


def create_env(
    node_id: str = NODE,
    *,
    parent_id: str | None = None,
    node_type: str = "page",
    content: Any = UNSET,
    class_ids: tuple[str, ...] = (),
    name: Any = UNSET,
    hlc: tuple[int, int] = (1, 0),
    workspace_id: str = WS_A,
    env_id: str | None = None,
    actor_id: str = ACTOR,
) -> RelayEnvelope:
    """Build an ``object.create`` envelope; ``content`` may be a token list or a bare string."""
    payload: dict[str, Any] = {"objectId": node_id, "nodeType": node_type, "parentId": parent_id}
    if class_ids:
        payload["classIds"] = list(class_ids)
    if name is not UNSET:
        payload["name"] = name
    if content is not UNSET:
        payload["contentAst"] = [{"type": "text", "text": content}] if isinstance(content, str) else content
    return make_env(
        "object.create",
        payload,
        hlc=hlc,
        env_id=env_id,
        workspace_id=workspace_id,
        actor_id=actor_id,
        affected=(node_id,),
    )


def update_env(node_id: str, *, hlc: tuple[int, int], actor_id: str = ACTOR, **fields: Any) -> RelayEnvelope:
    """Build an ``object.update`` envelope; keyword fields are the carried payload fields."""
    payload: dict[str, Any] = {"objectId": node_id}
    payload.update(fields)
    return make_env("object.update", payload, hlc=hlc, actor_id=actor_id, affected=(node_id,))


def move_env(
    node_id: str,
    *,
    parent_id: str | None,
    after_id: str | None = None,
    hlc: tuple[int, int],
    actor_id: str = ACTOR,
) -> RelayEnvelope:
    payload: dict[str, Any] = {"objectId": node_id, "parentId": parent_id}
    if after_id is not None:
        payload["afterId"] = after_id
    return make_env("object.move", payload, hlc=hlc, actor_id=actor_id, affected=(node_id,))


def delete_env(node_id: str, *, permanent: bool = False, hlc: tuple[int, int]) -> RelayEnvelope:
    payload: dict[str, Any] = {"objectId": node_id}
    if permanent:
        payload["permanent"] = True
    return make_env("object.delete", payload, hlc=hlc, affected=(node_id,))


def class_env(op_type: str, class_id: str, *, hlc: tuple[int, int], **fields: Any) -> RelayEnvelope:
    payload: dict[str, Any] = {"classId": class_id}
    payload.update(fields)
    return make_env(op_type, payload, hlc=hlc, affected=(class_id,))


def extends_env(class_id: str, parents: list[str], *, hlc: tuple[int, int]) -> RelayEnvelope:
    return make_env("class.setExtends", {"classId": class_id, "parentClassIds": parents}, hlc=hlc, affected=(class_id,))


def property_set_env(
    node_id: str,
    schema_id: str,
    *,
    value: Any,
    idx: int = 0,
    metadata: Any = UNSET,
    hlc: tuple[int, int],
    actor_id: str = ACTOR,
) -> RelayEnvelope:
    payload: dict[str, Any] = {"objectId": node_id, "propertySchemaId": schema_id, "value": value, "idx": idx}
    if metadata is not UNSET:
        payload["metadata"] = metadata
    return make_env("property.set", payload, hlc=hlc, actor_id=actor_id, affected=(node_id,))


def property_unset_env(node_id: str, schema_id: str, *, idx: int = 0, hlc: tuple[int, int]) -> RelayEnvelope:
    return make_env(
        "property.unset",
        {"objectId": node_id, "propertySchemaId": schema_id, "idx": idx},
        hlc=hlc,
        affected=(node_id,),
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[LocalStore]:
    instance = LocalStore(tmp_path / "store.db")
    yield instance
    instance.close()


def raw_rows(store: LocalStore, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Run a raw SQL assertion against ``store``'s own database file."""
    with sqlite3.connect(_db_path(store)) as raw:
        return raw.execute(sql, params).fetchall()


class TestMigrations:
    def test_fresh_db_creates_every_table_at_latest_version(self, store: LocalStore, tmp_path: Path) -> None:
        with sqlite3.connect(tmp_path / "store.db") as raw:
            tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            version = raw.execute("PRAGMA user_version").fetchone()[0]
            columns = {row[1] for row in raw.execute("PRAGMA table_info(nodes)")}
        assert tables >= {"relay_outbox", "relay_operations", "sync_watermark", "nodes"}
        assert tables >= V2_TABLES
        assert version == 4
        # v2 node column names: is_active replaces archived; row-LWW columns
        # replace the v1 node_content_hlc watermark.
        assert {"is_active", "class_ids", "content_plain", "hlc_physical", "hlc_logical", "actor_id"} <= columns
        assert "archived" not in columns
        assert "node_content_hlc" not in tables

    def test_opening_same_database_twice_is_idempotent(self, tmp_path: Path) -> None:
        first = LocalStore(tmp_path / "s.db")
        first.set_cursor(WS_A, 3)
        first.close()
        second = LocalStore(tmp_path / "s.db")
        assert second.cursor(WS_A) == 3
        second.close()

    def test_rerunning_full_migration_chain_with_data_is_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "s.db"
        instance = LocalStore(path)
        instance.enqueue(create_env())
        instance.apply_remote(create_env(content="hi", hlc=(2, 0)))
        instance.set_cursor(WS_A, 7)
        instance.close()
        # Simulate an interrupted/future downgrade: the next open must re-run every
        # migration step without "duplicate column"/"table already exists" errors.
        with sqlite3.connect(path) as raw:
            raw.execute("PRAGMA user_version = 0")
        reopened = LocalStore(path)
        assert reopened.cursor(WS_A) == 7
        assert len(reopened.pending_outbox(WS_A)) == 1
        assert reopened.node(WS_A, NODE) is not None
        reopened.close()

    def test_v1_shape_database_is_reshaped_data_preserving(self, tmp_path: Path) -> None:
        """A pre-v2 cache (archived polarity, no v2 columns) upgrades in place."""
        path = tmp_path / "legacy.db"
        with sqlite3.connect(path) as raw:
            raw.executescript(
                """
                CREATE TABLE nodes (
                    workspace_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    parent_id TEXT,
                    node_type TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '',
                    icon TEXT,
                    color TEXT,
                    archived INTEGER NOT NULL DEFAULT 0,
                    content TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, id)
                );
                CREATE TABLE node_content_hlc (node_id TEXT PRIMARY KEY, physical INTEGER, logical INTEGER);
                INSERT INTO nodes (workspace_id, id, parent_id, node_type, name, archived, content, updated_at)
                VALUES ('ws-a', 'legacy-archived', NULL, 'page', '', 1, '"old"', '2026-01-01');
                INSERT INTO node_content_hlc (node_id, physical, logical) VALUES ('legacy-archived', 9, 1);
                PRAGMA user_version = 2;
                """
            )
        upgraded = LocalStore(path)
        row = upgraded.node(WS_A, "legacy-archived")
        assert row is not None
        assert row.is_active is False  # archived=1 → is_active=0
        assert row.content == '"old"'
        assert row.node_type == "page"
        upgraded.close()
        with sqlite3.connect(path) as raw:
            assert "node_content_hlc" not in {r[0] for r in raw.execute("SELECT name FROM sqlite_master")}


class TestOutbox:
    def test_enqueue_and_pending_roundtrip(self, store: LocalStore) -> None:
        env = create_env()
        store.enqueue(env)
        assert store.pending_outbox(WS_A) == [env]

    def test_pending_filters_workspace_and_respects_limit(self, store: LocalStore) -> None:
        a1 = create_env("n-a1")
        a2 = create_env("n-a2")
        b1 = create_env(workspace_id=WS_B)
        store.enqueue(a1)
        store.enqueue(b1)
        store.enqueue(a2)
        assert [env.id for env in store.pending_outbox(WS_A, limit=1)] == [a1.id]
        assert [env.id for env in store.pending_outbox(WS_A)] == [a1.id, a2.id]

    def test_mark_outbox_sent_removes_only_listed(self, store: LocalStore) -> None:
        e1, e2, e3 = create_env("n1"), create_env("n2"), create_env("n3")
        for env in (e1, e2, e3):
            store.enqueue(env)
        store.mark_outbox_sent([e1.id, e3.id])
        assert [env.id for env in store.pending_outbox(WS_A)] == [e2.id]

    def test_quarantine_parks_rows_with_reason(self, store: LocalStore, tmp_path: Path) -> None:
        env = create_env()
        store.enqueue(env)
        store.quarantine_outbox([env.id], "unknown op type")
        assert store.pending_outbox(WS_A) == []
        store.close()
        with sqlite3.connect(tmp_path / "store.db") as raw:
            row = raw.execute("SELECT state, quarantine_reason FROM relay_outbox").fetchone()
        assert row == ("quarantined", "unknown op type")

    def test_enqueue_skips_object_update_without_writable_field(self, store: LocalStore) -> None:
        store.enqueue(update_env(NODE, hlc=(2, 0)))
        store.enqueue(update_env(NODE, hlc=(3, 0), contentAst=None, name=None))
        assert store.pending_outbox(WS_A) == []

    def test_enqueue_accepts_object_update_with_any_writable_field(self, store: LocalStore) -> None:
        store.enqueue(update_env(NODE, hlc=(2, 0), name="only a name"))
        store.enqueue(update_env(NODE, hlc=(3, 0), contentAst=[{"type": "text", "text": "hi"}]))
        assert len(store.pending_outbox(WS_A)) == 2


class TestWatermark:
    def test_cursor_defaults_to_zero_and_roundtrips(self, store: LocalStore) -> None:
        assert store.cursor(WS_A) == 0
        store.set_cursor(WS_A, 42)
        assert store.cursor(WS_A) == 42

    def test_restore_epoch_defaults_to_zero_and_roundtrips(self, store: LocalStore) -> None:
        assert store.stored_restore_epoch(WS_A) == 0
        store.set_restore_epoch(WS_A, 5)
        assert store.stored_restore_epoch(WS_A) == 5


class TestWipe:
    def test_wipe_clears_every_table_for_workspace(self, store: LocalStore, tmp_path: Path) -> None:
        store.enqueue(create_env())
        store.apply_remote(create_env("parent", content="hello", hlc=(2, 0)))
        store.apply_remote(create_env("child", parent_id="parent", node_type="block", hlc=(3, 0)))
        store.apply_remote(property_set_env("parent", "schema-1", value={"v": 1}, hlc=(4, 0)))
        store.set_cursor(WS_A, 9)
        store.set_restore_epoch(WS_A, 4)
        store.wipe(WS_A)
        assert store.cursor(WS_A) == 0
        assert store.stored_restore_epoch(WS_A) == 0
        assert store.pending_outbox(WS_A) == []
        assert store.node(WS_A, NODE) is None
        store.close()
        with sqlite3.connect(tmp_path / "store.db") as raw:
            assert raw.execute("SELECT COUNT(*) FROM relay_operations").fetchone()[0] == 0
            assert raw.execute("SELECT COUNT(*) FROM node_child_order").fetchone()[0] == 0
            assert raw.execute("SELECT COUNT(*) FROM property_value").fetchone()[0] == 0

    def test_wipe_leaves_other_workspaces_untouched(self, store: LocalStore) -> None:
        store.apply_remote(create_env())
        store.apply_remote(create_env(workspace_id=WS_B, env_id=new_uuid7()))
        store.wipe(WS_A)
        remaining = store.node(WS_B, NODE)
        assert remaining is not None


class TestObjectCreate:
    def test_create_inserts_v2_row(self, store: LocalStore) -> None:
        store.apply_remote(create_env(parent_id=None, node_type="page", name="My Page"))
        store.apply_remote(update_env(NODE, hlc=(2, 0), icon="📄", color="red"))
        row = store.node(WS_A, NODE)
        assert row == NodeRow(
            id=NODE,
            workspace_id=WS_A,
            parent_id=None,
            node_type="page",
            name="My Page",
            class_ids=(),
            icon="📄",
            color="red",
            is_active=True,
            content="[]",
            content_plain="",
        )

    def test_create_defaults_child_node_type_to_block(self, store: LocalStore) -> None:
        store.apply_remote(create_env("parent", parent_id=None))
        store.apply_remote(create_env("child", parent_id="parent", node_type=""))
        assert store.node(WS_A, "child").node_type == "block"

    def test_create_content_ast_stored_as_json_with_derived_plaintext(self, store: LocalStore) -> None:
        store.apply_remote(
            create_env(
                content=[{"type": "text", "text": "hello"}, {"type": "hard_break"}, {"type": "text", "text": "world"}]
            )
        )
        row = store.node(WS_A, NODE)
        assert json.loads(row.content or "") == [
            {"type": "text", "text": "hello"},
            {"type": "hard_break"},
            {"type": "text", "text": "world"},
        ]
        assert row.content_plain == "hello world"

    def test_create_class_ids_seed_or_set_membership(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(class_ids=("c-b", "c-a")))
        row = store.node(WS_A, NODE)
        assert row.class_ids == ("c-a", "c-b")  # projected sorted from the OR-Set
        assert raw_rows(
            store,
            "SELECT class_id, present FROM class_member_set WHERE node_id = ? ORDER BY class_id",
            (NODE,),
        ) == [("c-a", 1), ("c-b", 1)]

    def test_block_without_parent_raises_placement_error(self, store: LocalStore) -> None:
        with pytest.raises(PlacementError, match="block must have a parent"):
            store.apply_remote(create_env(node_type="block", parent_id=None))

    def test_class_with_parent_raises_placement_error(self, store: LocalStore) -> None:
        store.apply_remote(create_env("parent", parent_id=None))
        with pytest.raises(PlacementError, match="class is tree-external"):
            store.apply_remote(create_env("cls", node_type="class", parent_id="parent"))

    def test_missing_parent_raises_not_found(self, store: LocalStore) -> None:
        with pytest.raises(NotFoundError, match="parent"):
            store.apply_remote(create_env("child", parent_id="missing"))

    def test_class_parent_raises_move_guard(self, store: LocalStore) -> None:
        store.apply_remote(class_env("class.create", "cls-1", hlc=(1, 0), name="Tag"))
        with pytest.raises(MoveGuardError, match="tree-external"):
            store.apply_remote(create_env("child", parent_id="cls-1"))

    def test_recreate_is_a_tree_no_op_but_unions_class_ids(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env("parent", parent_id=None))
        store.apply_remote(create_env("child", parent_id="parent", node_type="block", hlc=(2, 0)))
        before = raw_rows(store, "SELECT COUNT(*) FROM node_child_order")
        # Re-issue under a DIFFERENT parent with payload drift: tree untouched.
        assert store.apply_remote(create_env("child", parent_id=NODE, name="drift", hlc=(3, 0))) is False
        row = store.node(WS_A, "child")
        assert row is not None
        assert row.parent_id == "parent"
        assert row.name is None
        assert raw_rows(store, "SELECT COUNT(*) FROM node_child_order") == before
        # ...but classIds still seed the OR-Set (the convergence carrier).
        assert store.apply_remote(create_env("child", parent_id="parent", class_ids=("c-1",), hlc=(4, 0))) is False
        assert store.node(WS_A, "child").class_ids == ("c-1",)
        assert raw_rows(store, "SELECT COUNT(*) FROM node_child_order WHERE child_id = 'child'") == [(1,)]


class TestObjectUpdate:
    def test_fields_apply_and_stored_hlc_advances(self, store: LocalStore) -> None:
        store.apply_remote(create_env(hlc=(10, 0)))
        assert store.apply_remote(update_env(NODE, hlc=(11, 0), name="N", icon="i", color="c")) is True
        row = store.node(WS_A, NODE)
        assert row is not None and (row.name, row.icon, row.color) == ("N", "i", "c")
        assert raw_rows(store, "SELECT hlc_physical, hlc_logical FROM nodes WHERE id = ?", (NODE,)) == [(11, 0)]

    def test_lower_or_equal_hlc_update_dropped(self, store: LocalStore) -> None:
        store.apply_remote(create_env(hlc=(10, 0), content="v10"))
        assert store.apply_remote(update_env(NODE, hlc=(9, 0), name="stale")) is False
        assert store.apply_remote(update_env(NODE, hlc=(10, 0), name="equal")) is False
        row = store.node(WS_A, NODE)
        assert row is not None and row.name is None

    def test_equal_hlc_breaks_tie_on_actor(self, store: LocalStore) -> None:
        store.apply_remote(create_env(hlc=(10, 0)))
        store.apply_remote(update_env(NODE, hlc=(10, 0), actor_id=ACTOR, name="first"))
        # Same HLC, lexicographically greater actor wins; smaller loses.
        assert store.apply_remote(update_env(NODE, hlc=(10, 0), actor_id="zzz", name="second")) is True
        assert store.apply_remote(update_env(NODE, hlc=(10, 0), actor_id="aaa", name="third")) is False
        assert store.node(WS_A, NODE).name == "second"

    def test_content_ast_update_replaces_content_and_plaintext(self, store: LocalStore) -> None:
        store.apply_remote(create_env(content="orig", hlc=(10, 0)))
        assert store.apply_remote(update_env(NODE, hlc=(11, 0), contentAst=[{"type": "text", "text": "new"}])) is True
        row = store.node(WS_A, NODE)
        assert row is not None
        assert json.loads(row.content or "") == [{"type": "text", "text": "new"}]
        assert row.content_plain == "new"

    def test_content_delta_b64_without_ast_fails_loud(self, store: LocalStore) -> None:
        store.apply_remote(create_env(hlc=(10, 0)))
        with pytest.raises(UnsupportedCarrierError, match="Yjs"):
            store.apply_remote(update_env(NODE, hlc=(11, 0), contentDeltaB64="AAAA"))

    def test_node_type_flip_respects_placement(self, store: LocalStore) -> None:
        store.apply_remote(create_env("parent", parent_id=None))
        store.apply_remote(create_env("child", parent_id="parent", node_type="block", hlc=(2, 0)))
        assert store.apply_remote(update_env("child", hlc=(3, 0), nodeType="page")) is True
        assert store.node(WS_A, "child").node_type == "page"
        assert store.apply_remote(update_env("child", hlc=(4, 0), nodeType="block")) is True
        assert store.node(WS_A, "child").node_type == "block"
        # Demoting a PARENTLESS page to a block violates placement.
        store.apply_remote(create_env("lone-page", parent_id=None, hlc=(2, 0)))
        with pytest.raises(PlacementError):
            store.apply_remote(update_env("lone-page", hlc=(5, 0), nodeType="block"))
        # Declaring a parented node a class violates placement.
        with pytest.raises(PlacementError):
            store.apply_remote(update_env("child", hlc=(6, 0), nodeType="class"))


class TestObjectMove:
    def test_reparent_and_after_id_midpoint(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env("p", parent_id=None, hlc=(1, 0)))
        for index, node in enumerate(("x", "y", "z")):
            store.apply_remote(create_env(node, parent_id="p", node_type="block", hlc=(2 + index, 0)))
        # Enter placement: z jumps the queue to sit right after x.
        assert store.apply_remote(move_env("z", parent_id="p", after_id="x", hlc=(9, 0))) is True
        assert [row.id for row in store.children(WS_A, "p")] == ["x", "z", "y"]
        assert raw_rows(
            store,
            "SELECT position FROM node_child_order WHERE parent_id = 'p' AND child_id = 'z'",
        ) == [("a`",)]  # midpoint between "a" (x) and "aa" (y)

    def test_append_at_end_when_after_id_is_last(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env("p", parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env("x", parent_id="p", node_type="block", hlc=(2, 0)))
        store.apply_remote(create_env("y", parent_id="p", node_type="block", hlc=(3, 0)))
        store.apply_remote(move_env("x", parent_id="p", after_id="y", hlc=(4, 0)))
        assert [row.id for row in store.children(WS_A, "p")] == ["y", "x"]
        assert raw_rows(
            store,
            "SELECT position FROM node_child_order WHERE parent_id = 'p' AND child_id = 'x'",
        ) == [("aaa",)]

    def test_after_id_not_a_sibling_falls_back_to_append(self, store: LocalStore) -> None:
        store.apply_remote(create_env("p", parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env("other", parent_id=None, hlc=(2, 0)))
        store.apply_remote(create_env("x", parent_id="p", node_type="block", hlc=(3, 0)))
        store.apply_remote(create_env("y", parent_id="p", node_type="block", hlc=(4, 0)))
        store.apply_remote(move_env("y", parent_id="p", after_id="other", hlc=(5, 0)))
        assert [row.id for row in store.children(WS_A, "p")] == ["x", "y"]

    def test_reparent_carries_a_single_child_order_row(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env("a", parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env("b", parent_id=None, hlc=(2, 0)))
        store.apply_remote(create_env("c", parent_id="a", node_type="block", hlc=(3, 0)))
        store.apply_remote(move_env("c", parent_id="b", hlc=(4, 0)))
        assert store.node(WS_A, "c").parent_id == "b"
        assert raw_rows(
            store,
            "SELECT COUNT(*) FROM node_child_order WHERE child_id = 'c'",
        ) == [(1,)]
        assert [row.id for row in store.children(WS_A, "b")] == ["c"]

    def test_block_to_root_raises_placement_error(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env("p", parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env("c", parent_id="p", node_type="block", hlc=(2, 0)))
        with pytest.raises(PlacementError):
            store.apply_remote(move_env("c", parent_id=None, hlc=(3, 0)))
        # The throw rolls back: still parented, one child_order row.
        assert store.node(WS_A, "c").parent_id == "p"
        assert raw_rows(store, "SELECT COUNT(*) FROM node_child_order WHERE child_id = 'c'") == [(1,)]

    def test_root_move_of_page_drops_child_order_row(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env("p", parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env("sub", parent_id="p", node_type="page", hlc=(2, 0)))
        store.apply_remote(move_env("sub", parent_id=None, hlc=(3, 0)))
        assert store.node(WS_A, "sub").parent_id is None
        assert raw_rows(store, "SELECT COUNT(*) FROM node_child_order WHERE child_id = 'sub'") == [(0,)]

    def test_class_parent_raises_move_guard(self, store: LocalStore) -> None:
        store.apply_remote(create_env("p", parent_id=None, hlc=(1, 0)))
        store.apply_remote(class_env("class.create", "cls-1", hlc=(2, 0), name="Tag"))
        store.apply_remote(create_env("c", parent_id="p", node_type="block", hlc=(3, 0)))
        with pytest.raises(MoveGuardError):
            store.apply_remote(move_env("c", parent_id="cls-1", hlc=(4, 0)))

    def test_own_descendant_move_raises_move_guard(self, store: LocalStore) -> None:
        store.apply_remote(create_env("outer", parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env("inner", parent_id="outer", node_type="block", hlc=(2, 0)))
        with pytest.raises(MoveGuardError, match="own subtree"):
            store.apply_remote(move_env("outer", parent_id="inner", hlc=(3, 0)))

    def test_older_move_after_newer_is_dropped(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env("p", parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env("q", parent_id=None, hlc=(2, 0)))
        store.apply_remote(create_env("c", parent_id="p", node_type="block", hlc=(3, 0)))
        store.apply_remote(move_env("c", parent_id="q", hlc=(4, 0)))
        assert store.node(WS_A, "c").parent_id == "q"
        store.apply_remote(move_env("c", parent_id="p", hlc=(5, 0)))
        assert store.node(WS_A, "c").parent_id == "p"
        # Replay the older move with a fresh envelope id: must not regress.
        assert store.apply_remote(move_env("c", parent_id="q", hlc=(4, 0))) is False
        assert store.node(WS_A, "c").parent_id == "p"
        assert raw_rows(store, "SELECT COUNT(*) FROM node_child_order WHERE child_id = 'c'") == [(1,)]


class TestObjectDelete:
    def test_soft_delete_trashes_subtree(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env("p", parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env("c", parent_id="p", node_type="block", content="child", hlc=(2, 0)))
        store.apply_remote(delete_env("p", hlc=(3, 0)))
        assert store.node(WS_A, "p").is_active is False
        assert store.node(WS_A, "c").is_active is False
        assert store.nodes(WS_A) == []
        assert [row.id for row in store.nodes(WS_A, include_inactive=True)] == ["c", "p"]
        assert raw_rows(
            store,
            "SELECT is_permanent FROM trash WHERE node_id = 'p'",
        ) == [(0,)]

    def test_permanent_delete_hard_removes_subtree(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env("p", parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env("c", parent_id="p", node_type="block", hlc=(2, 0)))
        store.apply_remote(property_set_env("c", "s-1", value=1, hlc=(3, 0)))
        store.apply_remote(delete_env("p", permanent=True, hlc=(4, 0)))
        assert store.node(WS_A, "p") is None
        assert store.node(WS_A, "c") is None
        assert store.children(WS_A, "p") == []
        assert raw_rows(store, "SELECT COUNT(*) FROM property_value WHERE node_id = 'c'") == [(0,)]
        assert raw_rows(
            store,
            "SELECT is_permanent FROM trash WHERE node_id = 'p'",
        ) == [(1,)]


class TestClassOps:
    def test_class_create_registry_and_class_node(self, store: LocalStore) -> None:
        store.apply_remote(class_env("class.create", "cls-1", hlc=(1, 0), name="Book", icon="📕", color="blue"))
        row = store.node(WS_A, "cls-1")
        assert row is not None
        assert row.node_type == "class"
        assert row.parent_id is None
        assert row.name == "Book"
        # Reference semantics (appliers.ts upsertClassNode): the create's own
        # (hlc, actor) never beats its INSERT, so icon/color land on the class
        # node only via a later, higher-HLC class.update.
        assert row.icon is None
        registry = raw_rows(
            store,
            "SELECT name, icon, color, active FROM class WHERE id = 'cls-1'",
        )
        assert registry == [("Book", "📕", "blue", 1)]
        store.apply_remote(class_env("class.update", "cls-1", hlc=(2, 0), icon="📕", color="blue"))
        row = store.node(WS_A, "cls-1")
        assert row is not None and (row.icon, row.color) == ("📕", "blue")

    def test_class_update_and_delete(self, store: LocalStore) -> None:
        store.apply_remote(class_env("class.create", "cls-1", hlc=(1, 0), name="Book"))
        store.apply_remote(class_env("class.update", "cls-1", hlc=(2, 0), name="Novel", description="long form"))
        assert store.node(WS_A, "cls-1").name == "Novel"
        assert raw_rows(store, "SELECT description FROM class WHERE id = 'cls-1'") == [("long form",)]
        store.apply_remote(class_env("class.delete", "cls-1", hlc=(3, 0)))
        assert store.node(WS_A, "cls-1").is_active is False
        assert raw_rows(store, "SELECT active FROM class WHERE id = 'cls-1'") == [(0,)]

    def test_set_extends_maintains_closure_and_replaces(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(class_env("class.create", "a", hlc=(1, 0), name="Entity"))
        store.apply_remote(class_env("class.create", "b", hlc=(2, 0), name="Source"))
        store.apply_remote(class_env("class.create", "c", hlc=(3, 0), name="Annotated"))
        store.apply_remote(extends_env("b", ["a"], hlc=(4, 0)))
        store.apply_remote(extends_env("c", ["b"], hlc=(5, 0)))
        closure = raw_rows(
            store,
            "SELECT class_id, ancestor_id FROM class_hierarchy ORDER BY class_id, ancestor_id",
        )
        assert closure == [("a", "a"), ("b", "a"), ("b", "b"), ("c", "a"), ("c", "b"), ("c", "c")]
        # Replace [b] with []: only the self-row remains.
        store.apply_remote(extends_env("c", [], hlc=(6, 0)))
        assert raw_rows(
            store,
            "SELECT ancestor_id FROM class_hierarchy WHERE class_id = 'c' ORDER BY ancestor_id",
        ) == [("c",)]

    def test_set_extends_fails_loud_on_cycles(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(class_env("class.create", "a", hlc=(1, 0), name="A"))
        store.apply_remote(class_env("class.create", "b", hlc=(2, 0), name="B"))
        store.apply_remote(extends_env("b", ["a"], hlc=(3, 0)))
        with pytest.raises(CycleError, match="cannot extend itself"):
            store.apply_remote(extends_env("a", ["a"], hlc=(4, 0)))
        with pytest.raises(CycleError, match="would cycle"):
            store.apply_remote(extends_env("a", ["b"], hlc=(5, 0)))
        # A thrown apply rolls back: closure and edges keep the prefix state.
        assert raw_rows(
            store,
            "SELECT ancestor_id FROM class_hierarchy WHERE class_id = 'a' ORDER BY ancestor_id",
        ) == [("a",)]
        assert raw_rows(store, "SELECT COUNT(*) FROM class_extends") == [(1,)]

    def test_set_extends_requires_existing_classes(self, store: LocalStore) -> None:
        store.apply_remote(class_env("class.create", "a", hlc=(1, 0), name="A"))
        with pytest.raises(NotFoundError):
            store.apply_remote(extends_env("a", ["ghost"], hlc=(2, 0)))


class TestClassPropertyBindings:
    def test_set_inserts_row_and_patch_keeps_omitted_fields(self, store: LocalStore) -> None:
        store.apply_remote(class_env("class.create", "cls-1", hlc=(1, 0), name="Task"))
        store.apply_remote(
            make_env(
                "class.property.set",
                {"classId": "cls-1", "propertySchemaId": "ps-1", "sequence": 0, "defaultValue": "medium"},
                hlc=(2, 0),
            )
        )
        assert raw_rows(
            store,
            "SELECT sequence, required, readonly, hide_when_empty, default_value, hlc_physical"
            " FROM class_property WHERE class_id = 'cls-1' AND property_schema_id = 'ps-1'",
        ) == [(0, None, None, None, '"medium"', 2)]

        # Patch only default + required: sequence survives via COALESCE.
        store.apply_remote(
            make_env(
                "class.property.set",
                {"classId": "cls-1", "propertySchemaId": "ps-1", "defaultValue": "low", "required": True},
                hlc=(3, 0),
            )
        )
        assert raw_rows(
            store,
            "SELECT sequence, required, default_value, hlc_physical FROM class_property"
            " WHERE class_id = 'cls-1' AND property_schema_id = 'ps-1'",
        ) == [(0, 1, '"low"', 3)]

    def test_stale_set_dropped_by_row_lww(self, store: LocalStore) -> None:
        store.apply_remote(class_env("class.create", "cls-1", hlc=(1, 0), name="Task"))
        store.apply_remote(
            make_env(
                "class.property.set",
                {"classId": "cls-1", "propertySchemaId": "ps-1", "defaultValue": "low"},
                hlc=(3, 0),
            )
        )
        assert (
            store.apply_remote(
                make_env(
                    "class.property.set",
                    {"classId": "cls-1", "propertySchemaId": "ps-1", "defaultValue": "stale"},
                    hlc=(2, 0),
                )
            )
            is False
        )
        assert raw_rows(
            store, "SELECT default_value FROM class_property WHERE class_id = 'cls-1' AND property_schema_id = 'ps-1'"
        ) == [('"low"',)]

    def test_explicit_json_null_default_is_a_real_default(self, store: LocalStore) -> None:
        store.apply_remote(class_env("class.create", "cls-1", hlc=(1, 0), name="Task"))
        store.apply_remote(
            make_env(
                "class.property.set", {"classId": "cls-1", "propertySchemaId": "ps-1", "defaultValue": None}, hlc=(2, 0)
            )
        )
        assert raw_rows(
            store, "SELECT default_value FROM class_property WHERE class_id = 'cls-1' AND property_schema_id = 'ps-1'"
        ) == [("null",)]

    def test_unset_deletes_the_binding_row(self, store: LocalStore) -> None:
        store.apply_remote(class_env("class.create", "cls-1", hlc=(1, 0), name="Task"))
        store.apply_remote(
            make_env(
                "class.property.set",
                {"classId": "cls-1", "propertySchemaId": "ps-1", "defaultValue": "low"},
                hlc=(2, 0),
            )
        )
        store.apply_remote(
            make_env("class.property.unset", {"classId": "cls-1", "propertySchemaId": "ps-1"}, hlc=(3, 0))
        )
        assert raw_rows(store, "SELECT COUNT(*) FROM class_property") == [(0,)]


class TestClassUnassign:
    def _seed_classed_node(self, store: LocalStore, *, node_id: str = "n-x") -> None:
        store.apply_remote(class_env("class.create", "cls-x", hlc=(1, 0), name="X"))
        store.apply_remote(
            make_env(
                "propertySchema.create", {"propertySchemaId": "ps-x", "name": "effort", "type": "text"}, hlc=(2, 0)
            )
        )
        store.apply_remote(
            make_env(
                "class.property.set",
                {"classId": "cls-x", "propertySchemaId": "ps-x", "sequence": 0, "defaultValue": "xs"},
                hlc=(3, 0),
            )
        )
        store.apply_remote(
            make_env("object.create", {"objectId": node_id, "nodeType": "page", "classIds": ["cls-x"]}, hlc=(4, 0)),
        )

    def test_missing_node_fails_loud(self, store: LocalStore) -> None:
        with pytest.raises(NotFoundError, match="does not exist"):
            store.apply_remote(make_env("class.unassign", {"objectId": "ghost", "classId": "cls-x"}, hlc=(2, 0)))

    def test_newer_remove_clears_then_newer_add_restores(self, store: LocalStore) -> None:
        self._seed_classed_node(store)
        assert (
            store.apply_remote(make_env("class.unassign", {"objectId": "n-x", "classId": "cls-x"}, hlc=(5, 0))) is True
        )
        assert store.node(WS_A, "n-x").class_ids == ()
        assert store.get_effective_properties("n-x") == []
        row = raw_rows(
            store,
            "SELECT present, hlc_physical FROM class_member_set WHERE node_id = 'n-x' AND class_id = 'cls-x'",
        )
        assert row == [(0, 5)]
        # Re-add with a newer HLC restores membership and the derived default.
        store.apply_remote(make_env("object.create", {"objectId": "n-x", "classIds": ["cls-x"]}, hlc=(6, 0)))
        assert store.node(WS_A, "n-x").class_ids == ("cls-x",)
        effective = store.get_effective_properties("n-x")
        assert [(row.source, row.value, row.bound_by) for row in effective] == [("default", "xs", "cls-x")]

    def test_stale_remove_loses_to_newer_add(self, store: LocalStore) -> None:
        self._seed_classed_node(store)
        # Re-add at a higher HLC, then a stale (lower-HLC) remove must no-op.
        store.apply_remote(make_env("object.create", {"objectId": "n-x", "classIds": ["cls-x"]}, hlc=(6, 0)))
        assert (
            store.apply_remote(make_env("class.unassign", {"objectId": "n-x", "classId": "cls-x"}, hlc=(5, 5))) is True
        )
        assert store.node(WS_A, "n-x").class_ids == ("cls-x",)
        assert raw_rows(
            store,
            "SELECT present, hlc_physical FROM class_member_set WHERE node_id = 'n-x' AND class_id = 'cls-x'",
        ) == [(1, 6)]

    def test_exact_hlc_tie_add_wins_in_either_delivery_order(self, store: LocalStore) -> None:
        self._seed_classed_node(store)
        # Order 1: remove first, then the re-issued create at the SAME
        # (hlc, actor) — the add's >= comparator wins.
        store.apply_remote(make_env("class.unassign", {"objectId": "n-x", "classId": "cls-x"}, hlc=(7, 0)))
        store.apply_remote(make_env("object.create", {"objectId": "n-x", "classIds": ["cls-x"]}, hlc=(7, 0)))
        assert store.node(WS_A, "n-x").class_ids == ("cls-x",)

        # Order 2: the add lands first, then the equal-(hlc, actor) remove
        # (strictly-greater gate) is dropped.
        store.apply_remote(make_env("object.create", {"objectId": "n-y", "nodeType": "page"}, hlc=(8, 0)))
        store.apply_remote(make_env("object.create", {"objectId": "n-y", "classIds": ["cls-x"]}, hlc=(9, 0)))
        store.apply_remote(make_env("class.unassign", {"objectId": "n-y", "classId": "cls-x"}, hlc=(9, 0)))
        assert store.node(WS_A, "n-y").class_ids == ("cls-x",)


class TestPropertySchema:
    def test_registry_create_update_delete(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {
                    "propertySchemaId": "ps-1",
                    "name": "Status",
                    "type": "select",
                    "multi": False,
                    "scope": "class",
                    "options": [{"id": "o1", "label": "Open"}],
                },
                hlc=(1, 0),
            )
        )
        assert raw_rows(
            store,
            "SELECT name, type, multi, scope, options, active FROM property_schema WHERE id = 'ps-1'",
        ) == [("Status", "select", 0, "class", json.dumps([{"id": "o1", "label": "Open"}]), 1)]
        store.apply_remote(make_env("propertySchema.update", {"propertySchemaId": "ps-1", "name": "State"}, hlc=(2, 0)))
        store.apply_remote(make_env("propertySchema.delete", {"propertySchemaId": "ps-1"}, hlc=(3, 0)))
        assert raw_rows(store, "SELECT name, active FROM property_schema WHERE id = 'ps-1'") == [("State", 0)]


class TestPropertyValues:
    def test_set_lww_converges_both_orders(self, tmp_path: Path) -> None:
        laptop = property_set_env(
            NODE, "s-1", value={"n": "laptop"}, metadata={"since": "1962"}, hlc=(5, 0), actor_id="actor-laptop"
        )
        phone = property_set_env(
            NODE, "s-1", value={"n": "phone"}, metadata={"since": "1963"}, hlc=(5, 5), actor_id="actor-phone"
        )

        forward = LocalStore(tmp_path / "forward.db")
        forward.apply_remote(create_env(hlc=(1, 0)))
        forward.apply_remote(laptop.model_copy(update={"id": new_uuid7()}))
        forward.apply_remote(phone.model_copy(update={"id": new_uuid7()}))

        backward = LocalStore(tmp_path / "backward.db")
        backward.apply_remote(create_env(hlc=(1, 0)))
        backward.apply_remote(phone.model_copy(update={"id": new_uuid7()}))
        backward.apply_remote(laptop.model_copy(update={"id": new_uuid7()}))

        expected = (json.dumps({"n": "phone"}), json.dumps({"since": "1963"}), "actor-phone")
        for instance in (forward, backward):
            row = raw_rows(
                instance,
                "SELECT value, metadata, actor_id FROM property_value"
                " WHERE node_id = ? AND property_schema_id = 's-1' AND idx = 0",
                (NODE,),
            )
            assert row == [expected]
            instance.close()

    def test_unset_tombstone_wins_over_later_lower_set(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(hlc=(1, 0)))
        store.apply_remote(property_set_env(NODE, "s-1", value=1, hlc=(2, 0)))
        store.apply_remote(property_unset_env(NODE, "s-1", hlc=(3, 0)))
        assert raw_rows(store, "SELECT COUNT(*) FROM property_value WHERE node_id = ?", (NODE,)) == [(0,)]
        # A lower-HLC set must not resurrect the value.
        assert store.apply_remote(property_set_env(NODE, "s-1", value=2, hlc=(2, 5))) is False
        assert raw_rows(store, "SELECT COUNT(*) FROM property_value WHERE node_id = ?", (NODE,)) == [(0,)]
        # A higher-HLC set does.
        assert store.apply_remote(property_set_env(NODE, "s-1", value=3, hlc=(4, 0))) is True
        assert raw_rows(store, "SELECT value FROM property_value WHERE node_id = ?", (NODE,)) == [("3",)]


class TestAssetsAndCollections:
    def test_asset_attach_detach(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(hlc=(1, 0)))
        store.apply_remote(
            make_env(
                "asset.attach",
                {
                    "objectId": NODE,
                    "assetId": "asset-1",
                    "hash": "a" * 64,
                    "mimeType": "image/png",
                    "size": 1234,
                    "originalName": "cover.png",
                },
                hlc=(2, 0),
                affected=(NODE,),
            )
        )
        assert raw_rows(
            store,
            "SELECT hash, mime_type, size, original_name FROM node_asset WHERE node_id = ? AND asset_id = 'asset-1'",
            (NODE,),
        ) == [("a" * 64, "image/png", 1234, "cover.png")]
        store.apply_remote(
            make_env("asset.detach", {"objectId": NODE, "assetId": "asset-1"}, hlc=(3, 0), affected=(NODE,))
        )
        assert raw_rows(store, "SELECT COUNT(*) FROM node_asset") == [(0,)]

    def test_collection_membership_is_add_wins_or_set(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(hlc=(1, 0)))
        add = make_env(
            "collection.member.add", {"collectionId": "col-1", "objectId": NODE}, hlc=(2, 0), affected=("col-1", NODE)
        )
        remove = make_env(
            "collection.member.remove",
            {"collectionId": "col-1", "objectId": NODE},
            hlc=(2, 0),
            affected=("col-1", NODE),
        )
        # Equal (hlc, actor): the add wins over the remove.
        store.apply_remote(add.model_copy(update={"id": new_uuid7()}))
        store.apply_remote(remove.model_copy(update={"id": new_uuid7()}))
        assert raw_rows(
            store,
            "SELECT present FROM collection_member WHERE collection_id = 'col-1' AND object_id = ?",
            (NODE,),
        ) == [(1,)]
        # A strictly higher-HLC remove clears membership.
        store.apply_remote(
            make_env(
                "collection.member.remove",
                {"collectionId": "col-1", "objectId": NODE},
                hlc=(3, 0),
                affected=("col-1", NODE),
            )
        )
        assert raw_rows(
            store,
            "SELECT present FROM collection_member WHERE collection_id = 'col-1' AND object_id = ?",
            (NODE,),
        ) == [(0,)]


class TestApplyRemote:
    def test_dedupe_returns_false_on_second_apply(self, store: LocalStore) -> None:
        env = create_env()
        assert store.apply_remote(env) is True
        assert store.apply_remote(env) is False

    def test_unknown_op_type_is_logged_and_skipped(self, store: LocalStore, caplog: pytest.LogCaptureFixture) -> None:
        env = make_env("plugin.op", {"plugin": "x"})
        assert store.apply_remote(env) is False
        assert any("plugin.op" in record.getMessage() for record in caplog.records)


class TestNodesQuery:
    def test_nodes_filters_by_parent(self, store: LocalStore) -> None:
        store.apply_remote(create_env("n1", parent_id=None))
        store.apply_remote(create_env("n2", parent_id="n1", node_type="block"))
        assert sorted(row.id for row in store.nodes(WS_A)) == ["n1", "n2"]
        assert [row.id for row in store.nodes(WS_A, parent_id="n1")] == ["n2"]

    def test_node_returns_none_for_missing(self, store: LocalStore) -> None:
        assert store.node(WS_A, "missing") is None


class TestSnapshotRestore:
    def test_restore_maps_v2_server_schema(self, store: LocalStore) -> None:
        mirror = '[{"type":"text","text":"snap"}]'
        blob = make_server_snapshot(
            [
                {
                    "id": "n1",
                    "workspace_id": WS_A,
                    "node_type": "page",
                    "parent_id": None,
                    "class_ids": json.dumps(["c-1"]),
                    "name": "Snap Page",
                    "content": mirror,
                    "icon": "📄",
                    "color": None,
                    "is_active": 0,
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "created_by": ACTOR,
                    "hlc_physical": 5,
                    "hlc_logical": 2,
                    "actor_id": ACTOR,
                },
                {"id": "n2", "workspace_id": WS_A, "node_type": "block", "parent_id": "n1", "content": "[]"},
            ]
        )
        assert store.restore_snapshot(blob, workspace_id=WS_A) is True
        rows = {row.id: row for row in store.nodes(WS_A, include_inactive=True)}
        # Same-name, same-polarity mapping: node_type/is_active/class_ids/name
        # copy verbatim and the row-LWW columns seed the LWW baseline.
        assert rows["n1"].node_type == "page"
        assert rows["n1"].is_active is False  # is_active=0
        assert rows["n1"].content == mirror
        assert rows["n1"].class_ids == ("c-1",)
        assert rows["n1"].name == "Snap Page"
        assert rows["n2"].node_type == "block"
        assert rows["n2"].is_active is True
        # Seeded LWW baseline (hlc 5,2): lower/equal writes must lose.
        assert store.apply_remote(update_env("n1", hlc=(4, 9), contentAst=[{"type": "text", "text": "stale"}])) is False
        assert store.node(WS_A, "n1").content == mirror
        assert store.apply_remote(update_env("n1", hlc=(5, 3), contentAst=[{"type": "text", "text": "newer"}])) is True
        assert json.loads(store.node(WS_A, "n1").content or "") == [{"type": "text", "text": "newer"}]

    def test_restore_without_hlc_columns_has_no_lww_baseline(self, store: LocalStore) -> None:
        # Pre-HLC server snapshots lack hlc columns: restore must still work
        # and simply not seed an LWW baseline.
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE node (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                node_type TEXT NOT NULL CHECK (node_type IN ('page', 'block', 'class')),
                parent_id TEXT,
                content TEXT NOT NULL DEFAULT '[]',
                is_active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            "INSERT INTO node (id, workspace_id, node_type, content) VALUES ('legacy', ?, 'page', 'old')",
            (WS_A,),
        )
        blob = conn.serialize()
        conn.close()
        assert store.restore_snapshot(blob, workspace_id=WS_A) is True
        row = store.node(WS_A, "legacy")
        assert row is not None and row.node_type == "page" and row.content == "old"
        # No baseline → any later update wins.
        assert (
            store.apply_remote(update_env("legacy", hlc=(1, 0), contentAst=[{"type": "text", "text": "new"}])) is True
        )
        assert json.loads(store.node(WS_A, "legacy").content or "") == [{"type": "text", "text": "new"}]

    def test_restore_filters_by_workspace(self, store: LocalStore) -> None:
        blob = make_server_snapshot(
            [
                {"id": "n1", "workspace_id": WS_A, "node_type": "page", "content": "[]"},
                {"id": "n2", "workspace_id": WS_B, "node_type": "page", "content": "[]"},
            ]
        )
        assert store.restore_snapshot(blob, workspace_id=WS_A) is True
        assert [row.id for row in store.nodes(WS_A, include_inactive=True)] == ["n1"]

    def test_restore_with_zero_overlapping_columns_returns_false(self, store: LocalStore) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE node (only_server_col TEXT)")
        conn.execute("INSERT INTO node VALUES ('x')")
        blob = conn.serialize()
        conn.close()
        assert store.restore_snapshot(blob, workspace_id=WS_A) is False

    def test_restore_rejects_unrecognized_snapshot_tables(self, store: LocalStore) -> None:
        # The plural ``nodes`` table is a client-cache invention, not a server
        # snapshot — accepting it is how the original double-fake bug hid.
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE nodes (id TEXT, workspace_id TEXT, node_type TEXT)")
        conn.execute("INSERT INTO nodes VALUES ('x', ?, 'page')", (WS_A,))
        blob = conn.serialize()
        conn.close()
        assert store.restore_snapshot(blob, workspace_id=WS_A) is False

    def test_restore_corrupt_blob_returns_false_and_leaves_state_untouched(self, store: LocalStore) -> None:
        store.apply_remote(create_env(content="local", hlc=(2, 0)))
        store.set_cursor(WS_A, 17)
        assert store.restore_snapshot(b"this is not a sqlite database", workspace_id=WS_A) is False
        assert store.node(WS_A, NODE).content_plain == "local"
        assert store.cursor(WS_A) == 17
        # The store is still fully usable after a failed restore.
        assert (
            store.apply_remote(update_env(NODE, hlc=(3, 0), contentAst=[{"type": "text", "text": "still works"}]))
            is True
        )


# --------------------------------------------------------------------- helpers


def _db_path(store: LocalStore) -> Path:
    """Recover the db path for raw assertions (tests only)."""
    return Path(str(store._conn.execute("PRAGMA database_list").fetchone()[2]))  # noqa: SLF001
