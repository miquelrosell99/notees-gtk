"""Tests for the SM-2 / FSRS-5 scheduler port (``notees_gtk.core.srs``).

Mirrors ``packages/domain/test/srs.test.ts`` case-for-case (same ``TODAY``,
same expectations), plus exact-float vectors for the FSRS-5 recurrence arms —
second review on an initialized card (late review, and a lapse on a reviewed
card). The expected floats were produced by running the TypeScript reference
(``packages/domain/src/srs.ts``) itself and are asserted with ``==``
(bit-for-byte parity is the contract; any ULP drift is a port bug, not a
test-tolerance question).
"""

from __future__ import annotations

from notees_gtk.core.srs import (
    UNSEEN_CARD_STATE,
    CardSrsState,
    add_days,
    day_diff,
    is_card_due,
    next_card_state,
)

TODAY = "2026-10-28"


def reviewed_state(**overrides: object) -> CardSrsState:
    """A mutable-copy helper: UNSEEN plus the given field overrides."""
    base = {
        "due": None,
        "reps": 0,
        "interval": 0,
        "ease": None,
        "difficulty": None,
        "stability": None,
    }
    base.update(overrides)
    return CardSrsState(**base)  # type: ignore[arg-type]


class TestSm2:
    def test_first_good_reps_1_interval_1_due_tomorrow_ease_2_5(self) -> None:
        state = next_card_state(UNSEEN_CARD_STATE, "good", "sm2", TODAY)
        assert state == CardSrsState(
            due="2026-10-29", reps=1, interval=1, ease=2.5, difficulty=None, stability=None
        )

    def test_lapse_resets_reps_and_interval_floors_ease_at_1_3(self) -> None:
        reviewed = reviewed_state(due="2026-10-28", reps=5, interval=21, ease=1.35)
        state = next_card_state(reviewed, "again", "sm2", TODAY)
        assert state.reps == 0
        assert state.interval == 1
        assert state.ease == 1.3

    def test_second_good_uses_fixed_interval_6(self) -> None:
        first = next_card_state(UNSEEN_CARD_STATE, "good", "sm2", TODAY)
        second = next_card_state(first, "good", "sm2", first.due or TODAY)
        assert second == CardSrsState(
            due="2026-11-04", reps=2, interval=6, ease=2.5, difficulty=None, stability=None
        )

    def test_third_review_multiplies_interval_by_ease(self) -> None:
        first = next_card_state(UNSEEN_CARD_STATE, "good", "sm2", TODAY)
        second = next_card_state(first, "good", "sm2", first.due or TODAY)
        third = next_card_state(second, "easy", "sm2", second.due or TODAY)
        # round(6 * 2.6) = 15; easy bumps ease 2.5 -> 2.6 (exact-float vector
        # from the TS reference).
        assert third == CardSrsState(
            due="2026-11-19", reps=3, interval=15, ease=2.6, difficulty=None, stability=None
        )

    def test_first_hard_ease_delta_is_exact_float(self) -> None:
        state = next_card_state(UNSEEN_CARD_STATE, "hard", "sm2", TODAY)
        assert state.ease == 2.3600000000000003  # TS reference vector, exact ==

    def test_interval_rounding_is_half_up_not_bankers(self) -> None:
        """Math.round parity: an exact odd ``.5`` rounds UP. ``121 * 0.5`` is
        exactly ``60.5`` in IEEE doubles (0.5 is a power of two — asserted
        here so the vector cannot silently stop pinning the half-up case);
        Python's banker's ``round(60.5)`` is 60 while the JS reference's
        ``Math.round(60.5)`` is 61."""
        assert 121 * 0.5 == 60.5
        assert round(60.5) == 60  # the Python pitfall this vector pins
        state = reviewed_state(due="2026-10-01", reps=2, interval=121, ease=0.5)
        graded = next_card_state(state, "good", "sm2", TODAY)
        assert graded.interval == 61
        assert graded.due == "2026-12-28"


class TestFsrs:
    def test_first_good_initializes_difficulty_and_stability(self) -> None:
        state = next_card_state(UNSEEN_CARD_STATE, "good", "fsrs", TODAY)
        assert state.ease is None  # sm2 field untouched
        assert state.difficulty is not None
        assert state.stability is not None and state.stability > 0
        assert state.due == TODAY  # same-day re-view for the first fsrs step

    def test_mid_deck_switch_initializes_fsrs_fields_keeps_ease(self) -> None:
        sm2 = next_card_state(UNSEEN_CARD_STATE, "good", "sm2", TODAY)
        fsrs = next_card_state(
            reviewed_state(due=sm2.due, reps=sm2.reps, interval=sm2.interval, ease=sm2.ease),
            "good",
            "fsrs",
            TODAY,
        )
        assert fsrs.ease == 2.5  # stale sm2 field preserved, not corrupted
        assert fsrs.stability is not None

    def test_first_again_reps_zero(self) -> None:
        state = next_card_state(UNSEEN_CARD_STATE, "again", "fsrs", TODAY)
        assert state.reps == 0
        assert state.due == TODAY

    def test_second_review_late_recurrence_exact_float(self) -> None:
        first = next_card_state(UNSEEN_CARD_STATE, "good", "fsrs", TODAY)
        # Reviewed 5 days after the same-day due: the elapsed slip drives
        # retrievability below 1, so the recurrence arm actually moves S.
        state = next_card_state(first, "hard", "fsrs", "2026-11-02")
        assert state == CardSrsState(
            due="2026-11-10", reps=2, interval=8, ease=None, difficulty=8.6553, stability=8.161333832200427
        )

    def test_lapse_on_reviewed_card_exact_float(self) -> None:
        first = next_card_state(UNSEEN_CARD_STATE, "good", "fsrs", TODAY)
        on_time_second = next_card_state(first, "good", "fsrs", first.due or TODAY)
        # One day after the second review's due day, graded "again": the
        # lapse arm w11·D'^(−w12)·((S+1)^w13 − 1)·e^((1−R)·w14) — exact-float
        # vector from the TS reference.
        state = next_card_state(on_time_second, "again", "fsrs", "2026-11-01")
        assert state == CardSrsState(
            due="2026-11-02", reps=0, interval=1, ease=None, difficulty=10, stability=1.0475201239225211
        )


class TestIsCardDue:
    def test_unseen_due_today_and_future(self) -> None:
        assert is_card_due(UNSEEN_CARD_STATE, TODAY) is True
        assert is_card_due(reviewed_state(due="2026-10-29"), TODAY) is False
        assert is_card_due(reviewed_state(due=TODAY), TODAY) is True

    def test_overdue_is_due(self) -> None:
        assert is_card_due(reviewed_state(due="2026-10-01"), TODAY) is True


class TestDayMath:
    def test_add_days_crosses_month_and_year(self) -> None:
        assert add_days("2026-10-28", 4) == "2026-11-01"
        assert add_days("2026-12-31", 1) == "2027-01-01"
        assert add_days("2024-02-28", 1) == "2024-02-29"  # leap day

    def test_day_diff_counts_whole_days(self) -> None:
        assert day_diff("2026-10-28", "2026-11-01") == 4
        assert day_diff("2026-10-28", "2026-10-28") == 0
        assert day_diff("2026-11-01", "2026-10-28") == -4
