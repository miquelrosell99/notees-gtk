"""Tests for the local SQLite store: outbox, op-id dedupe, v2 appliers, migrations, snapshots.

The applier semantics asserted here are the v2 convergence rules ported from
``v2/packages/store/src/appliers.ts``: row-level LWW by (hlc, actor), OR-Set
class/tag/collection membership, user-defined class order (class.reorder,
LWW-by-arrival), title-is-content (no ``name`` writes; pages/classes carry
text-only content), m2m class extends with a maintained closure (cycles fail
loud), fractional child-order positions, property tombstones, and
soft/permanent deletes with trash retention.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
from conftest import make_server_snapshot

from notees_gtk.core.protocol.clock import Hlc
from notees_gtk.core.protocol.ids import new_uuid7
from notees_gtk.core.protocol.models import PROTOCOL_VERSION, RelayEnvelope
from notees_gtk.data.errors import (
    CycleError,
    EnvelopeValidationError,
    MoveGuardError,
    NotFoundError,
    PlacementError,
    UnsupportedCarrierError,
)
from notees_gtk.data.store import LocalStore, NodeRow

WS_A = "ws-a"
WS_B = "ws-b"
ACTOR = "actor-1"
ACTOR_OTHER = "actor-2"
BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)

#: Sentinel: "field not carried" (distinct from an explicit None/null value).
UNSET: Any = object()


def uid(label: str) -> str:
    """Deterministic test UUID for an id label.

    The strict payload validator (op-types.ts parity) requires UUID-shaped
    ids on the wire, so tests label nodes/classes/tags and derive the UUID.
    """
    return str(uuid5(NAMESPACE_URL, f"notees-gtk/test/{label}"))


NODE = uid("node-1")


V2_TABLES = {
    "node_child_order",
    "class_property",
    "class_member_set",
    "tag_member_set",
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
    tag_ids: tuple[str, ...] = (),
    name: Any = UNSET,
    hlc: tuple[int, int] = (1, 0),
    workspace_id: str = WS_A,
    env_id: str | None = None,
    actor_id: str = ACTOR,
) -> RelayEnvelope:
    """Build an ``object.create`` envelope; ``content`` may be a token list or a bare string.

    Mirrors the client builder (``build_object_create``): the ``name``
    convenience becomes a single text token when no explicit content is
    given; when both are given, content wins and name is dropped.
    """
    payload: dict[str, Any] = {"objectId": node_id, "nodeType": node_type, "parentId": parent_id}
    if class_ids:
        payload["classIds"] = list(class_ids)
    if tag_ids:
        payload["tagIds"] = list(tag_ids)
    if name is not UNSET and content is UNSET:
        payload["contentAst"] = [{"type": "text", "text": name}]
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


def class_env(op_type: str, class_id: str, *, hlc: tuple[int, int], name: Any = UNSET, **fields: Any) -> RelayEnvelope:
    """Build a class op envelope; the ``name`` convenience wraps into a text
    token (``contentAst``) exactly like the client builder."""
    payload: dict[str, Any] = {"classId": class_id}
    if name is not UNSET and "contentAst" not in fields:
        payload["contentAst"] = [{"type": "text", "text": name}]
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
        assert version == 6
        # v2 node column names: is_active replaces archived; row-LWW columns
        # replace the v1 node_content_hlc watermark.
        assert {"is_active", "class_ids", "content_plain", "hlc_physical", "hlc_logical", "actor_id"} <= columns
        # v5 tags + v6 class order (web schema v5→v6 / v6→v7 parity).
        assert {"tag_ids", "class_order"} <= columns
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
        a1 = create_env(uid("n-a1"))
        a2 = create_env(uid("n-a2"))
        b1 = create_env(workspace_id=WS_B)
        store.enqueue(a1)
        store.enqueue(b1)
        store.enqueue(a2)
        assert [env.id for env in store.pending_outbox(WS_A, limit=1)] == [a1.id]
        assert [env.id for env in store.pending_outbox(WS_A)] == [a1.id, a2.id]

    def test_mark_outbox_sent_removes_only_listed(self, store: LocalStore) -> None:
        e1, e2, e3 = create_env(uid("n1")), create_env(uid("n2")), create_env(uid("n3"))
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
        store.enqueue(update_env(NODE, hlc=(3, 0), contentAst=None, icon=None))
        assert store.pending_outbox(WS_A) == []

    def test_enqueue_accepts_object_update_with_any_writable_field(self, store: LocalStore) -> None:
        store.enqueue(update_env(NODE, hlc=(2, 0), icon="📄"))
        store.enqueue(update_env(NODE, hlc=(3, 0), contentAst=[{"type": "text", "text": "hi"}]))
        assert len(store.pending_outbox(WS_A)) == 2

    def test_enqueue_rejects_retired_name_field(self, store: LocalStore) -> None:
        """Producer-side half of the 422 gate: a ``name`` key (title-is-content)
        fails strict validation instead of riding the outbox to a quarantine."""
        with pytest.raises(EnvelopeValidationError, match="name"):
            store.enqueue(update_env(NODE, hlc=(2, 0), name="legacy"))


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
        store.apply_remote(create_env(uid("parent"), content="hello", hlc=(2, 0)))
        store.apply_remote(create_env(uid("child"), parent_id=uid("parent"), node_type="block", hlc=(3, 0)))
        store.apply_remote(create_env(tag_ids=(uid("t-a"),), hlc=(4, 0)))
        store.apply_remote(property_set_env(uid("parent"), uid("schema-1"), value={"v": 1}, hlc=(5, 0)))
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
            assert raw.execute("SELECT COUNT(*) FROM tag_member_set").fetchone()[0] == 0

    def test_wipe_leaves_other_workspaces_untouched(self, store: LocalStore) -> None:
        store.apply_remote(create_env())
        store.apply_remote(create_env(workspace_id=WS_B, env_id=new_uuid7()))
        store.wipe(WS_A)
        remaining = store.node(WS_B, NODE)
        assert remaining is not None


class TestObjectCreate:
    def test_create_inserts_v2_row(self, store: LocalStore) -> None:
        # The builder's name convenience is the node's initial text content
        # (title-is-content: no name column write).
        store.apply_remote(create_env(parent_id=None, node_type="page", name="My Page"))
        store.apply_remote(update_env(NODE, hlc=(2, 0), icon="📄", color="red"))
        row = store.node(WS_A, NODE)
        assert row == NodeRow(
            id=NODE,
            workspace_id=WS_A,
            parent_id=None,
            node_type="page",
            name=None,  # title-is-content: the retired cache is never written
            class_ids=(),
            tag_ids=(),
            icon="📄",
            color="red",
            is_active=True,
            content=json.dumps([{"type": "text", "text": "My Page"}]),
            content_plain="My Page",
        )

    def test_name_convenience_yields_to_explicit_content_ast(self, store: LocalStore) -> None:
        """Builder parity: when both are given, contentAst wins and name drops."""
        store.apply_remote(create_env(name="Dropped", content="Winner"))
        row = store.node(WS_A, NODE)
        assert row is not None
        assert row.content == json.dumps([{"type": "text", "text": "Winner"}])
        assert row.content_plain == "Winner"

    def test_page_create_flattens_rich_content_to_text_only(self, store: LocalStore) -> None:
        """Title-is-content: pages/classes carry text-only content — a page
        created with rich tokens flattens to a single text token."""
        rich = [{"type": "mention", "text": "[[bob]]", "displayText": "Bob"}, {"type": "text", "text": " said hi"}]
        store.apply_remote(create_env(content=rich))
        row = store.node(WS_A, NODE)
        assert row is not None
        assert json.loads(row.content or "") == [{"type": "text", "text": "Bob said hi"}]
        assert row.content_plain == "Bob said hi"

    def test_block_create_keeps_the_full_token_stream(self, store: LocalStore) -> None:
        store.apply_remote(create_env(uid("parent"), parent_id=None))
        rich = [{"type": "mention", "text": "[[bob]]", "displayText": "Bob"}, {"type": "text", "text": " said hi"}]
        store.apply_remote(create_env(uid("child"), parent_id=uid("parent"), node_type="block", content=rich))
        row = store.node(WS_A, uid("child"))
        assert row is not None
        assert json.loads(row.content or "") == rich
        assert row.content_plain == "Bob said hi"

    def test_create_defaults_child_node_type_to_block(self, store: LocalStore) -> None:
        store.apply_remote(create_env(uid("parent"), parent_id=None))
        # The applier defaults nodeType by context (workspace root → page,
        # child → block); the wire payload simply omits it (an explicit
        # nodeType must be one of page|block|class — the strict schema
        # rejects anything else, like the server's).
        store.apply_remote(
            make_env(
                "object.create",
                {"objectId": uid("child"), "parentId": uid("parent")},
                hlc=(2, 0),
                affected=(uid("child"),),
            )
        )
        assert store.node(WS_A, uid("child")).node_type == "block"

    def test_create_content_ast_stored_as_json_with_derived_plaintext(self, store: LocalStore) -> None:
        # Title-is-content: a PAGE flattens rich tokens to text-only content
        # (blocks keep the full stream — see test_block_create_keeps_the_full_token_stream).
        store.apply_remote(
            create_env(
                content=[{"type": "text", "text": "hello"}, {"type": "hard_break"}, {"type": "text", "text": "world"}]
            )
        )
        row = store.node(WS_A, NODE)
        assert json.loads(row.content or "") == [{"type": "text", "text": "hello world"}]
        assert row.content_plain == "hello world"

    def test_create_class_ids_seed_or_set_membership(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(class_ids=(uid("c-b"), uid("c-a"))))
        row = store.node(WS_A, NODE)
        assert row.class_ids == tuple(sorted((uid("c-a"), uid("c-b"))))  # projected sorted from the OR-Set
        assert raw_rows(
            store,
            "SELECT class_id, present FROM class_member_set WHERE node_id = ? ORDER BY class_id",
            (NODE,),
        ) == sorted([(uid("c-a"), 1), (uid("c-b"), 1)])

    def test_block_without_parent_raises_placement_error(self, store: LocalStore) -> None:
        with pytest.raises(PlacementError, match="block must have a parent"):
            store.apply_remote(create_env(node_type="block", parent_id=None))

    def test_class_with_parent_raises_placement_error(self, store: LocalStore) -> None:
        store.apply_remote(create_env(uid("parent"), parent_id=None))
        with pytest.raises(PlacementError, match="class is tree-external"):
            store.apply_remote(create_env(uid("cls"), node_type="class", parent_id=uid("parent")))

    def test_missing_parent_raises_not_found(self, store: LocalStore) -> None:
        with pytest.raises(NotFoundError, match="parent"):
            store.apply_remote(create_env(uid("child"), parent_id=uid("missing")))

    def test_class_parent_raises_move_guard(self, store: LocalStore) -> None:
        store.apply_remote(class_env("class.create", uid("cls-1"), hlc=(1, 0), name="Tag"))
        with pytest.raises(MoveGuardError, match="tree-external"):
            store.apply_remote(create_env(uid("child"), parent_id=uid("cls-1")))

    def test_recreate_is_a_tree_no_op_but_unions_class_ids(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(uid("parent"), parent_id=None))
        store.apply_remote(create_env(uid("child"), parent_id=uid("parent"), node_type="block", hlc=(2, 0)))
        before = raw_rows(store, "SELECT COUNT(*) FROM node_child_order")
        # Re-issue under a DIFFERENT parent with payload drift: tree untouched.
        assert store.apply_remote(create_env(uid("child"), parent_id=NODE, name="drift", hlc=(3, 0))) is False
        row = store.node(WS_A, uid("child"))
        assert row is not None
        assert row.parent_id == uid("parent")
        assert row.name is None
        assert row.content == "[]"  # re-create never rewrites content either
        assert raw_rows(store, "SELECT COUNT(*) FROM node_child_order") == before
        # ...but classIds still seed the OR-Set (the convergence carrier).
        assert (
            store.apply_remote(create_env(uid("child"), parent_id=uid("parent"), class_ids=(uid("c-1"),), hlc=(4, 0)))
            is False
        )
        assert store.node(WS_A, uid("child")).class_ids == (uid("c-1"),)
        assert raw_rows(store, f"SELECT COUNT(*) FROM node_child_order WHERE child_id = '{uid('child')}'") == [(1,)]


class TestObjectUpdate:
    def test_fields_apply_and_stored_hlc_advances(self, store: LocalStore) -> None:
        store.apply_remote(create_env(hlc=(10, 0)))
        assert (
            store.apply_remote(
                update_env(NODE, hlc=(11, 0), contentAst=[{"type": "text", "text": "N"}], icon="i", color="c")
            )
            is True
        )
        row = store.node(WS_A, NODE)
        assert row is not None and (row.content_plain, row.icon, row.color) == ("N", "i", "c")
        assert raw_rows(store, "SELECT hlc_physical, hlc_logical FROM nodes WHERE id = ?", (NODE,)) == [(11, 0)]

    def test_strict_payload_rejects_retired_name_field(self, store: LocalStore) -> None:
        """Apply-time half of the 422 gate: object.create/update payloads
        carrying the retired ``name`` field fail strict validation (the web
        server's zod .strict() schemas reject it)."""
        store.apply_remote(create_env(hlc=(10, 0)))
        with pytest.raises(EnvelopeValidationError, match="name"):
            store.apply_remote(make_env("object.create", {"objectId": uid("n-name"), "name": "legacy"}))
        with pytest.raises(EnvelopeValidationError, match="name"):
            store.apply_remote(update_env(NODE, hlc=(11, 0), name="legacy"))
        # The rejected apply wrote nothing (no dedupe record either).
        assert raw_rows(store, "SELECT COUNT(*) FROM relay_operations") == [(1,)]
        assert store.apply_remote(update_env(NODE, hlc=(11, 0), icon="ok")) is True

    def test_promoting_a_block_flattens_its_content_to_text_only(self, store: LocalStore) -> None:
        store.apply_remote(create_env(uid("parent"), parent_id=None))
        rich = [{"type": "mention", "text": "[[bob]]", "displayText": "Bob"}, {"type": "text", "text": " said hi"}]
        store.apply_remote(
            create_env(uid("child"), parent_id=uid("parent"), node_type="block", content=rich, hlc=(2, 0))
        )
        assert store.apply_remote(update_env(uid("child"), hlc=(3, 0), nodeType="page")) is True
        row = store.node(WS_A, uid("child"))
        assert row is not None
        assert row.node_type == "page"
        assert json.loads(row.content or "") == [{"type": "text", "text": "Bob said hi"}]

    def test_page_content_update_flattens_rich_tokens(self, store: LocalStore) -> None:
        store.apply_remote(create_env(hlc=(10, 0)))
        rich = [{"type": "mention", "text": "[[bob]]", "displayText": "Bob"}, {"type": "text", "text": " said hi"}]
        assert store.apply_remote(update_env(NODE, hlc=(11, 0), contentAst=rich)) is True
        row = store.node(WS_A, NODE)
        assert row is not None
        assert json.loads(row.content or "") == [{"type": "text", "text": "Bob said hi"}]
        assert row.content_plain == "Bob said hi"

    def test_lower_or_equal_hlc_update_dropped(self, store: LocalStore) -> None:
        store.apply_remote(create_env(hlc=(10, 0), content="v10"))
        assert store.apply_remote(update_env(NODE, hlc=(9, 0), icon="stale")) is False
        assert store.apply_remote(update_env(NODE, hlc=(10, 0), icon="equal")) is False
        row = store.node(WS_A, NODE)
        assert row is not None and row.icon is None

    def test_equal_hlc_breaks_tie_on_actor(self, store: LocalStore) -> None:
        store.apply_remote(create_env(hlc=(10, 0)))
        store.apply_remote(update_env(NODE, hlc=(10, 0), actor_id=ACTOR, icon="first"))
        # Same HLC, lexicographically greater actor wins; smaller loses.
        assert store.apply_remote(update_env(NODE, hlc=(10, 0), actor_id="zzz", icon="second")) is True
        assert store.apply_remote(update_env(NODE, hlc=(10, 0), actor_id="aaa", icon="third")) is False
        assert store.node(WS_A, NODE).icon == "second"

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
        store.apply_remote(create_env(uid("parent"), parent_id=None))
        store.apply_remote(create_env(uid("child"), parent_id=uid("parent"), node_type="block", hlc=(2, 0)))
        assert store.apply_remote(update_env(uid("child"), hlc=(3, 0), nodeType="page")) is True
        assert store.node(WS_A, uid("child")).node_type == "page"
        assert store.apply_remote(update_env(uid("child"), hlc=(4, 0), nodeType="block")) is True
        assert store.node(WS_A, uid("child")).node_type == "block"
        # Demoting a PARENTLESS page to a block violates placement.
        store.apply_remote(create_env(uid("lone-page"), parent_id=None, hlc=(2, 0)))
        with pytest.raises(PlacementError):
            store.apply_remote(update_env(uid("lone-page"), hlc=(5, 0), nodeType="block"))
        # Declaring a parented node a class violates placement.
        with pytest.raises(PlacementError):
            store.apply_remote(update_env(uid("child"), hlc=(6, 0), nodeType="class"))


class TestObjectMove:
    def test_reparent_and_after_id_midpoint(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        for index, node in enumerate((uid("x"), uid("y"), uid("z"))):
            store.apply_remote(create_env(node, parent_id=uid("p"), node_type="block", hlc=(2 + index, 0)))
        # Enter placement: z jumps the queue to sit right after x.
        assert store.apply_remote(move_env(uid("z"), parent_id=uid("p"), after_id=uid("x"), hlc=(9, 0))) is True
        assert [row.id for row in store.children(WS_A, uid("p"))] == [uid("x"), uid("z"), uid("y")]
        assert raw_rows(
            store,
            f"SELECT position FROM node_child_order WHERE parent_id = '{uid('p')}' AND child_id = '{uid('z')}'",
        ) == [("a`",)]  # midpoint between "a" (x) and "aa" (y)

    def test_append_at_end_when_after_id_is_last(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env(uid("x"), parent_id=uid("p"), node_type="block", hlc=(2, 0)))
        store.apply_remote(create_env(uid("y"), parent_id=uid("p"), node_type="block", hlc=(3, 0)))
        store.apply_remote(move_env(uid("x"), parent_id=uid("p"), after_id=uid("y"), hlc=(4, 0)))
        assert [row.id for row in store.children(WS_A, uid("p"))] == [uid("y"), uid("x")]
        assert raw_rows(
            store,
            f"SELECT position FROM node_child_order WHERE parent_id = '{uid('p')}' AND child_id = '{uid('x')}'",
        ) == [("aaa",)]

    def test_after_id_not_a_sibling_falls_back_to_append(self, store: LocalStore) -> None:
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env(uid("other"), parent_id=None, hlc=(2, 0)))
        store.apply_remote(create_env(uid("x"), parent_id=uid("p"), node_type="block", hlc=(3, 0)))
        store.apply_remote(create_env(uid("y"), parent_id=uid("p"), node_type="block", hlc=(4, 0)))
        store.apply_remote(move_env(uid("y"), parent_id=uid("p"), after_id=uid("other"), hlc=(5, 0)))
        assert [row.id for row in store.children(WS_A, uid("p"))] == [uid("x"), uid("y")]

    def test_reparent_carries_a_single_child_order_row(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(uid("a"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env(uid("b"), parent_id=None, hlc=(2, 0)))
        store.apply_remote(create_env(uid("c"), parent_id=uid("a"), node_type="block", hlc=(3, 0)))
        store.apply_remote(move_env(uid("c"), parent_id=uid("b"), hlc=(4, 0)))
        assert store.node(WS_A, uid("c")).parent_id == uid("b")
        assert raw_rows(
            store,
            f"SELECT COUNT(*) FROM node_child_order WHERE child_id = '{uid('c')}'",
        ) == [(1,)]
        assert [row.id for row in store.children(WS_A, uid("b"))] == [uid("c")]

    def test_block_to_root_raises_placement_error(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env(uid("c"), parent_id=uid("p"), node_type="block", hlc=(2, 0)))
        with pytest.raises(PlacementError):
            store.apply_remote(move_env(uid("c"), parent_id=None, hlc=(3, 0)))
        # The throw rolls back: still parented, one child_order row.
        assert store.node(WS_A, uid("c")).parent_id == uid("p")
        assert raw_rows(store, f"SELECT COUNT(*) FROM node_child_order WHERE child_id = '{uid('c')}'") == [(1,)]

    def test_root_move_of_page_drops_child_order_row(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env(uid("sub"), parent_id=uid("p"), node_type="page", hlc=(2, 0)))
        store.apply_remote(move_env(uid("sub"), parent_id=None, hlc=(3, 0)))
        assert store.node(WS_A, uid("sub")).parent_id is None
        assert raw_rows(store, "SELECT COUNT(*) FROM node_child_order WHERE child_id = 'sub'") == [(0,)]

    def test_class_parent_raises_move_guard(self, store: LocalStore) -> None:
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(class_env("class.create", uid("cls-1"), hlc=(2, 0), name="Tag"))
        store.apply_remote(create_env(uid("c"), parent_id=uid("p"), node_type="block", hlc=(3, 0)))
        with pytest.raises(MoveGuardError):
            store.apply_remote(move_env(uid("c"), parent_id=uid("cls-1"), hlc=(4, 0)))

    def test_own_descendant_move_raises_move_guard(self, store: LocalStore) -> None:
        store.apply_remote(create_env(uid("outer"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env(uid("inner"), parent_id=uid("outer"), node_type="block", hlc=(2, 0)))
        with pytest.raises(MoveGuardError, match="own subtree"):
            store.apply_remote(move_env(uid("outer"), parent_id=uid("inner"), hlc=(3, 0)))

    def test_older_move_after_newer_is_dropped(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env(uid("q"), parent_id=None, hlc=(2, 0)))
        store.apply_remote(create_env(uid("c"), parent_id=uid("p"), node_type="block", hlc=(3, 0)))
        store.apply_remote(move_env(uid("c"), parent_id=uid("q"), hlc=(4, 0)))
        assert store.node(WS_A, uid("c")).parent_id == uid("q")
        store.apply_remote(move_env(uid("c"), parent_id=uid("p"), hlc=(5, 0)))
        assert store.node(WS_A, uid("c")).parent_id == uid("p")
        # Replay the older move with a fresh envelope id: must not regress.
        assert store.apply_remote(move_env(uid("c"), parent_id=uid("q"), hlc=(4, 0))) is False
        assert store.node(WS_A, uid("c")).parent_id == uid("p")
        assert raw_rows(store, f"SELECT COUNT(*) FROM node_child_order WHERE child_id = '{uid('c')}'") == [(1,)]


class TestObjectDelete:
    def test_soft_delete_trashes_subtree(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env(uid("c"), parent_id=uid("p"), node_type="block", content="child", hlc=(2, 0)))
        store.apply_remote(delete_env(uid("p"), hlc=(3, 0)))
        assert store.node(WS_A, uid("p")).is_active is False
        assert store.node(WS_A, uid("c")).is_active is False
        assert store.nodes(WS_A) == []
        assert [row.id for row in store.nodes(WS_A, include_inactive=True)] == [uid("c"), uid("p")]
        assert raw_rows(
            store,
            f"SELECT is_permanent FROM trash WHERE node_id = '{uid('p')}'",
        ) == [(0,)]

    def test_permanent_delete_hard_removes_subtree(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env(uid("c"), parent_id=uid("p"), node_type="block", hlc=(2, 0)))
        store.apply_remote(property_set_env(uid("c"), uid("s-1"), value=1, hlc=(3, 0)))
        store.apply_remote(delete_env(uid("p"), permanent=True, hlc=(4, 0)))
        assert store.node(WS_A, uid("p")) is None
        assert store.node(WS_A, uid("c")) is None
        assert store.children(WS_A, uid("p")) == []
        assert raw_rows(store, f"SELECT COUNT(*) FROM property_value WHERE node_id = '{uid('c')}'") == [(0,)]
        assert raw_rows(
            store,
            f"SELECT is_permanent FROM trash WHERE node_id = '{uid('p')}'",
        ) == [(1,)]


class TestClassOps:
    def test_class_create_registry_and_class_node(self, store: LocalStore) -> None:
        store.apply_remote(class_env("class.create", uid("cls-1"), hlc=(1, 0), name="Book", icon="📕", color="blue"))
        row = store.node(WS_A, uid("cls-1"))
        assert row is not None
        assert row.node_type == "class"
        assert row.parent_id is None
        # Title-is-content: the class title IS its content; the registry
        # ``name`` is a derived cache of that text (never the node column).
        assert row.name is None
        assert row.content == json.dumps([{"type": "text", "text": "Book"}])
        assert row.content_plain == "Book"
        # Reference semantics (appliers.ts upsertClassNode): the create's own
        # (hlc, actor) never beats its INSERT, so icon/color land on the class
        # node only via a later, higher-HLC class.update.
        assert row.icon is None
        registry = raw_rows(
            store,
            f"SELECT name, icon, color, active FROM class WHERE id = '{uid('cls-1')}'",
        )
        assert registry == [("Book", "📕", "blue", 1)]
        store.apply_remote(class_env("class.update", uid("cls-1"), hlc=(2, 0), icon="📕", color="blue"))
        row = store.node(WS_A, uid("cls-1"))
        assert row is not None and (row.icon, row.color) == ("📕", "blue")

    def test_class_create_content_ast_derives_the_display_name(self, store: LocalStore) -> None:
        """A class created with a contentAst (text-only title) stores the
        flattened content on the class node and the excerpt in the registry
        name cache."""
        store.apply_remote(
            class_env(
                "class.create",
                uid("cls-1"),
                hlc=(1, 0),
                contentAst=[{"type": "text", "text": "Meeting"}, {"type": "text", "text": " notes"}],
            )
        )
        row = store.node(WS_A, uid("cls-1"))
        assert row is not None
        assert row.content == json.dumps([{"type": "text", "text": "Meeting notes"}])
        assert row.content_plain == "Meeting notes"
        assert raw_rows(store, f"SELECT name FROM class WHERE id = '{uid('cls-1')}'") == [("Meeting notes",)]

    def test_class_create_flattens_rich_content_to_text_only(self, store: LocalStore) -> None:
        rich = [{"type": "mention", "text": "[[bob]]", "displayText": "Bob"}, {"type": "text", "text": " notes"}]
        store.apply_remote(class_env("class.create", uid("cls-1"), hlc=(1, 0), contentAst=rich))
        row = store.node(WS_A, uid("cls-1"))
        assert row is not None
        assert json.loads(row.content or "") == [{"type": "text", "text": "Bob notes"}]
        assert raw_rows(store, f"SELECT name FROM class WHERE id = '{uid('cls-1')}'") == [("Bob notes",)]

    def test_class_update_and_delete(self, store: LocalStore) -> None:
        store.apply_remote(class_env("class.create", uid("cls-1"), hlc=(1, 0), name="Book"))
        store.apply_remote(class_env("class.update", uid("cls-1"), hlc=(2, 0), name="Novel", description="long form"))
        row = store.node(WS_A, uid("cls-1"))
        assert row is not None
        assert row.content == json.dumps([{"type": "text", "text": "Novel"}])
        assert row.content_plain == "Novel"  # the registry name cache follows
        assert raw_rows(store, f"SELECT description FROM class WHERE id = '{uid('cls-1')}'") == [("long form",)]
        store.apply_remote(class_env("class.delete", uid("cls-1"), hlc=(3, 0)))
        assert store.node(WS_A, uid("cls-1")).is_active is False
        assert raw_rows(store, f"SELECT active FROM class WHERE id = '{uid('cls-1')}'") == [(0,)]

    def test_set_extends_maintains_closure_and_replaces(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(class_env("class.create", uid("a"), hlc=(1, 0), name="Entity"))
        store.apply_remote(class_env("class.create", uid("b"), hlc=(2, 0), name="Source"))
        store.apply_remote(class_env("class.create", uid("c"), hlc=(3, 0), name="Annotated"))
        store.apply_remote(extends_env(uid("b"), [uid("a")], hlc=(4, 0)))
        store.apply_remote(extends_env(uid("c"), [uid("b")], hlc=(5, 0)))
        closure = raw_rows(
            store,
            "SELECT class_id, ancestor_id FROM class_hierarchy ORDER BY class_id, ancestor_id",
        )
        # Closure rows are selected in (class_id, ancestor_id) order — uuid
        # string order, so the expectation sorts by the derived uuids.
        assert closure == sorted(
            [
                (uid("a"), uid("a")),
                (uid("b"), uid("a")),
                (uid("b"), uid("b")),
                (uid("c"), uid("a")),
                (uid("c"), uid("b")),
                (uid("c"), uid("c")),
            ]
        )
        # Replace [b] with []: only the self-row remains.
        store.apply_remote(extends_env(uid("c"), [], hlc=(6, 0)))
        assert raw_rows(
            store,
            f"SELECT ancestor_id FROM class_hierarchy WHERE class_id = '{uid('c')}' ORDER BY ancestor_id",
        ) == [(uid("c"),)]

    def test_set_extends_fails_loud_on_cycles(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(class_env("class.create", uid("a"), hlc=(1, 0), name="A"))
        store.apply_remote(class_env("class.create", uid("b"), hlc=(2, 0), name="B"))
        store.apply_remote(extends_env(uid("b"), [uid("a")], hlc=(3, 0)))
        with pytest.raises(CycleError, match="cannot extend itself"):
            store.apply_remote(extends_env(uid("a"), [uid("a")], hlc=(4, 0)))
        with pytest.raises(CycleError, match="would cycle"):
            store.apply_remote(extends_env(uid("a"), [uid("b")], hlc=(5, 0)))
        # A thrown apply rolls back: closure and edges keep the prefix state.
        assert raw_rows(
            store,
            f"SELECT ancestor_id FROM class_hierarchy WHERE class_id = '{uid('a')}' ORDER BY ancestor_id",
        ) == [(uid("a"),)]
        assert raw_rows(store, "SELECT COUNT(*) FROM class_extends") == [(1,)]

    def test_set_extends_requires_existing_classes(self, store: LocalStore) -> None:
        store.apply_remote(class_env("class.create", uid("a"), hlc=(1, 0), name="A"))
        with pytest.raises(NotFoundError):
            store.apply_remote(extends_env(uid("a"), [uid("ghost")], hlc=(2, 0)))


class TestClassPropertyBindings:
    def test_set_inserts_row_and_patch_keeps_omitted_fields(self, store: LocalStore) -> None:
        store.apply_remote(class_env("class.create", uid("cls-1"), hlc=(1, 0), name="Task"))
        store.apply_remote(
            make_env(
                "class.property.set",
                {"classId": uid("cls-1"), "propertySchemaId": uid("ps-1"), "sequence": 0, "defaultValue": "medium"},
                hlc=(2, 0),
            )
        )
        assert raw_rows(
            store,
            "SELECT sequence, required, readonly, hide_when_empty, default_value, hlc_physical"
            f" FROM class_property WHERE class_id = '{uid('cls-1')}' AND property_schema_id = '{uid('ps-1')}'",
        ) == [(0, None, None, None, '"medium"', 2)]

        # Patch only default + required: sequence survives via COALESCE.
        store.apply_remote(
            make_env(
                "class.property.set",
                {"classId": uid("cls-1"), "propertySchemaId": uid("ps-1"), "defaultValue": "low", "required": True},
                hlc=(3, 0),
            )
        )
        assert raw_rows(
            store,
            "SELECT sequence, required, default_value, hlc_physical FROM class_property"
            f" WHERE class_id = '{uid('cls-1')}' AND property_schema_id = '{uid('ps-1')}'",
        ) == [(0, 1, '"low"', 3)]

    def test_stale_set_dropped_by_row_lww(self, store: LocalStore) -> None:
        store.apply_remote(class_env("class.create", uid("cls-1"), hlc=(1, 0), name="Task"))
        store.apply_remote(
            make_env(
                "class.property.set",
                {"classId": uid("cls-1"), "propertySchemaId": uid("ps-1"), "defaultValue": "low"},
                hlc=(3, 0),
            )
        )
        assert (
            store.apply_remote(
                make_env(
                    "class.property.set",
                    {"classId": uid("cls-1"), "propertySchemaId": uid("ps-1"), "defaultValue": "stale"},
                    hlc=(2, 0),
                )
            )
            is False
        )
        assert raw_rows(
            store,
            f"SELECT default_value FROM class_property WHERE class_id = '{uid('cls-1')}' AND property_schema_id = '{uid('ps-1')}'",
        ) == [('"low"',)]

    def test_explicit_json_null_default_is_a_real_default(self, store: LocalStore) -> None:
        store.apply_remote(class_env("class.create", uid("cls-1"), hlc=(1, 0), name="Task"))
        store.apply_remote(
            make_env(
                "class.property.set",
                {"classId": uid("cls-1"), "propertySchemaId": uid("ps-1"), "defaultValue": None},
                hlc=(2, 0),
            )
        )
        assert raw_rows(
            store,
            f"SELECT default_value FROM class_property WHERE class_id = '{uid('cls-1')}' AND property_schema_id = '{uid('ps-1')}'",
        ) == [("null",)]

    def test_unset_deletes_the_binding_row(self, store: LocalStore) -> None:
        store.apply_remote(class_env("class.create", uid("cls-1"), hlc=(1, 0), name="Task"))
        store.apply_remote(
            make_env(
                "class.property.set",
                {"classId": uid("cls-1"), "propertySchemaId": uid("ps-1"), "defaultValue": "low"},
                hlc=(2, 0),
            )
        )
        store.apply_remote(
            make_env("class.property.unset", {"classId": uid("cls-1"), "propertySchemaId": uid("ps-1")}, hlc=(3, 0))
        )
        assert raw_rows(store, "SELECT COUNT(*) FROM class_property") == [(0,)]


class TestClassUnassign:
    def _seed_classed_node(self, store: LocalStore, *, node_id: str = uid("n-x")) -> None:
        store.apply_remote(class_env("class.create", uid("cls-x"), hlc=(1, 0), name="X"))
        store.apply_remote(
            make_env(
                "propertySchema.create", {"propertySchemaId": uid("ps-x"), "name": "effort", "type": "text"}, hlc=(2, 0)
            )
        )
        store.apply_remote(
            make_env(
                "class.property.set",
                {"classId": uid("cls-x"), "propertySchemaId": uid("ps-x"), "sequence": 0, "defaultValue": "xs"},
                hlc=(3, 0),
            )
        )
        store.apply_remote(
            make_env(
                "object.create", {"objectId": node_id, "nodeType": "page", "classIds": [uid("cls-x")]}, hlc=(4, 0)
            ),
        )

    def test_missing_node_fails_loud(self, store: LocalStore) -> None:
        with pytest.raises(NotFoundError, match="does not exist"):
            store.apply_remote(
                make_env("class.unassign", {"objectId": uid("ghost"), "classId": uid("cls-x")}, hlc=(2, 0))
            )

    def test_newer_remove_clears_then_newer_add_restores(self, store: LocalStore) -> None:
        self._seed_classed_node(store)
        assert (
            store.apply_remote(
                make_env("class.unassign", {"objectId": uid("n-x"), "classId": uid("cls-x")}, hlc=(5, 0))
            )
            is True
        )
        assert store.node(WS_A, uid("n-x")).class_ids == ()
        assert store.get_effective_properties(uid("n-x")) == []
        row = raw_rows(
            store,
            f"SELECT present, hlc_physical FROM class_member_set WHERE node_id = '{uid('n-x')}' AND class_id = '{uid('cls-x')}'",
        )
        assert row == [(0, 5)]
        # Re-add with a newer HLC restores membership and the derived default.
        store.apply_remote(make_env("object.create", {"objectId": uid("n-x"), "classIds": [uid("cls-x")]}, hlc=(6, 0)))
        assert store.node(WS_A, uid("n-x")).class_ids == (uid("cls-x"),)
        effective = store.get_effective_properties(uid("n-x"))
        assert [(row.source, row.value, row.bound_by) for row in effective] == [("default", "xs", uid("cls-x"))]

    def test_stale_remove_loses_to_newer_add(self, store: LocalStore) -> None:
        self._seed_classed_node(store)
        # Re-add at a higher HLC, then a stale (lower-HLC) remove must no-op.
        store.apply_remote(make_env("object.create", {"objectId": uid("n-x"), "classIds": [uid("cls-x")]}, hlc=(6, 0)))
        assert (
            store.apply_remote(
                make_env("class.unassign", {"objectId": uid("n-x"), "classId": uid("cls-x")}, hlc=(5, 5))
            )
            is True
        )
        assert store.node(WS_A, uid("n-x")).class_ids == (uid("cls-x"),)
        assert raw_rows(
            store,
            f"SELECT present, hlc_physical FROM class_member_set WHERE node_id = '{uid('n-x')}' AND class_id = '{uid('cls-x')}'",
        ) == [(1, 6)]

    def test_exact_hlc_tie_add_wins_in_either_delivery_order(self, store: LocalStore) -> None:
        self._seed_classed_node(store)
        # Order 1: remove first, then the re-issued create at the SAME
        # (hlc, actor) — the add's >= comparator wins.
        store.apply_remote(make_env("class.unassign", {"objectId": uid("n-x"), "classId": uid("cls-x")}, hlc=(7, 0)))
        store.apply_remote(make_env("object.create", {"objectId": uid("n-x"), "classIds": [uid("cls-x")]}, hlc=(7, 0)))
        assert store.node(WS_A, uid("n-x")).class_ids == (uid("cls-x"),)

        # Order 2: the add lands first, then the equal-(hlc, actor) remove
        # (strictly-greater gate) is dropped.
        store.apply_remote(make_env("object.create", {"objectId": uid("n-y"), "nodeType": "page"}, hlc=(8, 0)))
        store.apply_remote(make_env("object.create", {"objectId": uid("n-y"), "classIds": [uid("cls-x")]}, hlc=(9, 0)))
        store.apply_remote(make_env("class.unassign", {"objectId": uid("n-y"), "classId": uid("cls-x")}, hlc=(9, 0)))
        assert store.node(WS_A, uid("n-y")).class_ids == (uid("cls-x"),)


class TestTagOps:
    """First-class page-scoped tags (2026-10-01 lockstep): the tagIds add
    carrier rides object.create (OR-Set seed, add-wins per pair) and
    tag.unassign is the strictly-gated remove complement — identical gating
    to class membership, own ``tag_member_set`` table."""

    def test_tag_ids_seed_or_set_membership_projected_sorted(self, store: LocalStore) -> None:
        store.apply_remote(create_env(tag_ids=(uid("t-b"), uid("t-a"))))
        row = store.node(WS_A, NODE)
        assert row.tag_ids == tuple(sorted((uid("t-a"), uid("t-b"))))  # projected sorted by id
        assert raw_rows(
            store,
            "SELECT tag_id, present FROM tag_member_set WHERE node_id = ? ORDER BY tag_id",
            (NODE,),
        ) == sorted([(uid("t-a"), 1), (uid("t-b"), 1)])
        # The membership is its own table: class OR-Set untouched.
        assert raw_rows(store, "SELECT COUNT(*) FROM class_member_set WHERE node_id = ?", (NODE,)) == [(0,)]

    def test_tag_unassign_then_idempotent_reassign(self, store: LocalStore) -> None:
        store.apply_remote(create_env(tag_ids=(uid("t-a"),), hlc=(1, 0)))
        assert store.apply_remote(make_env("tag.unassign", {"objectId": NODE, "tagId": uid("t-a")}, hlc=(2, 0))) is True
        assert store.node(WS_A, NODE).tag_ids == ()
        assert raw_rows(
            store,
            "SELECT tag_id, present FROM tag_member_set WHERE node_id = ? ORDER BY tag_id",
            (NODE,),
        ) == [(uid("t-a"), 0)]
        # The re-issued object.create add carrier restores membership
        # (add-wins with a newer HLC), idempotent across replays.
        reassign = create_env(tag_ids=(uid("t-a"),), hlc=(3, 0))
        assert store.apply_remote(reassign.model_copy(update={"id": new_uuid7()})) is False  # re-create: tree no-op
        assert store.node(WS_A, NODE).tag_ids == (uid("t-a"),)
        assert store.apply_remote(reassign.model_copy(update={"id": new_uuid7()})) is False
        assert store.node(WS_A, NODE).tag_ids == (uid("t-a"),)

    def test_stale_remove_loses_to_newer_add(self, store: LocalStore) -> None:
        store.apply_remote(create_env(tag_ids=(uid("t-a"),), hlc=(1, 0)))
        store.apply_remote(create_env(tag_ids=(uid("t-a"),), hlc=(6, 0)))
        # A stale (lower-HLC) remove must no-op: membership stands.
        assert store.apply_remote(make_env("tag.unassign", {"objectId": NODE, "tagId": uid("t-a")}, hlc=(5, 5))) is True
        assert store.node(WS_A, NODE).tag_ids == (uid("t-a"),)
        assert raw_rows(
            store,
            "SELECT present, hlc_physical FROM tag_member_set WHERE node_id = ? AND tag_id = ?",
            (NODE, uid("t-a")),
        ) == [(1, 6)]

    def test_exact_hlc_tie_first_in_log_wins(self, store: LocalStore) -> None:
        """Web parity (``tagMemberUpsert`` in packages/store/src/appliers.ts):
        the tag add's actor tiebreak is strictly-greater, so an exact
        (hlc, actor) tie resolves first-in-log-wins — deterministic per op
        order, and the relay log is the single global order, so every replica
        converges to the same winner. (The classIds seeding uses >= add-wins;
        that asymmetry is deliberate on the web.)"""
        store.apply_remote(create_env(tag_ids=(uid("t-a"),), hlc=(1, 0)))
        # Log order 1: remove first, then the re-issued create at the SAME
        # (hlc, actor) — the add's strictly-greater gate fails, the remove
        # stands.
        store.apply_remote(make_env("tag.unassign", {"objectId": NODE, "tagId": uid("t-a")}, hlc=(7, 0)))
        store.apply_remote(create_env(tag_ids=(uid("t-a"),), hlc=(7, 0)))
        assert store.node(WS_A, NODE).tag_ids == ()

        # Log order 2: the add lands first, then the equal-(hlc, actor)
        # remove (strictly-greater gate) is dropped.
        other = uid("n-y")
        store.apply_remote(
            make_env("object.create", {"objectId": other, "nodeType": "page", "tagIds": [uid("t-a")]}, hlc=(8, 0))
        )
        store.apply_remote(make_env("tag.unassign", {"objectId": other, "tagId": uid("t-a")}, hlc=(8, 0)))
        assert store.node(WS_A, other).tag_ids == (uid("t-a"),)

    def test_missing_node_fails_loud(self, store: LocalStore) -> None:
        with pytest.raises(NotFoundError, match="does not exist"):
            store.apply_remote(make_env("tag.unassign", {"objectId": uid("ghost"), "tagId": uid("t-a")}, hlc=(2, 0)))

    def test_permanent_delete_clears_tag_membership(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(tag_ids=(uid("t-a"),), hlc=(1, 0)))
        store.apply_remote(delete_env(NODE, permanent=True, hlc=(2, 0)))
        assert raw_rows(store, "SELECT COUNT(*) FROM tag_member_set WHERE node_id = ?", (NODE,)) == [(0,)]


class TestClassReorder:
    """User-defined class ORDER (2026-10-01 lockstep): class.reorder is
    display-only, LWW-by-arrival — the applier writes ``class_order``
    unconditionally (deterministic per op order, so replicas converge); the
    class_ids projection merges ordered members first, then any unlisted
    present members sorted by id."""

    def _seed_classed_node(self, store: LocalStore) -> None:
        for offset, label in enumerate(("c-a", "c-b", "c-c")):
            store.apply_remote(class_env("class.create", uid(label), hlc=(1 + offset, 0), name=label))
        store.apply_remote(create_env(class_ids=(uid("c-a"), uid("c-b"), uid("c-c")), hlc=(5, 0)))

    def test_reorder_puts_ordered_members_first(self, store: LocalStore) -> None:
        self._seed_classed_node(store)
        assert store.node(WS_A, NODE).class_ids == tuple(
            sorted((uid("c-a"), uid("c-b"), uid("c-c")))
        )  # sorted, no order yet
        assert (
            store.apply_remote(
                make_env("class.reorder", {"objectId": NODE, "classIds": [uid("c-c"), uid("c-a")]}, hlc=(6, 0))
            )
            is True
        )
        assert store.node(WS_A, NODE).class_ids == (
            uid("c-c"),
            uid("c-a"),
            uid("c-b"),
        )  # ordered first, then unlisted by id
        assert raw_rows(store, "SELECT class_order FROM nodes WHERE id = ?", (NODE,)) == [
            (json.dumps([uid("c-c"), uid("c-a")]),)
        ]

    def test_unlisted_present_members_append_sorted_by_id(self, store: LocalStore) -> None:
        self._seed_classed_node(store)
        store.apply_remote(make_env("class.reorder", {"objectId": NODE, "classIds": [uid("c-c")]}, hlc=(6, 0)))
        assert store.node(WS_A, NODE).class_ids == (uid("c-c"),) + tuple(sorted((uid("c-a"), uid("c-b"))))

    def test_unassign_still_ordered(self, store: LocalStore) -> None:
        """The class.unassign/recompute path honors the stored order: the
        unassigned member leaves every position; the rest keep theirs. A
        later re-add reclaims the member's original ordered slot (the
        class_order list is not pruned by unassign)."""
        self._seed_classed_node(store)
        store.apply_remote(
            make_env("class.reorder", {"objectId": NODE, "classIds": [uid("c-c"), uid("c-a"), uid("c-b")]}, hlc=(6, 0))
        )
        store.apply_remote(make_env("class.unassign", {"objectId": NODE, "classId": uid("c-a")}, hlc=(7, 0)))
        assert store.node(WS_A, NODE).class_ids == (uid("c-c"), uid("c-b"))
        # Re-adding the member restores its original ordered position.
        store.apply_remote(create_env(class_ids=(uid("c-a"),), hlc=(8, 0)))
        assert store.node(WS_A, NODE).class_ids == (uid("c-c"), uid("c-a"), uid("c-b"))

    def test_reorder_is_lww_by_arrival(self, store: LocalStore) -> None:
        """No HLC gating: the last class.reorder op to arrive wins, so
        replicas applying the same ops in the same order converge."""
        self._seed_classed_node(store)
        store.apply_remote(make_env("class.reorder", {"objectId": NODE, "classIds": [uid("c-b")]}, hlc=(6, 0)))
        store.apply_remote(
            make_env("class.reorder", {"objectId": NODE, "classIds": [uid("c-c"), uid("c-b")]}, hlc=(1, 0))
        )
        assert store.node(WS_A, NODE).class_ids == (uid("c-c"), uid("c-b"), uid("c-a"))

    def test_convergence_two_stores_same_op_order(self, tmp_path: Path) -> None:
        """Deterministic per op order: the same op sequence applied to two
        fresh stores (and the reverse interleave) ends byte-identical."""
        first = LocalStore(tmp_path / "first.db")
        second = LocalStore(tmp_path / "second.db")
        ops = [
            class_env("class.create", uid("c-a"), hlc=(1, 0), name="A"),
            class_env("class.create", uid("c-b"), hlc=(2, 0), name="B"),
            class_env("class.create", uid("c-c"), hlc=(3, 0), name="C"),
            create_env(class_ids=(uid("c-a"), uid("c-b"), uid("c-c")), hlc=(4, 0)),
            make_env("class.reorder", {"objectId": NODE, "classIds": [uid("c-c"), uid("c-a")]}, hlc=(5, 0)),
            make_env("class.unassign", {"objectId": NODE, "classId": uid("c-a")}, hlc=(6, 0)),
            create_env(class_ids=(uid("c-a"),), hlc=(7, 0)),
        ]
        for envelope in ops:
            first.apply_remote(envelope.model_copy(update={"id": new_uuid7()}))
        for envelope in ops:
            second.apply_remote(envelope.model_copy(update={"id": new_uuid7()}))
        assert first.node(WS_A, NODE).class_ids == second.node(WS_A, NODE).class_ids
        assert first.node(WS_A, NODE).class_ids == (uid("c-c"), uid("c-a"), uid("c-b"))
        assert raw_rows(first, "SELECT class_order FROM nodes WHERE id = ?", (NODE,)) == raw_rows(
            second, "SELECT class_order FROM nodes WHERE id = ?", (NODE,)
        )
        first.close()
        second.close()

    def test_missing_node_fails_loud(self, store: LocalStore) -> None:
        with pytest.raises(NotFoundError, match="does not exist"):
            store.apply_remote(make_env("class.reorder", {"objectId": uid("ghost"), "classIds": []}, hlc=(2, 0)))


class TestPropertySchema:
    def test_registry_create_update_delete(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {
                    "propertySchemaId": uid("ps-1"),
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
            f"SELECT name, type, multi, scope, options, active FROM property_schema WHERE id = '{uid('ps-1')}'",
        ) == [("Status", "select", 0, "class", json.dumps([{"id": "o1", "label": "Open"}]), 1)]
        store.apply_remote(
            make_env("propertySchema.update", {"propertySchemaId": uid("ps-1"), "name": "State"}, hlc=(2, 0))
        )
        store.apply_remote(make_env("propertySchema.delete", {"propertySchemaId": uid("ps-1")}, hlc=(3, 0)))
        assert raw_rows(store, f"SELECT name, active FROM property_schema WHERE id = '{uid('ps-1')}'") == [("State", 0)]


class TestPropertyValues:
    def test_set_lww_converges_both_orders(self, tmp_path: Path) -> None:
        laptop = property_set_env(
            NODE, uid("s-1"), value={"n": "laptop"}, metadata={"since": "1962"}, hlc=(5, 0), actor_id="actor-laptop"
        )
        phone = property_set_env(
            NODE, uid("s-1"), value={"n": "phone"}, metadata={"since": "1963"}, hlc=(5, 5), actor_id="actor-phone"
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
                f"SELECT value, metadata, actor_id FROM property_value"
                f" WHERE node_id = ? AND property_schema_id = '{uid('s-1')}' AND idx = 0",
                (NODE,),
            )
            assert row == [expected]
            instance.close()

    def test_unset_tombstone_wins_over_later_lower_set(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(hlc=(1, 0)))
        store.apply_remote(property_set_env(NODE, uid("s-1"), value=1, hlc=(2, 0)))
        store.apply_remote(property_unset_env(NODE, uid("s-1"), hlc=(3, 0)))
        assert raw_rows(store, "SELECT COUNT(*) FROM property_value WHERE node_id = ?", (NODE,)) == [(0,)]
        # A lower-HLC set must not resurrect the value.
        assert store.apply_remote(property_set_env(NODE, uid("s-1"), value=2, hlc=(2, 5))) is False
        assert raw_rows(store, "SELECT COUNT(*) FROM property_value WHERE node_id = ?", (NODE,)) == [(0,)]
        # A higher-HLC set does.
        assert store.apply_remote(property_set_env(NODE, uid("s-1"), value=3, hlc=(4, 0))) is True
        assert raw_rows(store, "SELECT value FROM property_value WHERE node_id = ?", (NODE,)) == [("3",)]


class TestAssetsAndCollections:
    def test_asset_attach_detach(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(hlc=(1, 0)))
        store.apply_remote(
            make_env(
                "asset.attach",
                {
                    "objectId": NODE,
                    "assetId": uid("asset-1"),
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
            f"SELECT hash, mime_type, size, original_name FROM node_asset WHERE node_id = ? AND asset_id = '{uid('asset-1')}'",
            (NODE,),
        ) == [("a" * 64, "image/png", 1234, "cover.png")]
        store.apply_remote(
            make_env("asset.detach", {"objectId": NODE, "assetId": uid("asset-1")}, hlc=(3, 0), affected=(NODE,))
        )
        assert raw_rows(store, "SELECT COUNT(*) FROM node_asset") == [(0,)]

    def test_collection_membership_is_add_wins_or_set(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(hlc=(1, 0)))
        add = make_env(
            "collection.member.add",
            {"collectionId": uid("col-1"), "objectId": NODE},
            hlc=(2, 0),
            affected=(uid("col-1"), NODE),
        )
        remove = make_env(
            "collection.member.remove",
            {"collectionId": uid("col-1"), "objectId": NODE},
            hlc=(2, 0),
            affected=(uid("col-1"), NODE),
        )
        # Equal (hlc, actor): the add wins over the remove.
        store.apply_remote(add.model_copy(update={"id": new_uuid7()}))
        store.apply_remote(remove.model_copy(update={"id": new_uuid7()}))
        assert raw_rows(
            store,
            f"SELECT present FROM collection_member WHERE collection_id = '{uid('col-1')}' AND object_id = ?",
            (NODE,),
        ) == [(1,)]
        # A strictly higher-HLC remove clears membership.
        store.apply_remote(
            make_env(
                "collection.member.remove",
                {"collectionId": uid("col-1"), "objectId": NODE},
                hlc=(3, 0),
                affected=(uid("col-1"), NODE),
            )
        )
        assert raw_rows(
            store,
            f"SELECT present FROM collection_member WHERE collection_id = '{uid('col-1')}' AND object_id = ?",
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
        store.apply_remote(create_env(uid("n1"), parent_id=None))
        store.apply_remote(create_env(uid("n2"), parent_id=uid("n1"), node_type="block"))
        assert sorted(row.id for row in store.nodes(WS_A)) == [uid("n1"), uid("n2")]
        assert [row.id for row in store.nodes(WS_A, parent_id=uid("n1"))] == [uid("n2")]

    def test_node_returns_none_for_missing(self, store: LocalStore) -> None:
        assert store.node(WS_A, uid("missing")) is None


class TestSnapshotRestore:
    def test_restore_maps_v2_server_schema(self, store: LocalStore) -> None:
        mirror = '[{"type":"text","text":"snap"}]'
        blob = make_server_snapshot(
            [
                {
                    "id": uid("n1"),
                    "workspace_id": WS_A,
                    "node_type": "page",
                    "parent_id": None,
                    "class_ids": json.dumps([uid("c-1")]),
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
                {"id": uid("n2"), "workspace_id": WS_A, "node_type": "block", "parent_id": uid("n1"), "content": "[]"},
            ]
        )
        assert store.restore_snapshot(blob, workspace_id=WS_A) is True
        rows = {row.id: row for row in store.nodes(WS_A, include_inactive=True)}
        # Same-name, same-polarity mapping: node_type/is_active/class_ids/name
        # copy verbatim and the row-LWW columns seed the LWW baseline.
        assert rows[uid("n1")].node_type == "page"
        assert rows[uid("n1")].is_active is False  # is_active=0
        assert rows[uid("n1")].content == mirror
        assert rows[uid("n1")].class_ids == (uid("c-1"),)
        assert rows[uid("n1")].name == "Snap Page"
        assert rows[uid("n2")].node_type == "block"
        assert rows[uid("n2")].is_active is True
        # Seeded LWW baseline (hlc 5,2): lower/equal writes must lose.
        assert (
            store.apply_remote(update_env(uid("n1"), hlc=(4, 9), contentAst=[{"type": "text", "text": "stale"}]))
            is False
        )
        assert store.node(WS_A, uid("n1")).content == mirror
        assert (
            store.apply_remote(update_env(uid("n1"), hlc=(5, 3), contentAst=[{"type": "text", "text": "newer"}]))
            is True
        )
        assert json.loads(store.node(WS_A, uid("n1")).content or "") == [{"type": "text", "text": "newer"}]

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
            f"INSERT INTO node (id, workspace_id, node_type, content) VALUES ('{uid('legacy')}', ?, 'page', 'old')",
            (WS_A,),
        )
        blob = conn.serialize()
        conn.close()
        assert store.restore_snapshot(blob, workspace_id=WS_A) is True
        row = store.node(WS_A, uid("legacy"))
        assert row is not None and row.node_type == "page" and row.content == "old"
        # No baseline → any later update wins.
        assert (
            store.apply_remote(update_env(uid("legacy"), hlc=(1, 0), contentAst=[{"type": "text", "text": "new"}]))
            is True
        )
        assert json.loads(store.node(WS_A, uid("legacy")).content or "") == [{"type": "text", "text": "new"}]

    def test_restore_filters_by_workspace(self, store: LocalStore) -> None:
        blob = make_server_snapshot(
            [
                {"id": uid("n1"), "workspace_id": WS_A, "node_type": "page", "content": "[]"},
                {"id": uid("n2"), "workspace_id": WS_B, "node_type": "page", "content": "[]"},
            ]
        )
        assert store.restore_snapshot(blob, workspace_id=WS_A) is True
        assert [row.id for row in store.nodes(WS_A, include_inactive=True)] == [uid("n1")]

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
