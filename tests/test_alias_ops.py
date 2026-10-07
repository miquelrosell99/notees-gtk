"""Tests for the pure alias-chrome helpers (``notees_gtk.ui.alias_ops``).

Headless like the module under test: no GTK import, no display needed.
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from notees_gtk.core.protocol.clock import Clock
from notees_gtk.data.store import NodeRow
from notees_gtk.ui.alias_ops import alias_repoint_candidates, alias_update_envelope

WS = "ws-a"
ACTOR = "actor-1"
DEVICE = "alias-ops-test-device"
ALIAS_ID = str(uuid5(NAMESPACE_URL, "notees-gtk/alias-ops/alias"))
MAIN_ID = str(uuid5(NAMESPACE_URL, "notees-gtk/alias-ops/main"))


def row(node_id: str, *, aliased_node_id: str | None = None) -> NodeRow:
    """A minimal live node row for the candidate filter."""
    return NodeRow(
        id=node_id,
        workspace_id=WS,
        parent_id=None,
        is_class=False,
        present_as_main=True,
        name=None,
        class_ids=(),
        tag_ids=(),
        icon=None,
        color=None,
        cover_asset_id=None,
        banner_asset_id=None,
        aliased_node_id=aliased_node_id,
        is_active=True,
        content=None,
        content_plain="",
    )


class TestAliasUpdateEnvelope:
    """The alias-side write envelope: ``object.update`` over the carrier's
    OWN ``aliasedNodeId`` — presence-write to re-point, present-null to clear
    (the payload shape the applier's nullish-field rule consumes)."""

    def test_repoint_carries_the_target(self) -> None:
        clock = Clock("alias-ops-device")
        envelope = alias_update_envelope(
            workspace_id=WS,
            actor_id=ACTOR,
            device_id=DEVICE,
            node_id=ALIAS_ID,
            target_id=MAIN_ID,
            clock=clock,
        )
        assert envelope.op_type == "object.update"
        assert envelope.workspace_id == WS
        assert envelope.actor_id == ACTOR
        assert envelope.payload == {"objectId": ALIAS_ID, "aliasedNodeId": MAIN_ID}
        assert list(envelope.affected_node_ids) == [ALIAS_ID]

    def test_clear_carries_an_explicit_null(self) -> None:
        envelope = alias_update_envelope(
            workspace_id=WS,
            actor_id=ACTOR,
            device_id=DEVICE,
            node_id=ALIAS_ID,
            target_id=None,
            clock=Clock("alias-ops-device"),
        )
        # The key is PRESENT with a null value — a real CLEAR, never absent.
        assert envelope.payload == {"objectId": ALIAS_ID, "aliasedNodeId": None}


class TestAliasRepointCandidates:
    """The Change picker's filter: the alias itself and every already-aliased
    node are out; input order is preserved."""

    def test_self_and_already_aliased_rows_are_excluded(self) -> None:
        alias = row(ALIAS_ID, aliased_node_id=MAIN_ID)
        main = row(MAIN_ID)
        taken = row(str(uuid5(NAMESPACE_URL, "notees-gtk/alias-ops/taken")), aliased_node_id=str(uuid5(NAMESPACE_URL, "notees-gtk/alias-ops/other")))
        free_a = row(str(uuid5(NAMESPACE_URL, "notees-gtk/alias-ops/free-a")))
        free_b = row(str(uuid5(NAMESPACE_URL, "notees-gtk/alias-ops/free-b")))
        candidates = alias_repoint_candidates([alias, main, taken, free_a, free_b], ALIAS_ID)
        assert candidates == [main, free_a, free_b]

    def test_empty_input_yields_empty(self) -> None:
        assert alias_repoint_candidates([], ALIAS_ID) == []
