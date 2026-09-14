"""Unit tests for the Hybrid Logical Clock implementation.

Ports the advance/update matrix from ``tests/core/test_clock.py`` in the
Notees backend; semantics must stay identical to ``app/core/clock.py``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from notees_gtk.core.protocol.clock import Clock, Hlc, compare_hlc, max_hlc


class TestHlcComparison:
    def test_compare_equal(self) -> None:
        a = Hlc(physical=10, logical=5)
        b = Hlc(physical=10, logical=5)
        assert compare_hlc(a, b) == 0

    def test_compare_different_physical(self) -> None:
        a = Hlc(physical=10, logical=5)
        b = Hlc(physical=20, logical=0)
        assert compare_hlc(a, b) < 0
        assert compare_hlc(b, a) > 0

    def test_compare_same_physical_different_logical(self) -> None:
        a = Hlc(physical=10, logical=3)
        b = Hlc(physical=10, logical=5)
        assert compare_hlc(a, b) < 0
        assert compare_hlc(b, a) > 0

    def test_max_hlc_returns_greater(self) -> None:
        a = Hlc(physical=10, logical=5)
        b = Hlc(physical=20, logical=0)
        assert max_hlc(a, b) == b
        assert max_hlc(b, a) == b

    def test_max_hlc_returns_first_when_equal(self) -> None:
        a = Hlc(physical=10, logical=5)
        b = Hlc(physical=10, logical=5)
        assert max_hlc(a, b) == a


class TestHlcValidation:
    def test_negative_physical_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Hlc(physical=-1, logical=0)

    def test_negative_logical_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Hlc(physical=0, logical=-1)

    def test_zero_components_accepted(self) -> None:
        assert Hlc(physical=0, logical=0) == Hlc(physical=0, logical=0)

    def test_frozen(self) -> None:
        hlc = Hlc(physical=1, logical=1)
        with pytest.raises(ValidationError):
            hlc.logical = 2  # type: ignore[misc]


class TestClockAdvance:
    def test_advance_with_later_physical_time_resets_logical(self) -> None:
        clock = Clock("device-a")
        first = clock.advance(10)
        second = clock.advance(20)
        assert first == Hlc(physical=10, logical=0)
        assert second == Hlc(physical=20, logical=0)

    def test_advance_with_same_physical_time_increments_logical(self) -> None:
        clock = Clock("device-a")
        first = clock.advance(10)
        second = clock.advance(10)
        third = clock.advance(10)
        assert first == Hlc(physical=10, logical=0)
        assert second == Hlc(physical=10, logical=1)
        assert third == Hlc(physical=10, logical=2)

    def test_advance_with_earlier_physical_time_increments_logical(self) -> None:
        clock = Clock("device-a")
        clock.advance(20)
        result = clock.advance(10)
        assert result == Hlc(physical=20, logical=1)

    def test_advance_never_decreases(self) -> None:
        clock = Clock("device-a")
        previous = Hlc(physical=0, logical=0)
        for physical in [1, 1, 2, 2, 2, 1, 3]:
            current = clock.advance(physical)
            assert compare_hlc(current, previous) >= 0
            previous = current


class TestClockUpdate:
    def test_update_with_fresh_physical_time_and_remote(self) -> None:
        clock = Clock("device-a")
        clock.advance(10)
        received = Hlc(physical=15, logical=2)
        result = clock.update(received, physical_time=20)
        assert result == Hlc(physical=20, logical=0)

    def test_update_when_local_physical_is_max(self) -> None:
        clock = Clock("device-a")
        clock.advance(20)
        received = Hlc(physical=15, logical=2)
        result = clock.update(received, physical_time=18)
        assert result == Hlc(physical=20, logical=1)

    def test_update_when_received_physical_is_max(self) -> None:
        clock = Clock("device-a")
        clock.advance(10)
        received = Hlc(physical=20, logical=2)
        result = clock.update(received, physical_time=15)
        assert result == Hlc(physical=20, logical=3)

    def test_update_when_both_physical_equal(self) -> None:
        clock = Clock("device-a")
        clock.advance(10)
        received = Hlc(physical=10, logical=5)
        result = clock.update(received, physical_time=10)
        assert result == Hlc(physical=10, logical=6)

    def test_update_preserves_idempotency(self) -> None:
        clock = Clock("device-a")
        clock.advance(10)
        received = Hlc(physical=15, logical=0)
        first = clock.update(received, physical_time=15)
        second = clock.update(received, physical_time=15)
        # Re-applying the same remote HLC at the same physical time should not
        # cause the local clock to regress and should converge deterministically.
        assert compare_hlc(second, first) >= 0


class TestClockDeviceId:
    def test_device_id_is_stored(self) -> None:
        clock = Clock("device-a")
        assert clock.device_id == "device-a"
