"""Tests for the pure tag-chrome helpers (``notees_gtk.ui.tag_ops``).

Headless like the module under test: no GTK import, no display needed —
the pure-logic half of the Tags row (pill ordering, color resolution, the
page filter behind the add picker, the write envelopes).
"""

from __future__ import annotations

import json
from uuid import NAMESPACE_URL, uuid5

import pytest

from notees_gtk.core.protocol.clock import Clock
from notees_gtk.data.store import NodeRow
from notees_gtk.ui.tag_ops import (
    PRESET_HEX,
    contrast_for,
    css_color_for,
    effective_class_color,
    effective_node_color,
    renders_with_document_chrome,
    tag_assign_candidates,
    tag_assign_envelope,
    tag_pills,
    tag_unassign_envelope,
)

WS = "ws-a"
ACTOR = "actor-1"
DEVICE = "tag-ops-test-device"
NODE_ID = str(uuid5(NAMESPACE_URL, "notees-gtk/tag-ops/node"))
TAG_A_ID = str(uuid5(NAMESPACE_URL, "notees-gtk/tag-ops/tag-a"))
TAG_B_ID = str(uuid5(NAMESPACE_URL, "notees-gtk/tag-ops/tag-b"))
CLASS_ID = str(uuid5(NAMESPACE_URL, "notees-gtk/tag-ops/class"))
PARENT_CLASS_ID = str(uuid5(NAMESPACE_URL, "notees-gtk/tag-ops/parent-class"))


def row(
    node_id: str,
    *,
    parent_id: str | None = None,
    is_class: bool = False,
    is_embedded: bool = False,
    class_ids: tuple[str, ...] = (),
    tag_ids: tuple[str, ...] = (),
    color: str | None = None,
    icon: str | None = None,
    parentless_role: str | None = None,
    content: str | None = None,
    content_plain: str = "",
) -> NodeRow:
    """A minimal live node row for the pure filters."""
    return NodeRow(
        id=node_id,
        workspace_id=WS,
        parent_id=parent_id,
        is_class=is_class,
        is_embedded=is_embedded,
        name=None,
        class_ids=class_ids,
        tag_ids=tag_ids,
        icon=icon,
        color=color,
        cover_asset_id=None,
        banner_asset_id=None,
        aliased_node_id=None,
        description=None,
        list_style=None,
        parentless_role=parentless_role,
        is_active=True,
        content=content,
        content_plain=content_plain,
    )


def label_for(node_row: NodeRow) -> str:
    """The test stand-in for the display-name derivation (``tree.py``)."""
    return node_row.content_plain or node_row.id


class TestColorResolution:
    """The pill surface: preset tokens map to the web ``PRESET_HEX`` table,
    custom hex passes through, contrast picks black/white by luminance."""

    def test_preset_tokens_mirror_the_web_table(self) -> None:
        assert PRESET_HEX["sky"] == "#20a9e9"
        assert css_color_for("red") == "#e34d45"
        assert css_color_for("#Aa11bB") == "#aa11bb"

    def test_garbage_color_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="preset token"):
            css_color_for("var(--color-preset-red)")

    def test_contrast_picks_black_on_light_colors(self) -> None:
        assert contrast_for("#f3b816") == "#000000"  # yellow

    def test_contrast_picks_white_on_dark_colors(self) -> None:
        assert contrast_for("#123456") == "#ffffff"

    def test_contrast_threshold_matches_the_web_reference(self) -> None:
        # The web rule is luminance > 0.45 → black; the purple preset sits
        # just above the threshold, so it takes BLACK text there and here.
        assert contrast_for("#9662da") == "#000000"

    def test_contrast_needs_concrete_hex(self) -> None:
        with pytest.raises(ValueError, match="concrete"):
            contrast_for("sky")


class TestEffectiveColor:
    """The web ``effectiveNodeColor`` port: own color, else the first
    assigned class's chain color (BFS over the extends edges)."""

    def test_own_color_wins(self) -> None:
        by_id = {NODE_ID: row(NODE_ID, color="sky")}
        assert effective_node_color(by_id[NODE_ID], by_id, {}) == "sky"

    def test_first_class_with_a_chain_color_wins(self) -> None:
        classes = {
            CLASS_ID: row(CLASS_ID, is_class=True),
            PARENT_CLASS_ID: row(PARENT_CLASS_ID, is_class=True, color="#123456"),
        }
        by_id = {**classes, NODE_ID: row(NODE_ID, class_ids=(CLASS_ID, "missing-class"))}
        extends = {CLASS_ID: (PARENT_CLASS_ID,)}
        assert effective_node_color(by_id[NODE_ID], by_id, extends) == "#123456"

    def test_no_color_anywhere_is_none(self) -> None:
        by_id = {NODE_ID: row(NODE_ID, class_ids=(CLASS_ID,)), CLASS_ID: row(CLASS_ID, is_class=True)}
        assert effective_node_color(by_id[NODE_ID], by_id, {}) is None

    def test_class_color_walk_terminates_on_cycles(self) -> None:
        by_id = {CLASS_ID: row(CLASS_ID, is_class=True), PARENT_CLASS_ID: row(PARENT_CLASS_ID, is_class=True)}
        extends = {CLASS_ID: (PARENT_CLASS_ID,), PARENT_CLASS_ID: (CLASS_ID,)}
        assert effective_class_color(CLASS_ID, by_id, extends) is None


class TestRendersWithDocumentChrome:
    """The pure port of the store's ``_is_page_row`` rule — the tag picker
    lists pages only (tags ARE pages)."""

    def test_class_is_not_a_page(self) -> None:
        by_id = {CLASS_ID: row(CLASS_ID, is_class=True)}
        assert not renders_with_document_chrome(by_id[CLASS_ID], by_id, {})

    def test_parentless_node_is_a_page(self) -> None:
        by_id = {NODE_ID: row(NODE_ID)}
        assert renders_with_document_chrome(by_id[NODE_ID], by_id, {})

    def test_parentless_block_class_member_is_not_a_page(self) -> None:
        by_id = {
            NODE_ID: row(NODE_ID, class_ids=(CLASS_ID,)),
            CLASS_ID: row(CLASS_ID, is_class=True, parentless_role="block"),
        }
        assert not renders_with_document_chrome(by_id[NODE_ID], by_id, {})

    def test_parentless_role_demote_rides_extends(self) -> None:
        by_id = {
            NODE_ID: row(NODE_ID, class_ids=(CLASS_ID,)),
            CLASS_ID: row(CLASS_ID, is_class=True),
            PARENT_CLASS_ID: row(PARENT_CLASS_ID, is_class=True, parentless_role="block"),
        }
        extends = {CLASS_ID: (PARENT_CLASS_ID,)}
        assert not renders_with_document_chrome(by_id[NODE_ID], by_id, extends)

    def test_embedded_child_is_not_a_page(self) -> None:
        by_id = {PARENT_CLASS_ID: row(PARENT_CLASS_ID), NODE_ID: row(NODE_ID, parent_id=PARENT_CLASS_ID, is_embedded=True)}
        assert not renders_with_document_chrome(by_id[NODE_ID], by_id, {})

    def test_standalone_child_of_a_page_is_a_page(self) -> None:
        by_id = {PARENT_CLASS_ID: row(PARENT_CLASS_ID), NODE_ID: row(NODE_ID, parent_id=PARENT_CLASS_ID, is_embedded=False)}
        assert renders_with_document_chrome(by_id[NODE_ID], by_id, {})

    def test_standalone_child_of_an_embedded_block_is_not_a_page(self) -> None:
        block = row(CLASS_ID, parent_id=PARENT_CLASS_ID, is_embedded=True)
        node = row(NODE_ID, parent_id=CLASS_ID, is_embedded=False)
        by_id = {PARENT_CLASS_ID: row(PARENT_CLASS_ID), CLASS_ID: block, NODE_ID: node}
        assert not renders_with_document_chrome(node, by_id, {})

    def test_missing_parent_is_defensively_not_a_page(self) -> None:
        node = row(NODE_ID, parent_id="missing", is_embedded=False)
        assert not renders_with_document_chrome(node, {}, {})


class TestTagPills:
    """The header pill records: alphabetical by label, colors resolved,
    unknown tags skipped."""

    def test_pills_sort_alphabetically_casefolded_with_id_tiebreak(self) -> None:
        zebra = row(TAG_A_ID, content_plain="Zebra")
        apple = row(TAG_B_ID, content_plain="apple")
        by_id = {TAG_A_ID: zebra, TAG_B_ID: apple}
        pills = tag_pills((TAG_A_ID, TAG_B_ID), by_id, {}, label_for)
        assert [pill.tag_id for pill in pills] == [TAG_B_ID, TAG_A_ID]
        assert [pill.label for pill in pills] == ["apple", "Zebra"]

    def test_label_tiebreak_falls_to_id(self) -> None:
        first = row(TAG_A_ID, content_plain="same")
        second = row(TAG_B_ID, content_plain="Same")
        by_id = {TAG_A_ID: first, TAG_B_ID: second}
        pills = tag_pills((TAG_B_ID, TAG_A_ID), by_id, {}, label_for)
        assert [pill.tag_id for pill in pills] == [TAG_A_ID, TAG_B_ID]

    def test_unknown_tag_ids_are_skipped(self) -> None:
        by_id = {TAG_A_ID: row(TAG_A_ID, content_plain="Known")}
        pills = tag_pills(("missing", TAG_A_ID), by_id, {}, label_for)
        assert [pill.tag_id for pill in pills] == [TAG_A_ID]

    def test_own_color_resolves_to_hex_and_contrast(self) -> None:
        by_id = {TAG_A_ID: row(TAG_A_ID, content_plain="Tag", color="yellow", icon="🏷")}
        (pill,) = tag_pills((TAG_A_ID,), by_id, {}, label_for)
        assert pill.color == "#f3b816"
        assert pill.foreground == "#000000"
        assert pill.icon == "🏷"

    def test_class_chain_color_resolves_when_own_is_unset(self) -> None:
        by_id = {
            TAG_A_ID: row(TAG_A_ID, content_plain="Tag", class_ids=(CLASS_ID,)),
            CLASS_ID: row(CLASS_ID, is_class=True),
            PARENT_CLASS_ID: row(PARENT_CLASS_ID, is_class=True, color="#112233"),
        }
        (pill,) = tag_pills((TAG_A_ID,), by_id, {CLASS_ID: (PARENT_CLASS_ID,)}, label_for)
        assert pill.color == "#112233"
        assert pill.foreground == "#ffffff"

    def test_uncolored_tag_carries_no_surface(self) -> None:
        (pill,) = tag_pills((TAG_A_ID,), {TAG_A_ID: row(TAG_A_ID, content_plain="Tag")}, {}, label_for)
        assert pill.color is None
        assert pill.foreground is None


class TestTagAssignCandidates:
    """The add picker's filter: pages only, minus the node itself, minus
    its already-assigned tags; input order preserved."""

    def test_filter_composition(self) -> None:
        node = row(NODE_ID, tag_ids=(TAG_A_ID,))
        tag_a = row(TAG_A_ID, content_plain="Tag A")
        tag_b = row(TAG_B_ID, content_plain="Tag B")
        block = row(CLASS_ID, parent_id=NODE_ID, is_embedded=True)
        by_id = {NODE_ID: node, TAG_A_ID: tag_a, TAG_B_ID: tag_b, CLASS_ID: block}
        candidates = tag_assign_candidates([node, tag_b, block, tag_a], NODE_ID, by_id, {})
        assert candidates == [tag_b]

    def test_empty_input_yields_empty(self) -> None:
        assert tag_assign_candidates([], NODE_ID, {}, {}) == []


class TestTagWriteEnvelopes:
    """The OR-Set carrier pair: assign rides a re-issued ``object.create``
    over the node's own id; remove is the ``tag.unassign`` tombstone —
    the web ``assignTag``/``unassignTag`` shapes."""

    def test_assign_carrier_reissues_create_with_the_tag(self) -> None:
        content = json.dumps([{"type": "text", "text": "Hello"}])
        node = row(NODE_ID, color=None, content=content)
        envelope = tag_assign_envelope(
            workspace_id=WS,
            actor_id=ACTOR,
            device_id=DEVICE,
            node=node,
            tag_id=TAG_A_ID,
            clock=Clock("tag-ops-device"),
        )
        assert envelope.op_type == "object.create"
        assert envelope.workspace_id == WS
        assert envelope.actor_id == ACTOR
        assert envelope.payload == {
            "objectId": NODE_ID,
            "parentId": None,
            "contentAst": [{"type": "text", "text": "Hello"}],
            "tagIds": [TAG_A_ID],
        }
        assert list(envelope.affected_node_ids) == [NODE_ID]

    def test_assign_carrier_carries_the_parent_and_omits_absent_content(self) -> None:
        node = row(NODE_ID, parent_id=PARENT_CLASS_ID)
        envelope = tag_assign_envelope(
            workspace_id=WS,
            actor_id=ACTOR,
            device_id=DEVICE,
            node=node,
            tag_id=TAG_A_ID,
            clock=Clock("tag-ops-device"),
        )
        assert envelope.payload["parentId"] == PARENT_CLASS_ID
        assert "contentAst" not in envelope.payload

    def test_unassign_envelope_is_the_tombstone(self) -> None:
        envelope = tag_unassign_envelope(
            workspace_id=WS,
            actor_id=ACTOR,
            device_id=DEVICE,
            node_id=NODE_ID,
            tag_id=TAG_A_ID,
            clock=Clock("tag-ops-device"),
        )
        assert envelope.op_type == "tag.unassign"
        assert envelope.payload == {"objectId": NODE_ID, "tagId": TAG_A_ID}
        assert list(envelope.affected_node_ids) == [NODE_ID]
