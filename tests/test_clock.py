"""Unit tests for the Hybrid Logical Clock implementation.

Semantics verified against ``packages/protocol/src/hlc.ts`` (the norm):
``advance``/``now`` reset the logical counter when physical time moves ahead
and increment it otherwise; ``update`` merges a received HLC as
``max(physicalTime, last, received)`` with the logical counter resolved per
branch. Tests below pin the advance/update matrix so the Python port cannot
drift from the TypeScript reference.
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


class TestHlcTsParity:
    """Direct port of the branch matrix in ``hlc.ts`` Clock.update/now.

    physical = max(physicalTime, last.physical, received.physical); the
    logical component then resolves by which operand(s) won the max.
    """

    @pytest.mark.parametrize(
        ("last", "received", "physical_time", "expected"),
        [
            # physical == last.physical == received.physical → max logical + 1
            ((100, 5), (100, 3), 100, (100, 6)),
            ((100, 5), (100, 9), 90, (100, 10)),
            # physical == last.physical only → last logical + 1
            ((200, 5), (100, 3), 150, (200, 6)),
            ((200, 5), (100, 3), 200, (200, 6)),
            # physical == received.physical only → received logical + 1
            ((100, 5), (200, 3), 150, (200, 4)),
            ((100, 5), (150, 9), 120, (150, 10)),
            # fresh physical time beats both → logical resets to 0
            ((100, 5), (150, 3), 200, (200, 0)),
            ((100, 5), (50, 3), 200, (200, 0)),
        ],
    )
    def test_update_branch_matrix(
        self,
        last: tuple[int, int],
        received: tuple[int, int],
        physical_time: int,
        expected: tuple[int, int],
    ) -> None:
        clock = Clock("device-a")
        # Seed ``last`` exactly: the first advance sets (last.physical, 0),
        # each same-tick advance bumps the logical component by one.
        clock.advance(last[0])
        for _ in range(last[1]):
            clock.advance(last[0])
        result = clock.update(Hlc(physical=received[0], logical=received[1]), physical_time=physical_time)
        assert result == Hlc(physical=expected[0], logical=expected[1])

    @pytest.mark.parametrize(
        ("seed", "physical_time", "expected"),
        [
            ((0, 0), 10, (10, 0)),  # ahead → reset
            ((10, 3), 10, (10, 4)),  # equal → increment
            ((10, 3), 5, (10, 4)),  # behind → increment
        ],
    )
    def test_now_matrix(self, seed: tuple[int, int], physical_time: int, expected: tuple[int, int]) -> None:
        clock = Clock("device-a")
        clock.advance(seed[0])
        for _ in range(seed[1]):
            clock.advance(seed[0])
        assert clock.advance(physical_time) == Hlc(physical=expected[0], logical=expected[1])
