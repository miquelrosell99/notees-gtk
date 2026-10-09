"""Tests for the local SQLite store: outbox, op-id dedupe, appliers, migrations, snapshots.

The applier semantics asserted here are the convergence rules ported from
``packages/store/src/appliers.ts``: row-level LWW by (hlc, actor), OR-Set
class/tag/collection membership, user-defined class order (class.reorder,
LWW-by-arrival), title-is-content (no ``name`` writes; class rows carry
text-only content while pages keep the rich token stream on content writes —
create-as-main and promotion remain the lossy boundaries), the wire node
fields (object.update maps coverAssetId/bannerAssetId/aliasedNodeId onto the
derived node columns, present-null clears; the alias target gets the
write-time cycle check and resolve_alias walks the chain), the class
conversion capability (class.create on an existing node declares it a class),
the Revision-11 render-state model (``is_class`` identity + ``present_as_main``
render bit; classes are containers and always roots), m2m class extends with
a maintained closure (cycles fail loud), fractional child-order positions,
property tombstones, and soft/permanent deletes with trash retention — plus
the property-correctness waves: PG4 extends-aware binding resolution (the
diamond rule), PG6 apply-time value validation (shape/scalar/cardinality/
precision/filter/existence), the M38 asset property type (the implicit
asset-class filter), and PC2 typed binding defaults (write-side fail-loud +
read-side drop).
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
    PropertyValueShapeError,
    UnsupportedCarrierError,
)
from notees_gtk.data.store import SCHEMA_VERSION, LocalStore, NodeRow

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


WIRE_TABLES = {
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
    "property_value_element_tombstone",
    "node_asset",
    "collection_member",
    "trash",
    "workspace_feature",
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
    """Build a minimal valid envelope for store tests."""
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
    present_as_main: bool | None = None,
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
    given; when both are given, content wins and name is dropped. The
    Revision-11 render bit rides ``presentAsMain``; when omitted the applier
    defaults it by context (parentless → main, parented → inline).
    """
    payload: dict[str, Any] = {"objectId": node_id, "parentId": parent_id}
    if present_as_main is not None:
        payload["presentAsMain"] = present_as_main
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
    before_id: str | None = None,
    hlc: tuple[int, int],
    actor_id: str = ACTOR,
) -> RelayEnvelope:
    payload: dict[str, Any] = {"objectId": node_id, "parentId": parent_id}
    if after_id is not None:
        payload["afterId"] = after_id
    if before_id is not None:
        payload["beforeId"] = before_id
    return make_env("object.move", payload, hlc=hlc, actor_id=actor_id, affected=(node_id,))


def delete_env(node_id: str, *, permanent: bool = False, hlc: tuple[int, int]) -> RelayEnvelope:
    payload: dict[str, Any] = {"objectId": node_id}
    if permanent:
        payload["permanent"] = True
    return make_env("object.delete", payload, hlc=hlc, affected=(node_id,))


def restore_env(node_id: str, *, hlc: tuple[int, int]) -> RelayEnvelope:
    return make_env("object.restore", {"objectId": node_id}, hlc=hlc, affected=(node_id,))


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
        assert tables >= WIRE_TABLES
        assert version == SCHEMA_VERSION
        # node column names: is_active replaces archived; row-LWW columns
        # replace the node_content_hlc watermark.
        assert {"is_active", "class_ids", "content_plain", "hlc_physical", "hlc_logical", "actor_id"} <= columns
        # v5 tags + v6 class order (web schema v5→v6 / v6→v7 parity) and the
        # Revision-11 render-state booleans (web schema v7→v8 parity).
        assert {"tag_ids", "class_order", "is_class", "present_as_main"} <= columns
        assert "archived" not in columns
        assert "node_type" not in columns
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

    def test_legacy_shape_database_is_reshaped_data_preserving(self, tmp_path: Path) -> None:
        """A pre-reshape cache (archived polarity, no row-LWW columns) upgrades in place."""
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
        # Revision-11 mapping straight from the reshape: page → presents
        # as main (the v7 rebuild is a no-op on this path).
        assert row.is_class is False
        assert row.present_as_main is True
        upgraded.close()
        with sqlite3.connect(path) as raw:
            assert "node_content_hlc" not in {r[0] for r in raw.execute("SELECT name FROM sqlite_master")}

    def test_v6_to_v7_migration_maps_node_type_to_the_render_bits(self, tmp_path: Path) -> None:
        """The Revision-11 table rebuild: page → (0, 1), block → (0, 0),
        class → (1, 0); parented pages keep their parent (the render bit is
        independent of placement) and classes stay roots."""
        path = tmp_path / "v6.db"
        with sqlite3.connect(path) as raw:
            raw.executescript(
                """
                CREATE TABLE nodes (
                    workspace_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    parent_id TEXT,
                    node_type TEXT NOT NULL DEFAULT 'block'
                        CHECK (node_type IN ('page', 'block', 'class')),
                    name TEXT,
                    class_ids TEXT NOT NULL DEFAULT '[]',
                    class_order TEXT NOT NULL DEFAULT '[]',
                    tag_ids TEXT NOT NULL DEFAULT '[]',
                    icon TEXT,
                    color TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    content TEXT,
                    content_plain TEXT NOT NULL DEFAULT '',
                    created_at TEXT,
                    updated_at TEXT NOT NULL,
                    created_by TEXT,
                    updated_by TEXT,
                    hlc_physical INTEGER NOT NULL DEFAULT 0,
                    hlc_logical INTEGER NOT NULL DEFAULT 0,
                    actor_id TEXT,
                    PRIMARY KEY (workspace_id, id)
                );
                INSERT INTO nodes (workspace_id, id, parent_id, node_type, content_plain, updated_at)
                VALUES ('ws-a', 'v6-root-page', NULL, 'page', 'root', '2026-01-01'),
                       ('ws-a', 'v6-child-page', 'v6-root-page', 'page', 'child page', '2026-01-01'),
                       ('ws-a', 'v6-block', 'v6-root-page', 'block', 'block', '2026-01-01'),
                       ('ws-a', 'v6-class', NULL, 'class', 'class', '2026-01-01');
                PRAGMA user_version = 6;
                """
            )
        upgraded = LocalStore(path)
        with sqlite3.connect(path) as raw:
            version = raw.execute("PRAGMA user_version").fetchone()[0]
            columns = {row[1] for row in raw.execute("PRAGMA table_info(nodes)")}
        assert version == SCHEMA_VERSION
        assert "node_type" not in columns
        assert {"is_class", "present_as_main"} <= columns
        root_page = upgraded.node(WS_A, "v6-root-page")
        assert root_page is not None
        assert (root_page.is_class, root_page.present_as_main) == (False, True)
        assert root_page.parent_id is None
        child_page = upgraded.node(WS_A, "v6-child-page")
        assert child_page is not None
        # Parented page: bit preserved as main, parent edge untouched.
        assert (child_page.is_class, child_page.present_as_main) == (False, True)
        assert child_page.parent_id == "v6-root-page"
        block = upgraded.node(WS_A, "v6-block")
        assert block is not None
        assert (block.is_class, block.present_as_main) == (False, False)
        assert block.parent_id == "v6-root-page"
        class_row = upgraded.node(WS_A, "v6-class")
        assert class_row is not None
        assert (class_row.is_class, class_row.present_as_main) == (True, False)
        assert class_row.parent_id is None
        upgraded.close()

    def test_v11_to_v12_migration_moves_the_render_contracts_to_the_schema(self, tmp_path: Path) -> None:
        """An on-disk v11 database upgrades in place —
        property_schema gains display/readonly/hide_when_empty (NULL
        defaults, zero backfill) and class_property is REBUILT without the
        retired binding columns (readonly/hide_when_empty pre-v3.1.0, display
        the v11 experiment): the surviving per-class columns
        (sequence, required, default_value, active + LWW causality) keep
        their rows verbatim."""
        path = tmp_path / "v11.db"
        instance = LocalStore(path)
        instance.apply_remote(class_env("class.create", uid("cls-1"), hlc=(1, 0), name="Task"))
        instance.apply_remote(
            make_env(
                "class.property.set",
                {
                    "classId": uid("cls-1"),
                    "propertySchemaId": uid("ps-1"),
                    "sequence": 3,
                    "required": True,
                    "defaultValue": "low",
                },
                hlc=(2, 0),
            )
        )
        instance.close()
        # Downgrade the on-disk shape to v11: property_schema loses the three
        # render-contract columns; class_property regains the retired binding columns
        # (with data — the rebuild must drop them).
        with sqlite3.connect(path) as raw:
            raw.execute("ALTER TABLE property_schema DROP COLUMN display")
            raw.execute("ALTER TABLE property_schema DROP COLUMN readonly")
            raw.execute("ALTER TABLE property_schema DROP COLUMN hide_when_empty")
            raw.executescript(
                """
                CREATE TABLE class_property_v11 (
                    class_id TEXT NOT NULL,
                    property_schema_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL DEFAULT 0,
                    required INTEGER,
                    readonly INTEGER,
                    hide_when_empty INTEGER,
                    default_value TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    display TEXT,
                    hlc_physical INTEGER NOT NULL DEFAULT 0,
                    hlc_logical INTEGER NOT NULL DEFAULT 0,
                    actor_id TEXT,
                    PRIMARY KEY (class_id, property_schema_id)
                );
                INSERT INTO class_property_v11
                    SELECT class_id, property_schema_id, sequence, required, 0, 1,
                           default_value, active, 'bullet', hlc_physical, hlc_logical, actor_id
                    FROM class_property;
                DROP TABLE class_property;
                ALTER TABLE class_property_v11 RENAME TO class_property;
                CREATE INDEX IF NOT EXISTS idx_class_property_class ON class_property (class_id);
                """
            )
            raw.execute("PRAGMA user_version = 11")
        upgraded = LocalStore(path)
        with sqlite3.connect(path) as raw:
            version = raw.execute("PRAGMA user_version").fetchone()[0]
            binding_columns = {row[1] for row in raw.execute("PRAGMA table_info(class_property)")}
            schema_columns = {row[1] for row in raw.execute("PRAGMA table_info(property_schema)")}
            rows = raw.execute(
                "SELECT sequence, required, default_value, active FROM class_property"
                f" WHERE class_id = '{uid('cls-1')}' AND property_schema_id = '{uid('ps-1')}'"
            ).fetchall()
        assert version == SCHEMA_VERSION
        # The binding table carries only the per-class mechanics now.
        assert {"readonly", "hide_when_empty", "display"} & binding_columns == set()
        assert {"sequence", "required", "default_value", "active"} <= binding_columns
        # The schema table gained the render contracts.
        assert {"display", "readonly", "hide_when_empty"} <= schema_columns
        # The pre-batch row keeps its data — required survives, the retired
        # display value is gone with the column.
        assert rows == [(3, 1, '"low"', 1)]
        # The migrated store takes new schema-side display writes like a
        # fresh v12 one.
        upgraded.apply_remote(
            make_env(
                "propertySchema.create",
                {"propertySchemaId": uid("ps-1"), "name": "State", "type": "select", "display": "inline"},
                hlc=(3, 0),
            )
        )
        assert raw_rows(upgraded, f"SELECT display FROM property_schema WHERE id = '{uid('ps-1')}'") == [("inline",)]
        upgraded.close()

    def test_v12_to_v13_migration_adds_the_wire_node_fields(self, tmp_path: Path) -> None:
        """The wire node fields (web schema v15→v16 parity): an on-disk v12
        database upgrades in place — the three nullable node columns are
        added idempotently and the migrated table maps the fields on
        update, exactly like a fresh v13 create."""
        path = tmp_path / "v12.db"
        instance = LocalStore(path)
        instance.apply_remote(create_env(name="Page", hlc=(1, 0)))
        instance.apply_remote(update_env(NODE, hlc=(2, 0), coverAssetId=uid("wf-cover")))
        instance.close()
        # Downgrade the on-disk shape to v12: drop the three columns
        # (SQLite 3.35+ DROP COLUMN; the v11→v12 precedent).
        with sqlite3.connect(path) as raw:
            raw.execute("ALTER TABLE nodes DROP COLUMN cover_asset_id")
            raw.execute("ALTER TABLE nodes DROP COLUMN banner_asset_id")
            raw.execute("ALTER TABLE nodes DROP COLUMN aliased_node_id")
            raw.execute("PRAGMA user_version = 12")
        upgraded = LocalStore(path)
        row = upgraded.node(WS_A, NODE)
        assert row is not None
        # The pre-upgrade value is gone with the column; the field maps again.
        assert (row.cover_asset_id, row.banner_asset_id, row.aliased_node_id) == (None, None, None)
        assert upgraded.apply_remote(update_env(NODE, hlc=(3, 0), aliasedNodeId=uid("wf-main"))) is True
        assert upgraded.node(WS_A, NODE).aliased_node_id == uid("wf-main")
        # Idempotent: a second open is a no-op that stays current.
        upgraded.close()
        reopened = LocalStore(path)
        assert raw_rows(reopened, "SELECT aliased_node_id FROM nodes WHERE id = ?", (NODE,)) == [
            (uid("wf-main"),)
        ]
        reopened.close()

    def test_v13_to_v14_migration_adds_the_description_column(self, tmp_path: Path) -> None:
        """The page-subtitle wire node field (web schema v17→v18 parity): an
        on-disk v13 database upgrades in place — the nullable description
        column is added idempotently and the migrated table maps the field on
        update (set and clear), exactly like a fresh v14 create."""
        path = tmp_path / "v13.db"
        instance = LocalStore(path)
        instance.apply_remote(create_env(name="Page", hlc=(1, 0)))
        instance.close()
        # Downgrade the on-disk shape to v13: drop the column (SQLite 3.35+
        # DROP COLUMN; the v11→v12 precedent).
        with sqlite3.connect(path) as raw:
            raw.execute("ALTER TABLE nodes DROP COLUMN description")
            raw.execute("PRAGMA user_version = 13")
        upgraded = LocalStore(path)
        row = upgraded.node(WS_A, NODE)
        assert row is not None
        assert row.description is None
        assert upgraded.apply_remote(update_env(NODE, hlc=(2, 0), description="New subtitle")) is True
        assert upgraded.node(WS_A, NODE).description == "New subtitle"
        # The clear (present-null) maps too, exactly like color.
        assert upgraded.apply_remote(update_env(NODE, hlc=(3, 0), description=None)) is True
        assert upgraded.node(WS_A, NODE).description is None
        # Idempotent: a second open is a no-op that stays current.
        upgraded.close()
        reopened = LocalStore(path)
        assert raw_rows(reopened, "PRAGMA user_version") == [(SCHEMA_VERSION,)]
        reopened.close()


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

    def test_enqueue_accepts_a_nullish_field_clear_as_the_one_field(self, store: LocalStore) -> None:
        """A null on a NULLISH field (color, the M27 wire node fields,
        ``description``) is a real CLEAR write — the server's presence-based
        refine accepts the envelope, so the outbox must not swallow it (the
        color precedent extended to the wire node fields)."""
        store.enqueue(update_env(NODE, hlc=(2, 0), color=None))
        store.enqueue(update_env(NODE, hlc=(3, 0), aliasedNodeId=None))
        store.enqueue(update_env(NODE, hlc=(4, 0), coverAssetId=None, bannerAssetId=None))
        store.enqueue(update_env(NODE, hlc=(5, 0), description=None))
        assert [env.hlc.physical for env in store.pending_outbox(WS_A)] == [2, 3, 4, 5]

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
        store.apply_remote(create_env(uid("child"), parent_id=uid("parent"), hlc=(3, 0)))
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
        store.apply_remote(create_env(parent_id=None, name="My Page"))
        store.apply_remote(update_env(NODE, hlc=(2, 0), icon="📄", color="red"))
        row = store.node(WS_A, NODE)
        assert row == NodeRow(
            id=NODE,
            workspace_id=WS_A,
            parent_id=None,
            is_class=False,
            present_as_main=True,  # parentless create defaults to main
            name=None,  # title-is-content: the retired cache is never written
            class_ids=(),
            tag_ids=(),
            icon="📄",
            color="red",
            cover_asset_id=None,
            banner_asset_id=None,
            aliased_node_id=None,
            description=None,
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

    def test_parentless_create_flattens_rich_content_to_text_only(self, store: LocalStore) -> None:
        """Content flatten invariant (SCHEMA.md): document-chrome nodes
        (is_class or present_as_main) carry text-only content — a parentless
        node (present_as_main defaults to 1) created with rich tokens
        flattens to a single text token."""
        rich = [{"type": "mention", "text": "[[bob]]", "displayText": "Bob"}, {"type": "text", "text": " said hi"}]
        store.apply_remote(create_env(content=rich))
        row = store.node(WS_A, NODE)
        assert row is not None
        assert json.loads(row.content or "") == [{"type": "text", "text": "Bob said hi"}]
        assert row.content_plain == "Bob said hi"

    def test_block_create_keeps_the_full_token_stream(self, store: LocalStore) -> None:
        store.apply_remote(create_env(uid("parent"), parent_id=None))
        rich = [{"type": "mention", "text": "[[bob]]", "displayText": "Bob"}, {"type": "text", "text": " said hi"}]
        store.apply_remote(create_env(uid("child"), parent_id=uid("parent"), content=rich))
        row = store.node(WS_A, uid("child"))
        assert row is not None
        assert json.loads(row.content or "") == rich
        assert row.content_plain == "Bob said hi"

    def test_create_defaults_present_as_main_by_context(self, store: LocalStore) -> None:
        store.apply_remote(create_env(uid("parent"), parent_id=None))
        # The applier defaults the render bit by context (workspace root →
        # main, child → inline); the wire payload simply omits it (an
        # explicit presentAsMain must be a boolean — the strict schema
        # rejects anything else, like the server's).
        store.apply_remote(
            make_env(
                "object.create",
                {"objectId": uid("child"), "parentId": uid("parent")},
                hlc=(2, 0),
                affected=(uid("child"),),
            )
        )
        assert store.node(WS_A, uid("parent")).present_as_main is True
        assert store.node(WS_A, uid("child")).present_as_main is False

    def test_create_explicit_present_as_main_wins_over_the_default(self, store: LocalStore) -> None:
        store.apply_remote(create_env(uid("parent"), parent_id=None))
        store.apply_remote(create_env(uid("child"), parent_id=uid("parent"), present_as_main=True, hlc=(2, 0)))
        assert store.node(WS_A, uid("child")).present_as_main is True

    def test_create_content_ast_stored_as_json_with_derived_plaintext(self, store: LocalStore) -> None:
        # Content flatten invariant: a PARENTLESS node (present_as_main=1)
        # flattens rich tokens to text-only content (inline blocks keep the
        # full stream — see test_block_create_keeps_the_full_token_stream).
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

    def test_parentless_create_is_legal_and_defaults_to_main(self, store: LocalStore) -> None:
        """Semantic inversion (Revision 11): parentless non-class nodes are
        legal — they render with document chrome by the second cascade
        branch, and the render bit defaults to main."""
        store.apply_remote(create_env(uid("lone"), parent_id=None, hlc=(1, 0)))
        row = store.node(WS_A, uid("lone"))
        assert row is not None
        assert row.parent_id is None
        assert (row.is_class, row.present_as_main) == (False, True)

    def test_retired_node_type_key_rejected_by_strict_validation(self, store: LocalStore) -> None:
        """No wire compat of any kind: object.create/update payloads carrying
        the retired nodeType key fail strict validation like any other
        unknown key (pydantic extra="forbid", the zod .strict() parity)."""
        store.apply_remote(create_env(uid("parent"), parent_id=None, hlc=(1, 0)))
        with pytest.raises(EnvelopeValidationError, match="nodeType"):
            store.apply_remote(
                make_env(
                    "object.create",
                    {"objectId": uid("legacy"), "nodeType": "page", "parentId": uid("parent")},
                    hlc=(2, 0),
                    affected=(uid("legacy"),),
                )
            )
        with pytest.raises(EnvelopeValidationError, match="nodeType"):
            store.apply_remote(
                make_env(
                    "object.update",
                    {"objectId": uid("parent"), "nodeType": "page"},
                    hlc=(2, 0),
                    affected=(uid("parent"),),
                )
            )
        assert store.node(WS_A, uid("legacy")) is None
        assert raw_rows(store, "SELECT COUNT(*) FROM relay_operations") == [(1,)]

    def test_missing_parent_raises_not_found(self, store: LocalStore) -> None:
        with pytest.raises(NotFoundError, match="parent"):
            store.apply_remote(create_env(uid("child"), parent_id=uid("missing")))

    def test_class_parent_is_legal_for_non_class_children(self, store: LocalStore) -> None:
        """Semantic inversion (Revision 11, spec I4): classes are containers —
        a class node may parent non-class children (object.create only ever
        makes is_class=0 nodes)."""
        store.apply_remote(class_env("class.create", uid("cls-1"), hlc=(1, 0), name="Tag"))
        rich = [{"type": "mention", "text": "[[bob]]", "displayText": "Bob"}, {"type": "text", "text": " said hi"}]
        store.apply_remote(create_env(uid("child"), parent_id=uid("cls-1"), content=rich, hlc=(2, 0)))
        row = store.node(WS_A, uid("child"))
        assert row is not None
        assert row.parent_id == uid("cls-1")
        assert (row.is_class, row.present_as_main) == (False, False)
        # Inline child of a class keeps its full rich token stream.
        assert json.loads(row.content or "") == rich

    def test_recreate_is_a_tree_no_op_but_unions_class_ids(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(uid("parent"), parent_id=None))
        store.apply_remote(create_env(uid("child"), parent_id=uid("parent"), hlc=(2, 0)))
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

    def test_create_with_before_id_places_before_the_anchor(self, store: LocalStore) -> None:
        """Create placement honors anchors: a child created with beforeId
        lands immediately before that sibling (midpoint below the first
        child when the anchor is first)."""
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env(uid("x"), parent_id=uid("p"), hlc=(2, 0)))
        store.apply_remote(create_env(uid("y"), parent_id=uid("p"), hlc=(3, 0)))
        store.apply_remote(
            make_env(
                "object.create",
                {"objectId": uid("z"), "parentId": uid("p"), "beforeId": uid("x")},
                hlc=(4, 0),
                affected=(uid("z"),),
            )
        )
        assert [row.id for row in store.children(WS_A, uid("p"))] == [uid("z"), uid("x"), uid("y")]
        assert raw_rows(
            store,
            f"SELECT position FROM node_child_order WHERE parent_id = '{uid('p')}' AND child_id = '{uid('z')}'",
        ) == [("`",)]  # _midpoint_between("", "a"): one slot below the first child


class TestObjectUpdate:
    def test_fields_apply_and_stored_hlc_advances(self, store: LocalStore) -> None:
        store.apply_remote(create_env(hlc=(10, 0)))
        assert (
            store.apply_remote(
                update_env(NODE, hlc=(11, 0), contentAst=[{"type": "text", "text": "N"}], icon="i", color="#123abc")
            )
            is True
        )
        row = store.node(WS_A, NODE)
        assert row is not None and (row.content_plain, row.icon, row.color) == ("N", "i", "#123abc")
        assert raw_rows(store, "SELECT hlc_physical, hlc_logical FROM nodes WHERE id = ?", (NODE,)) == [(11, 0)]

    def test_color_presence_vs_null_clear(self, store: LocalStore) -> None:
        """A color field ABSENT from the payload writes nothing; a
        color field PRESENT with null writes NULL (the clear the UI's "No
        color" sends). Presence, not value, gates the write."""
        store.apply_remote(create_env(hlc=(10, 0)))
        store.apply_remote(update_env(NODE, hlc=(11, 0), color="red"))
        # An unrelated write without a color key leaves the color untouched.
        store.apply_remote(update_env(NODE, hlc=(12, 0), icon="📄"))
        assert store.node(WS_A, NODE).color == "red"
        # The explicit null clears it.
        store.apply_remote(update_env(NODE, hlc=(13, 0), color=None))
        assert store.node(WS_A, NODE).color is None
        assert raw_rows(store, "SELECT color FROM nodes WHERE id = ?", (NODE,)) == [(None,)]

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

    def test_promotion_toggle_flattens_content_to_text_only(self, store: LocalStore) -> None:
        """The presentAsMain toggle is promotion/demotion: a 0→1 flip
        (promotion) stringifies the rich token stream in the same op."""
        store.apply_remote(create_env(uid("parent"), parent_id=None))
        rich = [{"type": "mention", "text": "[[bob]]", "displayText": "Bob"}, {"type": "text", "text": " said hi"}]
        store.apply_remote(create_env(uid("child"), parent_id=uid("parent"), content=rich, hlc=(2, 0)))
        assert store.apply_remote(update_env(uid("child"), hlc=(3, 0), presentAsMain=True)) is True
        row = store.node(WS_A, uid("child"))
        assert row is not None
        assert row.present_as_main is True
        assert json.loads(row.content or "") == [{"type": "text", "text": "Bob said hi"}]

    def test_demotion_does_not_unflatten_content(self, store: LocalStore) -> None:
        """A 1→0 demotion leaves the (already flattened) content untouched —
        demotion never un-flattens."""
        store.apply_remote(create_env(uid("parent"), parent_id=None))
        rich = [{"type": "mention", "text": "[[bob]]", "displayText": "Bob"}, {"type": "text", "text": " said hi"}]
        store.apply_remote(
            create_env(uid("child"), parent_id=uid("parent"), present_as_main=True, content=rich, hlc=(2, 0))
        )
        # Created straight as main: content flattened at create time.
        assert json.loads(store.node(WS_A, uid("child")).content or "") == [{"type": "text", "text": "Bob said hi"}]
        assert store.apply_remote(update_env(uid("child"), hlc=(3, 0), presentAsMain=False)) is True
        row = store.node(WS_A, uid("child"))
        assert row is not None
        assert row.present_as_main is False
        assert json.loads(row.content or "") == [{"type": "text", "text": "Bob said hi"}]

    def test_main_presenting_content_update_keeps_the_rich_tokens(self, store: LocalStore) -> None:
        """The 2026-10-07 title-flatten ruling: the object.update path no
        longer flattens rich content to text-only for present-as-main nodes —
        a page's own content may carry inline tokens (mentions, external
        links). Class rows still flatten; create-as-main and promotion remain
        the lossy boundaries (mirrors the monorepo store test)."""
        store.apply_remote(create_env(hlc=(10, 0)))
        rich = [{"type": "mention", "text": "[[bob]]", "displayText": "Bob"}, {"type": "text", "text": " said hi"}]
        assert store.apply_remote(update_env(NODE, hlc=(11, 0), contentAst=rich)) is True
        row = store.node(WS_A, NODE)
        assert row is not None
        assert json.loads(row.content or "") == rich
        # Display-name derivation still flattens to text for labels.
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

    def test_present_as_main_toggle_promotes_and_demotes_in_place(self, store: LocalStore) -> None:
        """The toggle joins the row LWW set and preserves identity — the node
        stays parented in place; only the render bit flips."""
        store.apply_remote(create_env(uid("parent"), parent_id=None))
        store.apply_remote(create_env(uid("child"), parent_id=uid("parent"), hlc=(2, 0)))
        assert store.node(WS_A, uid("child")).present_as_main is False
        assert store.apply_remote(update_env(uid("child"), hlc=(3, 0), presentAsMain=True)) is True
        assert store.node(WS_A, uid("child")).present_as_main is True
        assert store.node(WS_A, uid("child")).parent_id == uid("parent")
        assert store.apply_remote(update_env(uid("child"), hlc=(4, 0), presentAsMain=False)) is True
        assert store.node(WS_A, uid("child")).present_as_main is False

    def test_present_as_main_toggle_respects_row_lww(self, store: LocalStore) -> None:
        store.apply_remote(create_env(uid("parent"), parent_id=None))
        store.apply_remote(create_env(uid("child"), parent_id=uid("parent"), hlc=(2, 0)))
        assert store.apply_remote(update_env(uid("child"), hlc=(2, 0), presentAsMain=True)) is False
        assert store.node(WS_A, uid("child")).present_as_main is False
        assert store.apply_remote(update_env(uid("child"), hlc=(3, 0), presentAsMain=True)) is True
        assert store.node(WS_A, uid("child")).present_as_main is True
        # A stale toggle at an older HLC never regresses the bit.
        assert store.apply_remote(update_env(uid("child"), hlc=(2, 5), presentAsMain=False)) is False
        assert store.node(WS_A, uid("child")).present_as_main is True


class TestObjectMove:
    def test_reparent_and_after_id_midpoint(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        for index, node in enumerate((uid("x"), uid("y"), uid("z"))):
            store.apply_remote(create_env(node, parent_id=uid("p"), hlc=(2 + index, 0)))
        # Enter placement: z jumps the queue to sit right after x.
        assert store.apply_remote(move_env(uid("z"), parent_id=uid("p"), after_id=uid("x"), hlc=(9, 0))) is True
        assert [row.id for row in store.children(WS_A, uid("p"))] == [uid("x"), uid("z"), uid("y")]
        assert raw_rows(
            store,
            f"SELECT position FROM node_child_order WHERE parent_id = '{uid('p')}' AND child_id = '{uid('z')}'",
        ) == [("a`",)]  # midpoint between "a" (x) and "aa" (y)

    def test_append_at_end_when_after_id_is_last(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env(uid("x"), parent_id=uid("p"), hlc=(2, 0)))
        store.apply_remote(create_env(uid("y"), parent_id=uid("p"), hlc=(3, 0)))
        store.apply_remote(move_env(uid("x"), parent_id=uid("p"), after_id=uid("y"), hlc=(4, 0)))
        assert [row.id for row in store.children(WS_A, uid("p"))] == [uid("y"), uid("x")]
        assert raw_rows(
            store,
            f"SELECT position FROM node_child_order WHERE parent_id = '{uid('p')}' AND child_id = '{uid('x')}'",
        ) == [("aaa",)]

    def test_after_id_not_a_sibling_falls_back_to_append(self, store: LocalStore) -> None:
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env(uid("other"), parent_id=None, hlc=(2, 0)))
        store.apply_remote(create_env(uid("x"), parent_id=uid("p"), hlc=(3, 0)))
        store.apply_remote(create_env(uid("y"), parent_id=uid("p"), hlc=(4, 0)))
        store.apply_remote(move_env(uid("y"), parent_id=uid("p"), after_id=uid("other"), hlc=(5, 0)))
        assert [row.id for row in store.children(WS_A, uid("p"))] == [uid("x"), uid("y")]

    def test_before_id_places_the_node_immediately_before_the_anchor(self, store: LocalStore) -> None:
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        for index, node in enumerate((uid("x"), uid("y"), uid("z"))):
            store.apply_remote(create_env(node, parent_id=uid("p"), hlc=(2 + index, 0)))
        # z jumps the queue to sit right before y.
        assert store.apply_remote(move_env(uid("z"), parent_id=uid("p"), before_id=uid("y"), hlc=(9, 0))) is True
        assert [row.id for row in store.children(WS_A, uid("p"))] == [uid("x"), uid("z"), uid("y")]
        assert raw_rows(
            store,
            f"SELECT position FROM node_child_order WHERE parent_id = '{uid('p')}' AND child_id = '{uid('z')}'",
        ) == [("a`",)]  # midpoint between "a" (x) and "aa" (y)

    def test_before_id_against_the_first_child_yields_a_position_below_it(self, store: LocalStore) -> None:
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        for index, node in enumerate((uid("x"), uid("y"), uid("z"))):
            store.apply_remote(create_env(node, parent_id=uid("p"), hlc=(2 + index, 0)))
        assert store.apply_remote(move_env(uid("z"), parent_id=uid("p"), before_id=uid("x"), hlc=(9, 0))) is True
        assert [row.id for row in store.children(WS_A, uid("p"))] == [uid("z"), uid("x"), uid("y")]
        (position,) = raw_rows(
            store,
            f"SELECT position FROM node_child_order WHERE parent_id = '{uid('p')}' AND child_id = '{uid('z')}'",
        )[0]
        assert position == "`"  # _midpoint_between("", "a"): the only slot before the first child
        assert position < "a"

    def test_before_id_not_a_sibling_falls_back_to_append(self, store: LocalStore) -> None:
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env(uid("other"), parent_id=None, hlc=(2, 0)))
        store.apply_remote(create_env(uid("x"), parent_id=uid("p"), hlc=(3, 0)))
        store.apply_remote(create_env(uid("y"), parent_id=uid("p"), hlc=(4, 0)))
        store.apply_remote(move_env(uid("y"), parent_id=uid("p"), before_id=uid("other"), hlc=(5, 0)))
        assert [row.id for row in store.children(WS_A, uid("p"))] == [uid("x"), uid("y")]

    def test_after_id_wins_when_both_anchors_are_present(self, store: LocalStore) -> None:
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        for index, node in enumerate((uid("x"), uid("y"), uid("z"))):
            store.apply_remote(create_env(node, parent_id=uid("p"), hlc=(2 + index, 0)))
        # At most one anchor is meaningful: afterId is looked up first, so
        # the append-after-z branch wins over the before-y branch.
        assert (
            store.apply_remote(
                move_env(uid("x"), parent_id=uid("p"), after_id=uid("z"), before_id=uid("y"), hlc=(9, 0))
            )
            is True
        )
        assert [row.id for row in store.children(WS_A, uid("p"))] == [uid("y"), uid("z"), uid("x")]
        assert raw_rows(
            store,
            f"SELECT position FROM node_child_order WHERE parent_id = '{uid('p')}' AND child_id = '{uid('x')}'",
        ) == [("aaaa",)]  # append-at-end: z ("aaa") was the last sibling

    def test_reparent_carries_a_single_child_order_row(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(uid("a"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env(uid("b"), parent_id=None, hlc=(2, 0)))
        store.apply_remote(create_env(uid("c"), parent_id=uid("a"), hlc=(3, 0)))
        store.apply_remote(move_env(uid("c"), parent_id=uid("b"), hlc=(4, 0)))
        assert store.node(WS_A, uid("c")).parent_id == uid("b")
        assert raw_rows(
            store,
            f"SELECT COUNT(*) FROM node_child_order WHERE child_id = '{uid('c')}'",
        ) == [(1,)]
        assert [row.id for row in store.children(WS_A, uid("b"))] == [uid("c")]

    def test_move_to_root_is_legal_and_drops_child_order_row(self, store: LocalStore, tmp_path: Path) -> None:
        """Semantic inversion (Revision 11): moving a parented non-class node
        to the workspace root is legal — it renders with document chrome by
        the second cascade branch; the throw-rollback behavior of the old
        guard is pinned by test_own_descendant_move_raises_move_guard."""
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env(uid("c"), parent_id=uid("p"), hlc=(2, 0)))
        assert store.apply_remote(move_env(uid("c"), parent_id=None, hlc=(3, 0))) is True
        row = store.node(WS_A, uid("c"))
        assert row is not None
        assert row.parent_id is None
        # Moves never write the render bit: the bit stays inline (unread for
        # parentless nodes — benign by construction).
        assert row.present_as_main is False
        assert raw_rows(store, f"SELECT COUNT(*) FROM node_child_order WHERE child_id = '{uid('c')}'") == [(0,)]

    def test_root_move_of_main_presenting_node_keeps_the_bit(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env(uid("sub"), parent_id=uid("p"), present_as_main=True, hlc=(2, 0)))
        store.apply_remote(move_env(uid("sub"), parent_id=None, hlc=(3, 0)))
        row = store.node(WS_A, uid("sub"))
        assert row is not None
        assert row.parent_id is None
        # Moves never write the bit: parentless-with-bit-set is benign.
        assert row.present_as_main is True
        assert raw_rows(store, f"SELECT COUNT(*) FROM node_child_order WHERE child_id = '{uid('sub')}'") == [(0,)]

    def test_move_under_class_parent_is_legal(self, store: LocalStore) -> None:
        """Semantic inversion (Revision 11, spec I4): classes are containers —
        moving a non-class node under a class parent is legal."""
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(class_env("class.create", uid("cls-1"), hlc=(2, 0), name="Tag"))
        store.apply_remote(create_env(uid("c"), parent_id=uid("p"), hlc=(3, 0)))
        assert store.apply_remote(move_env(uid("c"), parent_id=uid("cls-1"), hlc=(4, 0))) is True
        row = store.node(WS_A, uid("c"))
        assert row is not None
        assert row.parent_id == uid("cls-1")

    def test_class_node_move_to_parent_raises_move_guard(self, store: LocalStore) -> None:
        """Classes are always roots: moving an is_class=1 node under any
        parent is rejected (the DB CHECK would fire anyway — the guard
        surfaces it friendly)."""
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(class_env("class.create", uid("cls-1"), hlc=(2, 0), name="Tag"))
        with pytest.raises(MoveGuardError, match="classes are always roots"):
            store.apply_remote(move_env(uid("cls-1"), parent_id=uid("p"), hlc=(3, 0)))
        # The throw rolls back: the class node stays parentless.
        assert store.node(WS_A, uid("cls-1")).parent_id is None

    def test_own_descendant_move_raises_move_guard(self, store: LocalStore) -> None:
        store.apply_remote(create_env(uid("outer"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env(uid("inner"), parent_id=uid("outer"), hlc=(2, 0)))
        with pytest.raises(MoveGuardError, match="own subtree"):
            store.apply_remote(move_env(uid("outer"), parent_id=uid("inner"), hlc=(3, 0)))

    def test_older_move_after_newer_is_dropped(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env(uid("q"), parent_id=None, hlc=(2, 0)))
        store.apply_remote(create_env(uid("c"), parent_id=uid("p"), hlc=(3, 0)))
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
        store.apply_remote(create_env(uid("c"), parent_id=uid("p"), content="child", hlc=(2, 0)))
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
        store.apply_remote(create_env(uid("c"), parent_id=uid("p"), hlc=(2, 0)))
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


class TestObjectRestore:
    """Lockstep with the TS reference's applyObjectRestore: whole-tree
    restore, independent-trash exclusion, the
    dangling-parent corner, fail-loud on permanently deleted ids."""

    def test_restore_reactivates_subtree_and_consumes_trash_row(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env(uid("c"), parent_id=uid("p"), content="child", hlc=(2, 0)))
        store.apply_remote(create_env(uid("g"), parent_id=uid("c"), content="grandchild", hlc=(3, 0)))
        store.apply_remote(delete_env(uid("p"), hlc=(4, 0)))
        assert store.node(WS_A, uid("g")).is_active is False

        store.apply_remote(restore_env(uid("p"), hlc=(5, 0)))
        assert store.node(WS_A, uid("p")).is_active is True
        assert store.node(WS_A, uid("c")).is_active is True
        assert store.node(WS_A, uid("g")).is_active is True
        assert raw_rows(store, f"SELECT 1 FROM trash WHERE node_id = '{uid('p')}'") == []
        # Tree placement survived the round-trip.
        assert [row.id for row in store.children(WS_A, uid("p"))] == [uid("c")]

    def test_independently_trashed_descendant_stays_trashed(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env(uid("c"), parent_id=uid("p"), content="child", hlc=(2, 0)))
        store.apply_remote(create_env(uid("s"), parent_id=uid("p"), content="sibling", hlc=(3, 0)))
        store.apply_remote(delete_env(uid("c"), hlc=(4, 0)))
        store.apply_remote(delete_env(uid("p"), hlc=(5, 0)))

        store.apply_remote(restore_env(uid("p"), hlc=(6, 0)))
        assert store.node(WS_A, uid("p")).is_active is True
        assert store.node(WS_A, uid("s")).is_active is True
        assert store.node(WS_A, uid("c")).is_active is False
        # Its own trash row survives — a later child restore still works.
        assert raw_rows(store, f"SELECT 1 FROM trash WHERE node_id = '{uid('c')}'") == [(1,)]
        store.apply_remote(restore_env(uid("c"), hlc=(7, 0)))
        assert store.node(WS_A, uid("c")).is_active is True

    def test_dangling_parent_reparents_to_workspace_root(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(create_env(uid("g"), parent_id=uid("p"), content="grandchild", hlc=(2, 0)))
        store.apply_remote(delete_env(uid("g"), hlc=(3, 0)))
        # Legacy corner: the parent's row disappears after the trash (imported
        # data with a pruned subtree) while the trashed node survives.
        store._conn.execute(  # noqa: SLF001
            "DELETE FROM node_child_order WHERE parent_id = ? OR child_id = ?", (uid("p"), uid("p"))
        )
        store._conn.execute("DELETE FROM trash WHERE node_id = ?", (uid("p"),))  # noqa: SLF001
        store._conn.execute("DELETE FROM nodes WHERE id = ?", (uid("p"),))  # noqa: SLF001

        store.apply_remote(restore_env(uid("g"), hlc=(4, 0)))
        row = store.node(WS_A, uid("g"))
        assert row is not None
        assert row.is_active is True
        assert row.parent_id is None

    def test_restore_of_permanently_deleted_node_fails_loud(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(uid("p"), parent_id=None, hlc=(1, 0)))
        store.apply_remote(delete_env(uid("p"), permanent=True, hlc=(2, 0)))
        with pytest.raises(NotFoundError, match="does not exist"):
            store.apply_remote(restore_env(uid("p"), hlc=(3, 0)))


class TestClassOps:
    def test_class_create_registry_and_class_node(self, store: LocalStore) -> None:
        store.apply_remote(class_env("class.create", uid("cls-1"), hlc=(1, 0), name="Book", icon="📕", color="blue"))
        row = store.node(WS_A, uid("cls-1"))
        assert row is not None
        assert row.is_class is True
        assert row.present_as_main is False
        assert row.parent_id is None
        # Title-is-content: the class title IS its content; the registry
        # ``name`` is a derived cache of that text (never the node column).
        assert row.name is None
        assert row.content == json.dumps([{"type": "text", "text": "Book"}])
        assert row.content_plain == "Book"
        # Reference semantics (appliers.ts upsertClassNode): create-time
        # icon/color ride the INSERT — the LWW-gated UPDATE can never beat
        # the create's own (hlc, actor) — so both land on the class node
        # row immediately, same as the registry row (the color fixture
        # asserts exactly this for color).
        assert (row.icon, row.color) == ("📕", "blue")
        registry = raw_rows(
            store,
            f"SELECT name, icon, color, active FROM class WHERE id = '{uid('cls-1')}'",
        )
        assert registry == [("Book", "📕", "blue", 1)]
        store.apply_remote(class_env("class.update", uid("cls-1"), hlc=(2, 0), icon="📕", color="blue"))
        row = store.node(WS_A, uid("cls-1"))
        assert row is not None and (row.icon, row.color) == ("📕", "blue")

    def test_class_update_null_color_clears_both_rows(self, store: LocalStore) -> None:
        """class.update with an explicit null clears the color on the
        class node row AND the registry row; a color-absent update leaves the
        cleared state untouched."""
        store.apply_remote(class_env("class.create", uid("cls-1"), hlc=(1, 0), name="Book", color="blue"))
        store.apply_remote(class_env("class.update", uid("cls-1"), hlc=(2, 0), color=None))
        row = store.node(WS_A, uid("cls-1"))
        assert row is not None and row.color is None
        assert raw_rows(store, f"SELECT color FROM class WHERE id = '{uid('cls-1')}'") == [(None,)]
        store.apply_remote(class_env("class.update", uid("cls-1"), hlc=(3, 0), description="long form"))
        assert store.node(WS_A, uid("cls-1")).color is None

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
            "SELECT sequence, required, default_value, hlc_physical"
            f" FROM class_property WHERE class_id = '{uid('cls-1')}' AND property_schema_id = '{uid('ps-1')}'",
        ) == [(0, None, '"medium"', 2)]

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

    def test_display_position_is_schema_sourced_on_effective_rows(self, store: LocalStore) -> None:
        """The position is PROPERTY-level — it rides the SCHEMA row
        (propertySchema.create/update), and the effective read carries it on
        both authored and derived rows; a stored NULL/'panel' sanitizes to
        None. The binding row knows nothing about it."""
        store.apply_remote(class_env("class.create", uid("cls-1"), hlc=(1, 0), name="Task"))
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {"propertySchemaId": uid("ps-1"), "name": "State", "type": "select", "display": "bullet"},
                hlc=(2, 0),
            )
        )
        store.apply_remote(
            make_env(
                "class.property.set",
                {
                    "classId": uid("cls-1"),
                    "propertySchemaId": uid("ps-1"),
                    "sequence": 1,
                    "defaultValue": "todo",
                },
                hlc=(3, 0),
            )
        )
        assert raw_rows(
            store,
            f"SELECT display FROM property_schema WHERE id = '{uid('ps-1')}'",
        ) == [("bullet",)]
        member = uid("member")
        store.apply_remote(create_env(member, class_ids=(uid("cls-1"),), hlc=(4, 0), name="Buy milk"))
        # Derived default row: the schema's display rides along.
        assert [(row.source, row.value, row.display) for row in store.get_effective_properties(member)] == [
            ("default", "todo", "bullet")
        ]
        # Authored row: same schema supplies the display.
        store.apply_remote(
            make_env(
                "property.set",
                {"objectId": member, "propertySchemaId": uid("ps-1"), "value": "doing", "idx": 0},
                hlc=(5, 0),
                affected=(member,),
            )
        )
        assert [(row.source, row.value, row.display) for row in store.get_effective_properties(member)] == [
            ("authored", "doing", "bullet")
        ]
        # 'panel' stores on the SCHEMA row but reads as None (the sanitizer).
        store.apply_remote(
            make_env("propertySchema.update", {"propertySchemaId": uid("ps-1"), "display": "panel"}, hlc=(6, 0))
        )
        assert raw_rows(store, f"SELECT display FROM property_schema WHERE id = '{uid('ps-1')}'") == [("panel",)]
        assert [row.display for row in store.get_effective_properties(member)] == [None]

    def test_schema_render_contracts_keep_vs_clear_via_update(self, store: LocalStore) -> None:
        """propertySchema.update's key-PRESENCE contract — absent
        keeps the stored contract, an explicit null clears it (the number
        formats keep-vs-clear shape, now for display/readonly/hideWhenEmpty)."""
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {
                    "propertySchemaId": uid("ps-1"),
                    "name": "State",
                    "type": "select",
                    "display": "inline",
                    "readonly": True,
                    "hideWhenEmpty": True,
                },
                hlc=(1, 0),
            )
        )

        def row() -> tuple:
            return raw_rows(
                store,
                f"SELECT display, readonly, hide_when_empty FROM property_schema WHERE id = '{uid('ps-1')}'",
            )[0]

        assert row() == ("inline", 1, 1)
        # Absent keeps all three.
        store.apply_remote(
            make_env("propertySchema.update", {"propertySchemaId": uid("ps-1"), "name": "State"}, hlc=(2, 0))
        )
        assert row() == ("inline", 1, 1)
        # A present null clears; a present value writes.
        store.apply_remote(
            make_env(
                "propertySchema.update",
                {"propertySchemaId": uid("ps-1"), "display": None, "hideWhenEmpty": None, "readonly": False},
                hlc=(3, 0),
            )
        )
        assert row() == (None, 0, None)

    def test_class_property_set_render_contract_keys_fail_loud(self, store: LocalStore) -> None:
        """display/readonly/hideWhenEmpty are retired keys on the
        binding payload — the apply-time strict gate rejects the envelope
        outright (required stays a real binding field)."""
        store.apply_remote(class_env("class.create", uid("cls-1"), hlc=(1, 0), name="Task"))
        for key, value in (("display", "bullet"), ("readonly", True), ("hideWhenEmpty", True)):
            with pytest.raises(EnvelopeValidationError, match=key):
                store.apply_remote(
                    make_env(
                        "class.property.set",
                        {"classId": uid("cls-1"), "propertySchemaId": uid("ps-1"), key: value},
                        hlc=(2, 0),
                    )
                )

    def test_inactive_binding_keeps_schema_contracts_on_unbound_authored(self, store: LocalStore) -> None:
        """PC4: an inactive binding never becomes a candidate, so an
        authored value reads unbound — required/sequence drop to None (they
        are binding-sourced) — while the schema's render contracts still
        ride the row (they are PROPERTY-level, unbound values included)."""
        store.apply_remote(class_env("class.create", uid("cls-1"), hlc=(1, 0), name="Task"))
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {"propertySchemaId": uid("ps-1"), "name": "State", "type": "select", "display": "bullet"},
                hlc=(2, 0),
            )
        )
        store.apply_remote(
            make_env(
                "class.property.set",
                {"classId": uid("cls-1"), "propertySchemaId": uid("ps-1"), "sequence": 1, "required": True},
                hlc=(3, 0),
            )
        )
        store.apply_remote(
            make_env(
                "class.property.set",
                {"classId": uid("cls-1"), "propertySchemaId": uid("ps-1"), "active": False},
                hlc=(4, 0),
            )
        )
        member = uid("member")
        store.apply_remote(create_env(member, class_ids=(uid("cls-1"),), hlc=(5, 0), name="Buy milk"))
        store.apply_remote(
            make_env(
                "property.set",
                {"objectId": member, "propertySchemaId": uid("ps-1"), "value": "doing", "idx": 0},
                hlc=(6, 0),
                affected=(member,),
            )
        )
        # The binding ROW survives, flagged inactive…
        assert raw_rows(
            store,
            f"SELECT active FROM class_property WHERE class_id = '{uid('cls-1')}' AND property_schema_id = '{uid('ps-1')}'",
        ) == [(0,)]
        # …the effective read marks the value unbound (binding metadata
        # None) while the schema's display still rides.
        assert [
            (row.source, row.bound_by, row.required, row.sequence, row.display)
            for row in store.get_effective_properties(member)
        ] == [("authored", None, None, None, "bullet")]

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
            make_env("object.create", {"objectId": node_id, "classIds": [uid("cls-x")]}, hlc=(4, 0)),
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
        store.apply_remote(make_env("object.create", {"objectId": uid("n-y")}, hlc=(8, 0)))
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
        store.apply_remote(make_env("object.create", {"objectId": other, "tagIds": [uid("t-a")]}, hlc=(8, 0)))
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

    def test_number_formats_roundtrip_and_keep_vs_clear(self, store: LocalStore, tmp_path: Path) -> None:
        """Lockstep with the TS reference: numberPad/numberDecimals/
        numberRounding ride propertySchema.create; update keeps absent fields
        and clears explicit nulls (key presence, not value)."""
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {
                    "propertySchemaId": uid("ps-fmt"),
                    "name": "Dex number",
                    "type": "number",
                    "numberPad": 4,
                    "numberDecimals": 1,
                    "numberRounding": "floor",
                },
                hlc=(1, 0),
            )
        )
        def row() -> tuple:
            return raw_rows(
                store,
                f"SELECT number_pad, number_decimals, number_rounding FROM property_schema WHERE id = '{uid('ps-fmt')}'",
            )[0]

        assert row() == (4, 1, "floor")
        store.apply_remote(make_env("propertySchema.update", {"propertySchemaId": uid("ps-fmt"), "name": "Dex"}, hlc=(2, 0)))
        assert row() == (4, 1, "floor")  # absent keeps
        store.apply_remote(
            make_env(
                "propertySchema.update",
                {"propertySchemaId": uid("ps-fmt"), "numberDecimals": None, "numberRounding": None},
                hlc=(3, 0),
            )
        )
        assert row() == (4, None, None)  # explicit null clears

    def test_option_icon_rides_verbatim_through_create_and_wholesale_update(self, store: LocalStore) -> None:
        """The appliers serialize raw ``payload["options"]`` verbatim,
        so an option's icon (and any additive decoration) lands in the stored
        options JSON on create and on the wholesale options replace."""
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {
                    "propertySchemaId": uid("ps-icon"),
                    "name": "Stage",
                    "type": "select",
                    "options": [
                        {"id": "opt-a", "label": "A", "icon": "mdiCircle", "color": "yellow"},
                        {"id": "opt-b", "label": "B"},
                    ],
                },
                hlc=(1, 0),
            )
        )
        stored = raw_rows(store, f"SELECT options FROM property_schema WHERE id = '{uid('ps-icon')}'")
        assert json.loads(stored[0][0]) == [
            {"id": "opt-a", "label": "A", "icon": "mdiCircle", "color": "yellow"},
            {"id": "opt-b", "label": "B"},
        ]
        store.apply_remote(
            make_env(
                "propertySchema.update",
                {
                    "propertySchemaId": uid("ps-icon"),
                    "options": [{"id": "opt-c", "label": "C", "icon": "mdiCheckCircle"}],
                },
                hlc=(2, 0),
            )
        )
        replaced = raw_rows(store, f"SELECT options FROM property_schema WHERE id = '{uid('ps-icon')}'")
        assert json.loads(replaced[0][0]) == [{"id": "opt-c", "label": "C", "icon": "mdiCheckCircle"}]


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
        store.apply_remote(create_env(uid("n2"), parent_id=uid("n1")))
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
                    "is_class": 0,
                    "present_as_main": 1,
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
                {
                    "id": uid("n2"),
                    "workspace_id": WS_A,
                    "is_class": 0,
                    "present_as_main": 0,
                    "parent_id": uid("n1"),
                    "content": "[]",
                },
            ]
        )
        assert store.restore_snapshot(blob, workspace_id=WS_A) is True
        rows = {row.id: row for row in store.nodes(WS_A, include_inactive=True)}
        # Same-name, same-polarity mapping: is_class/present_as_main/
        # is_active/class_ids/name copy verbatim and the row-LWW columns seed
        # the LWW baseline.
        assert rows[uid("n1")].present_as_main is True
        assert rows[uid("n1")].is_class is False
        assert rows[uid("n1")].is_active is False  # is_active=0
        assert rows[uid("n1")].content == mirror
        assert rows[uid("n1")].class_ids == (uid("c-1"),)
        assert rows[uid("n1")].name == "Snap Page"
        assert rows[uid("n2")].present_as_main is False
        assert rows[uid("n2")].is_class is False
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
                is_class INTEGER NOT NULL DEFAULT 0,
                present_as_main INTEGER NOT NULL DEFAULT 0,
                parent_id TEXT,
                content TEXT NOT NULL DEFAULT '[]',
                is_active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            f"INSERT INTO node (id, workspace_id, present_as_main, content) VALUES ('{uid('legacy')}', ?, 1, 'old')",
            (WS_A,),
        )
        blob = conn.serialize()
        conn.close()
        assert store.restore_snapshot(blob, workspace_id=WS_A) is True
        row = store.node(WS_A, uid("legacy"))
        assert row is not None and row.present_as_main is True and row.content == "old"
        # No baseline → any later update wins.
        assert (
            store.apply_remote(update_env(uid("legacy"), hlc=(1, 0), contentAst=[{"type": "text", "text": "new"}]))
            is True
        )
        assert json.loads(store.node(WS_A, uid("legacy")).content or "") == [{"type": "text", "text": "new"}]

    def test_restore_filters_by_workspace(self, store: LocalStore) -> None:
        blob = make_server_snapshot(
            [
                {"id": uid("n1"), "workspace_id": WS_A, "is_class": 0, "present_as_main": 1, "content": "[]"},
                {"id": uid("n2"), "workspace_id": WS_B, "is_class": 0, "present_as_main": 1, "content": "[]"},
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
        conn.execute("CREATE TABLE nodes (id TEXT, workspace_id TEXT, is_class INTEGER)")
        conn.execute("INSERT INTO nodes VALUES ('x', ?, 0)", (WS_A,))
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


class TestWorkspaceFeatureToggle:
    """The workspace.feature.set applier: LWW rows, the
    family archival re-derivation with the event→meeting/birthday/trip
    cascade, the tasks-enable family ensure, and F4 routing."""

    TASK_CLASS = "00000000-0000-0000-0001-000000000012"
    EVENT_CLASS = "00000000-0000-0000-0001-000000000040"
    MEETING_CLASS = "00000000-0000-0000-0001-000000000039"
    BIRTHDAY_CLASS = "00000000-0000-0000-0001-000000000041"
    TRIP_CLASS = "00000000-0000-0000-0001-000000000047"
    BOOK_CLASS = "00000000-0000-0000-0001-000000000024"

    @staticmethod
    def feature_env(feature: str, enabled: bool, *, hlc: tuple[int, int], actor_id: str = ACTOR) -> RelayEnvelope:
        return make_env(
            "workspace.feature.set",
            {"feature": feature, "enabled": enabled},
            hlc=hlc,
            actor_id=actor_id,
            affected=(),
        )

    def _seed_calendar_family(self, store: LocalStore) -> None:
        for class_id, name, hlc in (
            (self.EVENT_CLASS, "event", (1, 0)),
            (self.MEETING_CLASS, "meeting", (2, 0)),
            (self.BIRTHDAY_CLASS, "birthday", (3, 0)),
            # The #14 follow-up (owner list, 2026-10-06): trip extends
            # event, so the events toggle cascades to it too.
            (self.TRIP_CLASS, "trip", (4, 0)),
        ):
            store.apply_remote(class_env("class.create", class_id, hlc=hlc, name=name))

    def test_lww_row_and_absent_reads_enabled(self, store: LocalStore) -> None:
        assert store.is_feature_enabled(WS_A, "tasks") is True  # absent row (F2)
        assert store.apply_remote(self.feature_env("tasks", True, hlc=(10, 0))) is True
        assert store.apply_remote(self.feature_env("tasks", False, hlc=(20, 0))) is True
        assert store.is_feature_enabled(WS_A, "tasks") is False
        # A stale toggle drops whole (ignored, no state change).
        assert store.apply_remote(self.feature_env("tasks", True, hlc=(15, 0))) is False
        assert store.is_feature_enabled(WS_A, "tasks") is False
        assert store.feature_rows(WS_A) == [("tasks", False, 20, 0, ACTOR)]

    def test_disable_cascades_through_the_extends_children(self, store: LocalStore) -> None:
        self._seed_calendar_family(store)
        store.apply_remote(self.feature_env("events", False, hlc=(30, 0)))
        for class_id in (self.EVENT_CLASS, self.MEETING_CLASS, self.BIRTHDAY_CLASS, self.TRIP_CLASS):
            assert raw_rows(store, "SELECT active FROM class WHERE id = ?", (class_id,)) == [(0,)]
            assert raw_rows(store, "SELECT is_active FROM nodes WHERE id = ? AND is_class = 1", (class_id,)) == [(0,)]
        # Meetings off alone leaves the event base live; the trip child
        # comes back with the events base (it has no toggle of its own).
        store.apply_remote(self.feature_env("meetings", False, hlc=(40, 0)))
        store.apply_remote(self.feature_env("events", True, hlc=(50, 0)))
        assert raw_rows(store, "SELECT active FROM class WHERE id = ?", (self.EVENT_CLASS,)) == [(1,)]
        assert raw_rows(store, "SELECT active FROM class WHERE id = ?", (self.BIRTHDAY_CLASS,)) == [(1,)]
        assert raw_rows(store, "SELECT active FROM class WHERE id = ?", (self.TRIP_CLASS,)) == [(1,)]
        # Per-class re-derivation: the meetings-off meeting stays archived…
        assert raw_rows(store, "SELECT active FROM class WHERE id = ?", (self.MEETING_CLASS,)) == [(0,)]
        # …until its own toggle comes back on.
        store.apply_remote(self.feature_env("meetings", True, hlc=(60, 0)))
        assert raw_rows(store, "SELECT active FROM class WHERE id = ?", (self.MEETING_CLASS,)) == [(1,)]

    def test_disable_never_touches_memberships(self, store: LocalStore) -> None:
        self._seed_calendar_family(store)
        member = uid("member")
        store.apply_remote(create_env(member, class_ids=(self.EVENT_CLASS,), hlc=(10, 0), name="Launch day"))
        store.apply_remote(self.feature_env("events", False, hlc=(30, 0)))
        assert store.node(WS_A, member).class_ids == (self.EVENT_CLASS,)
        assert raw_rows(
            store,
            "SELECT present FROM class_member_set WHERE node_id = ? AND class_id = ?",
            (member, self.EVENT_CLASS),
        ) == [(1,)]

    def test_tasks_enable_authors_the_family_at_fixed_ids(self, store: LocalStore) -> None:
        assert store.apply_remote(self.feature_env("tasks", True, hlc=(10, 0))) is True
        row = store.node(WS_A, self.TASK_CLASS)
        assert row is not None and row.is_class is True and row.is_active is True
        assert row.content == json.dumps([{"type": "text", "text": "Task"}])
        assert raw_rows(store, "SELECT name, icon, active FROM class WHERE id = ?", (self.TASK_CLASS,)) == [
            ("Task", "mdiCheckboxMarkedCircleOutline", 1)
        ]
        # Six schemas + bindings at the fixed ids, seeded before any real
        # binding write (hlc 0/0, NULL actor — any later class.property.set
        # wins the row LWW).
        assert raw_rows(store, "SELECT COUNT(*) FROM property_schema WHERE id LIKE '00000000-0000-0000-0003-%'") == [
            (6,)
        ]
        assert raw_rows(
            store,
            "SELECT COUNT(*) FROM class_property WHERE class_id = ?"
            " AND hlc_physical = 0 AND hlc_logical = 0 AND actor_id IS NULL",
            (self.TASK_CLASS,),
        ) == [(6,)]
        options = raw_rows(
            store,
            "SELECT options FROM property_schema WHERE id = '00000000-0000-0000-0003-000000000001'",
        )
        assert '"00000000-0000-0000-0004-000000000008"' in options[0][0]  # backlog option id

    def test_task_family_seed_pins_the_designed_status_style_and_display(self, store: LocalStore) -> None:
        """The Status options carry the designed glyphs (fixed ids,
        MDI icons, color tokens) and ONLY the Status SCHEMA defaults
        to display="bullet" — the rest stay NULL ('panel'). The binding
        table knows nothing about display."""
        assert store.apply_remote(self.feature_env("tasks", True, hlc=(10, 0))) is True
        status_rows = raw_rows(
            store, "SELECT options FROM property_schema WHERE id = ?", ("00000000-0000-0000-0003-000000000001",)
        )
        stored_status = json.loads(status_rows[0][0])
        assert [(o["id"], o["label"], o["icon"], o["color"]) for o in stored_status] == [
            ("00000000-0000-0000-0004-000000000008", "Backlog", "mdiCircleOutline", "gray"),
            ("00000000-0000-0000-0004-000000000009", "Pending", "mdiCircle", "yellow"),
            ("00000000-0000-0000-0004-00000000000a", "Doing", "mdiCircleHalfFull", "orange"),
            ("00000000-0000-0000-0004-00000000000b", "Reviewing", "mdiEyeCircleOutline", "blue"),
            ("00000000-0000-0000-0004-00000000000c", "Done", "mdiCheckCircle", "green"),
            ("00000000-0000-0000-0004-00000000000d", "Cancelled", "mdiCloseCircle", "red"),
        ]
        assert all(set(option) == {"id", "label", "icon", "color"} for option in stored_status)
        # The priority options stay decoration-free (id + label only).
        priority_rows = raw_rows(
            store, "SELECT options FROM property_schema WHERE id = ?", ("00000000-0000-0000-0003-000000000004",)
        )
        stored_priority = json.loads(priority_rows[0][0])
        assert [(o["id"], o["label"]) for o in stored_priority] == [
            ("00000000-0000-0000-0004-00000000000e", "Low"),
            ("00000000-0000-0000-0004-00000000000f", "Medium"),
            ("00000000-0000-0000-0004-000000000010", "High"),
            ("00000000-0000-0000-0004-000000000011", "Urgent"),
        ]
        assert all(set(option) == {"id", "label"} for option in stored_priority)
        displays = dict(
            raw_rows(
                store,
                "SELECT id, display FROM property_schema WHERE id LIKE '00000000-0000-0000-0003-%'",
            )
        )
        assert displays == {
            "00000000-0000-0000-0003-000000000001": "bullet",  # Status
            "00000000-0000-0000-0003-000000000002": None,  # Deadline
            "00000000-0000-0000-0003-000000000003": None,  # Scheduled
            "00000000-0000-0000-0003-000000000004": None,  # Priority
            "00000000-0000-0000-0003-000000000005": None,  # Closed
            "00000000-0000-0000-0003-000000000006": None,  # Recurrence
        }
        binding_columns = {row[1] for row in raw_rows(store, "PRAGMA table_info(class_property)")}
        assert "display" not in binding_columns

    def test_ensure_is_insert_or_ignore_and_never_clobbers(self, store: LocalStore) -> None:
        store.apply_remote(self.feature_env("tasks", True, hlc=(10, 0)))
        store._conn.execute("UPDATE class SET name = 'Custom' WHERE id = ?", (self.TASK_CLASS,))  # noqa: SLF001
        # A later winning enable re-runs the ensure: the user's row survives.
        store.apply_remote(self.feature_env("tasks", True, hlc=(20, 0)))
        assert raw_rows(store, "SELECT name FROM class WHERE id = ?", (self.TASK_CLASS,)) == [("Custom",)]

    def test_f4_routes_managed_base_delete_and_children_stay_plain(self, store: LocalStore) -> None:
        store.apply_remote(class_env("class.create", self.TASK_CLASS, hlc=(1, 0), name="Task"))
        member = uid("member")
        store.apply_remote(create_env(member, class_ids=(self.TASK_CLASS,), hlc=(2, 0), name="Buy milk"))
        # The managed base delete routes to the toggle: memberships survive…
        assert store.apply_remote(class_env("class.delete", self.TASK_CLASS, hlc=(30, 0))) is True
        assert store.is_feature_enabled(WS_A, "tasks") is False
        assert store.node(WS_A, member).class_ids == (self.TASK_CLASS,)
        assert store.node(WS_A, self.TASK_CLASS).is_active is False
        # …and NO feature row appears for a non-managed child delete, which
        # keeps the lossy semantics (membership tombstoning).
        store.apply_remote(class_env("class.create", self.BOOK_CLASS, hlc=(40, 0), name="Book"))
        book_member = uid("book-member")
        store.apply_remote(create_env(book_member, class_ids=(self.BOOK_CLASS,), hlc=(41, 0), name="Dune"))
        assert store.apply_remote(class_env("class.delete", self.BOOK_CLASS, hlc=(50, 0))) is True
        assert store.node(WS_A, book_member).class_ids == ()
        assert raw_rows(
            store,
            "SELECT present FROM class_member_set WHERE node_id = ? AND class_id = ?",
            (book_member, self.BOOK_CLASS),
        ) == [(0,)]
        assert raw_rows(store, "SELECT COUNT(*) FROM workspace_feature WHERE feature = 'sources'") == [(0,)]


class TestPropertyUnsetCarrierTrash:
    """PB2: unsetting a node-backed text value trashes the carrier
    block — trash + retention under the three guards (text schema, node-ref
    value, active non-class child of the owner, exclusively referenced)."""

    @staticmethod
    def _text_schema(store: LocalStore, *, hlc: tuple[int, int] = (1, 0)) -> str:
        schema_id = uid("schema-text")
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {"propertySchemaId": schema_id, "name": "notes", "type": "text"},
                hlc=hlc,
            )
        )
        return schema_id

    def test_unset_trashes_the_orphaned_carrier(self, store: LocalStore) -> None:
        schema_id = self._text_schema(store)
        store.apply_remote(create_env(name="Page", hlc=(2, 0)))
        store.apply_remote(create_env(uid("carrier"), parent_id=NODE, content="rich notes", hlc=(3, 0)))
        carrier = uid("carrier")
        store.apply_remote(property_set_env(NODE, schema_id, value={"nodeId": carrier}, hlc=(4, 0)))
        store.apply_remote(property_unset_env(NODE, schema_id, hlc=(5, 0)))
        assert raw_rows(store, "SELECT COUNT(*) FROM property_value WHERE node_id = ?", (NODE,)) == [(0,)]
        assert store.node(WS_A, carrier).is_active is False
        assert raw_rows(store, "SELECT is_permanent FROM trash WHERE node_id = ?", (carrier,)) == [(0,)]

    def test_scalar_text_value_has_no_carrier(self, store: LocalStore) -> None:
        schema_id = self._text_schema(store)
        store.apply_remote(create_env(name="Page", hlc=(2, 0)))
        store.apply_remote(property_set_env(NODE, schema_id, value="kuhn-1962", hlc=(4, 0)))
        store.apply_remote(property_unset_env(NODE, schema_id, hlc=(5, 0)))
        assert raw_rows(store, "SELECT COUNT(*) FROM trash") == [(0,)]

    def test_still_referenced_carrier_survives(self, store: LocalStore) -> None:
        schema_id = self._text_schema(store)
        store.apply_remote(create_env(name="Page", hlc=(2, 0)))
        store.apply_remote(create_env(uid("other"), name="Other", hlc=(3, 0)))
        store.apply_remote(create_env(uid("carrier"), parent_id=NODE, content="shared", hlc=(4, 0)))
        carrier = uid("carrier")
        store.apply_remote(property_set_env(NODE, schema_id, value={"nodeId": carrier}, hlc=(5, 0)))
        store.apply_remote(property_set_env(uid("other"), schema_id, value={"nodeId": carrier}, hlc=(6, 0)))
        store.apply_remote(property_unset_env(NODE, schema_id, hlc=(7, 0)))
        assert store.node(WS_A, carrier).is_active is True
        assert raw_rows(store, "SELECT COUNT(*) FROM trash") == [(0,)]

    def test_guards_reject_class_non_child_and_inactive_carriers(self, store: LocalStore) -> None:
        schema_id = self._text_schema(store)
        store.apply_remote(create_env(name="Page", hlc=(2, 0)))
        # Not a child of the owner: an orphan root node referenced by the value.
        store.apply_remote(create_env(uid("carrier"), name="elsewhere", hlc=(3, 0)))
        store.apply_remote(property_set_env(NODE, schema_id, value={"nodeId": uid("carrier")}, hlc=(4, 0)))
        store.apply_remote(property_unset_env(NODE, schema_id, hlc=(5, 0)))
        assert store.node(WS_A, uid("carrier")).is_active is True
        # A class node is never a carrier.
        store.apply_remote(class_env("class.create", uid("cls"), hlc=(6, 0), name="Genre"))
        store.apply_remote(property_set_env(NODE, schema_id, value={"nodeId": uid("cls")}, hlc=(7, 0)))
        store.apply_remote(property_unset_env(NODE, schema_id, hlc=(8, 0)))
        assert store.node(WS_A, uid("cls")).is_active is True
        assert raw_rows(store, "SELECT COUNT(*) FROM trash") == [(0,)]

    def test_legacy_bare_uuid_value_shape_trashes_the_carrier(self, store: LocalStore) -> None:
        schema_id = self._text_schema(store)
        store.apply_remote(create_env(name="Page", hlc=(2, 0)))
        store.apply_remote(create_env(uid("carrier"), parent_id=NODE, content="legacy", hlc=(3, 0)))
        carrier = uid("carrier")
        store.apply_remote(property_set_env(NODE, schema_id, value=carrier, hlc=(4, 0)))
        store.apply_remote(property_unset_env(NODE, schema_id, hlc=(5, 0)))
        assert store.node(WS_A, carrier).is_active is False
        assert raw_rows(store, "SELECT is_permanent FROM trash WHERE node_id = ?", (carrier,)) == [(0,)]

    def test_element_remove_trashes_the_carrier_too(self, store: LocalStore) -> None:
        schema_id = self._text_schema(store)
        store.apply_remote(create_env(name="Page", hlc=(2, 0)))
        store.apply_remote(create_env(uid("carrier"), parent_id=NODE, content="element", hlc=(3, 0)))
        carrier = uid("carrier")
        element = new_uuid7()
        store.apply_remote(
            make_env(
                "property.set",
                {"objectId": NODE, "propertySchemaId": schema_id, "value": {"nodeId": carrier}, "elementId": element},
                hlc=(4, 0),
                affected=(NODE,),
            )
        )
        store.apply_remote(
            make_env(
                "property.unset",
                {"objectId": NODE, "propertySchemaId": schema_id, "elementId": element},
                hlc=(5, 0),
                affected=(NODE,),
            )
        )
        assert store.node(WS_A, carrier).is_active is False
        assert raw_rows(
            store, "SELECT COUNT(*) FROM property_value_element_tombstone WHERE element_id = ?", (element,)
        ) == [(1,)]


class TestPropertyWireBatch:
    """PG5/PC4/PC6 unit semantics on the appliers beyond the
    vendored fixtures."""

    def test_same_idx_element_adds_coexist_and_order_by_element_id(self, store: LocalStore) -> None:
        store.apply_remote(create_env(name="Page", hlc=(1, 0)))
        schema_id = uid("schema-multi")
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {"propertySchemaId": schema_id, "name": "tag", "type": "text", "multi": True},
                hlc=(2, 0),
            )
        )
        first, second = new_uuid7(), new_uuid7()
        store.apply_remote(
            make_env(
                "property.set",
                {"objectId": NODE, "propertySchemaId": schema_id, "value": "one", "elementId": first, "idx": 0},
                hlc=(3, 0),
                affected=(NODE,),
            )
        )
        store.apply_remote(
            make_env(
                "property.set",
                {"objectId": NODE, "propertySchemaId": schema_id, "value": "two", "elementId": second, "idx": 0},
                hlc=(4, 0),
                affected=(NODE,),
            )
        )
        # Same idx, distinct rows — the retired UNIQUE would have rejected this.
        assert raw_rows(
            store, "SELECT id, value, idx FROM property_value WHERE node_id = ? ORDER BY id", (NODE,)
        ) == sorted([(first, '"one"', 0), (second, '"two"', 0)])
        effective = store.get_effective_properties(NODE)
        assert [(row.value, row.idx) for row in effective] == [("one", 0), ("two", 0)]

    def test_element_remove_tombstones_and_newer_add_revives(self, store: LocalStore) -> None:
        store.apply_remote(create_env(name="Page", hlc=(1, 0)))
        schema_id = uid("schema-multi")
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {"propertySchemaId": schema_id, "name": "tag", "type": "text", "multi": True},
                hlc=(2, 0),
            )
        )
        element = new_uuid7()

        def add(value: str, hlc: tuple[int, int]) -> None:
            store.apply_remote(
                make_env(
                    "property.set",
                    {
                        "objectId": NODE,
                        "propertySchemaId": schema_id,
                        "value": value,
                        "elementId": element,
                        "idx": 0,
                    },
                    hlc=hlc,
                    affected=(NODE,),
                )
            )

        add("live", (3, 0))
        store.apply_remote(
            make_env(
                "property.unset",
                {"objectId": NODE, "propertySchemaId": schema_id, "elementId": element},
                hlc=(4, 0),
                affected=(NODE,),
            )
        )
        assert raw_rows(store, "SELECT COUNT(*) FROM property_value WHERE id = ?", (element,)) == [(0,)]
        assert store.get_effective_properties(NODE) == []
        # A strictly-older re-add stays dropped (the tombstone wins)…
        add("stale", (3, 5))
        assert raw_rows(store, "SELECT COUNT(*) FROM property_value WHERE id = ?", (element,)) == [(0,)]
        # …a newer one revives the element (add-wins over older tombstones).
        add("revived", (5, 0))
        assert raw_rows(store, "SELECT value FROM property_value WHERE id = ?", (element,)) == [('"revived"',)]
        assert [row.value for row in store.get_effective_properties(NODE)] == ["revived"]

    def test_element_remove_malformed_addressing_is_a_no_op(self, store: LocalStore) -> None:
        store.apply_remote(create_env(name="Page", hlc=(1, 0)))
        store.apply_remote(create_env(uid("other"), name="Other", hlc=(2, 0)))
        schema_id = uid("schema-multi")
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {"propertySchemaId": schema_id, "name": "tag", "type": "text", "multi": True},
                hlc=(3, 0),
            )
        )
        element = new_uuid7()
        store.apply_remote(
            make_env(
                "property.set",
                {"objectId": uid("other"), "propertySchemaId": schema_id, "value": "x", "elementId": element},
                hlc=(4, 0),
                affected=(uid("other"),),
            )
        )
        store.apply_remote(
            make_env(
                "property.unset",
                {"objectId": NODE, "propertySchemaId": schema_id, "elementId": element},
                hlc=(5, 0),
                affected=(NODE,),
            )
        )
        # The live row (owned by the OTHER node) survives; the tombstone was
        # still recorded under the payload's addressing.
        assert raw_rows(store, "SELECT value FROM property_value WHERE id = ?", (element,)) == [('"x"',)]
        assert store.get_effective_properties(uid("other"))[0].value == "x"

    def test_permanent_delete_purges_the_subtrees_element_tombstones(self, store: LocalStore) -> None:
        store.apply_remote(create_env(name="Page", hlc=(1, 0)))
        store.apply_remote(create_env(uid("carrier"), parent_id=NODE, content="block", hlc=(2, 0)))
        schema_id = uid("schema-multi")
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {"propertySchemaId": schema_id, "name": "tag", "type": "text", "multi": True},
                hlc=(3, 0),
            )
        )
        element = new_uuid7()
        store.apply_remote(
            make_env(
                "property.set",
                {"objectId": uid("carrier"), "propertySchemaId": schema_id, "value": "v", "elementId": element},
                hlc=(4, 0),
                affected=(uid("carrier"),),
            )
        )
        store.apply_remote(
            make_env(
                "property.unset",
                {"objectId": uid("carrier"), "propertySchemaId": schema_id, "elementId": element},
                hlc=(5, 0),
                affected=(uid("carrier"),),
            )
        )
        assert raw_rows(store, "SELECT COUNT(*) FROM property_value_element_tombstone") == [(1,)]
        store.apply_remote(make_env("object.delete", {"objectId": uid("carrier"), "permanent": True}, hlc=(6, 0)))
        assert raw_rows(store, "SELECT COUNT(*) FROM property_value_element_tombstone") == [(0,)]

    def test_pc4_inactive_binding_survives_unset_deletes(self, store: LocalStore) -> None:
        store.apply_remote(create_env(name="Page", hlc=(1, 0)))
        class_id = uid("cls")
        schema_id = uid("schema-sel")
        item = uid("item")
        store.apply_remote(class_env("class.create", class_id, hlc=(2, 0), name="Pipeline"))
        store.apply_remote(create_env(item, class_ids=(class_id,), hlc=(3, 0), name="Deal"))
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {
                    "propertySchemaId": schema_id,
                    "name": "stage",
                    "type": "select",
                    "options": [{"id": "a", "label": "A"}],
                },
                hlc=(4, 0),
            )
        )
        store.apply_remote(
            class_env(
                "class.property.set",
                class_id,
                hlc=(5, 0),
                **{"propertySchemaId": schema_id, "defaultValue": "a", "active": True},
            )
        )
        assert [row.value for row in store.get_effective_properties(item)] == ["a"]
        store.apply_remote(
            class_env("class.property.set", class_id, hlc=(6, 0), **{"propertySchemaId": schema_id, "active": False})
        )
        # The ROW survives (unlike class.property.unset)…
        assert raw_rows(
            store,
            "SELECT active, default_value FROM class_property WHERE class_id = ? AND property_schema_id = ?",
            (class_id, schema_id),
        ) == [(0, '"a"')]
        assert store.get_effective_properties(item) == []
        # …and a plain unset deletes it outright.
        store.apply_remote(class_env("class.property.unset", class_id, hlc=(7, 0), **{"propertySchemaId": schema_id}))
        assert raw_rows(store, "SELECT COUNT(*) FROM class_property WHERE class_id = ?", (class_id,)) == [(0,)]

    def test_pc6_normalize_only_on_qualified_schemas_and_well_formed_strings(self, store: LocalStore) -> None:
        store.apply_remote(create_env(name="Page", hlc=(1, 0)))
        store.apply_remote(create_env(uid("target"), name="Ada", hlc=(2, 0)))
        qualified = uid("schema-q")
        plain = uid("schema-p")
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {
                    "propertySchemaId": qualified,
                    "name": "membership",
                    "type": "object",
                    "multi": True,
                    "dateQualified": True,
                },
                hlc=(3, 0),
            )
        )
        store.apply_remote(
            make_env(
                "propertySchema.create", {"propertySchemaId": plain, "name": "plain", "type": "object"}, hlc=(4, 0)
            )
        )
        day = "00000000-0000-0000-00dd-202003040000"
        store.apply_remote(
            property_set_env(
                NODE,
                qualified,
                value={"nodeId": uid("target")},
                idx=0,
                metadata={"startDate": "2020-03-04", "endDate": "not-a-date", "note": "2020-03-04"},
                hlc=(5, 0),
            )
        )
        store.apply_remote(
            property_set_env(
                NODE,
                plain,
                value={"nodeId": uid("target")},
                idx=0,
                metadata={"startDate": "2020-03-04"},
                hlc=(6, 0),
            )
        )
        assert raw_rows(
            store, "SELECT metadata FROM property_value WHERE node_id = ? AND property_schema_id = ?", (NODE, qualified)
        ) == [(json.dumps({"startDate": {"nodeId": day}, "endDate": "not-a-date", "note": "2020-03-04"}),)]
        # The non-qualified schema rides through untouched.
        assert raw_rows(
            store, "SELECT metadata FROM property_value WHERE node_id = ? AND property_schema_id = ?", (NODE, plain)
        ) == [(json.dumps({"startDate": "2020-03-04"}),)]

    def test_pc6_read_leniency_for_legacy_string_rows(self, store: LocalStore) -> None:
        """A pre-PC6 replica's stored string row (written by a foreign
        client) reads verbatim — reads accept both shapes."""
        store.apply_remote(create_env(name="Page", hlc=(1, 0)))
        qualified = uid("schema-q")
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {"propertySchemaId": qualified, "name": "m", "type": "object", "dateQualified": True},
                hlc=(2, 0),
            )
        )
        store.apply_remote(property_set_env(NODE, qualified, value={"nodeId": NODE}, hlc=(3, 0)))
        store._conn.execute(  # noqa: SLF001 — rewrite the row the pre-PC6 way
            "UPDATE property_value SET metadata = ? WHERE node_id = ? AND property_schema_id = ?",
            (json.dumps({"startDate": "2018-06-01"}), NODE, qualified),
        )
        effective = store.get_effective_properties(NODE)
        assert effective[0].metadata == {"startDate": "2018-06-01"}


class TestPg4ExtendsAwareBindings:
    """PG4: the diamond rule — own binding (distance 0) → shortest
    extends-path → earliest class-assignment HLC, ties by class id. A subclass
    node inherits its ancestors' bindings; ``bound_by`` names the ANCESTOR
    whose row supplies the default + metadata. Mirrors the monorepo store's
    property-values.test.ts PG4 suite."""

    BASE = uid("pg4-base")
    MID = uid("pg4-mid")
    LEAF = uid("pg4-leaf")
    MID_B = uid("pg4-mid-b")
    LEAF_B = uid("pg4-leaf-b")
    SCHEMA = uid("pg4-schema")

    def _chain_store(self, store: LocalStore, bind_on: list[str]) -> None:
        """BASE <- MID <- LEAF chain, one text schema bound on each named class."""
        store.apply_remote(create_env(name="Owner", hlc=(1, 0)))
        for class_id, name, hlc in [
            (self.BASE, "Base", (2, 0)),
            (self.MID, "Mid", (3, 0)),
            (self.LEAF, "Leaf", (4, 0)),
        ]:
            store.apply_remote(class_env("class.create", class_id, hlc=hlc, name=name))
        store.apply_remote(extends_env(self.MID, [self.BASE], hlc=(5, 0)))
        store.apply_remote(extends_env(self.LEAF, [self.MID], hlc=(6, 0)))
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {"propertySchemaId": self.SCHEMA, "name": "genre", "type": "text"},
                hlc=(7, 0),
            )
        )
        for i, class_id in enumerate(bind_on):
            store.apply_remote(
                class_env(
                    "class.property.set",
                    class_id,
                    hlc=(8 + i, 0),
                    **{"propertySchemaId": self.SCHEMA, "sequence": i, "defaultValue": f"dft-{i}"},
                )
            )

    @staticmethod
    def _assign(store: LocalStore, node_id: str, class_id: str, hlc: tuple[int, int]) -> None:
        # Class assignment rides the re-issued object.create OR-Set add; one
        # envelope per class so the assignment HLCs order the winners.
        store.apply_remote(create_env(node_id, class_ids=(class_id,), hlc=hlc, name="Owner"))

    def test_inherited_binding_derives_with_the_ancestor_as_bound_by(self, store: LocalStore) -> None:
        self._chain_store(store, [self.BASE])
        self._assign(store, NODE, self.LEAF, (20, 0))
        effective = store.get_effective_properties(NODE)
        assert [(row.value, row.source, row.bound_by) for row in effective] == [("dft-0", "default", self.BASE)]

    def test_own_binding_beats_inherited_and_shortest_path_wins(self, store: LocalStore) -> None:
        self._chain_store(store, [self.BASE, self.MID])
        # A node carrying MID itself: the own binding (distance 0) supplies
        # the default over BASE's inherited row (distance 1).
        self._assign(store, NODE, self.MID, (20, 0))
        winner = next(row for row in store.get_effective_properties(NODE) if row.property_schema_id == self.SCHEMA)
        assert (winner.value, winner.source, winner.bound_by) == ("dft-1", "default", self.MID)
        # A LEAF-only node sees MID at distance 1 and BASE at distance 2 —
        # the shorter path supplies the default.
        other = uid("pg4-leaf-node")
        store.apply_remote(create_env(other, name="Owner", hlc=(19, 0)))
        self._assign(store, other, self.LEAF, (21, 0))
        winner_b = next(row for row in store.get_effective_properties(other) if row.property_schema_id == self.SCHEMA)
        assert (winner_b.value, winner_b.bound_by) == ("dft-1", self.MID)

    def test_diamond_tie_at_equal_distance_resolves_by_assignment_hlc(self, store: LocalStore) -> None:
        store.apply_remote(create_env(name="Owner", hlc=(1, 0)))
        for class_id, name, hlc in [
            (self.BASE, "Base", (2, 0)),
            (self.MID, "MidA", (3, 0)),
            (self.MID_B, "MidB", (4, 0)),
            (self.LEAF, "LeafA", (5, 0)),
            (self.LEAF_B, "LeafB", (6, 0)),
        ]:
            store.apply_remote(class_env("class.create", class_id, hlc=hlc, name=name))
        store.apply_remote(extends_env(self.LEAF, [self.MID], hlc=(7, 0)))
        store.apply_remote(extends_env(self.LEAF_B, [self.MID_B], hlc=(7, 0)))
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {"propertySchemaId": self.SCHEMA, "name": "genre", "type": "text"},
                hlc=(8, 0),
            )
        )
        store.apply_remote(
            class_env(
                "class.property.set",
                self.MID,
                hlc=(9, 0),
                **{"propertySchemaId": self.SCHEMA, "sequence": 0, "defaultValue": "from-A"},
            )
        )
        store.apply_remote(
            class_env(
                "class.property.set",
                self.MID_B,
                hlc=(9, 0),
                **{"propertySchemaId": self.SCHEMA, "sequence": 0, "defaultValue": "from-B"},
            )
        )
        # LEAF assigned first (earliest add HLC) — its path to MID wins the tie.
        self._assign(store, NODE, self.LEAF, (20, 0))
        self._assign(store, NODE, self.LEAF_B, (21, 0))
        winner = next(row for row in store.get_effective_properties(NODE) if row.property_schema_id == self.SCHEMA)
        assert (winner.value, winner.bound_by) == ("from-A", self.MID)
        # Reversed assignment order on another node flips the winner.
        other = uid("pg4-other")
        store.apply_remote(create_env(other, name="Other", hlc=(19, 0)))
        self._assign(store, other, self.LEAF_B, (20, 0))
        self._assign(store, other, self.LEAF, (21, 0))
        winner_b = next(row for row in store.get_effective_properties(other) if row.property_schema_id == self.SCHEMA)
        assert (winner_b.value, winner_b.bound_by) == ("from-B", self.MID_B)

    def test_inherited_binding_metadata_surfaces_on_authored_rows(self, store: LocalStore) -> None:
        self._chain_store(store, [self.BASE])
        # Required rides the BINDING; readonly is PROPERTY-level and
        # rides the SCHEMA — the authored row surfaces both, boundBy naming
        # the ancestor whose binding won.
        store.apply_remote(
            make_env(
                "propertySchema.update",
                {"propertySchemaId": self.SCHEMA, "readonly": True},
                hlc=(8, 0),
            )
        )
        store.apply_remote(
            class_env(
                "class.property.set",
                self.BASE,
                hlc=(9, 0),
                **{"propertySchemaId": self.SCHEMA, "required": True},
            )
        )
        self._assign(store, NODE, self.LEAF, (20, 0))
        store.apply_remote(property_set_env(NODE, self.SCHEMA, value="mine", hlc=(21, 0)))
        row = next(row for row in store.get_effective_properties(NODE) if row.property_schema_id == self.SCHEMA)
        assert row.value == "mine"
        assert row.source == "authored"
        assert row.bound_by == self.BASE
        assert row.required is True
        assert row.readonly is True


class TestPg6ApplyTimeValidation:
    """PG6: apply-time fail-loud validation at the property.set write
    path — scalar typing (number normalizes the migrated numeric-string
    encoding), the multi cardinality ceiling, the datePrecision ceiling,
    targetClassFilter membership (extends-aware), and node-typed target
    existence (trash counts as existence). Evidence-scoped deviations: image
    stays unchecked (PG14), text carrier refs stay existence-lenient (PB2),
    unknown schema ids store unchecked. Mirrors the monorepo store's
    property-validation.test.ts PG6 suite."""

    OWNER2 = uid("pg6-owner2")
    TARGET = uid("pg6-target")
    TARGET2 = uid("pg6-target2")
    GHOST = uid("pg6-ghost")
    CLASS_A = uid("pg6-class-a")
    CLASS_B = uid("pg6-class-b")
    SUB_A = uid("pg6-sub-a")  # extends CLASS_A — exercises the filter's class walk

    TEXT = uid("pg6-text")
    TEXT_MULTI = uid("pg6-text-multi")
    NUMBER = uid("pg6-number")
    BOOLEAN = uid("pg6-boolean")
    URL = uid("pg6-url")
    SELECT = uid("pg6-select")
    MULTI_SELECT = uid("pg6-multi-select")
    IMAGE = uid("pg6-image")
    DATE = uid("pg6-date")
    DATE_YEAR = uid("pg6-date-year")
    OBJECT_FILTERED = uid("pg6-object-filtered")
    RANGE = uid("pg6-range")
    UNKNOWN = uid("pg6-unknown")  # never created — the unknown-schema path

    YEAR_NODE = "00000000-0000-0000-00bb-202600000000"
    MONTH_NODE = "00000000-0000-0000-00aa-202609000000"
    DAY_NODE = "00000000-0000-0000-00dd-202609270000"

    def _seeded(self, store: LocalStore) -> None:
        store.apply_remote(create_env(name="Owner", hlc=(1, 0)))
        store.apply_remote(create_env(self.OWNER2, name="Owner 2", hlc=(2, 0)))
        store.apply_remote(create_env(self.TARGET, name="Target A", hlc=(3, 0)))
        store.apply_remote(create_env(self.TARGET2, name="Target B", hlc=(4, 0)))
        store.apply_remote(class_env("class.create", self.CLASS_A, hlc=(5, 0), name="ClassA"))
        store.apply_remote(class_env("class.create", self.CLASS_B, hlc=(6, 0), name="ClassB"))
        store.apply_remote(create_env(self.TARGET, class_ids=(self.CLASS_A,), hlc=(7, 0), name="Target A"))
        store.apply_remote(create_env(self.TARGET2, class_ids=(self.CLASS_B,), hlc=(8, 0), name="Target B"))
        schemas = [
            (self.TEXT, {"name": "notes", "type": "text"}),
            (self.TEXT_MULTI, {"name": "aliases", "type": "text", "multi": True}),
            (self.NUMBER, {"name": "count", "type": "number"}),
            (self.BOOLEAN, {"name": "done", "type": "boolean"}),
            (self.URL, {"name": "link", "type": "url"}),
            (self.SELECT, {"name": "state", "type": "select", "options": [{"id": "opt-1", "label": "One"}]}),
            (self.MULTI_SELECT, {"name": "tags", "type": "multi_select"}),
            (self.IMAGE, {"name": "cover", "type": "image"}),
            (self.DATE, {"name": "when", "type": "datetime"}),
            (self.DATE_YEAR, {"name": "yearOf", "type": "datetime", "datePrecision": "year"}),
            (self.OBJECT_FILTERED, {"name": "ref", "type": "object", "targetClassFilter": [self.CLASS_A]}),
            (self.RANGE, {"name": "span", "type": "datetime"}),
        ]
        for i, (schema_id, body) in enumerate(schemas):
            store.apply_remote(
                make_env("propertySchema.create", {"propertySchemaId": schema_id, **body}, hlc=(10 + i, 0))
            )
        # The date chain the date tests link to (year root → month → day).
        store.apply_remote(create_env(self.YEAR_NODE, hlc=(30, 0)))
        store.apply_remote(create_env(self.MONTH_NODE, parent_id=self.YEAR_NODE, hlc=(31, 0)))
        store.apply_remote(create_env(self.DAY_NODE, parent_id=self.MONTH_NODE, hlc=(32, 0)))

    def _value(self, store: LocalStore, schema_id: str, node_id: str = NODE, idx: int = 0) -> str | None:
        rows = raw_rows(
            store,
            "SELECT value FROM property_value WHERE node_id = ? AND property_schema_id = ? AND idx = ?",
            (node_id, schema_id, idx),
        )
        return rows[0][0] if rows else None

    def test_number_accepts_finite_and_normalizes_numeric_strings(self, store: LocalStore) -> None:
        self._seeded(store)
        store.apply_remote(property_set_env(NODE, self.NUMBER, value=42, hlc=(40, 0)))
        assert self._value(store, self.NUMBER) == "42"
        # migrated epoch-millis encoding (live-data evidence): normalizes.
        store.apply_remote(property_set_env(NODE, self.NUMBER, value="1757427533728", hlc=(41, 0)))
        assert self._value(store, self.NUMBER) == "1757427533728"
        for bad in ["abc", "", [1], {}, True, float("nan")]:
            with pytest.raises(PropertyValueShapeError):
                store.apply_remote(property_set_env(NODE, self.NUMBER, value=bad, hlc=(42, 0)))

    def test_boolean_url_select_accept_their_scalar_and_reject_other_shapes(self, store: LocalStore) -> None:
        self._seeded(store)
        store.apply_remote(property_set_env(NODE, self.BOOLEAN, value=True, hlc=(40, 0)))
        store.apply_remote(property_set_env(NODE, self.URL, value="https://x.test", hlc=(41, 0)))
        store.apply_remote(property_set_env(NODE, self.SELECT, value="opt-1", hlc=(42, 0)))
        for schema_id, bad in [(self.BOOLEAN, "true"), (self.URL, 42), (self.SELECT, ["opt-1"])]:
            with pytest.raises(PropertyValueShapeError):
                store.apply_remote(property_set_env(NODE, schema_id, value=bad, hlc=(43, 0)))

    def test_multi_select_takes_an_array_of_strings_only(self, store: LocalStore) -> None:
        self._seeded(store)
        store.apply_remote(property_set_env(NODE, self.MULTI_SELECT, value=["a", "b"], hlc=(40, 0)))
        assert self._value(store, self.MULTI_SELECT) == json.dumps(["a", "b"])
        for bad in ["a", [1], [None], {}]:
            with pytest.raises(PropertyValueShapeError):
                store.apply_remote(property_set_env(NODE, self.MULTI_SELECT, value=bad, hlc=(41, 0)))

    def test_image_passes_through_unchecked(self, store: LocalStore) -> None:
        self._seeded(store)
        # A migrated asset payload rides the live log — it must not fail.
        v1_payload = {"hash": "abc", "size": 12, "filename": "", "mime_type": "image/png"}
        store.apply_remote(property_set_env(NODE, self.IMAGE, value=v1_payload, hlc=(40, 0)))
        assert self._value(store, self.IMAGE) == json.dumps(v1_payload)

    def test_single_value_schema_rejects_idx_above_zero(self, store: LocalStore) -> None:
        self._seeded(store)
        with pytest.raises(PropertyValueShapeError):
            store.apply_remote(property_set_env(NODE, self.TEXT, value="x", idx=1, hlc=(40, 0)))
        store.apply_remote(property_set_env(NODE, self.TEXT_MULTI, value="one", idx=1, hlc=(41, 0)))
        assert self._value(store, self.TEXT_MULTI, idx=1) == json.dumps("one")

    def test_date_refs_may_not_claim_finer_granularity_than_the_schema(self, store: LocalStore) -> None:
        self._seeded(store)
        # day-precision schema (default): day/month/year refs all fine.
        store.apply_remote(property_set_env(NODE, self.DATE, value={"nodeId": self.DAY_NODE}, hlc=(40, 0)))
        store.apply_remote(property_set_env(NODE, self.DATE, value={"nodeId": self.YEAR_NODE}, hlc=(41, 0)))
        # year-precision schema: a year ref is fine, a day/month ref fails loud.
        store.apply_remote(property_set_env(NODE, self.DATE_YEAR, value={"nodeId": self.YEAR_NODE}, hlc=(42, 0)))
        for ref in (self.DAY_NODE, self.MONTH_NODE):
            with pytest.raises(PropertyValueShapeError):
                store.apply_remote(property_set_env(NODE, self.DATE_YEAR, value={"nodeId": ref}, hlc=(43, 0)))

    def test_target_class_filter_rejects_out_of_filter_targets(self, store: LocalStore) -> None:
        self._seeded(store)
        store.apply_remote(property_set_env(NODE, self.OBJECT_FILTERED, value={"nodeId": self.TARGET}, hlc=(40, 0)))
        assert self._value(store, self.OBJECT_FILTERED) == json.dumps({"nodeId": self.TARGET})
        # TARGET2 carries ClassB; the schema allows ClassA only.
        with pytest.raises(PropertyValueShapeError):
            store.apply_remote(
                property_set_env(NODE, self.OBJECT_FILTERED, value={"nodeId": self.TARGET2}, hlc=(41, 0))
            )

    def test_target_class_filter_is_extends_aware(self, store: LocalStore) -> None:
        self._seeded(store)
        # The bibliography model filters authors by `agent` while persons
        # EXTEND agent — a carried subclass satisfies the filter through the
        # class_hierarchy walk.
        store.apply_remote(class_env("class.create", self.SUB_A, hlc=(25, 0), name="SubA"))
        store.apply_remote(extends_env(self.SUB_A, [self.CLASS_A], hlc=(26, 0)))
        sub_target = uid("pg6-sub-target")
        store.apply_remote(create_env(sub_target, class_ids=(self.SUB_A,), hlc=(27, 0), name="Sub"))
        store.apply_remote(property_set_env(NODE, self.OBJECT_FILTERED, value={"nodeId": sub_target}, hlc=(40, 0)))
        assert self._value(store, self.OBJECT_FILTERED) == json.dumps({"nodeId": sub_target})

    def test_node_typed_refs_must_exist_and_trash_counts_as_existence(self, store: LocalStore) -> None:
        self._seeded(store)
        with pytest.raises(PropertyValueShapeError):
            store.apply_remote(property_set_env(NODE, self.DATE, value={"nodeId": self.GHOST}, hlc=(40, 0)))
        with pytest.raises(PropertyValueShapeError, match="does not exist"):
            store.apply_remote(property_set_env(NODE, self.OBJECT_FILTERED, value={"nodeId": self.GHOST}, hlc=(41, 0)))
        # datetime range: either end missing fails (an open other end is fine).
        store.apply_remote(
            property_set_env(NODE, self.RANGE, value={"start": {"nodeId": self.DAY_NODE}, "end": None}, hlc=(42, 0))
        )
        with pytest.raises(PropertyValueShapeError, match="does not exist"):
            store.apply_remote(
                property_set_env(
                    NODE,
                    self.RANGE,
                    value={"start": {"nodeId": self.DAY_NODE}, "end": {"nodeId": self.GHOST}},
                    hlc=(43, 0),
                )
            )
        # A TRASHED target still exists (trash is a state, not an absence) —
        # and it still carries ClassA, so the filtered schema accepts it.
        store.apply_remote(delete_env(self.TARGET, hlc=(50, 0)))
        store.apply_remote(
            property_set_env(self.OWNER2, self.OBJECT_FILTERED, value={"nodeId": self.TARGET}, hlc=(51, 0))
        )
        assert self._value(store, self.OBJECT_FILTERED, node_id=self.OWNER2) == json.dumps({"nodeId": self.TARGET})

    def test_text_carrier_refs_stay_existence_lenient(self, store: LocalStore) -> None:
        self._seeded(store)
        store.apply_remote(property_set_env(NODE, self.TEXT, value={"nodeId": self.GHOST}, hlc=(40, 0)))
        assert self._value(store, self.TEXT) == json.dumps({"nodeId": self.GHOST})

    def test_unknown_schema_ids_store_unchecked(self, store: LocalStore) -> None:
        self._seeded(store)
        store.apply_remote(property_set_env(NODE, self.UNKNOWN, value={"anything": True}, idx=7, hlc=(40, 0)))
        assert self._value(store, self.UNKNOWN, idx=7) == json.dumps({"anything": True})


class TestDatetimeValueUnion:
    """The unified datetime value union (SCHEMA.md "Datetime", owner
    2026-10-09): a point {nodeId, time?} or a range {start, end} of slots
    {nodeId, time?} anchored to the year/month/day node chain — either side
    open, both-open legal, legacy bare-uuid encodings normalizing. `time` is
    24h HH:MM minute precision and requires day precision on BOTH the slot's
    ref and the schema ceiling; a value carrying both nodeId and start/end is
    rejected outright. Mirrors the monorepo store's dates.test.ts datetime
    value union suite."""

    PUBLISHED = uid("dt-published")
    SPAN = uid("dt-span")
    YEARLY = uid("dt-yearly")

    CHAIN_A = {
        "year": "00000000-0000-0000-00bb-202600000000",
        "month": "00000000-0000-0000-00aa-202609000000",
        "day": "00000000-0000-0000-00dd-202609270000",
    }
    CHAIN_B = {
        "year": "00000000-0000-0000-00bb-202600000000",
        "month": "00000000-0000-0000-00aa-202610000000",
        "day": "00000000-0000-0000-00dd-202610040000",
    }
    CHAIN_C_DAY = "00000000-0000-0000-00dd-202801150000"  # never materialized — the ghost slot

    def _seeded(self, store: LocalStore) -> None:
        store.apply_remote(create_env(name="Owner", hlc=(1, 0)))
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {"propertySchemaId": self.PUBLISHED, "name": "published", "type": "datetime"},
                hlc=(2, 0),
            )
        )
        store.apply_remote(
            make_env(
                "propertySchema.create", {"propertySchemaId": self.SPAN, "name": "span", "type": "datetime"}, hlc=(3, 0)
            )
        )
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {"propertySchemaId": self.YEARLY, "name": "yearly", "type": "datetime", "datePrecision": "year"},
                hlc=(4, 0),
            )
        )
        # The chains the assertions link to (the ensureDateChain shape).
        store.apply_remote(create_env(self.CHAIN_A["year"], hlc=(5, 0)))
        store.apply_remote(create_env(self.CHAIN_A["month"], parent_id=self.CHAIN_A["year"], hlc=(6, 0)))
        store.apply_remote(create_env(self.CHAIN_A["day"], parent_id=self.CHAIN_A["month"], hlc=(7, 0)))
        store.apply_remote(create_env(self.CHAIN_B["month"], parent_id=self.CHAIN_B["year"], hlc=(8, 0)))
        store.apply_remote(create_env(self.CHAIN_B["day"], parent_id=self.CHAIN_B["month"], hlc=(9, 0)))

    def _value(self, store: LocalStore, schema_id: str) -> str | None:
        rows = raw_rows(
            store,
            "SELECT value FROM property_value WHERE node_id = ? AND property_schema_id = ? AND idx = 0",
            (NODE, schema_id),
        )
        return rows[0][0] if rows else None

    def test_a_timed_point_and_a_timed_range_end_round_trip_verbatim(self, store: LocalStore) -> None:
        self._seeded(store)
        store.apply_remote(
            property_set_env(
                NODE, self.PUBLISHED, value={"nodeId": self.CHAIN_A["day"], "time": "14:30"}, hlc=(40, 0)
            )
        )
        assert self._value(store, self.PUBLISHED) == json.dumps({"nodeId": self.CHAIN_A["day"], "time": "14:30"})
        store.apply_remote(
            property_set_env(
                NODE,
                self.SPAN,
                value={"start": {"nodeId": self.CHAIN_A["day"]}, "end": {"nodeId": self.CHAIN_B["day"], "time": "09:15"}},
                hlc=(41, 0),
            )
        )
        assert self._value(store, self.SPAN) == json.dumps(
            {"start": {"nodeId": self.CHAIN_A["day"]}, "end": {"nodeId": self.CHAIN_B["day"], "time": "09:15"}}
        )

    def test_a_both_open_range_is_a_legal_value(self, store: LocalStore) -> None:
        self._seeded(store)
        store.apply_remote(property_set_env(NODE, self.SPAN, value={"start": None, "end": None}, hlc=(40, 0)))
        assert self._value(store, self.SPAN) == json.dumps({"start": None, "end": None})

    def test_a_value_carrying_both_nodeid_and_start_end_is_rejected_outright(self, store: LocalStore) -> None:
        self._seeded(store)
        for bad in [
            {"nodeId": self.CHAIN_A["day"], "start": {"nodeId": self.CHAIN_A["day"]}, "end": None},
            {"nodeId": self.CHAIN_A["day"], "end": None},
        ]:
            with pytest.raises(PropertyValueShapeError):
                store.apply_remote(property_set_env(NODE, self.PUBLISHED, value=bad, hlc=(40, 0)))

    def test_a_malformed_time_is_rejected_on_points_and_range_slots_alike(self, store: LocalStore) -> None:
        self._seeded(store)
        for time in ["25:00", "9:30", "10:60", "14:30:00", 430, None]:
            with pytest.raises(PropertyValueShapeError):
                store.apply_remote(
                    property_set_env(NODE, self.PUBLISHED, value={"nodeId": self.CHAIN_A["day"], "time": time}, hlc=(40, 0))
                )
            with pytest.raises(PropertyValueShapeError):
                store.apply_remote(
                    property_set_env(
                        NODE,
                        self.SPAN,
                        value={"start": None, "end": {"nodeId": self.CHAIN_B["day"], "time": time}},
                        hlc=(41, 0),
                    )
                )

    def test_time_requires_day_precision_on_the_slots_ref(self, store: LocalStore) -> None:
        self._seeded(store)
        # A month/year anchor has no wall-clock time.
        with pytest.raises(PropertyValueShapeError):
            store.apply_remote(
                property_set_env(
                    NODE, self.PUBLISHED, value={"nodeId": self.CHAIN_A["month"], "time": "10:00"}, hlc=(40, 0)
                )
            )
        with pytest.raises(PropertyValueShapeError):
            store.apply_remote(
                property_set_env(
                    NODE,
                    self.SPAN,
                    value={"start": {"nodeId": self.CHAIN_A["year"], "time": "10:00"}, "end": None},
                    hlc=(41, 0),
                )
            )

    def test_time_requires_day_precision_on_the_schema_ceiling_too(self, store: LocalStore) -> None:
        self._seeded(store)
        # A full-day YEAR ref is fine at year precision…
        store.apply_remote(property_set_env(NODE, self.YEARLY, value={"nodeId": self.CHAIN_A["year"]}, hlc=(40, 0)))
        assert self._value(store, self.YEARLY) == json.dumps({"nodeId": self.CHAIN_A["year"]})
        # …but a timed DAY ref claims finer granularity than the ceiling.
        with pytest.raises(PropertyValueShapeError, match="finer granularity"):
            store.apply_remote(
                property_set_env(
                    NODE, self.YEARLY, value={"nodeId": self.CHAIN_A["day"], "time": "08:00"}, hlc=(41, 0)
                )
            )

    def test_each_non_null_range_slot_ref_gets_the_existence_check(self, store: LocalStore) -> None:
        self._seeded(store)
        # CHAIN_C (2028-01-15) is never materialized — a valid day-node id
        # with no node row (the date_range parity: either end missing fails,
        # open sides skip).
        with pytest.raises(PropertyValueShapeError, match="does not exist"):
            store.apply_remote(
                property_set_env(
                    NODE, self.SPAN, value={"start": {"nodeId": self.CHAIN_C_DAY}, "end": None}, hlc=(40, 0)
                )
            )
        with pytest.raises(PropertyValueShapeError, match="does not exist"):
            store.apply_remote(
                property_set_env(
                    NODE,
                    self.SPAN,
                    value={"start": None, "end": {"nodeId": self.CHAIN_C_DAY, "time": "12:00"}},
                    hlc=(41, 0),
                )
            )
        # Open sides store fine (no ref to check).
        store.apply_remote(property_set_env(NODE, self.SPAN, value={"start": None, "end": None}, hlc=(42, 0)))
        assert self._value(store, self.SPAN) == json.dumps({"start": None, "end": None})


class TestPb2OneShapePerType:
    """PB2: the one-shape-per-type gate — text = string-or-reference,
    datetime = the unified point/range union (a legacy bare uuid normalizes
    to a point), object/asset = node reference. The gate lives in the PG6
    validator the property.set applier consults; unknown schema ids store
    unchecked. Mirrors the monorepo store's property-values.test.ts PB2
    suite."""

    DATE_NODE = uid("pb2-date-node")

    def _seeded(self, store: LocalStore) -> LocalStore:
        store.apply_remote(create_env(name="Owner", hlc=(1, 0)))
        store.apply_remote(create_env(self.DATE_NODE, name="2026", hlc=(2, 0)))
        for schema_id, body, hlc in [
            (uid("pb2-text"), {"name": "notes", "type": "text"}, (3, 0)),
            (uid("pb2-date"), {"name": "when", "type": "datetime"}, (4, 0)),
            (uid("pb2-object"), {"name": "who", "type": "object"}, (5, 0)),
            (uid("pb2-range"), {"name": "span", "type": "datetime"}, (6, 0)),
        ]:
            store.apply_remote(make_env("propertySchema.create", {"propertySchemaId": schema_id, **body}, hlc=hlc))
        return store

    def _value(self, store: LocalStore, schema_id: str) -> str | None:
        rows = raw_rows(
            store, "SELECT value FROM property_value WHERE node_id = ? AND property_schema_id = ?", (NODE, schema_id)
        )
        return rows[0][0] if rows else None

    def test_text_accepts_a_scalar_and_a_reference(self, store: LocalStore) -> None:
        self._seeded(store)
        text = uid("pb2-text")
        store.apply_remote(property_set_env(NODE, text, value="kuhn1962", hlc=(40, 0)))
        assert self._value(store, text) == json.dumps("kuhn1962")
        store.apply_remote(property_set_env(NODE, text, value={"nodeId": self.DATE_NODE}, hlc=(41, 0)))
        assert self._value(store, text) == json.dumps({"nodeId": self.DATE_NODE})

    def test_text_rejects_non_string_non_reference_shapes(self, store: LocalStore) -> None:
        self._seeded(store)
        text = uid("pb2-text")
        for bad in [42, True, ["x"], {"nope": 1}, {"nodeId": 5}]:
            with pytest.raises(PropertyValueShapeError):
                store.apply_remote(property_set_env(NODE, text, value=bad, hlc=(40, 0)))

    def test_datetime_object_accept_refs_normalize_bare_uuid_reject_scalars(self, store: LocalStore) -> None:
        self._seeded(store)
        date = uid("pb2-date")
        obj = uid("pb2-object")
        store.apply_remote(property_set_env(NODE, date, value={"nodeId": self.DATE_NODE}, hlc=(40, 0)))
        assert self._value(store, date) == json.dumps({"nodeId": self.DATE_NODE})
        # Legacy bare uuid normalizes to the reference shape.
        store.apply_remote(property_set_env(NODE, date, value=self.DATE_NODE, hlc=(41, 0)))
        assert self._value(store, date) == json.dumps({"nodeId": self.DATE_NODE})
        # A date-looking string is NOT a reference — dates are nodes.
        with pytest.raises(PropertyValueShapeError):
            store.apply_remote(property_set_env(NODE, date, value="2026-09-27", hlc=(42, 0)))
        with pytest.raises(PropertyValueShapeError):
            store.apply_remote(property_set_env(NODE, obj, value="not-a-reference", hlc=(43, 0)))

    def test_datetime_accepts_either_side_open_and_rejects_bad_sides(self, store: LocalStore) -> None:
        self._seeded(store)
        rng = uid("pb2-range")
        store.apply_remote(property_set_env(NODE, rng, value={"start": self.DATE_NODE, "end": None}, hlc=(40, 0)))
        assert self._value(store, rng) == json.dumps({"start": {"nodeId": self.DATE_NODE}, "end": None})
        with pytest.raises(PropertyValueShapeError):
            store.apply_remote(property_set_env(NODE, rng, value="2026", hlc=(41, 0)))
        with pytest.raises(PropertyValueShapeError):
            store.apply_remote(property_set_env(NODE, rng, value={"start": 42, "end": None}, hlc=(42, 0)))
        # Both sides must be carried — an open range needs explicit nulls.
        with pytest.raises(PropertyValueShapeError):
            store.apply_remote(property_set_env(NODE, rng, value={"start": self.DATE_NODE}, hlc=(43, 0)))

    def test_null_values_bypass_shape_validation(self, store: LocalStore) -> None:
        self._seeded(store)
        date = uid("pb2-date")
        store.apply_remote(property_set_env(NODE, date, value=None, hlc=(40, 0)))
        assert self._value(store, date) == "null"

    def test_unknown_schema_ids_store_unchecked(self, store: LocalStore) -> None:
        self._seeded(store)
        loose = uid("pb2-loose")
        store.apply_remote(property_set_env(NODE, loose, value={"anything": True}, hlc=(40, 0)))
        assert self._value(store, loose) == json.dumps({"anything": True})


class TestPc2TypedDefaults:
    """PC2: a class-binding defaultValue must be typed per the schema type —
    the write path (class.property.set) fails loud, and the effective read
    drops a stored default that drifted out of match (schema delete+recreate
    with a different type). Mirrors the monorepo store's
    property-values.test.ts PC2 suite."""

    CLS = uid("pc2-cls")

    def _bound_store(self, store: LocalStore, type_: str, schema_id: str) -> None:
        store.apply_remote(create_env(name="Owner", hlc=(1, 0)))
        store.apply_remote(class_env("class.create", self.CLS, hlc=(2, 0), name="C"))
        store.apply_remote(
            make_env(
                "propertySchema.create", {"propertySchemaId": schema_id, "name": "slot", "type": type_}, hlc=(3, 0)
            )
        )

    def test_wrong_typed_default_fails_loud(self, store: LocalStore) -> None:
        self._bound_store(store, "text", uid("pc2-text"))
        with pytest.raises(PropertyValueShapeError):
            store.apply_remote(
                class_env(
                    "class.property.set",
                    self.CLS,
                    hlc=(4, 0),
                    **{"propertySchemaId": uid("pc2-text"), "defaultValue": True},
                )
            )
        with pytest.raises(PropertyValueShapeError):
            store.apply_remote(
                class_env(
                    "class.property.set",
                    self.CLS,
                    hlc=(5, 0),
                    **{"propertySchemaId": uid("pc2-text"), "defaultValue": {"nodeId": NODE}},
                )
            )

    def test_node_typed_schemas_reject_non_null_defaults(self, store: LocalStore) -> None:
        self._bound_store(store, "object", uid("pc2-object"))
        with pytest.raises(PropertyValueShapeError):
            store.apply_remote(
                class_env(
                    "class.property.set",
                    self.CLS,
                    hlc=(4, 0),
                    **{"propertySchemaId": uid("pc2-object"), "defaultValue": {"nodeId": NODE}},
                )
            )
        # JSON null IS a real default for any type.
        store.apply_remote(
            class_env(
                "class.property.set",
                self.CLS,
                hlc=(5, 0),
                **{"propertySchemaId": uid("pc2-object"), "defaultValue": None},
            )
        )
        assert raw_rows(
            store,
            "SELECT default_value FROM class_property WHERE class_id = ? AND property_schema_id = ?",
            (self.CLS, uid("pc2-object")),
        ) == [("null",)]

    def test_typed_defaults_store_and_derive(self, store: LocalStore) -> None:
        schema_id = uid("pc2-number")
        self._bound_store(store, "number", schema_id)
        store.apply_remote(
            class_env(
                "class.property.set",
                self.CLS,
                hlc=(4, 0),
                **{"propertySchemaId": schema_id, "defaultValue": 7},
            )
        )
        node = uid("pc2-node")
        store.apply_remote(create_env(node, class_ids=(self.CLS,), hlc=(10, 0), name="N"))
        effective = store.get_effective_properties(node)
        assert [(row.value, row.source, row.bound_by) for row in effective] == [(7, "default", self.CLS)]

    def test_drifted_default_yields_nothing_at_read(self, store: LocalStore) -> None:
        schema_id = uid("pc2-drift")
        self._bound_store(store, "number", schema_id)
        store.apply_remote(
            class_env(
                "class.property.set",
                self.CLS,
                hlc=(4, 0),
                **{"propertySchemaId": schema_id, "defaultValue": 7},
            )
        )
        # PG3-style drift: delete + recreate the schema under the SAME id as text.
        store.apply_remote(make_env("propertySchema.delete", {"propertySchemaId": schema_id}, hlc=(5, 0)))
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {"propertySchemaId": schema_id, "name": "slot", "type": "text"},
                hlc=(6, 0),
            )
        )
        node = uid("pc2-drift-node")
        store.apply_remote(create_env(node, class_ids=(self.CLS,), hlc=(10, 0), name="N"))
        # The binding row survives; the wrong-typed stored default is dropped.
        assert store.get_effective_properties(node) == []

    def test_unknown_schema_id_skips_default_validation(self, store: LocalStore) -> None:
        store.apply_remote(create_env(name="Owner", hlc=(1, 0)))
        store.apply_remote(class_env("class.create", self.CLS, hlc=(2, 0), name="C"))
        loose = uid("pc2-loose")
        store.apply_remote(
            class_env(
                "class.property.set",
                self.CLS,
                hlc=(3, 0),
                **{"propertySchemaId": loose, "defaultValue": {"anything": True}},
            )
        )
        assert raw_rows(store, "SELECT default_value FROM class_property WHERE property_schema_id = ?", (loose,)) == [
            (json.dumps({"anything": True}),)
        ]


class TestWireNodeFields:
    """M27 unit semantics (mirrors the monorepo store test): absence
    preserves, present-null clears, and the fields ride the row LWW exactly
    like `color`."""

    OTHER = uid("wf-other")
    TARGET = uid("wf-target")

    def test_absence_preserves_present_null_clears_and_the_fields_ride_the_row_lww(
        self, store: LocalStore
    ) -> None:
        store.apply_remote(create_env(name="Page", hlc=(10, 0)))
        assert (
            store.apply_remote(
                update_env(
                    NODE,
                    hlc=(11, 0),
                    coverAssetId=self.OTHER,
                    bannerAssetId=self.OTHER,
                    aliasedNodeId=self.TARGET,
                )
            )
            is True
        )
        # An absent field is not a write: an icon-only update keeps the fields.
        assert store.apply_remote(update_env(NODE, hlc=(12, 0), icon="mdiStar")) is True
        row = store.node(WS_A, NODE)
        assert row.icon == "mdiStar"
        assert (row.cover_asset_id, row.banner_asset_id, row.aliased_node_id) == (
            self.OTHER,
            self.OTHER,
            self.TARGET,
        )
        # A stale-HLC update loses the row LWW race: nothing changes.
        assert store.apply_remote(update_env(NODE, hlc=(9, 0), coverAssetId=None)) is False
        assert store.node(WS_A, NODE).cover_asset_id == self.OTHER
        # The winning clear.
        assert store.apply_remote(update_env(NODE, hlc=(13, 0), coverAssetId=None)) is True
        assert store.node(WS_A, NODE).cover_asset_id is None
        assert raw_rows(store, "SELECT cover_asset_id FROM nodes WHERE id = ?", (NODE,)) == [(None,)]


class TestAliasCycles:
    """M12 write-time alias-cycle validation + the resolve_alias chain
    walker (the extends-DAG precedent; mirrors the monorepo store test)."""

    A = uid("alias-a")
    B = uid("alias-b")
    C = uid("alias-c")
    D = uid("alias-d")

    def _alias(self, node_id: str, target: str | None, hlc: tuple[int, int]) -> RelayEnvelope:
        return update_env(node_id, hlc=hlc, aliasedNodeId=target)

    def test_a_plain_chain_sets_and_resolves_acyclic_repoints_stay_legal(self, store: LocalStore) -> None:
        for node in (self.A, self.B, self.C, self.D):
            store.apply_remote(create_env(node, name="Page", hlc=(1, 0)))
        store.apply_remote(self._alias(self.A, self.B, (2, 0)))
        store.apply_remote(self._alias(self.B, self.C, (3, 0)))
        assert store.node(WS_A, self.A).aliased_node_id == self.B
        assert store.resolve_alias(WS_A, self.A) == self.C
        assert store.resolve_alias(WS_A, self.B) == self.C
        assert store.resolve_alias(WS_A, self.C) == self.C
        # Re-pointing the middle of the chain is fine while it stays acyclic:
        # B → D (D carries no alias) collapses A's chain to D as well.
        store.apply_remote(self._alias(self.B, self.D, (4, 0)))
        assert store.resolve_alias(WS_A, self.A) == self.D
        assert store.resolve_alias(WS_A, self.B) == self.D

    def test_self_alias_the_one_edge_cycle_fails_loud_and_is_never_applied(self, store: LocalStore) -> None:
        store.apply_remote(create_env(self.A, name="Page", hlc=(1, 0)))
        with pytest.raises(CycleError, match="alias cycle"):
            store.apply_remote(self._alias(self.A, self.A, (2, 0)))
        assert store.node(WS_A, self.A).aliased_node_id is None

    def test_an_indirect_cycle_fails_loud_and_nothing_changes(self, store: LocalStore) -> None:
        for node in (self.A, self.B, self.C):
            store.apply_remote(create_env(node, name="Page", hlc=(1, 0)))
        store.apply_remote(self._alias(self.A, self.B, (2, 0)))
        store.apply_remote(self._alias(self.B, self.C, (3, 0)))
        # C → A would close A → B → C → A: rejected, never applied.
        with pytest.raises(CycleError, match="alias cycle"):
            store.apply_remote(self._alias(self.C, self.A, (4, 0)))
        assert store.node(WS_A, self.C).aliased_node_id is None
        assert store.node(WS_A, self.A).aliased_node_id == self.B
        assert store.node(WS_A, self.B).aliased_node_id == self.C
        # Same for a 2-cycle proposal: B → A revisits A's chain back to B.
        with pytest.raises(CycleError, match="alias cycle"):
            store.apply_remote(self._alias(self.B, self.A, (5, 0)))
        assert store.node(WS_A, self.B).aliased_node_id == self.C

    def test_clearing_an_alias_lands_null_and_reopens_the_chain(self, store: LocalStore) -> None:
        for node in (self.A, self.B):
            store.apply_remote(create_env(node, name="Page", hlc=(1, 0)))
        store.apply_remote(self._alias(self.A, self.B, (2, 0)))
        assert store.resolve_alias(WS_A, self.A) == self.B
        # Clearing cannot create a cycle — it never touches the check and lands.
        assert store.apply_remote(self._alias(self.A, None, (3, 0))) is True
        assert store.node(WS_A, self.A).aliased_node_id is None
        assert store.resolve_alias(WS_A, self.A) == self.A
        # With A's alias gone, B → A is acyclic and legal.
        store.apply_remote(self._alias(self.B, self.A, (4, 0)))
        assert store.resolve_alias(WS_A, self.B) == self.A

    def test_a_stale_hlc_alias_write_is_dropped_by_the_row_lww_before_any_check(self, store: LocalStore) -> None:
        for node in (self.A, self.B, self.C):
            store.apply_remote(create_env(node, name="Page", hlc=(1, 0)))
        store.apply_remote(self._alias(self.A, self.B, (2, 0)))
        store.apply_remote(self._alias(self.B, self.C, (3, 0)))
        # Older than both rows — dropped silently (LWW), no throw, no change.
        assert store.apply_remote(self._alias(self.A, self.C, (1, 5))) is False
        assert store.node(WS_A, self.A).aliased_node_id == self.B

    def test_resolve_alias_is_cycle_safe_and_depth_capped(self, store: LocalStore) -> None:
        for node in (self.A, self.B, self.C):
            store.apply_remote(create_env(node, name="Page", hlc=(1, 0)))
        store.apply_remote(self._alias(self.A, self.B, (2, 0)))
        store.apply_remote(self._alias(self.B, self.C, (3, 0)))
        assert store.resolve_alias(WS_A, self.A) == self.C
        # A cycle can only exist if it predates the write-path check (a
        # legacy row, a hand-edited store): close C → A in place and observe
        # the walker's ruling — every member's walk revisits its start and
        # yields the STARTING id unchanged.
        with sqlite3.connect(_db_path(store)) as raw:
            raw.execute("UPDATE nodes SET aliased_node_id = ? WHERE id = ?", (self.A, self.C))
        assert store.resolve_alias(WS_A, self.A) == self.A
        assert store.resolve_alias(WS_A, self.B) == self.B
        assert store.resolve_alias(WS_A, self.C) == self.C
        # Depth cap: a hand-built long chain resolves to the node reached at
        # the cap (best-effort terminal), never loops forever.
        with sqlite3.connect(_db_path(store)) as raw:
            raw.execute("UPDATE nodes SET aliased_node_id = NULL WHERE id IN (?, ?)", (self.A, self.B))
        previous = self.A
        for index in range(40):
            nxt = uid(f"alias-chain-{index:02d}")
            store.apply_remote(create_env(nxt, name="Link", hlc=(10 + index, 0)))
            store.apply_remote(self._alias(previous, nxt, (100 + index, 0)))
            previous = nxt
        assert store.resolve_alias(WS_A, self.A) != self.A


class TestAliasNodesOf:
    """The reverse alias read (the monorepo ``aliasNodesOf`` port): every
    live node whose alias-terminal is the main — direct + chain, self
    excluded, trashed rows skipped, id order."""

    MAIN = uid("ano-main")
    ALIAS = uid("ano-alias")
    CHAIN = uid("ano-chain")
    OTHER = uid("ano-other")

    def _alias(self, node_id: str, target: str | None, hlc: tuple[int, int]) -> RelayEnvelope:
        return update_env(node_id, hlc=hlc, aliasedNodeId=target)

    def test_lists_every_node_whose_terminal_is_the_main_chains_included_empty_when_none(
        self, store: LocalStore
    ) -> None:
        for node in (self.MAIN, self.ALIAS, self.CHAIN, self.OTHER):
            store.apply_remote(create_env(node, name="Page", hlc=(1, 0)))
        assert store.alias_nodes_of(WS_A, self.MAIN) == []
        # A plain alias and a chain member both land in the main's set.
        store.apply_remote(self._alias(self.ALIAS, self.MAIN, (2, 0)))
        store.apply_remote(self._alias(self.CHAIN, self.ALIAS, (3, 0)))
        assert store.alias_nodes_of(WS_A, self.MAIN) == sorted([self.ALIAS, self.CHAIN])
        # CHAIN points at ALIAS directly, so it lists under ALIAS's own set…
        assert store.alias_nodes_of(WS_A, self.ALIAS) == [self.CHAIN]
        # …while nothing points at CHAIN or OTHER.
        assert store.alias_nodes_of(WS_A, self.CHAIN) == []
        assert store.alias_nodes_of(WS_A, self.OTHER) == []

    def test_never_includes_the_main_itself_and_skips_trashed_aliases(self, store: LocalStore) -> None:
        for node in (self.MAIN, self.ALIAS, self.OTHER):
            store.apply_remote(create_env(node, name="Page", hlc=(1, 0)))
        store.apply_remote(self._alias(self.ALIAS, self.MAIN, (2, 0)))
        # A self-alias is rejected at the write path, so the main can only
        # enter its own set through a hand-built cycle — the read excludes
        # the seed id outright.
        assert store.alias_nodes_of(WS_A, self.MAIN) == [self.ALIAS]
        # Trash the alias (soft-delete keeps the row, flips is_active).
        store.apply_remote(make_env("object.delete", {"objectId": self.ALIAS}, hlc=(3, 0)))
        assert store.alias_nodes_of(WS_A, self.MAIN) == []
        # The trash never lists as a terminal either.
        assert store.alias_nodes_of(WS_A, self.ALIAS) == []
        assert store.alias_nodes_of(WS_A, self.OTHER) == []

    def test_the_walk_is_workspace_scoped(self, store: LocalStore) -> None:
        for node in (self.MAIN, self.ALIAS):
            store.apply_remote(create_env(node, name="Page", hlc=(1, 0)))
            store.apply_remote(create_env(node, name="Page", hlc=(1, 0), workspace_id=WS_B))
        store.apply_remote(self._alias(self.ALIAS, self.MAIN, (2, 0)))
        store.apply_remote(
            make_env("object.update", {"objectId": self.ALIAS, "aliasedNodeId": self.MAIN}, hlc=(2, 0), workspace_id=WS_B)
        )
        # Same ids in both workspaces, aliased in each: the recursive member
        # must not cross the workspace boundary on either side.
        assert store.alias_nodes_of(WS_A, self.MAIN) == [self.ALIAS]
        assert store.alias_nodes_of(WS_B, self.MAIN) == [self.ALIAS]
        assert store.alias_nodes_of(WS_A, self.CHAIN) == []


class TestAssetPropertyType:
    """M38 unit semantics (mirrors the monorepo store test): values validate
    as asset-node references — the implicit filter is the asset class;
    node-typed defaults stay unsupported; the retype path drops the explicit
    filter and preserves values + flags."""

    ASSET_CLASS = "00000000-0000-0000-0001-000000000009"
    SCHEMA = uid("asset-schema")
    ATTACHMENTS = "00000000-0000-0000-0000-000000000011"
    PAGE = uid("asset-page")
    ASSET_NODE = uid("asset-node")
    PLAIN_NODE = uid("asset-plain")
    SOURCE_CLASS = "00000000-0000-0000-0001-000000000023"

    def _world(self, store: LocalStore) -> None:
        store.apply_remote(class_env("class.create", self.ASSET_CLASS, hlc=(1, 0), name="Asset"))
        store.apply_remote(create_env(self.PAGE, name="Page", hlc=(2, 0)))
        store.apply_remote(create_env(self.ASSET_NODE, class_ids=(self.ASSET_CLASS,), hlc=(3, 0), name="File"))
        store.apply_remote(create_env(self.PLAIN_NODE, name="Plain", hlc=(4, 0)))

    def _schema_env(self, schema_id: str, hlc: tuple[int, int], **fields: Any) -> RelayEnvelope:
        payload: dict[str, Any] = {"propertySchemaId": schema_id, "name": "Attachment", "type": "asset"}
        payload.update(fields)
        return make_env("propertySchema.create", payload, hlc=hlc)

    def test_values_validate_as_asset_node_references_the_implicit_filter_is_the_asset_class(
        self, store: LocalStore
    ) -> None:
        self._world(store)
        assert store.apply_remote(self._schema_env(self.SCHEMA, (5, 0), multi=True, scope="class")) is True
        assert (
            store.apply_remote(
                class_env(
                    "class.property.set",
                    self.SOURCE_CLASS,
                    hlc=(6, 0),
                    **{"propertySchemaId": self.SCHEMA, "sequence": 0},
                )
            )
            is True
        )
        # A legacy bare-uuid carrier normalizes to {nodeId}.
        assert (
            store.apply_remote(property_set_env(self.PAGE, self.SCHEMA, value=self.ASSET_NODE, hlc=(7, 0)))
            is True
        )
        assert raw_rows(
            store,
            "SELECT value FROM property_value WHERE node_id = ? AND property_schema_id = ?",
            (self.PAGE, self.SCHEMA),
        ) == [(json.dumps({"nodeId": self.ASSET_NODE}),)]
        # A target NOT carrying the asset class fails loud (implicit filter)…
        with pytest.raises(PropertyValueShapeError, match="allowed classes"):
            store.apply_remote(
                property_set_env(self.PAGE, self.SCHEMA, value={"nodeId": self.PLAIN_NODE}, hlc=(8, 0))
            )
        # …as does a nonexistent node…
        with pytest.raises(PropertyValueShapeError, match="does not exist"):
            store.apply_remote(
                property_set_env(self.PAGE, self.SCHEMA, value={"nodeId": uid("asset-ghost")}, hlc=(9, 0))
            )
        # …and a non-reference shape fails the type's shape check.
        with pytest.raises(PropertyValueShapeError):
            store.apply_remote(property_set_env(self.PAGE, self.SCHEMA, value="not-a-ref", hlc=(10, 0)))

    def test_node_typed_defaults_stay_unsupported(self, store: LocalStore) -> None:
        self._world(store)
        assert store.apply_remote(self._schema_env(self.SCHEMA, (5, 0), multi=True, scope="class")) is True
        with pytest.raises(PropertyValueShapeError, match="must be null"):
            store.apply_remote(
                class_env(
                    "class.property.set",
                    self.SOURCE_CLASS,
                    hlc=(6, 0),
                    **{"propertySchemaId": self.SCHEMA, "defaultValue": {"nodeId": self.ASSET_NODE}},
                )
            )

    def test_retype_object_to_asset_drops_the_explicit_filter_and_preserves_values_and_flags(
        self, store: LocalStore
    ) -> None:
        self._world(store)
        # The pre-M38 seeded shape: object-typed with an explicit filter.
        assert (
            store.apply_remote(
                make_env(
                    "propertySchema.create",
                    {
                        "propertySchemaId": self.ATTACHMENTS,
                        "name": "Attachments",
                        "type": "object",
                        "multi": True,
                        "scope": "class",
                        "targetClassFilter": [self.ASSET_CLASS],
                    },
                    hlc=(5, 0),
                )
            )
            is True
        )
        assert (
            store.apply_remote(
                class_env(
                    "class.property.set",
                    self.SOURCE_CLASS,
                    hlc=(6, 0),
                    **{"propertySchemaId": self.ATTACHMENTS, "sequence": 0},
                )
            )
            is True
        )
        assert (
            store.apply_remote(
                property_set_env(self.PAGE, self.ATTACHMENTS, value={"nodeId": self.ASSET_NODE}, hlc=(7, 0))
            )
            is True
        )
        # A user-set render contract the retype must not clobber.
        assert (
            store.apply_remote(
                make_env("propertySchema.update", {"propertySchemaId": self.ATTACHMENTS, "display": "inline"}, hlc=(8, 0))
            )
            is True
        )
        # The M38 migration envelope: same id, type "asset", NO explicit filter.
        assert (
            store.apply_remote(
                self._schema_env(
                    self.ATTACHMENTS,
                    (9, 0),
                    multi=True,
                    scope="class",
                    options=[],
                    display="inline",
                )
            )
            is True
        )
        row = raw_rows(
            store,
            "SELECT type, target_class_filter, display FROM property_schema WHERE id = ?",
            (self.ATTACHMENTS,),
        )
        assert row == [("asset", None, "inline")]
        # Values are shape-compatible ({nodeId} → asset nodes): untouched.
        assert raw_rows(
            store,
            "SELECT value FROM property_value WHERE node_id = ? AND property_schema_id = ?",
            (self.PAGE, self.ATTACHMENTS),
        ) == [(json.dumps({"nodeId": self.ASSET_NODE}),)]
        # And the implicit filter now guards NEW writes.
        with pytest.raises(PropertyValueShapeError, match="allowed classes"):
            store.apply_remote(
                property_set_env(self.PAGE, self.ATTACHMENTS, value={"nodeId": self.PLAIN_NODE}, hlc=(10, 0))
            )


class TestClassCreateConversion:
    """M47 unit semantics beyond the fixture: conversion preserves the
    node's icon/color (absent payload fields never wipe) and the registry
    description survives a re-declaration."""

    def test_conversion_preserves_icon_color_and_description(self, store: LocalStore) -> None:
        store.apply_remote(create_env(NODE, name="Genre", hlc=(1, 0)))
        # object.create carries no appearance fields: the icon/color land
        # through object.update before the conversion.
        assert store.apply_remote(update_env(NODE, hlc=(2, 0), icon="mdiTag", color="purple")) is True
        # A bare payload converts: absent icon/color preserve on the NODE row
        # (the LWW-gated upsert never writes absent fields), the registry
        # adopts the node's title (effective-icon reads go through the node
        # row — the fresh registry row carries no icon/color, exactly like
        # the TS reference).
        assert store.apply_remote(class_env("class.create", NODE, hlc=(3, 0))) is True
        row = store.node(WS_A, NODE)
        assert row is not None
        assert (row.is_class, row.icon, row.color) == (True, "mdiTag", "purple")
        assert raw_rows(store, "SELECT name, icon, color, description, active FROM class WHERE id = ?", (NODE,)) == [
            ("Genre", None, None, None, 1)
        ]
        # A description written after the conversion survives a bare
        # re-declaration (absent fields preserve, they never wipe).
        assert store.apply_remote(class_env("class.update", NODE, hlc=(4, 0), description="kept")) is True
        assert store.apply_remote(class_env("class.create", NODE, hlc=(5, 0))) is True
        assert raw_rows(store, "SELECT name, description, active FROM class WHERE id = ?", (NODE,)) == [
            ("Genre", "kept", 1)
        ]


# --------------------------------------------------------------------- helpers


def _db_path(store: LocalStore) -> Path:
    """Recover the db path for raw assertions (tests only)."""
    return Path(str(store._conn.execute("PRAGMA database_list").fetchone()[2]))  # noqa: SLF001
