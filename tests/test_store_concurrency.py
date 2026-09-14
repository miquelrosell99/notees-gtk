"""Cross-thread safety tests for ``LocalStore``.

The GTK client constructs the store on the main thread while ``SyncEngine``
runs on worker threads against the same connection, so all public methods
must be safe to call concurrently. These tests force real overlap with a
``threading.Barrier`` and assert exact final state, not just absence of
exceptions.
"""

from __future__ import annotations

import threading
from pathlib import Path

from notees_gtk.core.protocol.clock import Hlc
from notees_gtk.core.protocol.models import RelayEnvelope
from notees_gtk.data.store import LocalStore

WS = "ws-threads"
ACTOR = "actor-threads"

ENQUEUE_THREADS = 8
ENQUEUES_PER_THREAD = 200
APPLY_THREADS = 2
APPLIED_NODES = 150


def make_envelope(op_type: str, payload: dict[str, object], *, logical: int) -> RelayEnvelope:
    """Build a minimal valid envelope with a distinct HLC for ordering tests."""
    return RelayEnvelope(
        workspace_id=WS,
        actor_id=ACTOR,
        hlc=Hlc(physical=1, logical=logical),
        op_type=op_type,
        payload=payload,
    )


def test_concurrent_enqueue_and_apply(tmp_path: Path) -> None:
    """8 threads enqueue while 2 threads apply the same creates concurrently.

    Expectations: no exception escapes any thread; the outbox holds exactly
    ``ENQUEUE_THREADS * ENQUEUES_PER_THREAD`` rows; op-id dedupe makes each
    create apply exactly once across both applier threads; the node mirror
    ends with exactly ``APPLIED_NODES`` rows.
    """
    store = LocalStore(tmp_path / "threads.db")

    enqueue_envs = [
        make_envelope("node.create", {"nodeId": f"n-{index}", "kind": "page"}, logical=index)
        for index in range(ENQUEUE_THREADS * ENQUEUES_PER_THREAD)
    ]
    apply_envs = [
        make_envelope("node.create", {"nodeId": f"a-{index}", "kind": "page"}, logical=10_000 + index)
        for index in range(APPLIED_NODES)
    ]

    barrier = threading.Barrier(ENQUEUE_THREADS + APPLY_THREADS)
    errors: list[BaseException] = []
    applied_true_counts = [0] * APPLY_THREADS

    def enqueue_worker(slice_: list[RelayEnvelope]) -> None:
        try:
            barrier.wait(timeout=30)
            for env in slice_:
                store.enqueue(env)
        except BaseException as exc:  # noqa: BLE001 — re-raised in the main thread below
            errors.append(exc)

    def apply_worker(index: int) -> None:
        try:
            barrier.wait(timeout=30)
            applied = 0
            for env in apply_envs:
                if store.apply_remote(env):
                    applied += 1
            applied_true_counts[index] = applied
        except BaseException as exc:  # noqa: BLE001 — re-raised in the main thread below
            errors.append(exc)

    threads = [
        threading.Thread(target=enqueue_worker, args=(enqueue_envs[start : start + ENQUEUES_PER_THREAD],))
        for start in range(0, len(enqueue_envs), ENQUEUES_PER_THREAD)
    ] + [threading.Thread(target=apply_worker, args=(index,)) for index in range(APPLY_THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads), "worker thread hung"
    assert errors == []
    assert len(store.pending_outbox(WS, limit=10_000)) == ENQUEUE_THREADS * ENQUEUES_PER_THREAD
    # Op-id dedupe: each create returned True exactly once across both appliers.
    assert sum(applied_true_counts) == APPLIED_NODES
    assert len(store.nodes(WS)) == APPLIED_NODES
