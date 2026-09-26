"""Typed store errors for the v2 appliers (port of v2 ``packages/store/src/errors.ts``).

Guard violations fail loud with a typed error so callers (and tests) can tell
a convergence-relevant rejection from a bug: cycle closes, cross-row move
guards, placement CHECK equivalents, unknown targets, and the reserved CRDT
carrier all have their own class.
"""

from __future__ import annotations

__all__ = [
    "CycleError",
    "MoveGuardError",
    "NotFoundError",
    "PlacementError",
    "StoreError",
    "UnsupportedCarrierError",
]


class StoreError(Exception):
    """Base class for all local-store application errors."""

    def __init__(self, message: str, op_type: str | None = None) -> None:
        self.op_type = op_type
        super().__init__(message)


class CycleError(StoreError):
    """class.setExtends closed a cycle (self-parent or multi-hop)."""


class MoveGuardError(StoreError):
    """Cross-row tree guard: a class may never parent, and a node may never
    move under itself or its own descendant."""


class NotFoundError(StoreError):
    """The op targets a node (or parent) that does not exist in the cache."""


class PlacementError(StoreError):
    """Placement CHECK equivalent: a block must have a parent, a class must not
    (the server enforces these as CHECK constraints on its derived schema; the
    client cache enforces them in the applier)."""


class UnsupportedCarrierError(StoreError):
    """``contentDeltaB64`` needs the Yjs port; reapply with the contentAst
    readable carrier."""
