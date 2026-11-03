"""Tests for the materialized node counts (the ``node_stats`` table —
``packages/store/src/stats.ts`` parity).

Every count derives from node_child_order + the edge index: child_count
(direct children), backlink_count (COUNT(DISTINCT source_id) of incoming
edges), reference_count (COUNT(*) of outgoing edges), descendant_count
(recursive over node_child_order), updated_at mirroring the node row's own
(the LWW-winning op). The appliers rebuild the affected ids PLUS their
ancestor closure after every tree/edge/liveness change, so counts stay
correct all the way up to the roots. No GTK reader consumes the table yet —
these tests pin the derived-state contract the backlink chrome will read.
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
ACTOR = "actor-1"
BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)
LATER_TS = datetime(2026, 1, 2, tzinfo=UTC)


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
    actor_id: str = ACTOR,
    affected: tuple[str, ...] = (),
    timestamp: datetime = BASE_TS,
) -> RelayEnvelope:
    """Build a minimal valid envelope for store tests."""
    return RelayEnvelope(
        id=env_id or new_uuid7(),
        protocolVersion=PROTOCOL_VERSION,
        workspaceId=workspace_id,
        actorId=actor_id,
        deviceId="stats-test-device",
        hlc=Hlc(physical=hlc[0], logical=hlc[1]),
        affectedNodeIds=list(affected),
        opType=op_type,
        payload=payload,
        timestamp=timestamp,
    )


def create_env(
    node_id: str,
    *,
    parent_id: str | None = None,
    content: Any = None,
    tag_ids: tuple[str, ...] = (),
    hlc: tuple[int, int] = (1, 0),
) -> RelayEnvelope:
    """An ``object.create`` envelope; ``content`` is a raw token list."""
    payload: dict[str, Any] = {"objectId": node_id, "parentId": parent_id}
    if tag_ids:
        payload["tagIds"] = list(tag_ids)
    if content is not None:
        payload["contentAst"] = content
    return make_env("object.create", payload, hlc=hlc, affected=(node_id,))


def update_env(
    node_id: str, *, hlc: tuple[int, int], timestamp: datetime = BASE_TS, **fields: Any
) -> RelayEnvelope:
    payload: dict[str, Any] = {"objectId": node_id}
    payload.update(fields)
    return make_env("object.update", payload, hlc=hlc, affected=(node_id,), timestamp=timestamp)


def delete_env(node_id: str, *, permanent: bool = False, hlc: tuple[int, int]) -> RelayEnvelope:
    payload: dict[str, Any] = {"objectId": node_id}
    if permanent:
        payload["permanent"] = True
    return make_env("object.delete", payload, hlc=hlc, affected=(node_id,))


def stats_of(store: LocalStore, node_id: str) -> tuple[Any, ...] | None:
    """The node's stats row: (child, backlink, reference, descendant, updated_at)."""
    row = store._conn.execute(  # noqa: SLF001
        "SELECT child_count, backlink_count, reference_count, descendant_count, updated_at"
        " FROM node_stats WHERE node_id = ?",
        (node_id,),
    ).fetchone()
    return row


@pytest.fixture
def store(tmp_path: Path) -> Iterator[LocalStore]:
    instance = LocalStore(tmp_path / "store.db")
    try:
        yield instance
    finally:
        instance.close()


class TreeIds:
    page: str
    block_1: str
    block_2: str
    grandchild: str
    target: str
    tag: str


def build_tree(store: LocalStore) -> TreeIds:
    """page ─┬─ block_1 (mentions target) ── grandchild
             └─ block_2                        page tagged with tag."""
    ids = TreeIds()
    ids.page = uid("stats-page")
    ids.block_1 = uid("stats-block-1")
    ids.block_2 = uid("stats-block-2")
    ids.grandchild = uid("stats-grandchild")
    ids.target = uid("stats-target")
    ids.tag = uid("stats-tag")
    store.apply_remote(create_env(ids.page, content=[{"type": "text", "text": "Page"}], hlc=(1, 0)))
    store.apply_remote(
        create_env(
            ids.block_1,
            parent_id=ids.page,
            content=[{"type": "mention", "targetNodeId": ids.target, "text": "see"}],
            hlc=(2, 0),
        )
    )
    store.apply_remote(
        create_env(ids.block_2, parent_id=ids.page, content=[{"type": "text", "text": "two"}], hlc=(3, 0))
    )
    store.apply_remote(
        create_env(ids.grandchild, parent_id=ids.block_1, content=[{"type": "text", "text": "g"}], hlc=(4, 0))
    )
    store.apply_remote(create_env(ids.target, content=[{"type": "text", "text": "T"}], hlc=(5, 0)))
    store.apply_remote(create_env(ids.tag, content=[{"type": "text", "text": "Tag"}], hlc=(6, 0)))
    # The re-issued create add carrier is the tag convergence path.
    store.apply_remote(create_env(ids.page, tag_ids=(ids.tag,), hlc=(7, 0)))
    return ids


class TestNodeStats:
    def test_counts_after_a_small_tree_build(self, store: LocalStore) -> None:
        ids = build_tree(store)
        ts = BASE_TS.isoformat()
        assert stats_of(store, ids.page) == (2, 0, 1, 3, ts)  # 2 children, the tag edge, 3 descendants
        assert stats_of(store, ids.block_1) == (1, 0, 1, 1, ts)  # the mention edge + grandchild
        assert stats_of(store, ids.block_2) == (0, 0, 0, 0, ts)
        assert stats_of(store, ids.grandchild) == (0, 0, 0, 0, ts)
        assert stats_of(store, ids.target) == (0, 1, 0, 0, ts)  # the backlink from block_1
        assert stats_of(store, ids.tag) == (0, 1, 0, 0, ts)  # the tag edge from page

    def test_untag_recomputes_both_sides(self, store: LocalStore) -> None:
        ids = build_tree(store)
        store.apply_remote(
            make_env("tag.unassign", {"objectId": ids.page, "tagId": ids.tag}, hlc=(8, 0))
        )
        assert stats_of(store, ids.page)[:4] == (2, 0, 0, 3)
        assert stats_of(store, ids.tag)[:4] == (0, 0, 0, 0)

    def test_soft_trash_keeps_rows_stable(self, store: LocalStore) -> None:
        """The flip reresolves edges and recomputes stats for the node +
        ancestors; the tree rows survive, so the counts recompute to the
        same values — the row must not drift or vanish."""
        ids = build_tree(store)
        ts = BASE_TS.isoformat()
        store.apply_remote(delete_env(ids.block_1, hlc=(7, 0)))
        assert stats_of(store, ids.block_1) == (1, 0, 1, 1, ts)
        assert stats_of(store, ids.page) == (2, 0, 1, 3, ts)
        assert stats_of(store, ids.target) == (0, 1, 0, 0, ts)

    def test_permanent_delete_shrinks_ancestors_and_lost_backlink_targets(
        self, store: LocalStore
    ) -> None:
        ids = build_tree(store)
        ts = BASE_TS.isoformat()
        store.apply_remote(delete_env(ids.block_1, permanent=True, hlc=(7, 0)))
        # The purge takes the subtree's stats rows with it ...
        assert stats_of(store, ids.block_1) is None
        assert stats_of(store, ids.grandchild) is None
        # ... the ancestors lose the descendants (block_1 + grandchild) ...
        assert stats_of(store, ids.page) == (1, 0, 1, 1, ts)  # block_2 alone survives
        # ... and the lost-backlink target recomputes without the purged source.
        assert stats_of(store, ids.target) == (0, 0, 0, 0, ts)

    def test_updated_at_mirrors_the_node_row(self, store: LocalStore) -> None:
        ids = build_tree(store)
        # A content write with a later envelope timestamp lands on the node
        # row; the stats rebuild mirrors it (order-invariance law).
        store.apply_remote(
            update_env(
                ids.block_2,
                hlc=(8, 0),
                timestamp=LATER_TS,
                contentAst=[{"type": "text", "text": "edited"}],
            )
        )
        node_updated_at = store._conn.execute(  # noqa: SLF001
            "SELECT updated_at FROM nodes WHERE id = ?", (ids.block_2,)
        ).fetchone()[0]
        assert node_updated_at == LATER_TS.isoformat()
        assert stats_of(store, ids.block_2) == (0, 0, 0, 0, LATER_TS.isoformat())
        # Untouched rows keep the earlier mirror.
        assert stats_of(store, ids.page) == (2, 0, 1, 3, BASE_TS.isoformat())

    def test_move_recomputes_old_and_new_ancestor_chains(self, store: LocalStore) -> None:
        ids = build_tree(store)
        ts = BASE_TS.isoformat()
        # Detach grandchild from block_1 and re-parent it at block_2.
        store.apply_remote(
            make_env(
                "object.move",
                {"objectId": ids.grandchild, "parentId": ids.block_2},
                hlc=(7, 0),
                affected=(ids.grandchild,),
            )
        )
        assert stats_of(store, ids.block_1) == (0, 0, 1, 0, ts)  # lost the child
        assert stats_of(store, ids.block_2) == (1, 0, 0, 1, ts)  # gained it
        assert stats_of(store, ids.page) == (2, 0, 1, 3, ts)  # still 3 descendants total

    def test_gone_node_rows_are_skipped_by_the_node_join(self, store: LocalStore) -> None:
        """Hard-deleted ids have no node row: the rebuild silently skips
        them (the stats row was already purged) and recomputes the survivors
        it was seeded with."""
        ids = build_tree(store)
        store.apply_remote(delete_env(ids.block_1, permanent=True, hlc=(7, 0)))
        # A second, no-op rebuild against the same (now partly gone) set
        # leaves the survivors exactly as the purge left them.
        store._rebuild_node_stats(WS_A, [ids.block_1, ids.target])  # noqa: SLF001
        assert stats_of(store, ids.block_1) is None
        assert stats_of(store, ids.target) == (0, 0, 0, 0, BASE_TS.isoformat())


def test_wipe_clears_stats_rows(store: LocalStore) -> None:
    build_tree(store)
    store.wipe(WS_A)
    with sqlite3.connect(store._conn.execute("PRAGMA database_list").fetchone()[2]) as raw:  # noqa: SLF001
        assert raw.execute("SELECT COUNT(*) FROM node_stats").fetchone()[0] == 0
        assert raw.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0
