"""Hybrid Logical Clock implementation.

Ports ``app/core/clock.py`` from the Notees backend (SPEC §2) line-for-line,
with :class:`Hlc` as a frozen Pydantic model so wire validation (both
components non-negative) lives on the type itself. HLCs provide causality
tracking with constant-size timestamps that combine a physical (wall-clock)
component with a logical counter.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["Clock", "Hlc", "compare_hlc", "max_hlc"]


class Hlc(BaseModel):
    """A Hybrid Logical Clock value.

    Attributes:
        physical: Wall-clock time component, typically a millisecond timestamp.
        logical: Monotonic counter used to break ties when physical times are equal.
    """

    model_config = ConfigDict(frozen=True)

    physical: int = Field(ge=0)
    logical: int = Field(ge=0)


def compare_hlc(a: Hlc, b: Hlc) -> int:
    """Total-order comparison of two HLC values.

    Args:
        a: First HLC value.
        b: Second HLC value.

    Returns:
        A negative value if ``a < b``, zero if ``a == b``, and a positive value
        if ``a > b``.
    """
    if a.physical != b.physical:
        return a.physical - b.physical
    return a.logical - b.logical


def max_hlc(a: Hlc, b: Hlc) -> Hlc:
    """Return the greater of two HLC values.

    Args:
        a: First HLC value.
        b: Second HLC value.

    Returns:
        ``a`` when the values are equal, otherwise the greater one.
    """
    return a if compare_hlc(a, b) >= 0 else b


class Clock:
    """Local HLC clock bound to a device identifier.

    The clock is advanced on local events and updated when receiving remote
    events, guaranteeing that the local ``last`` HLC never decreases.
    """

    def __init__(self, device_id: str) -> None:
        """Initialize a clock for ``device_id`` starting at HLC zero.

        Args:
            device_id: Identifier of the device this clock belongs to.
        """
        self.device_id = device_id
        self._last = Hlc(physical=0, logical=0)

    def advance(self, physical_time: int) -> Hlc:
        """Advance the clock to ``physical_time``.

        If ``physical_time`` is ahead of the current physical component, the
        logical counter resets to zero. Otherwise the logical counter is
        incremented while the physical component is held.

        Args:
            physical_time: Current wall-clock time in milliseconds.

        Returns:
            The new local HLC value.
        """
        if physical_time > self._last.physical:
            self._last = Hlc(physical=physical_time, logical=0)
        else:
            self._last = Hlc(
                physical=self._last.physical,
                logical=self._last.logical + 1,
            )
        return self._last

    def update(self, received: Hlc, physical_time: int) -> Hlc:
        """Merge a received HLC into the local clock.

        The new local HLC is the maximum of the local and received HLCs,
        adjusted for the current physical time. This ensures causal ordering
        across devices.

        Args:
            received: HLC received from a remote device.
            physical_time: Current wall-clock time in milliseconds.

        Returns:
            The new local HLC value.
        """
        if physical_time > self._last.physical and physical_time > received.physical:
            self._last = Hlc(physical=physical_time, logical=0)
        else:
            max_physical = max(self._last.physical, received.physical)
            logical = 0
            if max_physical == self._last.physical and max_physical == received.physical:
                logical = max(self._last.logical, received.logical) + 1
            elif max_physical == self._last.physical:
                logical = self._last.logical + 1
            else:
                logical = received.logical + 1
            self._last = Hlc(physical=max_physical, logical=logical)
        return self._last
