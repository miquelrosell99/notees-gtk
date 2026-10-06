"""Tests for the deterministic date-node ids (packages/domain/src/dates.ts
parity — the scheme is layout-fixed, never regenerate).

PC6 consumes :func:`day_node_id`: a well-formed ``YYYY-MM-DD``
qualifier string normalizes to the deterministic day-node ref, so every
client derives the same id without a graph write.
"""

from __future__ import annotations

import pytest

from notees_gtk.core.protocol.dates import (
    chain_node_ids,
    date_node_id,
    day_node_id,
    month_node_id,
    parse_date_node_id,
    parse_iso_date,
    year_node_id,
)


class TestIdGeneration:
    def test_the_fixed_layout(self) -> None:
        assert day_node_id("2020-03-04") == "00000000-0000-0000-00dd-202003040000"
        assert month_node_id("2020-03-04") == "00000000-0000-0000-00aa-202003000000"
        assert year_node_id("2020-03-04") == "00000000-0000-0000-00bb-202000000000"

    def test_per_precision(self) -> None:
        iso = "2020-03-04"
        assert date_node_id(iso, "year") == year_node_id(iso)
        assert date_node_id(iso, "month") == month_node_id(iso)
        assert date_node_id(iso, "day") == day_node_id(iso)

    def test_chain(self) -> None:
        assert chain_node_ids("2020-03-04") == {
            "year": "00000000-0000-0000-00bb-202000000000",
            "month": "00000000-0000-0000-00aa-202003000000",
            "day": "00000000-0000-0000-00dd-202003040000",
        }


class TestStrictParsing:
    @pytest.mark.parametrize(
        "bad",
        ["2020-13-01", "2020-02-30", "2019-02-29", "2020-03-04T10:00:00", "04/03/2020", "not-a-date", ""],
    )
    def test_malformed_dates_fail_loud(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_iso_date(bad)

    def test_leap_day_is_valid(self) -> None:
        assert day_node_id("2020-02-29") == "00000000-0000-0000-00dd-202002290000"


class TestParseDateNodeId:
    def test_round_trips_with_the_generators(self) -> None:
        for iso in ("2020-03-04", "1999-12-31", "2100-01-01"):
            for precision, generator in (("day", day_node_id), ("month", month_node_id), ("year", year_node_id)):
                node_id = generator(iso)
                parsed = parse_date_node_id(node_id)
                assert parsed is not None
                assert parsed["precision"] == precision

    def test_non_date_ids_return_none(self) -> None:
        assert parse_date_node_id("0192a000-0000-7000-8000-000000000001") is None
        assert parse_date_node_id("not-a-uuid") is None
        # Outside the 1900..2200 window.
        assert parse_date_node_id("00000000-0000-0000-00bb-089900000000") is None
