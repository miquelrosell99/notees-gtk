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
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from notees_gtk.core.api import (
    NetworkError,
    NoteesClient,
    QuarantinedError,
    RateLimitedError,
    ServerError,
)
from notees_gtk.core.protocol.clock import Clock
from notees_gtk.core.protocol.models import RelayEnvelope
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
                self._store.apply_remote(env)
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
        """
        applied = 0
        after = self._store.cursor(self._workspace_id)
        while True:
            page = self._client.catch_up(self._workspace_id, after_seq=after, limit=self._page_size)
            for env in page.envelopes:
                if self._store.apply_remote(env):
                    applied += 1
            if page.envelopes:
                newest = max(page.envelopes, key=lambda env: (env.hlc.physical, env.hlc.logical)).hlc
                self._clock.update(newest, _now_ms())
            if page.next_after_seq is not None:
                if page.next_after_seq <= after:
                    _log.warning("Catch-up made no progress (next_after_seq=%s after seq %s); aborting pull", page.next_after_seq, after)
                    break
                after = page.next_after_seq
                self._store.set_cursor(self._workspace_id, after)
            if not page.has_more or page.next_after_seq is None:
                break
        return PullResult(applied=applied, cursor=after)

    def sync(self) -> None:
        """Run one full sync round: push then pull."""
        self.push()
        self.pull()

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
