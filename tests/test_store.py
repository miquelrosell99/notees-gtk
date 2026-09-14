"""Tests for the local SQLite store: outbox, op-id dedupe, node cache, migrations, snapshots."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import make_server_snapshot
from notees_gtk.core.protocol.clock import Hlc
from notees_gtk.core.protocol.ids import new_uuid7
from notees_gtk.core.protocol.models import RelayEnvelope
from notees_gtk.data.store import LocalStore, NodeRow

WS_A = "ws-a"
WS_B = "ws-b"
ACTOR = "actor-1"
NODE = "node-1"
BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)

#: The v2 migration adds this column to relay_outbox via a guarded ALTER.
V2_COLUMN = "attempts"


def make_env(
    op_type: str,
    payload: dict[str, object],
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
        workspace_id=workspace_id,
        actor_id=actor_id,
        hlc=Hlc(physical=hlc[0], logical=hlc[1]),
        affected_node_ids=list(affected),
        op_type=op_type,
        payload=payload,
        timestamp=BASE_TS,
    )


def create_env(
    node_id: str = NODE,
    *,
    parent_id: str | None = None,
    kind: str = "page",
    content: object = None,
    hlc: tuple[int, int] = (1, 0),
    workspace_id: str = WS_A,
    env_id: str | None = None,
    **extra: object,
) -> RelayEnvelope:
    """Build a ``node.create`` envelope; pass ``content`` to include initial content."""
    payload: dict[str, object] = {
        "nodeId": node_id,
        "kind": kind,
        "parentId": parent_id,
        "classIds": [],
        "icon": None,
        "color": None,
    }
    payload.update(extra)
    if content is not None:
        payload["content"] = content
    return make_env(
        "node.create",
        payload,
        hlc=hlc,
        env_id=env_id,
        workspace_id=workspace_id,
        affected=(node_id,),
    )


def content_env(node_id: str, content: object, *, hlc: tuple[int, int]) -> RelayEnvelope:
    """Build a ``node.updateContent`` envelope (``content`` may be absent/None/AST/string)."""
    payload: dict[str, object] = {"nodeId": node_id}
    if content is not None:
        payload["content"] = content
    return make_env("node.updateContent", payload, hlc=hlc, affected=(node_id,))


@pytest.fixture
def store(tmp_path: Path) -> Iterator[LocalStore]:
    instance = LocalStore(tmp_path / "store.db")
    yield instance
    instance.close()


class TestMigrations:
    def test_fresh_db_creates_every_table_at_latest_version(self, store: LocalStore, tmp_path: Path) -> None:
        with sqlite3.connect(tmp_path / "store.db") as raw:
            tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            version = raw.execute("PRAGMA user_version").fetchone()[0]
        assert {"relay_outbox", "relay_operations", "sync_watermark", "nodes", "node_content_hlc"} <= tables
        assert version == 2

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
        instance.set_cursor(WS_A, 7)
        instance.close()
        # Simulate an interrupted/future downgrade: the next open must re-run every
        # migration step without "duplicate column"/"table already exists" errors.
        with sqlite3.connect(path) as raw:
            raw.execute("PRAGMA user_version = 0")
        reopened = LocalStore(path)
        assert reopened.cursor(WS_A) == 7
        assert len(reopened.pending_outbox(WS_A)) == 1
        reopened.close()

    def test_upgrade_from_v1_shape_adds_missing_column(self, tmp_path: Path) -> None:
        path = tmp_path / "s.db"
        instance = LocalStore(path)
        instance.enqueue(create_env())
        instance.close()
        # Simulate a v1-era database: same tables, but predating the v2 column.
        with sqlite3.connect(path) as raw:
            raw.execute(f"ALTER TABLE relay_outbox DROP COLUMN {V2_COLUMN}")
            raw.execute("PRAGMA user_version = 1")
        upgraded = LocalStore(path)
        with sqlite3.connect(path) as raw:
            columns = {row[1] for row in raw.execute("PRAGMA table_info(relay_outbox)")}
        assert V2_COLUMN in columns
        assert len(upgraded.pending_outbox(WS_A)) == 1
        upgraded.close()


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

    def test_enqueue_skips_update_content_without_content(self, store: LocalStore) -> None:
        store.enqueue(make_env("node.updateContent", {"nodeId": NODE}))
        store.enqueue(content_env(NODE, None, hlc=(2, 0)))
        assert store.pending_outbox(WS_A) == []


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
        store.apply_remote(create_env(hlc=(2, 0), content="hello"))
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
            assert raw.execute("SELECT COUNT(*) FROM node_content_hlc").fetchone()[0] == 0

    def test_wipe_leaves_other_workspaces_untouched(self, store: LocalStore) -> None:
        store.apply_remote(create_env())
        store.apply_remote(create_env(workspace_id=WS_B, env_id=new_uuid7()))
        store.wipe(WS_A)
        remaining = store.node(WS_B, NODE)
        assert remaining is not None


class TestApplyRemote:
    def test_dedupe_returns_false_on_second_apply(self, store: LocalStore) -> None:
        env = create_env()
        assert store.apply_remote(env) is True
        assert store.apply_remote(env) is False

    def test_unknown_op_type_is_logged_and_skipped(self, store: LocalStore, caplog: pytest.LogCaptureFixture) -> None:
        env = make_env("class.create", {"classId": "c1", "name": "C"})
        assert store.apply_remote(env) is False
        assert store.node(WS_A, "c1") is None
        assert any("class.create" in record.getMessage() for record in caplog.records)

    def test_node_create_inserts_node_row(self, store: LocalStore) -> None:
        store.apply_remote(create_env(parent_id=None, kind="page", icon="📄", color="red"))
        row = store.node(WS_A, NODE)
        assert row == NodeRow(
            id=NODE,
            workspace_id=WS_A,
            parent_id=None,
            node_type="page",
            name="",
            icon="📄",
            color="red",
            archived=False,
            content=None,
        )

    def test_node_create_reapply_upserts_without_duplicate(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(parent_id=None))
        # Simulate a dedupe gap: the op id record disappears (e.g. restored backup),
        # and the same create lands again — it must upsert, not duplicate.
        store.close()
        with sqlite3.connect(tmp_path / "store.db") as raw:
            raw.execute("DELETE FROM relay_operations")
        reopened = LocalStore(tmp_path / "store.db")
        assert reopened.apply_remote(create_env(parent_id="parent-9")) is True
        rows = reopened.nodes(WS_A)
        assert len(rows) == 1
        assert rows[0].parent_id == "parent-9"
        reopened.close()

    def test_node_create_content_is_gated_by_lww(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(content="v10", hlc=(10, 0)))
        assert store.node(WS_A, NODE).content == "v10"
        store.close()
        with sqlite3.connect(tmp_path / "store.db") as raw:
            raw.execute("DELETE FROM relay_operations")
        reopened = LocalStore(tmp_path / "store.db")
        # Stale re-application keeps the newer content; newer wins afterwards.
        reopened.apply_remote(create_env(content="stale", hlc=(5, 0)))
        assert reopened.node(WS_A, NODE).content == "v10"
        reopened.apply_remote(create_env(content="v11", hlc=(11, 0)))
        assert reopened.node(WS_A, NODE).content == "v11"
        reopened.close()

    def test_node_delete_removes_row_and_content_hlc(self, store: LocalStore, tmp_path: Path) -> None:
        store.apply_remote(create_env(content="hello", hlc=(2, 0)))
        store.apply_remote(make_env("node.delete", {"nodeId": NODE}, hlc=(3, 0), affected=(NODE,)))
        assert store.node(WS_A, NODE) is None
        store.close()
        with sqlite3.connect(tmp_path / "store.db") as raw:
            assert raw.execute("SELECT COUNT(*) FROM node_content_hlc").fetchone()[0] == 0

    def test_node_move_updates_parent(self, store: LocalStore) -> None:
        store.apply_remote(create_env(parent_id=None))
        store.apply_remote(make_env("node.move", {"nodeId": NODE, "newParentId": "p9"}, hlc=(2, 0), affected=(NODE,)))
        assert store.node(WS_A, NODE).parent_id == "p9"

    def test_update_icon_and_color(self, store: LocalStore) -> None:
        store.apply_remote(create_env(icon="📄", color="red"))
        store.apply_remote(make_env("node.updateIcon", {"nodeId": NODE, "icon": None}, hlc=(2, 0)))
        store.apply_remote(make_env("node.updateColor", {"nodeId": NODE, "color": "blue"}, hlc=(3, 0)))
        row = store.node(WS_A, NODE)
        assert row.icon is None
        assert row.color == "blue"

    def test_archive_and_restore_toggle_flag(self, store: LocalStore) -> None:
        store.apply_remote(create_env())
        store.apply_remote(make_env("node.archive", {"nodeId": NODE}, hlc=(2, 0)))
        assert store.node(WS_A, NODE).archived is True
        assert store.nodes(WS_A) == []
        assert [row.id for row in store.nodes(WS_A, include_archived=True)] == [NODE]
        store.apply_remote(make_env("node.restore", {"nodeId": NODE}, hlc=(3, 0)))
        assert store.node(WS_A, NODE).archived is False

    def test_update_content_string_stored_verbatim(self, store: LocalStore) -> None:
        store.apply_remote(create_env())
        mirror = '[{"type":"text","text":"hi"}]'
        assert store.apply_remote(content_env(NODE, mirror, hlc=(2, 0))) is True
        assert store.node(WS_A, NODE).content == mirror

    def test_update_content_ast_list_serialized_to_json(self, store: LocalStore) -> None:
        store.apply_remote(create_env())
        ast: list[dict[str, object]] = [{"type": "text", "text": "hi"}]
        assert store.apply_remote(content_env(NODE, ast, hlc=(2, 0))) is True
        assert json.loads(store.node(WS_A, NODE).content or "") == ast

    def test_update_content_missing_or_null_content_skipped(self, store: LocalStore) -> None:
        store.apply_remote(create_env(content="orig", hlc=(2, 0)))
        assert store.apply_remote(content_env(NODE, None, hlc=(3, 0))) is False
        assert store.apply_remote(content_env(NODE, None, hlc=(4, 0))) is False
        assert store.node(WS_A, NODE).content == "orig"

    def test_update_content_lww_skips_stale_and_equal(self, store: LocalStore) -> None:
        store.apply_remote(create_env(content="v10", hlc=(10, 0)))
        assert store.apply_remote(content_env(NODE, "stale", hlc=(5, 0))) is False
        assert store.apply_remote(content_env(NODE, "equal", hlc=(10, 0))) is False
        assert store.node(WS_A, NODE).content == "v10"
        assert store.apply_remote(content_env(NODE, "v11", hlc=(11, 0))) is True
        assert store.node(WS_A, NODE).content == "v11"


class TestNodesQuery:
    def test_nodes_filters_by_parent(self, store: LocalStore) -> None:
        store.apply_remote(create_env("n1", parent_id=None))
        store.apply_remote(create_env("n2", parent_id="n1"))
        assert sorted(row.id for row in store.nodes(WS_A)) == ["n1", "n2"]
        assert [row.id for row in store.nodes(WS_A, parent_id="n1")] == ["n2"]

    def test_node_returns_none_for_missing(self, store: LocalStore) -> None:
        assert store.node(WS_A, "missing") is None


class TestSnapshotRestore:
    def test_restore_maps_real_server_schema(self, store: LocalStore) -> None:
        mirror = '[{"type":"text","text":"snap"}]'
        blob = make_server_snapshot(
            [
                {
                    "id": "n1",
                    "workspace_id": WS_A,
                    "kind": "page",
                    "parent_id": None,
                    "content": mirror,
                    "icon": "📄",
                    "color": None,
                    "active": 0,
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "created_by": ACTOR,
                    "hlc_physical": 5,
                    "hlc_logical": 2,
                },
                {"id": "n2", "workspace_id": WS_A, "kind": "block", "content": "[]"},
            ]
        )
        assert store.restore_snapshot(blob, workspace_id=WS_A) is True
        rows = {row.id: row for row in store.nodes(WS_A, include_archived=True)}
        # Real-schema mapping: kind → node_type, active → archived INVERTED,
        # verbatim columns copied, client-only columns keep their defaults,
        # server-only columns (class_ids/created_by/…) are dropped.
        assert rows["n1"].node_type == "page"
        assert rows["n1"].archived is True  # active=0
        assert rows["n1"].content == mirror
        assert rows["n1"].icon == "📄"
        assert rows["n1"].parent_id is None
        assert rows["n1"].name == ""
        assert rows["n2"].node_type == "block"
        assert rows["n2"].archived is False  # active=1 (server default)
        # Seeded LWW baseline (hlc 5,2): lower/equal content HLCs must lose.
        assert store.apply_remote(content_env("n1", "stale echo", hlc=(4, 9))) is False
        assert store.node(WS_A, "n1").content == mirror
        assert store.apply_remote(content_env("n1", "newer", hlc=(5, 3))) is True
        assert store.node(WS_A, "n1").content == "newer"

    def test_restore_filters_by_workspace(self, store: LocalStore) -> None:
        blob = make_server_snapshot(
            [
                {"id": "n1", "workspace_id": WS_A, "kind": "page"},
                {"id": "n2", "workspace_id": WS_B, "kind": "page"},
            ]
        )
        assert store.restore_snapshot(blob, workspace_id=WS_A) is True
        assert [row.id for row in store.nodes(WS_A, include_archived=True)] == ["n1"]

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
        conn.execute("CREATE TABLE nodes (id TEXT, workspace_id TEXT, kind TEXT)")
        conn.execute("INSERT INTO nodes VALUES ('x', ?, 'page')", (WS_A,))
        blob = conn.serialize()
        conn.close()
        assert store.restore_snapshot(blob, workspace_id=WS_A) is False

    def test_restore_without_hlc_columns_skips_lww_baseline(self, store: LocalStore) -> None:
        # Pre-HLC server snapshots lack hlc_physical/hlc_logical: restore must
        # still work and simply not seed a content-HLC baseline.
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE node (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('page', 'block')),
                content TEXT NOT NULL DEFAULT '[]',
                active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            "INSERT INTO node (id, workspace_id, kind, content) VALUES ('legacy', ?, 'page', 'old')",
            (WS_A,),
        )
        blob = conn.serialize()
        conn.close()
        assert store.restore_snapshot(blob, workspace_id=WS_A) is True
        row = store.node(WS_A, "legacy")
        assert row is not None and row.node_type == "page" and row.content == "old"
        # No baseline → any later updateContent wins.
        assert store.apply_remote(content_env("legacy", "new", hlc=(1, 0))) is True
        assert store.node(WS_A, "legacy").content == "new"

    def test_restore_corrupt_blob_returns_false_and_leaves_state_untouched(self, store: LocalStore) -> None:
        store.apply_remote(create_env(content="local", hlc=(2, 0)))
        store.set_cursor(WS_A, 17)
        assert store.restore_snapshot(b"this is not a sqlite database", workspace_id=WS_A) is False
        assert store.node(WS_A, NODE).content == "local"
        assert store.cursor(WS_A) == 17
        # The store is still fully usable after a failed restore.
        assert store.apply_remote(content_env(NODE, "still works", hlc=(3, 0))) is True
