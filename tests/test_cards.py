"""Tests for the pure memorization-card reads (``notees_gtk.core.cards``).

Headless like the module under test: synthetic ``NodeRow`` / ``CardReview`` /
``EffectiveProperty`` fixtures, no store and no GTK.
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

import pytest

from notees_gtk.core.cards import (
    CardBucketRow,
    build_review_queue,
    card_due_day,
    card_members,
    card_srs_state,
    partition_cards_into_buckets,
)
from notees_gtk.core.protocol.dates import day_node_id
from notees_gtk.core.protocol.features import card_property_uuid, system_class_uuid
from notees_gtk.core.srs import UNSEEN_CARD_STATE
from notees_gtk.data.store import CardReview, EffectiveProperty, NodeRow

WS = "ws-cards"
TODAY = "2026-10-28"

DUE_SCHEMA = card_property_uuid("cardDue")
CARD_CLASS = system_class_uuid("card")
YEAR_CLASS = system_class_uuid("year")

DAY_2026_10_27 = day_node_id("2026-10-27")
DAY_2026_10_28 = day_node_id("2026-10-28")
DAY_2026_10_30 = day_node_id("2026-10-30")
DAY_2026_11_02 = day_node_id("2026-11-02")

SUBCLASS_ID = str(uuid5(NAMESPACE_URL, "notees-gtk/cards/subclass-of-card"))
OTHER_CLASS_ID = str(uuid5(NAMESPACE_URL, "notees-gtk/cards/other-class"))


def node_row(
    node_id: str,
    *,
    class_ids: tuple[str, ...] = (),
) -> NodeRow:
    """A minimal node row for the pure filters."""
    return NodeRow(
        id=node_id,
        workspace_id=WS,
        parent_id=None,
        is_class=False,
        is_embedded=False,
        name=None,
        class_ids=class_ids,
        tag_ids=(),
        icon=None,
        color=None,
        cover_asset_id=None,
        banner_asset_id=None,
        aliased_node_id=None,
        description=None,
        list_style=None,
        parentless_role=None,
        is_active=True,
        content=None,
        content_plain="",
    )


def effective_prop(schema_id: str, value: object, *, idx: int = 0) -> EffectiveProperty:
    """A minimal effective-property row carrying just what the reads touch."""
    return EffectiveProperty(
        property_schema_id=schema_id,
        idx=idx,
        element_id=f"elem-{schema_id}-{idx}",
        schema=None,
        value=value,
        metadata=None,
        source="authored",
        bound_by=None,
        hide_when_empty=None,
        display=None,
    )


def review(state_after: dict[str, object], *, grade: str = "good", algorithm: str = "fsrs") -> CardReview:
    """One review-history row; only ``state_after`` matters to the read."""
    return CardReview(
        session_id=str(uuid5(NAMESPACE_URL, f"notees-gtk/cards/session-{len(state_after)}")),
        grade=grade,
        algorithm=algorithm,
        state_before={},
        state_after=state_after,
        reviewed_at="2026-10-28T10:00:00+00:00",
    )


class TestCardDueDay:
    def test_point_value_reads_its_day(self) -> None:
        props = [effective_prop(DUE_SCHEMA, {"nodeId": DAY_2026_10_30})]
        assert card_due_day(props, DUE_SCHEMA) == "2026-10-30"

    def test_point_value_with_time_still_reads_its_day(self) -> None:
        props = [effective_prop(DUE_SCHEMA, {"nodeId": DAY_2026_10_30, "time": "09:30"})]
        assert card_due_day(props, DUE_SCHEMA) == "2026-10-30"

    def test_range_value_reads_start_day(self) -> None:
        props = [effective_prop(DUE_SCHEMA, {"start": {"nodeId": DAY_2026_10_28}, "end": {"nodeId": DAY_2026_11_02}})]
        assert card_due_day(props, DUE_SCHEMA) == "2026-10-28"

    def test_open_start_range_falls_back_to_end_day(self) -> None:
        props = [effective_prop(DUE_SCHEMA, {"start": None, "end": {"nodeId": DAY_2026_11_02}})]
        assert card_due_day(props, DUE_SCHEMA) == "2026-11-02"

    def test_missing_property_reads_none(self) -> None:
        assert card_due_day([], DUE_SCHEMA) is None

    def test_other_schema_id_is_ignored(self) -> None:
        props = [effective_prop(card_property_uuid("cardReps"), 3)]
        assert card_due_day(props, DUE_SCHEMA) is None

    @pytest.mark.parametrize(
        "value",
        [
            None,
            42,
            "not-a-slot",
            {"nodeId": "not-a-date-uuid"},
            {"start": {"nodeId": "not-a-date-uuid"}, "end": None},
            {"nodeId": day_node_id("2026-10-30")[:24] + "0000"},  # year-precision id, not a day
        ],
    )
    def test_malformed_values_read_none(self, value: object) -> None:
        props = [effective_prop(DUE_SCHEMA, value)]
        assert card_due_day(props, DUE_SCHEMA) is None


class TestPartition:
    def test_three_way_partition_and_sort(self) -> None:
        rows = [
            CardBucketRow("a", "2026-10-26"),  # overdue
            CardBucketRow("b", None),  # fresh
            CardBucketRow("c", "2026-10-30"),  # later
            CardBucketRow("d", TODAY),  # due today
            CardBucketRow("e", None),  # fresh
            CardBucketRow("f", "2026-10-24"),  # most overdue
        ]
        buckets = partition_cards_into_buckets(rows, TODAY)
        assert [row.id for row in buckets.due] == ["f", "a", "d"]
        assert [row.id for row in buckets.fresh] == ["b", "e"]
        assert [row.id for row in buckets.later] == ["c"]

    def test_day_tiebreaks_by_id(self) -> None:
        rows = [
            CardBucketRow("zz", "2026-10-26"),
            CardBucketRow("aa", "2026-10-26"),
            CardBucketRow("mm", "2026-10-28"),
        ]
        buckets = partition_cards_into_buckets(rows, TODAY)
        assert [row.id for row in buckets.due] == ["aa", "zz", "mm"]

    def test_fresh_keeps_input_order(self) -> None:
        rows = [CardBucketRow(id_, None) for id_ in ("z", "a", "m")]
        buckets = partition_cards_into_buckets(rows, TODAY)
        assert [row.id for row in buckets.fresh] == ["z", "a", "m"]


class TestReviewQueue:
    def test_due_then_fresh_future_dropped(self) -> None:
        rows = [
            CardBucketRow("future", "2026-11-05"),
            CardBucketRow("fresh-1", None),
            CardBucketRow("overdue", "2026-10-20"),
            CardBucketRow("fresh-2", None),
            CardBucketRow("due-today", TODAY),
            CardBucketRow("future-2", "2026-12-01"),
        ]
        assert build_review_queue(rows, TODAY) == ["overdue", "due-today", "fresh-1", "fresh-2"]

    def test_empty_handoff_yields_empty_queue(self) -> None:
        assert build_review_queue([], TODAY) == []


class TestCardSrsState:
    def test_empty_journal_is_unseen(self) -> None:
        assert card_srs_state([]) == UNSEEN_CARD_STATE

    def test_latest_row_wins(self) -> None:
        reviews = [
            review({"due": "2026-10-29", "reps": 1, "interval": 1, "ease": 2.5, "difficulty": None, "stability": None}),
            review({"due": "2026-11-04", "reps": 2, "interval": 6, "ease": 2.5, "difficulty": None, "stability": None}),
        ]
        state = card_srs_state(reviews)
        assert state.due == "2026-11-04"
        assert state.reps == 2
        assert state.interval == 6
        assert state.ease == 2.5

    def test_fsrs_fields_parse(self) -> None:
        reviews = [
            review(
                {
                    "due": "2026-10-28",
                    "reps": 1,
                    "interval": 0,
                    "ease": None,
                    "difficulty": 7.1949,
                    "stability": 3.173,
                }
            )
        ]
        state = card_srs_state(reviews)
        assert state.ease is None
        assert state.difficulty == 7.1949
        assert state.stability == 3.173

    def test_fractional_reps_fail_loud(self) -> None:
        reviews = [review({"due": TODAY, "reps": 1.5, "interval": 1, "ease": None, "difficulty": None, "stability": None})]
        with pytest.raises(ValueError, match="reps"):
            card_srs_state(reviews)


class TestCardMembers:
    def test_direct_card_membership(self) -> None:
        rows = [
            node_row("card-1", class_ids=(CARD_CLASS,)),
            node_row("plain"),
        ]
        assert [row.id for row in card_members(rows, {})] == ["card-1"]

    def test_subclass_membership_is_extends_aware(self) -> None:
        extends = {SUBCLASS_ID: (CARD_CLASS,)}
        rows = [
            node_row("sub", class_ids=(SUBCLASS_ID,)),
            node_row("other", class_ids=(OTHER_CLASS_ID,)),
        ]
        assert [row.id for row in card_members(rows, extends)] == ["sub"]

    def test_transitive_subclass_membership(self) -> None:
        grandchild = str(uuid5(NAMESPACE_URL, "notees-gtk/cards/grandchild"))
        extends = {SUBCLASS_ID: (CARD_CLASS,), grandchild: (SUBCLASS_ID,)}
        rows = [node_row("deep", class_ids=(grandchild,))]
        assert [row.id for row in card_members(rows, extends)] == ["deep"]

    def test_class_rows_can_be_members_too(self) -> None:
        rows = [node_row("class-row", class_ids=(CARD_CLASS,))]
        assert [row.id for row in card_members(rows, {})] == ["class-row"]

    def test_input_order_preserved(self) -> None:
        rows = [node_row("second", class_ids=(CARD_CLASS,)), node_row("first", class_ids=(CARD_CLASS,))]
        assert [row.id for row in card_members(rows, {})] == ["second", "first"]

    def test_year_system_class_is_not_a_card(self) -> None:
        rows = [node_row("year-node", class_ids=(YEAR_CLASS,))]
        assert card_members(rows, {}) == []

    def test_explicit_card_class_uuid_parameter(self) -> None:
        rows = [node_row("x", class_ids=(OTHER_CLASS_ID,))]
        assert [row.id for row in card_members(rows, {}, card_class_uuid=OTHER_CLASS_ID)] == ["x"]
