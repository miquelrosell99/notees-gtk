"""Pure memorization-card reads: the bucket partition, the review queue, and
the derived SRS-state read — the CardBuckets.tsx / reviewQueue.ts ports.

Everything here is store-shaped but GTK-free: inputs are plain row
dataclasses (``NodeRow`` / ``CardReview`` / ``EffectiveProperty`` from
:mod:`notees_gtk.data.store`, imported typing-only) and outputs are pure
records, so the headless test suite exercises the exact semantics the chrome
renders. Reference (all in the Notees monorepo):

- Buckets: ``apps/web/src/features/classview/CardBuckets.tsx``
  (``partitionCardsIntoBuckets``, ``cardRowsOf`` via ``scheduledIsoOf``)
- Queue: ``apps/web/src/features/cards/reviewQueue.ts``
  (``buildReviewQueue`` — due + fresh ONLY, future-due dropped)
- Due-day grammar: ``apps/web/src/features/calendar/calendarViewUtils.ts``
  (``scheduledIsoOf`` — a point reads its day, a range its start day)
- SRS-state derivation: the latest ``card.review`` row's ``stateAfter``
  (``LocalStore.list_card_reviews``), the journal twin of the web's
  effective-properties + history read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from notees_gtk.core.protocol.dates import parse_date_node_id
from notees_gtk.core.protocol.features import system_class_uuid
from notees_gtk.core.srs import UNSEEN_CARD_STATE, CardSrsState

if TYPE_CHECKING:
    from notees_gtk.data.store import CardReview, EffectiveProperty, NodeRow

__all__ = [
    "CardBucketRow",
    "CardBuckets",
    "build_review_queue",
    "card_due_day",
    "card_members",
    "card_srs_state",
    "partition_cards_into_buckets",
]

#: The extends-edge shape the class closure walks: class id → its direct
#: extends parents (the store's ``class_extends_edges()`` read).
type ExtendsMap = Mapping[str, Sequence[str]]


@dataclass(frozen=True)
class CardBucketRow:
    """One card row's bucket facts: the cardDue day (``YYYY-MM-DD``),
    ``None`` when the card has never been scheduled (no cardDue value)."""

    id: str
    due: str | None


@dataclass(frozen=True)
class CardBuckets:
    """The three-way cards partition (``CardBuckets.tsx`` parity).

    ``due`` — cardDue day on or before today (overdue or due now);
    ``fresh`` — no cardDue value (the never-reviewed set); ``later`` —
    cardDue day after today.
    """

    due: tuple[CardBucketRow, ...]
    fresh: tuple[CardBucketRow, ...]
    later: tuple[CardBucketRow, ...]


def _slot_day(slot: Any) -> str | None:
    """One datetime slot's day: a ``{"nodeId": …}`` (optionally ``time``)
    mapping whose node id parses at DAY precision → ``YYYY-MM-DD``;
    anything else → ``None`` (the ``slotIsoOf`` port)."""
    if not isinstance(slot, Mapping):
        return None
    node_id = slot.get("nodeId")
    if not isinstance(node_id, str):
        return None
    parsed = parse_date_node_id(node_id)
    if parsed is None or parsed["precision"] != "day":
        return None
    return f"{parsed['year']:04d}-{parsed['month']:02d}-{parsed['day']:02d}"


def _scheduled_day(value: Any) -> str | None:
    """The ``scheduledIsoOf`` port: a RANGE value reads its START slot day,
    falling back to the END slot day when the start side is open; a POINT
    value reads its own day; anything else → ``None``."""
    if isinstance(value, Mapping) and ("start" in value or "end" in value):
        start = _slot_day(value.get("start"))
        if start is not None:
            return start
        return _slot_day(value.get("end"))
    return _slot_day(value)


def card_due_day(effective_properties: Sequence[EffectiveProperty], due_schema_id: str) -> str | None:
    """The card's effective due day: the effective ``cardDue`` value through
    the datetime slot grammar (the web ``cardRowsOf`` → ``scheduledIsoOf``
    read the hub buckets and the review queue both schedule from)."""
    for prop in effective_properties:
        if prop.property_schema_id == due_schema_id:
            return _scheduled_day(prop.value)
    return None


def partition_cards_into_buckets(rows: Sequence[CardBucketRow], today_iso: str) -> CardBuckets:
    """The cards-bucket partition (``partitionCardsIntoBuckets`` port):
    due-or-before-today → Due, no day → Fresh, future → Later. Due and Later
    sort by day then id; Fresh keeps input order (there is no day to sort
    by). A card lands in exactly one bucket."""
    due: list[CardBucketRow] = []
    fresh: list[CardBucketRow] = []
    later: list[CardBucketRow] = []
    for row in rows:
        if row.due is None:
            fresh.append(row)
        elif row.due <= today_iso:
            due.append(row)
        else:
            later.append(row)
    due.sort(key=lambda row: (row.due or "", row.id))
    later.sort(key=lambda row: (row.due or "", row.id))
    return CardBuckets(due=tuple(due), fresh=tuple(fresh), later=tuple(later))


def build_review_queue(rows: Sequence[CardBucketRow], today_iso: str) -> list[str]:
    """The session's review order, ids only (``buildReviewQueue`` port):
    due cards (due ≤ today) sorted by due-date ascending — overdue first by
    construction — then fresh cards in input order. Future-due rows are NOT
    reviewable and never enter the queue, whatever the hand-off carried."""
    buckets = partition_cards_into_buckets(rows, today_iso)
    return [row.id for row in (*buckets.due, *buckets.fresh)]


def _integral(value: Any, field: str) -> int:
    """A wire SRS number that must be whole (reps/interval) — fail loud on a
    fractional value rather than silently truncating."""
    number = float(value)
    if not number.is_integer():
        raise ValueError(f"card SRS state field {field!r} must be integral, got {value!r}")
    return int(number)


def card_srs_state(reviews: Sequence[CardReview]) -> CardSrsState:
    """The card's derived SRS state: the LATEST review row's ``stateAfter``
    (``list_card_reviews`` is HLC-ordered, so the last row wins); a card
    never reviewed reads :data:`UNSEEN_CARD_STATE`. This is the GTK journal
    twin of the web's effective-properties + history read — the same derived
    state the appliers maintain."""
    if not reviews:
        return UNSEEN_CARD_STATE
    state = reviews[-1].state_after
    return CardSrsState(
        due=state.get("due"),
        reps=_integral(state.get("reps", 0), "reps"),
        interval=_integral(state.get("interval", 0), "interval"),
        ease=state.get("ease"),
        difficulty=state.get("difficulty"),
        stability=state.get("stability"),
    )


def card_members(
    rows: Sequence[NodeRow],
    extends: ExtendsMap,
    card_class_uuid: str = system_class_uuid("card"),
) -> list[NodeRow]:
    """The card members of ``rows`` — extends-aware: a node whose class set
    contains the ``card`` system class OR any class extending it (transitively)
    is a card. Input order is preserved."""
    descendants = _extends_descendants(card_class_uuid, extends)
    return [row for row in rows if any(class_id in descendants for class_id in row.class_ids)]


def _extends_descendants(class_id: str, extends: ExtendsMap) -> set[str]:
    """The class id plus every class that extends it, directly or transitively
    (the closure ``card_members`` filters by — a member of a ``card``
    subclass is a card)."""
    children: dict[str, list[str]] = {}
    for child, parents in extends.items():
        for parent in parents:
            children.setdefault(parent, []).append(child)
    seen = {class_id}
    queue = [class_id]
    while queue:
        current = queue.pop()
        for child in children.get(current, ()):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return seen
