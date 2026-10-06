"""Two-way relay sync engine for the Notees GTK client.

Mirrors the mobile client's sync contract (``mobile-sync.md``): the outbox is
drained in chunks of 100 with whole-chunk ack semantics (the server dedupes by
envelope id, so ``saved_ids`` may omit ids that were sent), 401/403 abort the
push so the caller can re-authenticate, other 4xx quarantine the chunk, and
network/5xx/429 failures retry within the same push on the backoff schedule
``[5, 15, 60, 300, 1800]`` seconds (429 honors ``Retry-After`` when present).
Pull pages catch-up from the persisted seq cursor, persisting the cursor after
every page so a mid-page crash only re-fetches the tail; op-id dedupe makes
catch-up/live overlap harmless.

Realtime (WIRE.md): :meth:`start_realtime` subscribes to the workspace's WS
acceleration path. Live ``ops`` apply directly; ops arriving while a catch-up
pull is in flight are buffered and drained when the pull finishes (op-id
dedupe makes the overlap harmless — every envelope applies exactly once). A
fresh ``hello`` after a reconnect advertises ``latest_seq``/``restore_epoch``:
an epoch change wipes local state (the server was restored from backup), and a
``latest_seq`` ahead of the cursor triggers a normal pull from the cursor.
The socket is only an accelerator — the seq cursor stays the authoritative
recovery mechanism.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from notees_gtk.core.api import (
    HelloInfo,
    NetworkError,
    NoteesClient,
    QuarantinedError,
    RateLimitedError,
    RealtimeClient,
    ServerError,
)
from notees_gtk.core.protocol.clock import Clock
from notees_gtk.core.protocol.models import RelayEnvelope
from notees_gtk.data.errors import StoreError
from notees_gtk.data.store import LocalStore

__all__ = ["BACKOFF_SECONDS", "OUTBOX_CHUNK_SIZE", "PullResult", "PushResult", "SyncEngine"]

_log = logging.getLogger(__name__)

#: Retry backoff schedule (seconds) for network/5xx/429 push failures.
BACKOFF_SECONDS: tuple[float, ...] = (5, 15, 60, 300, 1800)

#: Outbox envelopes submitted per ``POST /batch`` chunk.
OUTBOX_CHUNK_SIZE = 100


def _now_ms() -> int:
    """Return the current wall-clock time in milliseconds."""
    return int(time.time() * 1000)


@dataclass(frozen=True)
class PushResult:
    """Outcome of one :meth:`SyncEngine.push` round.

    Attributes:
        sent: Envelopes acknowledged by the server (whole-chunk acks).
        quarantined: Envelopes parked in quarantine (unretryable 4xx).
    """

    sent: int
    quarantined: int


@dataclass(frozen=True)
class PullResult:
    """Outcome of one :meth:`SyncEngine.pull` round.

    Attributes:
        applied: Envelopes newly applied to the local cache.
        cursor: The seq cursor adopted from the final catch-up page.
    """

    applied: int
    cursor: int


class SyncEngine:
    """Drives push/pull sync between a :class:`LocalStore` and the relay.

    Args:
        client: REST client for the Notees server (or a test fake).
        store: Local SQLite cache and outbox.
        actor_id: Public id of the syncing user; envelopes are stamped with it
            by the producers (Task 5/6), the engine itself is actor-agnostic.
        workspace_id: Workspace to sync.
        clock: Local HLC clock. Every catch-up page merges the newest received
            HLC into it so envelopes stamped after a sync stay causally ahead
            of state the client just pulled; defaults to a fresh clock bound
            to ``actor_id`` (callers that stamp envelopes, e.g. the GTK
            window, should inject their shared instance).
        sleeper: Callable sleeping ``n`` seconds between retries; injectable so
            tests can record the backoff schedule without real delays.
        page_size: Catch-up page size (server clamps to [1, 10,000]).
    """

    def __init__(
        self,
        client: NoteesClient,
        store: LocalStore,
        *,
        actor_id: str,
        workspace_id: str,
        clock: Clock | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        page_size: int = 1000,
    ) -> None:
        """Initialize the engine binding client, store, and workspace."""
        self._client = client
        self._store = store
        self._actor_id = actor_id
        self._workspace_id = workspace_id
        self._clock = clock if clock is not None else Clock(device_id=actor_id)
        self._sleeper = sleeper
        self._page_size = page_size
        # Realtime state (start_realtime/stop_realtime). The lock serializes
        # catch-up pulls (main thread or the WS reader thread after a
        # reconnect hello) with live-ops buffering/application.
        self._realtime: RealtimeClient | None = None
        self._rt_lock = threading.RLock()
        self._rt_buffer: list[tuple[list[RelayEnvelope], dict[str, int]]] = []
        self._pull_in_flight = False

    # ------------------------------------------------------------------- push

    def push(self) -> PushResult:
        """Drain the outbox in chunks of :data:`OUTBOX_CHUNK_SIZE`.

        Whole-chunk ack semantics: once ``submit_batch`` returns (HTTP 200),
        the entire chunk is applied to the local cache and removed from the
        outbox — the server dedupes by envelope id, so ``saved_ids`` may omit
        ids that were sent. 401/403 re-raise so the caller re-authenticates;
        other 4xx quarantine the chunk and the loop continues with the next
        one; network/5xx/429 retry on the backoff schedule, then give up for
        this round leaving the chunk pending.
        """
        sent = 0
        quarantined = 0
        while True:
            chunk = self._store.pending_outbox(self._workspace_id, limit=OUTBOX_CHUNK_SIZE)
            if not chunk:
                break
            ids = [env.id for env in chunk]
            try:
                delivered = self._submit_with_retry(chunk)
            except QuarantinedError as exc:
                self._store.quarantine_outbox(ids, reason=exc.detail)
                quarantined += len(chunk)
                _log.warning("Quarantined outbox chunk of %d envelopes: %s", len(chunk), exc.detail)
                continue
            if not delivered:
                _log.warning("Giving up on outbox chunk of %d envelopes this round", len(chunk))
                break
            for env in chunk:
                # Our own envelope was already server-accepted at this point;
                # a local mirror apply that violates a guard (a bug, not a
                # sync condition) must not stall the outbox forever.
                try:
                    self._store.apply_remote(env)
                except StoreError as exc:
                    _log.warning("Skipping local apply of %s (%s): %s", env.id, env.op_type, exc)
            self._store.mark_outbox_sent(ids)
            sent += len(chunk)
        return PushResult(sent=sent, quarantined=quarantined)

    def _submit_with_retry(self, chunk: list[RelayEnvelope]) -> bool:
        """Submit one chunk, sleeping :data:`BACKOFF_SECONDS` between attempts.

        Makes at most ``len(BACKOFF_SECONDS)`` attempts per round. Returns
        ``True`` when the batch was accepted; ``False`` when every attempt
        failed transiently (the chunk stays in the outbox for the next round).
        ``RateLimitedError.retry_after`` overrides the schedule slot when
        advertised. Quarantined (4xx) and authentication failures propagate to
        the caller.
        """
        for attempt in range(len(BACKOFF_SECONDS)):
            try:
                self._client.submit_batch(chunk)
                return True
            except (NetworkError, ServerError, RateLimitedError) as exc:
                if attempt == len(BACKOFF_SECONDS) - 1:
                    break
                retry_after = exc.retry_after if isinstance(exc, RateLimitedError) else None
                delay = retry_after if retry_after is not None else BACKOFF_SECONDS[attempt]
                _log.warning("Push attempt %d failed (%s); retrying in %ss", attempt + 1, exc.detail, delay)
                self._sleeper(delay)
        return False

    # ------------------------------------------------------------------- pull

    def pull(self) -> PullResult:
        """Page catch-up from the stored cursor and apply every envelope.

        The cursor is persisted after every page, so a mid-page crash only
        re-fetches the tail (op-id dedupe covers the overlap). The local HLC
        clock merges the newest envelope HLC of every page so subsequently
        stamped envelopes stay causally ahead of pulled state. The final
        page's ``next_after_seq`` covers the tail and is adopted as the
        stored cursor; a page reporting no cursor progress breaks the loop
        instead of spinning on a misbehaving server.

        A ``restore_epoch`` change on a catch-up response means the server was
        restored from backup: local state is wiped, the epoch re-persisted,
        and the pull restarts from seq 0. Envelopes whose application violates
        a store guard (a cycle close, a move guard, ...) are logged and
        skipped — one bad envelope must not stall the whole pull; its dedupe
        record rolled back with the throw, so a retry after a wipe can still
        apply it. Pulls are serialized with the realtime stream and any
        buffered live ops are drained afterwards (op-id dedupe makes the
        overlap harmless).
        """
        applied = 0
        with self._rt_lock:
            self._pull_in_flight = True
            try:
                after = self._store.cursor(self._workspace_id)
                while True:
                    page = self._client.catch_up(self._workspace_id, after_seq=after, limit=self._page_size)
                    stored_epoch = self._store.stored_restore_epoch(self._workspace_id)
                    if page.restore_epoch != stored_epoch:
                        _log.warning(
                            "Server restore_epoch changed %s → %s during catch-up; wiping local state",
                            stored_epoch,
                            page.restore_epoch,
                        )
                        self._store.wipe(self._workspace_id)
                        self._store.set_restore_epoch(self._workspace_id, page.restore_epoch)
                        after = 0
                        continue  # The page came from an obsolete epoch; re-fetch from seq 0.
                    for env in page.envelopes:
                        applied += self._apply_guarded(env)
                    if page.envelopes:
                        newest = max(page.envelopes, key=lambda env: (env.hlc.physical, env.hlc.logical)).hlc
                        self._clock.update(newest, _now_ms())
                    if page.next_after_seq is not None:
                        if page.next_after_seq <= after:
                            _log.warning(
                                "Catch-up made no progress (next_after_seq=%s after seq %s); aborting pull",
                                page.next_after_seq,
                                after,
                            )
                            break
                        after = page.next_after_seq
                        self._store.set_cursor(self._workspace_id, after)
                    if not page.has_more or page.next_after_seq is None:
                        break
            finally:
                self._pull_in_flight = False
            applied += self._drain_rt_buffer_locked()
        return PullResult(applied=applied, cursor=self._store.cursor(self._workspace_id))

    def _apply_guarded(self, env: RelayEnvelope) -> int:
        """Apply one envelope, quarantine-and-continue on store guard violations."""
        try:
            return 1 if self._store.apply_remote(env) else 0
        except StoreError as exc:
            _log.warning("Skipping envelope %s (%s): %s", env.id, env.op_type, exc)
            return 0

    def sync(self) -> None:
        """Run one full sync round: push then pull."""
        self.push()
        self.pull()

    # -------------------------------------------------------------- realtime

    @property
    def realtime_latest_seq(self) -> int | None:
        """Highest server seq advertised by the last realtime hello (None when stopped)."""
        return self._realtime.latest_seq if self._realtime is not None else None

    @property
    def realtime_restore_epoch(self) -> int | None:
        """Restore epoch advertised by the last realtime hello (None when stopped)."""
        return self._realtime.restore_epoch if self._realtime is not None else None

    def start_realtime(self) -> None:
        """Subscribe to the workspace's WS acceleration path (idempotent).

        Runs until :meth:`stop_realtime`; reconnects after abnormal closes on
        the client's backoff schedule.
        """
        if self._realtime is not None:
            return
        self._realtime = RealtimeClient(
            self._client,
            self._workspace_id,
            on_hello=self._on_rt_hello,
            on_ops=self._on_rt_ops,
            on_ack=lambda saved_ids: _log.info("Realtime batch acked (%d ids)", len(saved_ids)),
            on_error=lambda error: _log.warning("Realtime: %s", error),
        )
        self._realtime.start()

    def stop_realtime(self) -> None:
        """Unsubscribe and drain: clean close (code 1000), no reconnect."""
        realtime = self._realtime
        if realtime is None:
            return
        self._realtime = None
        realtime.stop()
        with self._rt_lock:
            self._rt_buffer.clear()

    def _on_rt_hello(self, info: HelloInfo) -> None:
        """Fresh handshake (initial connect or reconnect).

        A ``restore_epoch`` change means the server was restored from backup:
        wipe local state and re-persist the epoch. When the advertised
        ``latest_seq`` is ahead of the cursor, run the normal pull from the
        cursor (idempotent overlap with anything buffered); otherwise drain
        the buffer directly. Runs on the WS reader thread; the pull is
        serialized with any in-flight pull via ``_rt_lock``.
        """
        if info.restore_epoch != self._store.stored_restore_epoch(self._workspace_id):
            stored = self._store.stored_restore_epoch(self._workspace_id)
            _log.warning("Realtime restore_epoch changed %s → %s; wiping local state", stored, info.restore_epoch)
            self._store.wipe(self._workspace_id)
            self._store.set_restore_epoch(self._workspace_id, info.restore_epoch)
        if info.latest_seq > self._store.cursor(self._workspace_id):
            self.pull()
        else:
            with self._rt_lock:
                self._drain_rt_buffer_locked()

    def _on_rt_ops(self, envelopes: list[RelayEnvelope], seqs: dict[str, int]) -> None:
        """Live batch broadcast.

        Applies directly unless a catch-up pull is in flight — the consumer
        buffers while catching up and relies on op-id dedupe, so an envelope
        that rode both paths applies exactly once (WIRE.md overlap
        discipline). The socket is an accelerator: the seq cursor stays
        authoritative, so live ops deliberately do NOT advance it.
        """
        with self._rt_lock:
            if self._pull_in_flight:
                self._rt_buffer.append((envelopes, seqs))
                return
            for env in envelopes:
                self._apply_guarded(env)

    def _drain_rt_buffer_locked(self) -> int:
        """Apply every buffered live frame (caller holds ``_rt_lock``).

        Returns the number of envelopes newly applied; dedupe hits (the pull
        already applied the envelope) count as 0.
        """
        applied = 0
        while self._rt_buffer:
            envelopes, _seqs = self._rt_buffer.pop(0)
            for env in envelopes:
                applied += self._apply_guarded(env)
        return applied

    # --------------------------------------------------------------- snapshot

    def maybe_restore_snapshot(self) -> bool:
        """Probe the server and restore a newer snapshot when worthwhile.

        A ``restore_epoch`` change means the server was restored from backup:
        the local cache and watermarks are wiped and the epoch re-persisted.
        When a snapshot exists whose ``up_to_seq`` beats the local cursor, the
        blob is downloaded and its ``nodes`` table copied over (column
        intersection) with the cursor set to ``up_to_seq``.

        Returns ``True`` only when a snapshot blob was restored. Snapshots with
        ``up_to_seq=None`` (pre-cursor era) are skipped: this store tracks no
        HLC watermark to compare, and catch-up from 0 converges via op-id
        dedupe.
        """
        meta = self._client.snapshot_probe(self._workspace_id)
        stored_epoch = self._store.stored_restore_epoch(self._workspace_id)
        if meta.restore_epoch != stored_epoch:
            _log.warning("Server restore_epoch changed %s → %s; wiping local state", stored_epoch, meta.restore_epoch)
            self._store.wipe(self._workspace_id)
            self._store.set_restore_epoch(self._workspace_id, meta.restore_epoch)
        if not meta.has_snapshot:
            return False
        up_to_seq = meta.up_to_seq if meta.up_to_seq is not None else 0
        if up_to_seq <= self._store.cursor(self._workspace_id):
            return False
        blob = self._client.snapshot_data(self._workspace_id)
        if not self._store.restore_snapshot(blob, workspace_id=self._workspace_id):
            return False
        self._store.set_cursor(self._workspace_id, up_to_seq)
        _log.info("Restored snapshot up to seq %d", up_to_seq)
        return True
