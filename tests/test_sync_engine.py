"""Tests for the sync engine against a scripted in-memory fake relay.

The fake subclasses :class:`NoteesClient` (mypy-friendly, no mocking framework)
and overrides the four network methods with in-memory behavior. The engine's
injectable ``sleeper`` records backoff durations so retry schedules can be
asserted without waiting real seconds.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
from conftest import WsRelayStub, make_server_snapshot, wait_until

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
from notees_gtk.core.protocol.clock import Clock, Hlc
from notees_gtk.core.protocol.ids import new_uuid7
from notees_gtk.core.protocol.models import (
    PROTOCOL_VERSION,
    CatchUpPaginatedResponse,
    RelayEnvelope,
    new_envelope,
)
from notees_gtk.core.sync import PushResult, SyncEngine
from notees_gtk.data.store import LocalStore

WS = "ws-a"
ACTOR_A = "actor-a"
ACTOR_B = "actor-b"
BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)


def uid(label: str) -> str:
    """Deterministic test UUID — the strict payload validator requires UUID shapes."""
    from uuid import NAMESPACE_URL, uuid5

    return str(uuid5(NAMESPACE_URL, f"notees-gtk/test/{label}"))


def make_env(
    op_type: str,
    payload: dict[str, object],
    *,
    hlc: tuple[int, int] = (1, 0),
    actor: str = ACTOR_A,
    affected: tuple[str, ...] = (),
) -> RelayEnvelope:
    """Build a minimal valid v2 envelope for engine tests."""
    return RelayEnvelope(
        id=new_uuid7(),
        protocolVersion=PROTOCOL_VERSION,
        workspaceId=WS,
        actorId=actor,
        deviceId="engine-test-device",
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
    present_as_main: bool | None = None,
    content: object = None,
    hlc: tuple[int, int] = (1, 0),
    actor: str = ACTOR_A,
    **extra: object,
) -> RelayEnvelope:
    """Build an ``object.create`` envelope; ``content`` may be a token list or a bare string."""
    payload: dict[str, object] = {"objectId": node_id, "parentId": parent_id}
    if present_as_main is not None:
        payload["presentAsMain"] = present_as_main
    payload.update(extra)
    if content is not None:
        payload["contentAst"] = [{"type": "text", "text": content}] if isinstance(content, str) else content
    return make_env("object.create", payload, hlc=hlc, actor=actor, affected=(node_id,))


def content_env(node_id: str, content: object, *, hlc: tuple[int, int], actor: str = ACTOR_A) -> RelayEnvelope:
    """Build an ``object.update`` content envelope (token list or bare string)."""
    tokens = [{"type": "text", "text": content}] if isinstance(content, str) else content
    return make_env(
        "object.update", {"objectId": node_id, "contentAst": tokens}, hlc=hlc, actor=actor, affected=(node_id,)
    )


def _no_http(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected HTTP request: {request.url}")


class FakeRelayClient(NoteesClient):
    """Scripted in-memory relay standing in for the Notees server.

    Envelopes land in a seq-ordered operation log; ``batch_script`` queues
    exceptions to raise on subsequent ``submit_batch`` calls (one per call).
    """

    def __init__(self, workspace_id: str, *, base_url: str = "http://relay.fake", api_key: str | None = None) -> None:
        super().__init__(base_url, api_key=api_key, transport=httpx.MockTransport(_no_http))
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
    clock: Clock | None = None,
    page_size: int = 1000,
) -> SyncEngine:
    return SyncEngine(
        relay,
        store,
        actor_id=actor,
        workspace_id=WS,
        sleeper=sleeper if sleeper is not None else RecordingSleeper(),
        clock=clock,
        page_size=page_size,
    )


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(tmp_path / "store.db")


class TestPush:
    def test_push_drains_outbox_in_chunks_of_100(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        for i in range(150):
            store.enqueue(create_env(str(uuid5(NAMESPACE_URL, f"notees-gtk/test/n{i}"))))
        result = make_engine(relay, store).push()
        assert result == PushResult(sent=150, quarantined=0)
        assert [len(call) for call in relay.batch_calls] == [100, 50]
        assert store.pending_outbox(WS) == []

    def test_pushed_chunk_is_applied_to_local_cache(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        store.enqueue(create_env(uid("n1"), content="hello", hlc=(2, 0)))
        result = make_engine(relay, store).push()
        assert result.sent == 1
        row = store.node(WS, uid("n1"))
        assert row is not None and row.content_plain == "hello"

    def test_whole_chunk_ack_despite_saved_ids_omitting_duplicates(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        env = create_env(uid("n1"))
        store.enqueue(env)
        relay._ops[env.id] = (1, env)  # server already stored it → saved_ids omits it
        result = make_engine(relay, store).push()
        assert result.sent == 1
        assert store.pending_outbox(WS) == []

    def test_quarantined_chunk_is_parked_and_never_retried(self, store: LocalStore, tmp_path: Path) -> None:
        relay = FakeRelayClient(WS)
        relay.batch_script = [QuarantinedError("unknown op type", status=422)]
        store.enqueue(create_env(uid("n1")))
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
            store.enqueue(create_env(str(uuid5(NAMESPACE_URL, f"notees-gtk/test/n{i}"))))
        result = make_engine(relay, store).push()
        assert result == PushResult(sent=1, quarantined=100)
        assert [len(call) for call in relay.batch_calls] == [100, 1]

    @pytest.mark.parametrize("failure", [AuthenticationError("expired"), ForbiddenError("no access")])
    def test_auth_failures_abort_push_and_keep_outbox(self, store: LocalStore, failure: BaseException) -> None:
        relay = FakeRelayClient(WS)
        relay.batch_script = [failure]
        store.enqueue(create_env(uid("n1")))
        store.enqueue(create_env(uid("n2")))
        engine = make_engine(relay, store)
        with pytest.raises(type(failure)):
            engine.push()
        assert len(relay.batch_calls) == 1  # no retries on auth failure
        assert len(store.pending_outbox(WS)) == 2

    def test_network_errors_follow_backoff_schedule_then_give_up(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.batch_script = [NetworkError("boom")] * 5
        sleeper = RecordingSleeper()
        store.enqueue(create_env(uid("n1")))
        result = make_engine(relay, store, sleeper=sleeper).push()
        assert result == PushResult(sent=0, quarantined=0)
        assert sleeper.durations == [5, 15, 60, 300]
        assert len(relay.batch_calls) == 5
        assert len(store.pending_outbox(WS)) == 1  # outbox left intact for next round

    def test_retry_succeeds_after_transient_failures(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.batch_script = [NetworkError("x"), ServerError("y", status=500)]
        sleeper = RecordingSleeper()
        store.enqueue(create_env(uid("n1")))
        result = make_engine(relay, store, sleeper=sleeper).push()
        assert result == PushResult(sent=1, quarantined=0)
        assert sleeper.durations == [5, 15]
        assert store.pending_outbox(WS) == []

    def test_rate_limited_honors_retry_after_then_schedule(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.batch_script = [RateLimitedError("slow", retry_after=2.5), RateLimitedError("slow")]
        sleeper = RecordingSleeper()
        store.enqueue(create_env(uid("n1")))
        result = make_engine(relay, store, sleeper=sleeper).push()
        assert result.sent == 1
        assert sleeper.durations == [2.5, 15]

    def test_auth_error_mid_retry_aborts_immediately(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.batch_script = [NetworkError("x"), AuthenticationError("expired")]
        sleeper = RecordingSleeper()
        store.enqueue(create_env(uid("n1")))
        engine = make_engine(relay, store, sleeper=sleeper)
        with pytest.raises(AuthenticationError):
            engine.push()
        assert sleeper.durations == [5]


class TestPull:
    def test_pull_pages_until_final_next_after_seq_is_adopted(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.receive_remote(*(create_env(str(uuid5(NAMESPACE_URL, f"notees-gtk/test/n{i}"))) for i in range(250)))
        result = make_engine(relay, store, page_size=100).pull()
        assert result.applied == 250
        assert result.cursor == 250
        assert relay.catch_up_calls == [0, 100, 200]
        assert store.cursor(WS) == 250

    def test_pull_continues_from_persisted_cursor(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.receive_remote(create_env(uid("n1")), create_env(uid("n2")))
        store.set_cursor(WS, 1)
        result = make_engine(relay, store).pull()
        assert result.applied == 1
        assert relay.catch_up_calls == [1]
        assert result.cursor == 2

    def test_pull_never_double_applies_overlap_with_live_apply(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        env = create_env(uid("n1"), content="hi", hlc=(2, 0))
        relay.receive_remote(env)
        assert store.apply_remote(env) is True  # live frame arrives first
        result = make_engine(relay, store).pull()
        assert result.applied == 0  # catch-up overlap deduped by op id
        assert len(store.nodes(WS)) == 1
        assert store.node(WS, uid("n1")).content_plain == "hi"

    def test_pull_applies_newest_content_and_skips_stale_lww(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        # Server log order (ascending seq) but the HLC clocks are out of order.
        relay.receive_remote(create_env(uid("n1"), content="first", hlc=(1, 0)))
        relay.receive_remote(content_env(uid("n1"), "newer", hlc=(20, 0)))
        relay.receive_remote(content_env(uid("n1"), "stale", hlc=(10, 0)))
        result = make_engine(relay, store).pull()
        assert result.applied == 2  # create + newer content; stale skipped
        row = store.node(WS, uid("n1"))
        assert row is not None and row.content_plain == "newer"

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

        store_a.enqueue(create_env(uid("root")))
        store_a.enqueue(create_env(uid("child"), parent_id=uid("root")))
        store_a.enqueue(
            make_env(
                "object.update",
                {"objectId": uid("root"), "icon": "📄", "contentAst": [{"type": "text", "text": "Hello"}]},
                hlc=(5, 0),
                affected=(uid("root"),),
            )
        )
        assert engine_a.push() == PushResult(sent=3, quarantined=0)

        pulled = engine_b.pull()
        assert pulled.applied == 3
        assert pulled.cursor == 3
        assert store_b.node(WS, uid("root")) is not None and store_b.node(WS, uid("root")).icon == "📄"
        assert store_b.node(WS, uid("child")) is not None and store_b.node(WS, uid("child")).parent_id == uid("root")
        assert store_b.node(WS, uid("root")).content_plain == "Hello"

        # The catch-up echo of A's own pushed ops must not double-apply anywhere.
        assert engine_a.pull().applied == 0
        assert engine_b.pull().applied == 0

        # B edits; both sides converge after one sync round each.
        store_b.enqueue(content_env(uid("root"), "v2", hlc=(9, 0), actor=ACTOR_B))
        engine_b.sync()
        engine_a.sync()
        row_a = store_a.node(WS, uid("root"))
        row_b = store_b.node(WS, uid("root"))
        assert row_a is not None and row_a.content_plain == "v2"
        assert row_a == row_b


class TestSync:
    def test_sync_pushes_then_pulls(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.receive_remote(create_env(uid("theirs"), actor=ACTOR_B))
        relay.receive_remote(content_env(uid("theirs"), "remote text", hlc=(3, 0), actor=ACTOR_B))
        store.enqueue(create_env(uid("mine")))
        make_engine(relay, store).sync()
        assert store.pending_outbox(WS) == []
        assert store.node(WS, uid("mine")) is not None
        row = store.node(WS, uid("theirs"))
        assert row is not None and row.content_plain == "remote text"
        assert store.cursor(WS) == 3


class TestSnapshotRestore:
    def test_restore_epoch_change_wipes_cursor_and_cache(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.restore_epoch = 3
        store.apply_remote(create_env(uid("old")))
        store.set_cursor(WS, 12)
        store.set_restore_epoch(WS, 0)
        engine = make_engine(relay, store)
        assert engine.maybe_restore_snapshot() is False  # no snapshot staged
        assert store.cursor(WS) == 0
        assert store.node(WS, uid("old")) is None
        assert store.stored_restore_epoch(WS) == 3

    def test_newer_snapshot_restores_nodes_and_sets_cursor(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.snapshot_blob = make_server_snapshot(
            [
                {"id": uid("n1"), "workspace_id": WS, "is_class": 0, "present_as_main": 1, "content": "one", "updated_at": None},
                {
                    "id": uid("n2"),
                    "workspace_id": WS,
                    "is_class": 0,
                    "present_as_main": 0,
                    "parent_id": uid("n1"),
                    "content": "two",
                    "updated_at": None,
                },
            ]
        )
        relay.snapshot_up_to_seq = 42
        engine = make_engine(relay, store)
        assert engine.maybe_restore_snapshot() is True
        rows = store.nodes(WS, include_inactive=True)
        assert sorted(row.id for row in rows) == [uid("n1"), uid("n2")]
        assert store.node(WS, uid("n1")).content == "one"
        assert store.cursor(WS) == 42
        # Second call: snapshot no longer newer than the cursor → no re-download.
        assert engine.maybe_restore_snapshot() is False
        assert relay.snapshot_data_calls == 1

    def test_epoch_change_plus_snapshot_wipes_then_restores(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.restore_epoch = 7
        relay.snapshot_blob = make_server_snapshot(
            [{"id": uid("fresh"), "workspace_id": WS, "is_class": 0, "present_as_main": 1, "content": "restored", "updated_at": None}]
        )
        relay.snapshot_up_to_seq = 42
        store.apply_remote(create_env(uid("stale-local")))
        store.set_cursor(WS, 100)
        engine = make_engine(relay, store)
        assert engine.maybe_restore_snapshot() is True
        assert store.node(WS, uid("stale-local")) is None
        row = store.node(WS, uid("fresh"))
        assert row is not None and row.content == "restored"
        assert store.cursor(WS) == 42
        assert store.stored_restore_epoch(WS) == 7

    def test_corrupt_snapshot_blob_returns_false_and_keeps_state(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.snapshot_blob = b"this is not a sqlite database"
        relay.snapshot_up_to_seq = 9
        store.apply_remote(create_env(uid("local"), content="kept", hlc=(2, 0)))
        store.set_cursor(WS, 5)
        engine = make_engine(relay, store)
        assert engine.maybe_restore_snapshot() is False
        row = store.node(WS, uid("local"))
        assert row is not None and row.content_plain == "kept"
        assert store.cursor(WS) == 5

    def test_snapshot_older_than_cursor_is_skipped(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.snapshot_blob = make_server_snapshot(
            [{"id": uid("n1"), "workspace_id": WS, "is_class": 0, "present_as_main": 1, "updated_at": None}]
        )
        relay.snapshot_up_to_seq = 42
        store.set_cursor(WS, 100)
        engine = make_engine(relay, store)
        assert engine.maybe_restore_snapshot() is False
        assert relay.snapshot_data_calls == 0


class TestClockMerge:
    """Pull must merge received HLCs into the local clock.

    With a server clock ahead of the client's wall clock, an unmerged local
    clock restarts at Hlc(0,0) each launch and the server-side LWW gate drops
    the client's edits ("Save" appears to do nothing).
    """

    def test_pull_advances_shared_clock_past_received_hlc(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        future = (10**15, 0)  # far-future server clock, ahead of any wall time
        relay.receive_remote(create_env(uid("remote-node"), hlc=future))
        clock = Clock("device-under-test")
        make_engine(relay, store, clock=clock).pull()
        stamped = new_envelope(
            workspace_id=WS,
            actor_id=ACTOR_A,
            device_id="device-under-test",
            op_type="object.update",
            payload={"objectId": uid("remote-node"), "contentAst": []},
            clock=clock,
        )
        assert (stamped.hlc.physical, stamped.hlc.logical) > future

    def test_pull_merges_across_pages(self, store: LocalStore) -> None:
        relay = FakeRelayClient(WS)
        relay.receive_remote(
            create_env(uid("a"), hlc=(10**15, 0)),
            create_env(uid("b"), hlc=(10**15, 5)),
        )
        clock = Clock("device-under-test")
        make_engine(relay, store, clock=clock, page_size=1).pull()
        stamped = new_envelope(
            workspace_id=WS,
            actor_id=ACTOR_A,
            device_id="device-under-test",
            op_type="object.update",
            payload={"objectId": uid("a"), "contentAst": []},
            clock=clock,
        )
        assert (stamped.hlc.physical, stamped.hlc.logical) > (10**15, 5)


def test_window_wires_shared_clock_into_engine() -> None:
    """Static guard: the GTK window cannot be imported headless, so assert on
    source that the engine receives the same Clock instance the editor stamps
    envelopes with (otherwise merged HLC state would not be shared)."""
    source = (Path(__file__).resolve().parent.parent / "src" / "notees_gtk" / "ui" / "window.py").read_text(
        encoding="utf-8"
    )
    assert "clock=self._clock" in source


class _NoProgressRelay(FakeRelayClient):
    """Relay that claims more data but never advances the seq cursor."""

    def catch_up(self, workspace_id: str, after_seq: int = 0, limit: int = 1000) -> CatchUpPaginatedResponse:
        self.catch_up_calls.append(after_seq)
        return CatchUpPaginatedResponse(
            envelopes=[create_env(uid("stuck"))],
            next_after_seq=after_seq,
            has_more=True,
        )


class TestPullNoProgressGuard:
    def test_pull_breaks_when_the_server_makes_no_progress(self, store: LocalStore) -> None:
        relay = _NoProgressRelay(WS)
        result = make_engine(relay, store).pull()
        assert relay.catch_up_calls == [0]  # exactly one call — no infinite loop
        assert result.cursor == 0
        assert store.cursor(WS) == 0


# ---------------------------------------------------------------- realtime


def _ops_frame(*envelopes: RelayEnvelope) -> dict[str, object]:
    serialized = [env.model_dump(mode="json", by_alias=True) for env in envelopes]
    return {"type": "ops", "wsProtocolVersion": 2, "envelopes": serialized, "seqs": {}}


def _hello(restore_epoch: int, latest_seq: int) -> dict[str, object]:
    return {"type": "hello", "wsProtocolVersion": 2, "restoreEpoch": restore_epoch, "latestSeq": latest_seq}


class TestRealtime:
    """Engine wiring for the WS acceleration path (in-process stub relay).

    Each test uses ONE client for both paths: HTTP methods are the fake
    relay's in-memory overrides; the WS subscription connects to the real
    in-process stub server (via base_url + api_key).
    """

    def test_start_realtime_applies_remote_ops_frame_end_to_end(self, store: LocalStore, tmp_path: Path) -> None:
        def on_connect(conn: object, _index: int) -> None:
            conn.send(json.dumps(_hello(0, 0)))
            conn.send(json.dumps(_ops_frame(create_env(uid("live-node"), content="live"))))

        with WsRelayStub(on_connect=on_connect) as stub:
            relay = FakeRelayClient(WS, base_url=f"http://127.0.0.1:{stub.port}", api_key="k")
            engine = make_engine(relay, store)
            engine.start_realtime()
            try:
                assert wait_until(lambda: store.node(WS, uid("live-node")) is not None)
                assert store.node(WS, uid("live-node")).content_plain == "live"
            finally:
                engine.stop_realtime()
            assert stub.close_codes == [1000]

    def test_hello_with_newer_latest_seq_triggers_pull_from_cursor(self, store: LocalStore) -> None:
        def on_connect(conn: object, _index: int) -> None:
            conn.send(json.dumps(_hello(0, 1)))

        with WsRelayStub(on_connect=on_connect) as stub:
            relay = FakeRelayClient(WS, base_url=f"http://127.0.0.1:{stub.port}", api_key="k")
            relay.receive_remote(create_env(uid("pulled-node"), content="via catch-up"))
            engine = make_engine(relay, store)
            engine.start_realtime()
            try:
                assert wait_until(lambda: store.node(WS, uid("pulled-node")) is not None)
                assert store.node(WS, uid("pulled-node")).content_plain == "via catch-up"
                assert relay.catch_up_calls == [0]  # the pull ran from the stored cursor
            finally:
                engine.stop_realtime()

    def test_hello_restore_epoch_bump_wipes_and_pulls(self, store: LocalStore) -> None:
        def on_connect(conn: object, _index: int) -> None:
            conn.send(json.dumps(_hello(7, 1)))

        with WsRelayStub(on_connect=on_connect) as stub:
            relay = FakeRelayClient(WS, base_url=f"http://127.0.0.1:{stub.port}", api_key="k")
            relay.restore_epoch = 7  # consistent with the hello the stub sends
            relay.receive_remote(create_env(uid("post-restore-node")))
            store.apply_remote(create_env(uid("stale-local")))
            store.set_restore_epoch(WS, 0)
            engine = make_engine(relay, store)
            engine.start_realtime()
            try:
                assert wait_until(lambda: store.node(WS, uid("post-restore-node")) is not None)
                assert store.node(WS, uid("stale-local")) is None  # wiped
                assert store.stored_restore_epoch(WS) == 7
                assert engine.realtime_restore_epoch == 7
            finally:
                engine.stop_realtime()

    def test_buffer_during_pull_applies_envelope_exactly_once(self, store: LocalStore, tmp_path: Path) -> None:
        """An ops frame arriving mid-catch-up buffers; the pull applies the same
        envelope through the page and the drain dedupes — one relay_operations
        row, one node."""

        class RtRelay(FakeRelayClient):
            def __init__(self, workspace_id: str, stub: WsRelayStub) -> None:
                super().__init__(workspace_id, base_url=f"http://127.0.0.1:{stub.port}", api_key="k")
                self._stub = stub
                self.pushed_during_pull = False

            def catch_up(self, workspace_id: str, after_seq: int = 0, limit: int = 1000) -> CatchUpPaginatedResponse:
                if not self.pushed_during_pull:
                    self.pushed_during_pull = True
                    self._stub.send_all(_ops_frame(shared))  # same envelope, live path
                return super().catch_up(workspace_id, after_seq=after_seq, limit=limit)

        def on_connect(conn: object, _index: int) -> None:
            conn.send(json.dumps(_hello(0, 1)))

        with WsRelayStub(on_connect=on_connect) as stub:
            relay = RtRelay(WS, stub)
            shared = create_env(uid("overlap-node"), content="once")
            relay.receive_remote(shared)
            engine = make_engine(relay, store)
            engine.start_realtime()
            try:
                assert wait_until(lambda: store.node(WS, uid("overlap-node")) is not None)
                assert wait_until(lambda: relay.pushed_during_pull)
                assert wait_until(lambda: not engine._pull_in_flight)  # pull + drain finished
                with sqlite3.connect(tmp_path / "store.db") as raw:
                    op_count = raw.execute("SELECT COUNT(*) FROM relay_operations").fetchone()[0]
                assert op_count == 1  # catch-up applied it; the buffered copy deduped
            finally:
                engine.stop_realtime()

    def test_stop_realtime_does_not_reconnect(self, store: LocalStore) -> None:
        def on_connect(conn: object, _index: int) -> None:
            conn.send(json.dumps(_hello(0, 0)))

        with WsRelayStub(on_connect=on_connect) as stub:
            relay = FakeRelayClient(WS, base_url=f"http://127.0.0.1:{stub.port}", api_key="k")
            engine = make_engine(relay, store)
            engine.start_realtime()
            assert wait_until(lambda: stub.connection_count() == 1)
            engine.stop_realtime()
            time.sleep(0.5)  # beyond the fast reconnect schedule
            assert stub.connection_count() == 1
            assert engine.realtime_latest_seq is None

    def test_pull_restore_epoch_change_wipes_and_repulls(self, store: LocalStore) -> None:
        """A catch-up page advertising a new restore epoch wipes and restarts
        from seq 0 (HTTP path — no realtime needed)."""
        relay = FakeRelayClient(WS)
        relay.receive_remote(create_env(uid("post-epoch-node")))
        relay.restore_epoch = 5
        store.apply_remote(create_env(uid("stale-local")))
        store.set_restore_epoch(WS, 0)
        store.set_cursor(WS, 9)
        result = make_engine(relay, store).pull()
        assert store.node(WS, uid("stale-local")) is None  # wiped
        assert store.node(WS, uid("post-epoch-node")) is not None  # re-pulled from 0
        assert store.stored_restore_epoch(WS) == 5
        assert result.cursor == 1
