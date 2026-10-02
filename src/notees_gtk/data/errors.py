"""Typed store errors for the v2 appliers (port of v2 ``packages/store/src/errors.ts``).

Guard violations fail loud with a typed error so callers (and tests) can tell
a convergence-relevant rejection from a bug: cycle closes, cross-row move
guards, unknown targets, and the reserved CRDT carrier all have their own
class.
"""

from __future__ import annotations

__all__ = [
    "CycleError",
    "EnvelopeValidationError",
    "MoveGuardError",
    "NotFoundError",
    "StoreError",
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


class MoveGuardError(StoreError):
    """Cross-row tree guard: a node may never move under itself or its own
    descendant, and a class node may never move under any parent (classes
    are always roots — the friendly surface of the node table's
    ``is_class = 0 OR parent_id IS NULL`` CHECK)."""


class NotFoundError(StoreError):
    """The op targets a node (or parent) that does not exist in the cache."""


class UnsupportedCarrierError(StoreError):
    """``contentDeltaB64`` needs the Yjs port; reapply with the contentAst
    readable carrier."""
