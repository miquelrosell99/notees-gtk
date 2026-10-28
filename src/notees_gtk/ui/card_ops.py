"""Pure memorization-card write-envelope builders: the date-chain ensure, the
grade's property batch, and the ``card.review`` history record.

Headless-testable by design: no GTK imports here (the ``tag_ops.py``
precedent keeps pure op builders under ``ui/`` importable without the
``ui`` extra). Ports, from the Notees monorepo:

- :func:`date_chain_envelopes` — ``client-dates.ts`` ``ensureDateChain``
  (the year/month/day chain ensure: missing → ``object.create`` at the
  deterministic chain parent with the date-chain class, trashed/archived →
  ``object.restore``, live → no envelope; deterministic ids make a re-create
  an applier no-op);
- :func:`review_property_envelopes` — ``client.ts:694`` ``reviewCard``, the
  grade's schedule batch as ``property.set`` ops addressing the row's
  EXISTING idx (the taskCycle precedent, re-read at write time). The
  INACTIVE algorithm's fields are never written (the ``srs.ts`` mid-deck
  switch staleness contract);
- :func:`card_review_envelope` — the strict ``CardReviewPayload`` history op
  (op-types.ts ``card.review``): state records ride verbatim, one op per
  graded review, never journaled.

The session layer (Task 6) enqueues these in order — the property batch
first, the history op last — through the ``_enqueue_tag_write`` pattern.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from notees_gtk.core.protocol.clock import Clock
from notees_gtk.core.protocol.dates import (
    chain_node_ids,
    date_chain_class_id,
    date_chain_parent_id,
    day_node_id,
    parse_iso_date,
)
from notees_gtk.core.protocol.features import card_property_uuid
from notees_gtk.core.protocol.models import RelayEnvelope, new_envelope
from notees_gtk.core.srs import CardGrade, CardSrsState, SrsAlgorithm

if TYPE_CHECKING:
    from notees_gtk.data.store import EffectiveProperty, NodeRow

__all__ = [
    "card_review_envelope",
    "date_chain_envelopes",
    "review_property_envelopes",
]

#: One store read the pure builders take: the node row by id with TRASHED /
#: ARCHIVED rows visible (``LocalStore.node`` includes them — the raw read
#: ``ensureDateChain`` needs to tell missing / trashed / live apart).
type StoreReads = Callable[[str], NodeRow | None]


def _date_chain_label(iso_day: str, precision: str) -> str:
    """The compact stored label (the ``dates.ts`` ``dateNodeLabel`` port):
    year ``YYYY0000``, month ``YYYYMM00``, day ``YYYYMMDD``."""
    parsed = parse_iso_date(iso_day)
    if precision == "year":
        return f"{parsed.year:04d}0000"
    if precision == "month":
        return f"{parsed.year:04d}{parsed.month:02d}00"
    return f"{parsed.year:04d}{parsed.month:02d}{parsed.day:02d}"


def date_chain_envelopes(
    *,
    workspace_id: str,
    actor_id: str,
    device_id: str,
    iso_day: str,
    store_reads: StoreReads,
    clock: Clock,
) -> list[RelayEnvelope]:
    """Ensure the year/month/day node chain for ``iso_day`` exists and is
    live — the ``client-dates.ts`` ``ensureDateChain`` create + restore port
    (the parentage/label heal arm is web-side log hygiene and stays there).

    For each of year → month → day, in chain order: a MISSING row emits an
    ``object.create`` at the deterministic chain parent
    (:func:`date_chain_parent_id`) carrying the date-chain class
    (:func:`date_chain_class_id`) and the compact label; an existing but
    TRASHED/ARCHIVED row (``is_active = 0``) emits ``object.restore`` (every
    normal read filters active rows, so a trashed day page must come back
    before the due-day write can anchor to it); a live row emits nothing.
    The ids are content-addressed from the date, so a raced re-create is an
    applier no-op.

    Raises:
        ValueError: ``iso_day`` is not a well-formed calendar day
            (:func:`parse_iso_date`, fail loud — a malformed due day must
            never silently produce envelopes).
    """
    parse_iso_date(iso_day)  # fail loud before any envelope is built
    ids = chain_node_ids(iso_day)
    envelopes: list[RelayEnvelope] = []
    for precision in ("year", "month", "day"):
        node_id = ids[precision]
        row = store_reads(node_id)
        if row is None:
            chain_class = date_chain_class_id(node_id)
            assert chain_class is not None  # an id from chain_node_ids always parses
            envelopes.append(
                new_envelope(
                    workspace_id=workspace_id,
                    actor_id=actor_id,
                    device_id=device_id,
                    op_type="object.create",
                    payload={
                        "objectId": node_id,
                        "isEmbedded": False,
                        "parentId": date_chain_parent_id(node_id),
                        "classIds": [chain_class],
                        "contentAst": [{"type": "text", "text": _date_chain_label(iso_day, precision)}],
                    },
                    clock=clock,
                    affected_node_ids=(node_id,),
                )
            )
        elif not row.is_active:
            envelopes.append(
                new_envelope(
                    workspace_id=workspace_id,
                    actor_id=actor_id,
                    device_id=device_id,
                    op_type="object.restore",
                    payload={"objectId": node_id},
                    clock=clock,
                    affected_node_ids=(node_id,),
                )
            )
    return envelopes


def _idx_of(props: Sequence[EffectiveProperty], schema_id: str) -> int:
    """The row's EXISTING idx for ``schema_id`` (the ``client.ts:694``
    ``idxOf`` port) — the taskCycle precedent: default 0 when the effective
    read carries no such row."""
    for prop in props:
        if prop.property_schema_id == schema_id:
            return prop.idx
    return 0


def review_property_envelopes(
    *,
    workspace_id: str,
    actor_id: str,
    device_id: str,
    card_id: str,
    state_after: CardSrsState,
    algorithm: SrsAlgorithm,
    props: Sequence[EffectiveProperty],
    clock: Clock,
) -> list[RelayEnvelope]:
    """The grade's schedule batch — the ``client.ts:694`` ``reviewCard``
    port, one ``property.set`` per schedule field, in the web's order
    (cardDue, cardReps, cardInterval, then the algorithm's own fields).

    ``cardDue`` is the datetime point ``{"nodeId": <deterministic day id>}``;
    reps/interval ride as numbers; the INACTIVE algorithm's fields are never
    written (the mid-deck switch staleness contract — sm2 writes cardEase
    and never difficulty/stability, fsrs the reverse).

    Raises:
        ValueError: the due day is absent, or the active algorithm's field
            is missing from the state — fail loud, never write a null
            schedule value.
    """
    if state_after.due is None:
        raise ValueError("review property batch needs a due day, got None")
    writes: list[tuple[str, object]] = [
        (card_property_uuid("cardDue"), {"nodeId": day_node_id(state_after.due)}),
        (card_property_uuid("cardReps"), state_after.reps),
        (card_property_uuid("cardInterval"), state_after.interval),
    ]
    if algorithm == "sm2":
        if state_after.ease is None:
            raise ValueError("sm2 review property batch needs an ease value, got None")
        writes.append((card_property_uuid("cardEase"), state_after.ease))
    else:
        if state_after.difficulty is None or state_after.stability is None:
            raise ValueError("fsrs review property batch needs difficulty AND stability, got a null field")
        writes.append((card_property_uuid("cardDifficulty"), state_after.difficulty))
        writes.append((card_property_uuid("cardStability"), state_after.stability))

    envelopes: list[RelayEnvelope] = []
    for schema_id, value in writes:
        envelopes.append(
            new_envelope(
                workspace_id=workspace_id,
                actor_id=actor_id,
                device_id=device_id,
                op_type="property.set",
                payload={
                    "objectId": card_id,
                    "propertySchemaId": schema_id,
                    "value": value,
                    "idx": _idx_of(props, schema_id),
                },
                clock=clock,
                affected_node_ids=(card_id,),
            )
        )
    return envelopes


def _state_wire_dict(state: CardSrsState) -> dict[str, object]:
    """The ``card.review`` state record — the wire ``CardSrsState`` shape,
    riding verbatim from the pure state (ints stay ints; the strict payload
    model checks the shape without rewriting the stored bytes)."""
    return {
        "due": state.due,
        "reps": state.reps,
        "interval": state.interval,
        "ease": state.ease,
        "difficulty": state.difficulty,
        "stability": state.stability,
    }


def card_review_envelope(
    *,
    workspace_id: str,
    actor_id: str,
    device_id: str,
    card_id: str,
    grade: CardGrade,
    algorithm: SrsAlgorithm,
    state_before: CardSrsState,
    state_after: CardSrsState,
    session_id: str,
    reviewed_at: str,
    clock: Clock,
) -> RelayEnvelope:
    """The grade's audit-history record: the strict ``CardReviewPayload``
    (op-types.ts ``card.review``) via :func:`new_envelope` — one op per
    graded review, carrying the full state transition so every replica
    applies the same scheduling outcome. ``reviewed_at`` is the grading
    timestamp (``datetime.now(UTC).isoformat()`` at the session layer);
    ``session_id`` groups the reviews of one study session, minted at
    session start."""
    return new_envelope(
        workspace_id=workspace_id,
        actor_id=actor_id,
        device_id=device_id,
        op_type="card.review",
        payload={
            "cardId": card_id,
            "grade": grade,
            "algorithm": algorithm,
            "stateBefore": _state_wire_dict(state_before),
            "stateAfter": _state_wire_dict(state_after),
            "reviewedAt": reviewed_at,
            "sessionId": session_id,
        },
        clock=clock,
        affected_node_ids=(card_id,),
    )
