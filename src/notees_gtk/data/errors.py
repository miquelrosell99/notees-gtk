"""Typed store errors for the appliers (port of ``packages/store/src/errors.ts``).

Guard violations fail loud with a typed error so callers (and tests) can tell
a convergence-relevant rejection from a bug: cycle closes, cross-row move
guards, unknown targets, and the reserved CRDT carrier all have their own
class.
"""

from __future__ import annotations

__all__ = [
    "CycleError",
    "EnvelopeValidationError",
    "LockedSystemClassError",
    "MoveGuardError",
    "NotFoundError",
    "PropertyValueShapeError",
    "StoreError",
    "SystemExtendsLockError",
    "UnsupportedCarrierError",
]


class StoreError(Exception):
    """Base class for all local-store application errors."""

    def __init__(self, message: str, op_type: str | None = None) -> None:
        self.op_type = op_type
        super().__init__(message)


class EnvelopeValidationError(StoreError):
    """An envelope payload deviates from the op's strict wire schema.

    The apply-time half of the 422 ``validation_failed`` gate (the web
    store's ``validateEnvelope``): the sync engine treats it like any other
    guard violation — log, skip, keep pulling."""


class CycleError(StoreError):
    """class.setExtends closed a cycle (self-parent or multi-hop)."""


class SystemExtendsLockError(StoreError):
    """class.setExtends attempted to modify a system class's frozen extends
    mapping (SCHEMA.md "Class properties", owner ruling 2026-10-22 — system
    mappings are seeded platform structure; only an idempotent re-issue of
    the seeded parent set applies, every deviation fails loud and is never
    applied)."""


class LockedSystemClassError(StoreError):
    """object.delete (or an archival ``object.update {active: false}``)
    addressed at a LOCKED system class — the date chain (year/month/day) +
    table, asset, query, code, template, comment (owner ruling 2026-10-22,
    the classes-gallery batch; ``LOCKED_SYSTEM_CLASSES`` in
    ``core/protocol/features.ts`` parity). Locked classes are platform
    structure: they can never be archived or deleted. Fails loud, never
    applied."""


class MoveGuardError(StoreError):
    """Structural guard: a class node may never take a parent (classes are
    always roots — the friendly surface of the node table's
    ``is_class = 0 OR parent_id IS NULL`` CHECK), a date-chain node's
    structure is FIXED — its parent is the deterministic chain parent, its
    class set is its precision's system class (never declared a class
    itself) — and a node may never move into its own subtree."""


class NotFoundError(StoreError):
    """The op targets a node (or parent) that does not exist in the cache."""


class PropertyValueShapeError(StoreError):
    """PG6 apply-time value validation: a property.set value (or a
    class.property.set defaultValue) violates the schema's contract — shape,
    scalar typing, cardinality, datePrecision ceiling, targetClassFilter
    membership, or node-target existence. Deterministic on every replica:
    the same envelope is rejected everywhere, so the sync engine treats it
    like any other guard violation — log, skip, keep pulling."""


class UnsupportedCarrierError(StoreError):
    """``contentDeltaB64`` needs the Yjs port; reapply with the contentAst
    readable carrier."""
