"""Tests for the pure memorization-card write envelopes (``notees_gtk.ui.card_ops``).

Headless like the module under test: the builders take plain ids, states,
and synthetic store rows — no GTK, no live store.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from notees_gtk.core.protocol.clock import Clock
from notees_gtk.core.protocol.dates import chain_node_ids, day_node_id
from notees_gtk.core.protocol.features import card_property_uuid
from notees_gtk.core.srs import UNSEEN_CARD_STATE, CardSrsState
from notees_gtk.data.store import EffectiveProperty, NodeRow
from notees_gtk.ui.card_ops import (
    ReviewSessionState,
    card_review_envelope,
    date_chain_envelopes,
    review_property_envelopes,
)

WS = "ws-card-ops"
ACTOR = "actor-1"
DEVICE = "card-ops-test-device"
CARD_ID = str(uuid5(NAMESPACE_URL, "notees-gtk/card-ops/card"))
SESSION_ID = str(uuid4())

UNSEEN = UNSEEN_CARD_STATE

DAY = "2026-10-28"
CHAINS = chain_node_ids(DAY)
YEAR_ID = CHAINS["year"]
MONTH_ID = CHAINS["month"]
DAY_ID = CHAINS["day"]

YEAR_CLASS = "00000000-0000-0000-0001-000000000003"
MONTH_CLASS = "00000000-0000-0000-0001-000000000004"
DAY_CLASS = "00000000-0000-0000-0001-000000000005"

CLOCK = Clock(DEVICE)


def node_row(node_id: str, *, is_active: bool = True) -> NodeRow:
    """A minimal node row for the date-chain arms."""
    return NodeRow(
        id=node_id,
        workspace_id=WS,
        parent_id=None,
        is_class=False,
        is_embedded=False,
        name=None,
        class_ids=(),
        tag_ids=(),
        icon=None,
        color=None,
        cover_asset_id=None,
        banner_asset_id=None,
        aliased_node_id=None,
        description=None,
        list_style=None,
        parentless_role=None,
        is_active=is_active,
        content=None,
        content_plain="",
    )


def effective_prop(schema_id: str, value: object, *, idx: int = 0) -> EffectiveProperty:
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


def make_reads(rows: dict[str, NodeRow]) -> Callable[[str], NodeRow | None]:
    """The StoreReads seam over a fixture table (missing → None)."""

    def read(node_id: str) -> NodeRow | None:
        return rows.get(node_id)

    return read


class TestDateChainEnvelopes:
    def test_all_missing_emits_three_creates_in_chain_order(self) -> None:
        envelopes = date_chain_envelopes(
            workspace_id=WS,
            actor_id=ACTOR,
            device_id=DEVICE,
            iso_day=DAY,
            store_reads=make_reads({}),
            clock=CLOCK,
        )
        assert [env.op_type for env in envelopes] == ["object.create"] * 3
        year, month, day = (env.payload for env in envelopes)
        assert year == {
            "objectId": YEAR_ID,
            "isEmbedded": False,
            "parentId": None,
            "classIds": [YEAR_CLASS],
            "contentAst": [{"type": "text", "text": "20260000"}],
        }
        assert month == {
            "objectId": MONTH_ID,
            "isEmbedded": False,
            "parentId": YEAR_ID,
            "classIds": [MONTH_CLASS],
            "contentAst": [{"type": "text", "text": "20261000"}],
        }
        assert day == {
            "objectId": DAY_ID,
            "isEmbedded": False,
            "parentId": MONTH_ID,
            "classIds": [DAY_CLASS],
            "contentAst": [{"type": "text", "text": "20261028"}],
        }

    def test_all_live_emits_nothing(self) -> None:
        envelopes = date_chain_envelopes(
            workspace_id=WS,
            actor_id=ACTOR,
            device_id=DEVICE,
            iso_day=DAY,
            store_reads=make_reads({YEAR_ID: node_row(YEAR_ID), MONTH_ID: node_row(MONTH_ID), DAY_ID: node_row(DAY_ID)}),
            clock=CLOCK,
        )
        assert envelopes == []

    def test_trashed_day_emits_restore_only(self) -> None:
        envelopes = date_chain_envelopes(
            workspace_id=WS,
            actor_id=ACTOR,
            device_id=DEVICE,
            iso_day=DAY,
            store_reads=make_reads(
                {YEAR_ID: node_row(YEAR_ID), MONTH_ID: node_row(MONTH_ID), DAY_ID: node_row(DAY_ID, is_active=False)}
            ),
            clock=CLOCK,
        )
        assert len(envelopes) == 1
        assert envelopes[0].op_type == "object.restore"
        assert envelopes[0].payload == {"objectId": DAY_ID}
        assert list(envelopes[0].affected_node_ids) == [DAY_ID]

    def test_mixed_chain_create_restore_and_noop(self) -> None:
        envelopes = date_chain_envelopes(
            workspace_id=WS,
            actor_id=ACTOR,
            device_id=DEVICE,
            iso_day=DAY,
            store_reads=make_reads(
                {YEAR_ID: node_row(YEAR_ID), MONTH_ID: node_row(MONTH_ID, is_active=False)}
            ),
            clock=CLOCK,
        )
        assert [env.op_type for env in envelopes] == ["object.restore", "object.create"]
        assert envelopes[0].payload == {"objectId": MONTH_ID}
        assert envelopes[1].payload["objectId"] == DAY_ID
        assert envelopes[1].payload["parentId"] == MONTH_ID

    @pytest.mark.parametrize("iso_day", ["2026-13-40", "2026-10-28T09:30", "not-a-date", "2026-2-5"])
    def test_malformed_due_day_fails_loud(self, iso_day: str) -> None:
        with pytest.raises(ValueError, match="invalid ISO date"):
            date_chain_envelopes(
                workspace_id=WS,
                actor_id=ACTOR,
                device_id=DEVICE,
                iso_day=iso_day,
                store_reads=make_reads({}),
                clock=CLOCK,
            )


SM2_STATE = CardSrsState(due="2026-10-29", reps=1, interval=1, ease=2.5, difficulty=None, stability=None)
FSRS_STATE = CardSrsState(due="2026-10-28", reps=1, interval=0, ease=None, difficulty=7.1949, stability=3.173)


class TestReviewPropertyEnvelopes:
    def test_sm2_writes_ease_never_difficulty_or_stability(self) -> None:
        envelopes = review_property_envelopes(
            workspace_id=WS,
            actor_id=ACTOR,
            device_id=DEVICE,
            card_id=CARD_ID,
            state_after=SM2_STATE,
            algorithm="sm2",
            props=[],
            clock=CLOCK,
        )
        assert [env.op_type for env in envelopes] == ["property.set"] * 4
        by_schema = {env.payload["propertySchemaId"]: env.payload for env in envelopes}
        assert set(by_schema) == {
            card_property_uuid("cardDue"),
            card_property_uuid("cardReps"),
            card_property_uuid("cardInterval"),
            card_property_uuid("cardEase"),
        }
        assert by_schema[card_property_uuid("cardDue")]["value"] == {"nodeId": day_node_id("2026-10-29")}
        assert by_schema[card_property_uuid("cardReps")]["value"] == 1
        assert by_schema[card_property_uuid("cardInterval")]["value"] == 1
        assert by_schema[card_property_uuid("cardEase")]["value"] == 2.5
        for payload in by_schema.values():
            assert payload["objectId"] == CARD_ID
            assert payload["idx"] == 0  # the row's absent read → the taskCycle default
        assert all(list(env.affected_node_ids) == [CARD_ID] for env in envelopes)

    def test_fsrs_writes_difficulty_and_stability_never_ease(self) -> None:
        envelopes = review_property_envelopes(
            workspace_id=WS,
            actor_id=ACTOR,
            device_id=DEVICE,
            card_id=CARD_ID,
            state_after=FSRS_STATE,
            algorithm="fsrs",
            props=[],
            clock=CLOCK,
        )
        by_schema = {env.payload["propertySchemaId"]: env.payload for env in envelopes}
        assert set(by_schema) == {
            card_property_uuid("cardDue"),
            card_property_uuid("cardReps"),
            card_property_uuid("cardInterval"),
            card_property_uuid("cardDifficulty"),
            card_property_uuid("cardStability"),
        }
        assert card_property_uuid("cardEase") not in by_schema
        assert by_schema[card_property_uuid("cardDifficulty")]["value"] == 7.1949
        assert by_schema[card_property_uuid("cardStability")]["value"] == 3.173

    def test_existing_idx_addresses_the_row(self) -> None:
        props = [
            effective_prop(card_property_uuid("cardDue"), {"nodeId": day_node_id("2026-10-27")}, idx=2),
            effective_prop(card_property_uuid("cardReps"), 0, idx=1),
        ]
        envelopes = review_property_envelopes(
            workspace_id=WS,
            actor_id=ACTOR,
            device_id=DEVICE,
            card_id=CARD_ID,
            state_after=SM2_STATE,
            algorithm="sm2",
            props=props,
            clock=CLOCK,
        )
        by_schema = {env.payload["propertySchemaId"]: env.payload for env in envelopes}
        assert by_schema[card_property_uuid("cardDue")]["idx"] == 2
        assert by_schema[card_property_uuid("cardReps")]["idx"] == 1
        assert by_schema[card_property_uuid("cardInterval")]["idx"] == 0
        assert by_schema[card_property_uuid("cardEase")]["idx"] == 0

    def test_missing_due_day_fails_loud(self) -> None:
        state = CardSrsState(due=None, reps=1, interval=1, ease=2.5, difficulty=None, stability=None)
        with pytest.raises(ValueError, match="due day"):
            review_property_envelopes(
                workspace_id=WS,
                actor_id=ACTOR,
                device_id=DEVICE,
                card_id=CARD_ID,
                state_after=state,
                algorithm="sm2",
                props=[],
                clock=CLOCK,
            )

    def test_sm2_without_ease_fails_loud(self) -> None:
        state = CardSrsState(due="2026-10-29", reps=1, interval=1, ease=None, difficulty=None, stability=None)
        with pytest.raises(ValueError, match="ease"):
            review_property_envelopes(
                workspace_id=WS,
                actor_id=ACTOR,
                device_id=DEVICE,
                card_id=CARD_ID,
                state_after=state,
                algorithm="sm2",
                props=[],
                clock=CLOCK,
            )

    def test_fsrs_with_a_null_field_fails_loud(self) -> None:
        state = CardSrsState(due="2026-10-28", reps=1, interval=0, ease=None, difficulty=7.1949, stability=None)
        with pytest.raises(ValueError, match="difficulty AND stability"):
            review_property_envelopes(
                workspace_id=WS,
                actor_id=ACTOR,
                device_id=DEVICE,
                card_id=CARD_ID,
                state_after=state,
                algorithm="fsrs",
                props=[],
                clock=CLOCK,
            )


class TestCardReviewEnvelope:
    def test_payload_matches_the_wire_shape_verbatim(self) -> None:
        envelope = card_review_envelope(
            workspace_id=WS,
            actor_id=ACTOR,
            device_id=DEVICE,
            card_id=CARD_ID,
            grade="good",
            algorithm="sm2",
            state_before=CardSrsState(due=None, reps=0, interval=0, ease=None, difficulty=None, stability=None),
            state_after=SM2_STATE,
            session_id=SESSION_ID,
            reviewed_at="2026-10-28T10:15:00+00:00",
            clock=CLOCK,
        )
        assert envelope.op_type == "card.review"
        assert list(envelope.affected_node_ids) == [CARD_ID]
        # The strict CardReviewPayload accepts the payload as authored, and
        # the state records ride verbatim (ints stay ints on the wire).
        assert envelope.payload == {
            "cardId": CARD_ID,
            "grade": "good",
            "algorithm": "sm2",
            "stateBefore": {
                "due": None,
                "reps": 0,
                "interval": 0,
                "ease": None,
                "difficulty": None,
                "stability": None,
            },
            "stateAfter": {
                "due": "2026-10-29",
                "reps": 1,
                "interval": 1,
                "ease": 2.5,
                "difficulty": None,
                "stability": None,
            },
            "reviewedAt": "2026-10-28T10:15:00+00:00",
            "sessionId": SESSION_ID,
        }
        # model_dump is the serialization the outbox persists — aliases and all.
        dumped = envelope.model_dump(mode="json", by_alias=True)
        assert dumped["opType"] == "card.review"
        assert dumped["payload"]["stateAfter"]["ease"] == 2.5

    def test_fsrs_state_records_ride_verbatim(self) -> None:
        envelope = card_review_envelope(
            workspace_id=WS,
            actor_id=ACTOR,
            device_id=DEVICE,
            card_id=CARD_ID,
            grade="again",
            algorithm="fsrs",
            state_before=FSRS_STATE,
            state_after=FSRS_STATE,
            session_id=SESSION_ID,
            reviewed_at="2026-10-28T10:16:00+00:00",
            clock=CLOCK,
        )
        assert envelope.payload["stateAfter"] == {
            "due": "2026-10-28",
            "reps": 1,
            "interval": 0,
            "ease": None,
            "difficulty": 7.1949,
            "stability": 3.173,
        }


class TestReviewSessionState:
    def test_fresh_session_starts_on_the_first_card_unrevealed(self) -> None:
        state = ReviewSessionState(queue=("a", "b", "c"))
        assert state.current_card() == "a"
        assert state.revealed is False
        assert state.finished is False
        assert (state.reviewed, state.lapses) == (0, 0)

    def test_reveal_flips_and_grade_advances(self) -> None:
        state = ReviewSessionState(queue=("a", "b"))
        state.reveal()
        assert state.revealed is True
        state.grade(
            "good",
            CardSrsState(due=None, reps=0, interval=0, ease=None, difficulty=None, stability=None),
        )
        assert state.reviewed == 1
        assert state.lapses == 0
        assert state.revealed is False
        assert state.current_card() == "b"

    def test_grade_before_reveal_fails_loud(self) -> None:
        state = ReviewSessionState(queue=("a",))
        with pytest.raises(ValueError, match="before the reveal"):
            state.grade("good", UNSEEN)

    def test_double_reveal_fails_loud(self) -> None:
        state = ReviewSessionState(queue=("a",))
        state.reveal()
        with pytest.raises(ValueError, match="already revealed"):
            state.reveal()

    def test_lapse_counts_only_again_on_a_reviewed_card(self) -> None:
        state = ReviewSessionState(queue=("a", "b", "c", "d"))
        reviewed = CardSrsState(due="2026-10-20", reps=3, interval=9, ease=2.5, difficulty=None, stability=None)
        for grade, before in (
            ("again", reviewed),  # lapse: again on reps > 0
            ("again", UNSEEN),  # not a lapse: never-reviewed card
            ("good", reviewed),  # not a lapse: not "again"
            ("again", reviewed),  # lapse
        ):
            state.reveal()
            state.grade(grade, before)
        assert state.reviewed == 4
        assert state.lapses == 2
        assert state.finished is True

    def test_finished_session_fails_loud(self) -> None:
        state = ReviewSessionState(queue=("a",))
        state.reveal()
        state.grade("good", UNSEEN)
        assert state.finished is True
        assert state.current_card() is None
        with pytest.raises(ValueError, match="finished"):
            state.reveal()
        with pytest.raises(ValueError, match="finished"):
            state.grade("good", UNSEEN)
        with pytest.raises(ValueError, match="finished"):
            state.skip()

    def test_skip_advances_without_counting(self) -> None:
        state = ReviewSessionState(queue=("gone", "b"))
        state.skip()
        assert (state.reviewed, state.lapses) == (0, 0)
        assert state.current_card() == "b"
        assert state.revealed is False
