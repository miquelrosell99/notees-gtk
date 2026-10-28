"""Spaced-repetition scheduling for memorization cards — the pure scheduler.

Port of ``packages/domain/src/srs.ts`` (the Notees monorepo's SM-2 / FSRS-5
scheduler) function-for-function. Two algorithms, one state shape:

- **SM-2** (the Anki classic): ease factor + fixed first intervals
  (1, 6, then ``interval * ease``), lapse resets to reps 0 / interval 1
  with the ease floor at 1.3.
- **FSRS-5** (the default-parameter set, no fitting): stability ``S`` in
  days + difficulty ``D`` in [1, 10]; the first review initializes both from
  the weight vector, subsequent reviews move them through the published
  FSRS-5 recurrence.

One :class:`CardSrsState` carries BOTH algorithms' fields; the active
algorithm's fields move while the other's stay byte-for-byte stale (the
mid-deck algorithm-switch contract: an sm2-reviewed card graded under fsrs
initializes its fsrs fields from defaults and never touches the stale
``ease``, and symmetrically).

All date math is whole-day ``YYYY-MM-DD`` arithmetic over
:class:`datetime.date` — identical to the TS ``Date.UTC`` + ``86400_000``
slicing, so day arithmetic never crosses a DST boundary. Python floats are
IEEE-754 doubles like JS numbers; the recurrence is expected to match the
TS reference bit-for-bit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from math import exp, floor
from typing import Literal

__all__ = [
    "FSRS_D_MAX",
    "FSRS_D_MIN",
    "FSRS_RATING",
    "FSRS_W",
    "SM2_EASE_FLOOR",
    "SM2_INITIAL_EASE",
    "SM2_QUALITY",
    "UNSEEN_CARD_STATE",
    "CardGrade",
    "CardSrsState",
    "SrsAlgorithm",
    "add_days",
    "day_diff",
    "is_card_due",
    "next_card_state",
    "next_fsrs_state",
    "next_sm2_state",
    "retrievability",
]

#: The four review grades (pinned by the ``card.review`` wire payload).
CardGrade = Literal["again", "hard", "good", "easy"]

#: The spaced-repetition scheduler for the memorization-cards surface.
SrsAlgorithm = Literal["sm2", "fsrs"]


@dataclass(frozen=True)
class CardSrsState:
    """One SRS state record — the wire ``CardSrsState`` shape verbatim.

    Attributes:
        due: ``YYYY-MM-DD``; ``None`` = unseen.
        reps: Successful-repetition count (a lapse resets to 0).
        interval: Scheduled interval in days.
        ease: SM-2 ease factor; ``None`` until the sm2 algorithm first moves.
        difficulty: FSRS difficulty D in [1, 10]; ``None`` until fsrs moves.
        stability: FSRS stability S in days; ``None`` until fsrs moves.
    """

    due: str | None
    reps: int
    interval: int
    ease: float | None
    difficulty: float | None
    stability: float | None


#: The state of a card that has never been reviewed.
UNSEEN_CARD_STATE = CardSrsState(
    due=None, reps=0, interval=0, ease=None, difficulty=None, stability=None
)

#: SM-2 grade → Anki quality: again 1, hard 3, good 4, easy 5.
SM2_QUALITY: dict[CardGrade, int] = {"again": 1, "hard": 3, "good": 4, "easy": 5}

#: SM-2 initial ease (a card arriving from fsrs or unseen starts here).
SM2_INITIAL_EASE = 2.5

#: SM-2 ease floor — the lapse decrement never goes below.
SM2_EASE_FLOOR = 1.3

#: FSRS-5 default parameters (fixed; fitting from card_review history is a
#: follow-up) — the 19-entry ``w`` vector, ``packages/domain/src/srs.ts``.
FSRS_W: tuple[float, ...] = (
    0.40255,
    1.18385,
    3.173,
    15.69105,
    7.1949,
    0.5345,
    1.4604,
    0.0046,
    1.54575,
    0.1192,
    1.01925,
    1.9395,
    0.11,
    0.29605,
    2.2698,
    0.2315,
    2.9898,
    0.51655,
    0.6621,
)

#: FSRS grade → rating: again 1, hard 2, good 3, easy 4.
FSRS_RATING: dict[CardGrade, int] = {"again": 1, "hard": 2, "good": 3, "easy": 4}

#: FSRS difficulty bounds.
FSRS_D_MIN = 1
FSRS_D_MAX = 10


def _w(index: int) -> float:
    return FSRS_W[index]


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, value))


def _round_half_up(value: float) -> int:
    """The ``Math.round`` port — half-up, NOT Python's banker's ``round()``:
    an exact odd ``.5`` rounds UP here (``round(60.5) == 60`` in Python,
    ``Math.round(60.5) == 61`` in JS). The schedules only produce
    non-negative intervals, so ``floor(x + 0.5)`` matches the reference
    bit-for-bit."""
    return floor(value + 0.5)


def add_days(iso: str, days: int) -> str:
    """UTC day arithmetic: ``iso`` + ``days``, back to ``YYYY-MM-DD``.

    Whole-day :class:`datetime.date` arithmetic — identical to the TS
    ``Date.UTC`` + ``86400_000`` slicing (``srs.ts`` ``addDays``).
    """
    parsed = date.fromisoformat(iso)
    return (parsed + timedelta(days=days)).isoformat()


def day_diff(from_iso: str, to_iso: str) -> int:
    """Whole days from ``from_iso`` to ``to_iso`` (non-negative on
    due-or-late reviews) — the ``srs.ts`` ``dayDiff`` port."""
    return (date.fromisoformat(to_iso) - date.fromisoformat(from_iso)).days


def _next_sm2_ease(ease: float, quality: int) -> float:
    """SM-2 ease update: ``max(1.3, ease + 0.1 - (5-q)(0.08 + (5-q)0.02))``."""
    delta = 5 - quality
    return max(SM2_EASE_FLOOR, ease + 0.1 - delta * (0.08 + delta * 0.02))


def next_sm2_state(state: CardSrsState, grade: CardGrade, today_iso: str) -> CardSrsState:
    """The next state under SM-2 (the ``srs.ts`` ``nextSm2State`` port).

    SM-2 never touches the fsrs fields; ease initializes from
    :data:`SM2_INITIAL_EASE` when the card arrives from the fsrs algorithm
    (or unseen).
    """
    quality = SM2_QUALITY[grade]
    ease = _next_sm2_ease(state.ease if state.ease is not None else SM2_INITIAL_EASE, quality)
    if grade == "again":
        reps = 0
        interval = 1
    else:
        reps = state.reps + 1
        if state.reps == 0:
            interval = 1
        elif state.reps == 1:
            interval = 6
        else:
            prior_ease = state.ease if state.ease is not None else SM2_INITIAL_EASE
            interval = _round_half_up(state.interval * prior_ease)
    return CardSrsState(
        due=add_days(today_iso, interval),
        reps=reps,
        interval=interval,
        ease=ease,
        difficulty=state.difficulty,
        stability=state.stability,
    )


def retrievability(elapsed_days: float, stability: float) -> float:
    """Retrievability R(t,S) = (1 + t/(9S))^-1, t in elapsed days."""
    return (1 + elapsed_days / (9 * stability)) ** -1


def next_fsrs_state(state: CardSrsState, grade: CardGrade, today_iso: str) -> CardSrsState:
    """The next state under FSRS-5 (the ``srs.ts`` ``nextFsrsState`` port).

    FSRS never touches the sm2 ease field. First-review path: the incoming
    state lacks THIS algorithm's fields (mid-deck switch or unseen) —
    initialize from the FSRS-5 default weights, regardless of reps, with a
    same-day re-view (interval 0). Otherwise the elapsed days since the last
    review (scheduled interval plus the slip — or lead — between the due date
    and today) drive the retrievability; difficulty updates first — the
    stability recurrence consumes D', the post-update difficulty.
    """
    rating = FSRS_RATING[grade]
    ease = state.ease

    if state.stability is None or state.difficulty is None:
        stability = _w(rating - 1)
        difficulty = _clamp(_w(4), FSRS_D_MIN, FSRS_D_MAX)
        return CardSrsState(
            due=today_iso,
            reps=0 if rating == 1 else 1,
            interval=0,
            ease=ease,
            difficulty=difficulty,
            stability=stability,
        )

    stability = state.stability
    difficulty = state.difficulty
    elapsed = state.interval + (0 if state.due is None else day_diff(state.due, today_iso))
    r = retrievability(max(0, elapsed), stability)

    next_difficulty = _clamp(difficulty - _w(6) * (rating - 3), FSRS_D_MIN, FSRS_D_MAX)
    if rating == 1:
        # Lapse path: S' = w11 * D'^-w12 * ((S+1)^w13 - 1) * e^((1-R)w14)
        next_stability = (
            _w(11)
            * next_difficulty ** -_w(12)
            * ((stability + 1) ** _w(13) - 1)
            * exp((1 - r) * _w(14))
        )
    else:
        # S' = S * (1 + e^w8 * (11-D') * S^-w9 * (e^((1-R)w10) - 1))
        next_stability = stability * (
            1
            + exp(_w(8))
            * (11 - next_difficulty)
            * stability ** -_w(9)
            * (exp((1 - r) * _w(10)) - 1)
        )

    interval = max(0, _round_half_up(next_stability))
    reps = 0 if rating == 1 else state.reps + 1
    return CardSrsState(
        due=add_days(today_iso, interval),
        reps=reps,
        interval=interval,
        ease=ease,
        difficulty=next_difficulty,
        stability=next_stability,
    )


def next_card_state(
    state: CardSrsState,
    grade: CardGrade,
    algorithm: SrsAlgorithm,
    today_iso: str,
) -> CardSrsState:
    """The next SRS state after grading ``grade`` with ``algorithm`` on
    ``today_iso`` (``YYYY-MM-DD``). Pure: returns a fresh state, never
    mutates — the ``srs.ts`` ``nextCardState`` port."""
    if algorithm == "sm2":
        return next_sm2_state(state, grade, today_iso)
    return next_fsrs_state(state, grade, today_iso)


def is_card_due(state: CardSrsState, today_iso: str) -> bool:
    """A card is due when unseen, due today, or overdue — string compare on
    ``YYYY-MM-DD`` is chronological (the ``srs.ts`` ``isCardDue`` port)."""
    return state.due is None or state.due <= today_iso
