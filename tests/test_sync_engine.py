"""Tests for the sync engine against a scripted in-memory fake relay.

The fake subclasses :class:`NoteesClient` (mypy-friendly, no mocking framework)
and overrides the four network methods with in-memory behavior. The engine's
injectable ``sleeper`` records backoff durations so retry schedules can be
asserted without waiting real seconds.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from notees_gtk.core.api import (
    AuthenticationError,
    ForbiddenError,
    NetworkError,
    NoteesClient,
    QuarantinedError,
    RateLimitedError,
    ServerError,
    SnapshotMeta,
)
from notees_gtk.core.protocol.clock import Hlc
from notees_gtk.core.protocol.ids import new_uuid7
from notees_gtk.core.protocol.models import CatchUpPaginatedResponse, RelayEnvelope
from notees_gtk.core.sync import PushResult, SyncEngine
from notees_gtk.data.store import LocalStore

WS = "ws-a"
ACTOR_A = "actor-a"
ACTOR_B = "actor-b"
BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)


def make_env(
    op_type: str,
    payload: dict[str, object],
    *,
    hlc: tuple[int, int] = (1, 0),
    actor: str = ACTOR_A,
    affected: tuple[str, ...] = (),
) -> RelayEnvelope:
    """Build a minimal valid envelope for engine tests."""
    return RelayEnvelope(
        id=new_uuid7(),
        workspace_id=WS,
        actor_id=actor,
        hlc=Hlc(physical=hlc[0], logical=hlc[1]),
        affected_node_ids=list(affected),
        op_type=op_type,
        payload=payload,
        timestamp=BASE_TS,
    )


def create_env(
    node_id: str,
    *,
    parent_id: str | None = None,
    kind: str = "page",
    content: object = None,
    hlc: tuple[int, int] = (1, 0),
    actor: str = ACTOR_A,
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
    return make_env("node.create", payload, hlc=hlc, actor=actor, affected=(node_id,))


def content_env(node_id: str, content: object, *, hlc: tuple[int, int], actor: str = ACTOR_A) -> RelayEnvelope:
    """Build a ``node.updateContent`` envelope."""
    return make_env("node.updateContent", {"nodeId": node_id, "content": content}, hlc=hlc, actor=actor)


def make_server_snapshot(rows: list[dict[str, object]]) -> bytes:
    """Serialize a fake server-derived snapshot DB (more columns than the client cache)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT '',
            class_ids TEXT NOT NULL DEFAULT '[]',
            parent_id TEXT,
            content TEXT,
            icon TEXT,
            color TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT,
            updated_at TEXT,
            created_by TEXT,
            updated_by TEXT
        )
        """
    )
    for row in rows:
        cols = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        conn.execute(f"INSERT INTO nodes ({cols}) VALUES ({placeholders})", tuple(row.values()))
    blob = conn.serialize()
    conn.close()
    return blob


def _no_http(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected HTTP request: {request.url}")


class FakeRelayClient(NoteesClient):
    """Scripted in-memory relay standing in for the Notees server.

    Envelopes land in a seq-ordered operation log; ``batch_script`` queues
    exceptions to raise on subsequent ``submit_batch`` calls (one per call).
    """

    def __init__(self, workspace_id: str) -> None:
        super().__init__("http://relay.fake", transport=httpx.MockTransport(_no_http))
        self.workspace_id = workspace_id
        self._ops: dict[str, tuple[int, RelayEnvelope]] = {}
        self._next_seq = 0
        self.restore_epoch = 0
        self.snapshot_blob: bytes | None = None
        self.snapshot_up_to_seq: int | None = None
        self.batch_script: list[BaseException] = []
        self.batch_calls: list[list[RelayEnvelope]] = []
        self.catch_up_calls: list[int] = []
        self.snapshot_data_calls = 0

    def receive_remote(self, *envelopes: RelayEnvelope) -> None:
        """Simulate another device having pushed envelopes (bypasses the batch script)."""
        for env in envelopes:
            if env.id not in self._ops:
                self._next_seq += 1
                self._ops[env.id] = (self._next_seq, env)

    def submit_batch(self, envelopes: list[RelayEnvelope]) -> list[str]:
        self.batch_calls.append(list(envelopes))
        if self.batch_script:
            raise self.batch_script.pop(0)
        saved: list[str] = []
        for env in envelopes:
            assert env.workspace_id == self.workspace_id
            if env.id in self._ops:
                continue  # duplicate envelope ids are silently ignored server-side
            self._next_seq += 1
            self._ops[env.id] = (self._next_seq, env)
            saved.append(env.id)
        return saved

    def catch_up(self, workspace_id: str, after_seq: int = 0, limit: int = 1000) -> CatchUpPaginatedResponse:
        assert workspace_id == self.workspace_id
        self.catch_up_calls.append(after_seq)
        ordered = sorted((seq, env) for seq, env in self._ops.values() if seq > after_seq)
        page = ordered[:limit]
        return CatchUpPaginatedResponse(
            envelopes=[env for _, env in page],
            next_after_seq=page[-1][0] if page else None,
            has_more=len(ordered) > len(page),
            restore_epoch=self.restore_epoch,
            total_remaining=len(ordered),
        )

    def snapshot_probe(self, workspace_id: str) -> SnapshotMeta:
        assert workspace_id == self.workspace_id
        has_snapshot = self.snapshot_blob is not None
        return SnapshotMeta(
            snapshot_id="snap-1" if has_snapshot else "",
            workspace_id=workspace_id,
            hlc=Hlc(physical=0, logical=0),
            has_snapshot=has_snapshot,
            restore_epoch=self.restore_epoch,
            up_to_seq=self.snapshot_up_to_seq if has_snapshot else None,
        )

    def snapshot_data(self, workspace_id: str) -> bytes:
        assert workspace_id == self.workspace_id
        self.snapshot_data_calls += 1
        if self.snapshot_blob is None:
            raise AssertionError("no snapshot staged")
        return self.snapshot_blob


class RecordingSleeper:
    """Injectable engine sleeper that records durations instead of sleeping."""

    def __init__(self) -> None:
        self.durations: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.durations.append(seconds)


def make_engine(
    relay: FakeRelayClient,
    store: LocalStore,
    *,
    actor: str = ACTOR_A,
    sleeper: RecordingSleeper | None = None,
    page_size: int = 1000,
) -> SyncEngine:
    return SyncEngine(
        relay,
        store,
        actor_id=actor,
        workspace_id=WS,
        sleeper=sleeper if sleeper is not None else RecordingSleeper(),
        page_size=page_size,
    )


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(tmp_path / "store.db")


class TestPush:
    def test_push_drains_outbox_in_chunks_of_100(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        for i in range(150):
            store.enqueue(create_env(f"n{i}"))
        result = make_engine(relay, store).push()
        assert result == PushResult(sent=150, quarantined=0)
        assert [len(call) for call in relay.batch_calls] == [100, 50]
        assert store.pending_outbox(WS) == []

    def test_pushed_chunk_is_applied_to_local_cache(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        store.enqueue(create_env("n1", content="hello", hlc=(2, 0)))
        result = make_engine(relay, store).push()
        assert result.sent == 1
        row = store.node(WS, "n1")
        assert row is not None and row.content == "hello"

    def test_whole_chunk_ack_despite_saved_ids_omitting_duplicates(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        env = create_env("n1")
        store.enqueue(env)
        relay._ops[env.id] = (1, env)  # server already stored it → saved_ids omits it
        result = make_engine(relay, store).push()
        assert result.sent == 1
        assert store.pending_outbox(WS) == []

    def test_quarantined_chunk_is_parked_and_never_retried(self, store: LocalStore, tmp_path: Path) -> None:
        relay = FakeRelayClient(WS)
        relay.batch_script = [QuarantinedError("unknown op type", status=422)]
        store.enqueue(create_env("n1"))
        engine = make_engine(relay, store)
        assert engine.push() == PushResult(sent=0, quarantined=1)
        assert engine.push() == PushResult(sent=0, quarantined=0)
        assert len(relay.batch_calls) == 1  # quarantined chunk is not retried
        store.close()
        with sqlite3.connect(tmp_path / "store.db") as raw:
            row = raw.execute("SELECT state, quarantine_reason FROM relay_outbox").fetchone()
        assert row == ("quarantined", "unknown op type")

    def test_quarantined_chunk_does_not_stop_later_chunks(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.batch_script = [QuarantinedError("bad chunk", status=422)]
        for i in range(101):
            store.enqueue(create_env(f"n{i}"))
        result = make_engine(relay, store).push()
        assert result == PushResult(sent=1, quarantined=100)
        assert [len(call) for call in relay.batch_calls] == [100, 1]

    @pytest.mark.parametrize("failure", [AuthenticationError("expired"), ForbiddenError("no access")])
    def test_auth_failures_abort_push_and_keep_outbox(
        self, store: LocalStore, failure: BaseException
    ) -> None:
        relay = FakeRelayClient(WS)
        relay.batch_script = [failure]
        store.enqueue(create_env("n1"))
        store.enqueue(create_env("n2"))
        engine = make_engine(relay, store)
        with pytest.raises(type(failure)):
            engine.push()
        assert len(relay.batch_calls) == 1  # no retries on auth failure
        assert len(store.pending_outbox(WS)) == 2

    def test_network_errors_follow_backoff_schedule_then_give_up(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.batch_script = [NetworkError("boom")] * 5
        sleeper = RecordingSleeper()
        store.enqueue(create_env("n1"))
        result = make_engine(relay, store, sleeper=sleeper).push()
        assert result == PushResult(sent=0, quarantined=0)
        assert sleeper.durations == [5, 15, 60, 300]
        assert len(relay.batch_calls) == 5
        assert len(store.pending_outbox(WS)) == 1  # outbox left intact for next round

    def test_retry_succeeds_after_transient_failures(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.batch_script = [NetworkError("x"), ServerError("y", status=500)]
        sleeper = RecordingSleeper()
        store.enqueue(create_env("n1"))
        result = make_engine(relay, store, sleeper=sleeper).push()
        assert result == PushResult(sent=1, quarantined=0)
        assert sleeper.durations == [5, 15]
        assert store.pending_outbox(WS) == []

    def test_rate_limited_honors_retry_after_then_schedule(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.batch_script = [RateLimitedError("slow", retry_after=2.5), RateLimitedError("slow")]
        sleeper = RecordingSleeper()
        store.enqueue(create_env("n1"))
        result = make_engine(relay, store, sleeper=sleeper).push()
        assert result.sent == 1
        assert sleeper.durations == [2.5, 15]

    def test_auth_error_mid_retry_aborts_immediately(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.batch_script = [NetworkError("x"), AuthenticationError("expired")]
        sleeper = RecordingSleeper()
        store.enqueue(create_env("n1"))
        engine = make_engine(relay, store, sleeper=sleeper)
        with pytest.raises(AuthenticationError):
            engine.push()
        assert sleeper.durations == [5]


class TestPull:
    def test_pull_pages_until_final_next_after_seq_is_adopted(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.receive_remote(*(create_env(f"n{i}") for i in range(250)))
        result = make_engine(relay, store, page_size=100).pull()
        assert result.applied == 250
        assert result.cursor == 250
        assert relay.catch_up_calls == [0, 100, 200]
        assert store.cursor(WS) == 250

    def test_pull_continues_from_persisted_cursor(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.receive_remote(create_env("n1"), create_env("n2"))
        store.set_cursor(WS, 1)
        result = make_engine(relay, store).pull()
        assert result.applied == 1
        assert relay.catch_up_calls == [1]
        assert result.cursor == 2

    def test_pull_never_double_applies_overlap_with_live_apply(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        env = create_env("n1", content="hi", hlc=(2, 0))
        relay.receive_remote(env)
        assert store.apply_remote(env) is True  # live frame arrives first
        result = make_engine(relay, store).pull()
        assert result.applied == 0  # catch-up overlap deduped by op id
        assert len(store.nodes(WS)) == 1
        assert store.node(WS, "n1").content == "hi"

    def test_pull_applies_newest_content_and_skips_stale_lww(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        # Server log order (ascending seq) but the HLC clocks are out of order.
        relay.receive_remote(create_env("n1", content="first", hlc=(1, 0)))
        relay.receive_remote(content_env("n1", "newer", hlc=(20, 0)))
        relay.receive_remote(content_env("n1", "stale", hlc=(10, 0)))
        result = make_engine(relay, store).pull()
        assert result.applied == 2  # create + newer content; stale skipped
        row = store.node(WS, "n1")
        assert row is not None and row.content == "newer"

    def test_pull_empty_page_keeps_cursor(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        store.set_cursor(WS, 9)
        result = make_engine(relay, store).pull()
        assert result.applied == 0
        assert result.cursor == 9
        assert store.cursor(WS) == 9


class TestSyncConvergence:
    def test_full_sync_loop_converges_two_clients(self, tmp_path: Path) -> None:
        relay = FakeRelayClient(WS)
        store_a = LocalStore(tmp_path / "a.db")
        store_b = LocalStore(tmp_path / "b.db")
        engine_a = make_engine(relay, store_a, actor=ACTOR_A)
        engine_b = make_engine(relay, store_b, actor=ACTOR_B)

        store_a.enqueue(create_env("root", kind="page", icon="📄"))
        store_a.enqueue(create_env("child", kind="block", parent_id="root"))
        store_a.enqueue(content_env("root", '[{"type":"text","text":"Hello"}]', hlc=(5, 0)))
        assert engine_a.push() == PushResult(sent=3, quarantined=0)

        pulled = engine_b.pull()
        assert pulled.applied == 3
        assert pulled.cursor == 3
        assert store_b.node(WS, "root") is not None and store_b.node(WS, "root").icon == "📄"
        assert store_b.node(WS, "child") is not None and store_b.node(WS, "child").parent_id == "root"
        assert store_b.node(WS, "root").content == '[{"type":"text","text":"Hello"}]'

        # The catch-up echo of A's own pushed ops must not double-apply anywhere.
        assert engine_a.pull().applied == 0
        assert engine_b.pull().applied == 0

        # B edits; both sides converge after one sync round each.
        store_b.enqueue(content_env("root", "v2", hlc=(9, 0), actor=ACTOR_B))
        engine_b.sync()
        engine_a.sync()
        row_a = store_a.node(WS, "root")
        row_b = store_b.node(WS, "root")
        assert row_a is not None and row_a.content == "v2"
        assert row_a == row_b


class TestSync:
    def test_sync_pushes_then_pulls(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.receive_remote(create_env("theirs", actor=ACTOR_B))
        relay.receive_remote(content_env("theirs", "remote text", hlc=(3, 0), actor=ACTOR_B))
        store.enqueue(create_env("mine"))
        make_engine(relay, store).sync()
        assert store.pending_outbox(WS) == []
        assert store.node(WS, "mine") is not None
        row = store.node(WS, "theirs")
        assert row is not None and row.content == "remote text"
        assert store.cursor(WS) == 3


class TestSnapshotRestore:
    def test_restore_epoch_change_wipes_cursor_and_cache(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.restore_epoch = 3
        store.apply_remote(create_env("old"))
        store.set_cursor(WS, 12)
        store.set_restore_epoch(WS, 0)
        engine = make_engine(relay, store)
        assert engine.maybe_restore_snapshot() is False  # no snapshot staged
        assert store.cursor(WS) == 0
        assert store.node(WS, "old") is None
        assert store.stored_restore_epoch(WS) == 3

    def test_newer_snapshot_restores_nodes_and_sets_cursor(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.snapshot_blob = make_server_snapshot(
            [
                {"id": "n1", "workspace_id": WS, "kind": "page", "content": "one", "updated_at": None},
                {"id": "n2", "workspace_id": WS, "kind": "block", "content": "two", "updated_at": None},
            ]
        )
        relay.snapshot_up_to_seq = 42
        engine = make_engine(relay, store)
        assert engine.maybe_restore_snapshot() is True
        rows = store.nodes(WS, include_archived=True)
        assert sorted(row.id for row in rows) == ["n1", "n2"]
        assert store.node(WS, "n1").content == "one"
        assert store.cursor(WS) == 42
        # Second call: snapshot no longer newer than the cursor → no re-download.
        assert engine.maybe_restore_snapshot() is False
        assert relay.snapshot_data_calls == 1

    def test_epoch_change_plus_snapshot_wipes_then_restores(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.restore_epoch = 7
        relay.snapshot_blob = make_server_snapshot(
            [{"id": "fresh", "workspace_id": WS, "kind": "page", "content": "restored", "updated_at": None}]
        )
        relay.snapshot_up_to_seq = 42
        store.apply_remote(create_env("stale-local"))
        store.set_cursor(WS, 100)
        engine = make_engine(relay, store)
        assert engine.maybe_restore_snapshot() is True
        assert store.node(WS, "stale-local") is None
        row = store.node(WS, "fresh")
        assert row is not None and row.content == "restored"
        assert store.cursor(WS) == 42
        assert store.stored_restore_epoch(WS) == 7

    def test_corrupt_snapshot_blob_returns_false_and_keeps_state(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.snapshot_blob = b"this is not a sqlite database"
        relay.snapshot_up_to_seq = 9
        store.apply_remote(create_env("local", content="kept", hlc=(2, 0)))
        store.set_cursor(WS, 5)
        engine = make_engine(relay, store)
        assert engine.maybe_restore_snapshot() is False
        row = store.node(WS, "local")
        assert row is not None and row.content == "kept"
        assert store.cursor(WS) == 5

    def test_snapshot_older_than_cursor_is_skipped(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.snapshot_blob = make_server_snapshot(
            [{"id": "n1", "workspace_id": WS, "kind": "page", "updated_at": None}]
        )
        relay.snapshot_up_to_seq = 42
        store.set_cursor(WS, 100)
        engine = make_engine(relay, store)
        assert engine.maybe_restore_snapshot() is False
        assert relay.snapshot_data_calls == 0
