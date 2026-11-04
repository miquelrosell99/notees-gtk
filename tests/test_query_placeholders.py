"""Compile-time placeholder tests — port of ``packages/query/test/placeholders.test.ts``.

The ``{today}``-style editor-relative date tokens resolve at COMPILE time
against the injected ``now`` clock, in date-condition timestamps and
comparison-bound property values. Local-noon pins keep the tests
timezone-independent (the TS suite's convention).

Store-shape note: the GTK mirror stores envelope timestamps via Python
``isoformat()`` (``+00:00``) while the placeholder boundaries port the TS
``toISOString()`` shape (``Z`` + milliseconds) verbatim. The two formats
compare identically until the sub-second position, which the pinned
second-resolution world timestamps never reach — so the execution pins below
hold without format skew.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from notees_gtk.core.protocol.clock import Hlc
from notees_gtk.core.protocol.dates import chain_node_ids
from notees_gtk.core.protocol.ids import new_uuid7
from notees_gtk.core.protocol.models import PROTOCOL_VERSION, RelayEnvelope
from notees_gtk.core.query.compiler import CompileOptions, compile
from notees_gtk.core.query.execute import run_query
from notees_gtk.core.query.placeholders import (
    placeholder_end_date,
    placeholder_start_date,
    resolve_day_bounds,
    resolve_period_bounds,
    resolve_timestamp_placeholder,
    resolve_value_placeholder,
)

WS = "0192a000-0000-7000-8000-000000000001"
ACTOR = "0192a000-0000-7000-8000-000000000002"
NODE_A = "0192a000-0000-7000-8000-000000000101"
NODE_B = "0192a000-0000-7000-8000-000000000102"
OPENED = "0192a000-0000-7000-8000-000000000301"

#: Local noon: the UTC calendar day equals the local one for every real-world
#: offset, so the tests are timezone-independent (the TS convention).
NOW = datetime(2026, 10, 4, 12, 0, 0)  # Sunday 2026-10-04, 12:00 local
NOW_MS = int(NOW.timestamp() * 1000)


def _local_dt(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0, millis: int = 0) -> str:
    """The JS ``new Date(...).toISOString()`` shape for a LOCAL wall-clock time."""
    return datetime(year, month, day, hour, minute, second, millis * 1000).astimezone(UTC).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


class TestResolutionUnits:
    def test_today_is_the_local_calendar_day(self) -> None:
        assert placeholder_start_date("{today}", NOW) == "2026-10-04"
        assert placeholder_end_date("{today}", NOW) == "2026-10-04"

    def test_this_week_spans_the_iso_week_monday_start(self) -> None:
        assert placeholder_start_date("{this_week}", NOW) == "2026-09-28"
        assert placeholder_end_date("{this_week}", NOW) == "2026-10-04"
        # A Wednesday belongs to the same week.
        wednesday = datetime(2026, 10, 1, 12, 0, 0)
        assert placeholder_start_date("{this_week}", wednesday) == "2026-09-28"
        # A Monday starts a fresh week.
        monday = datetime(2026, 10, 5, 12, 0, 0)
        assert placeholder_start_date("{this_week}", monday) == "2026-10-05"

    def test_this_month_and_this_year_resolve_to_period_edges(self) -> None:
        assert placeholder_start_date("{this_month}", NOW) == "2026-10-01"
        assert placeholder_end_date("{this_month}", NOW) == "2026-10-31"
        assert placeholder_start_date("{this_year}", NOW) == "2026-01-01"
        assert placeholder_end_date("{this_year}", NOW) == "2026-12-31"
        # Leap-year February edge.
        leap = datetime(2024, 2, 10, 12, 0, 0)
        assert placeholder_end_date("{this_month}", leap) == "2024-02-29"

    def test_unknown_tokens_resolve_to_none_and_pass_through_verbatim(self) -> None:
        assert placeholder_start_date("{banana}", NOW) is None
        assert resolve_timestamp_placeholder("{banana}", "after", NOW) == "{banana}"
        assert resolve_value_placeholder("2026-10-04", NOW) == "2026-10-04"

    def test_timestamps_resolve_to_the_local_days_utc_boundaries(self) -> None:
        assert resolve_timestamp_placeholder("{today}", "after", NOW) == _local_dt(2026, 10, 4)
        assert resolve_timestamp_placeholder("{today}", "before", NOW) == _local_dt(2026, 10, 4, 23, 59, 59, 999)
        assert resolve_timestamp_placeholder("{this_year}", "after", NOW) == _local_dt(2026, 1, 1)
        # The bound IS a valid UTC instant: everything created inside the
        # local day falls between the two boundaries.
        assert _js_iso(NOW) >= _local_dt(2026, 10, 4)
        assert _js_iso(NOW) <= _local_dt(2026, 10, 4, 23, 59, 59, 999)

    def test_resolve_period_and_day_bounds(self) -> None:
        from_, to = resolve_period_bounds("this_week", NOW)
        assert (from_, to) == (_local_dt(2026, 9, 28), _local_dt(2026, 10, 4, 23, 59, 59, 999))
        # The period grammar value resolves through the token map.
        assert resolve_period_bounds("today", NOW) == (
            _local_dt(2026, 10, 4),
            _local_dt(2026, 10, 4, 23, 59, 59, 999),
        )
        with pytest.raises(ValueError, match="unknown date period"):
            resolve_period_bounds("someday", NOW)
        # A plain date resolves to that local day; an instant collapses to itself.
        assert resolve_day_bounds("2026-10-04", NOW) == (
            _local_dt(2026, 10, 4),
            _local_dt(2026, 10, 4, 23, 59, 59, 999),
        )
        instant = "2026-10-04T08:30:00Z"
        assert resolve_day_bounds(instant, NOW) == (instant, instant)


def _js_iso(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.astimezone()
    return aware.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ast(children: list[Any]) -> dict[str, Any]:
    return {"version": 2, "scope": {"type": "entire_workspace"}, "root": {"type": "group", "logic": "and", "children": children}}


class TestPlaceholderCompilation:
    def test_date_after_before_resolve_against_the_pinned_clock(self) -> None:
        from notees_gtk.core.protocol.query_ast import parse_query_ast

        ast = parse_query_ast(
            _ast(
                [
                    {"type": "date", "field": "created", "op": "after", "timestamp": "{today}"},
                    {"type": "date", "field": "created", "op": "before", "timestamp": "{this_week}"},
                ]
            )
        )
        compiled = compile(ast, CompileOptions(workspace_id=WS, now=NOW))
        assert list(compiled.params[1:]) == [  # the base workspace param precedes (D1)
            _local_dt(2026, 10, 4),
            _local_dt(2026, 10, 4, 23, 59, 59, 999),
        ]

    def test_a_comparison_bound_property_value_resolves_to_the_period_start(self) -> None:
        from notees_gtk.core.protocol.query_ast import parse_query_ast

        ast = parse_query_ast(_ast([{"type": "property", "schemaId": OPENED, "op": "eq", "value": "{today}"}]))
        compiled = compile(ast, CompileOptions(workspace_id=WS, now=NOW))
        # Same param shape as typing the date by hand: scalar + day/month/year
        # point arms + the range arm (start payload bound, then the end-side
        # day/month/year bounds).
        assert list(compiled.params[4:]) == [
            "2026-10-04",
            "20261004",
            "202610",
            "2026",
            "202610040000",
            "20261004",
            "202610",
            "2026",
        ]
        assert "substr(json_extract(value, '$.nodeId'), 25, 8)" in compiled.sql
        assert "json_extract(value, '$.start.nodeId')" in compiled.sql

    def test_range_ops_resolve_the_bound_the_same_way(self) -> None:
        from notees_gtk.core.protocol.query_ast import parse_query_ast

        ast = parse_query_ast(_ast([{"type": "property", "schemaId": OPENED, "op": "gte", "value": "{this_month}"}]))
        compiled = compile(ast, CompileOptions(workspace_id=WS, now=NOW))
        assert list(compiled.params[4:]) == ["2026-10-01", "202610010000", "202610010000"]

    def test_contains_keeps_the_token_literal_unknown_tokens_pass_through(self) -> None:
        from notees_gtk.core.protocol.query_ast import parse_query_ast

        ast = parse_query_ast(_ast([{"type": "property", "schemaId": OPENED, "op": "contains", "value": "{today}"}]))
        compiled = compile(ast, CompileOptions(workspace_id=WS, now=NOW))
        assert list(compiled.params[4:]) == ["{today}"]
        ast = parse_query_ast(
            _ast(
                [
                    {"type": "date", "field": "created", "op": "after", "timestamp": "{someday}"},
                    {"type": "property", "schemaId": OPENED, "op": "eq", "value": "{foo}"},
                ]
            )
        )
        compiled = compile(ast, CompileOptions(workspace_id=WS, now=NOW))
        assert list(compiled.params) == [
            WS,
            "{someday}",
            OPENED,
            OPENED,
            OPENED,
            "{foo}",
        ]


# --- execution against the real mirror ----------------------------------------------


def _envelope(op_type: str, payload: dict[str, Any], offset_ms: int) -> RelayEnvelope:
    physical = NOW_MS + offset_ms
    return RelayEnvelope(
        id=new_uuid7(),
        protocolVersion=PROTOCOL_VERSION,
        workspaceId=WS,
        actorId=ACTOR,
        deviceId="test-device-placeholders",
        hlc=Hlc(physical=physical, logical=0),
        affectedNodeIds=[],
        opType=op_type,
        payload=payload,
        timestamp=datetime.fromtimestamp(physical / 1000, UTC),
    )


def _text(value: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": value}]


def _date_chain(store: Any, iso: str, offset_ms: int) -> None:
    """Create the year → month → day chain for ``iso`` (the deterministic ids)."""
    chain = chain_node_ids(iso)
    store.apply_remote(_envelope("object.create", {"objectId": chain["year"], "contentAst": _text(iso[:4])}, offset_ms))
    store.apply_remote(
        _envelope("object.create", {"objectId": chain["month"], "parentId": chain["year"], "contentAst": _text(iso[:7])}, offset_ms + 1)
    )
    store.apply_remote(
        _envelope(
            "object.create", {"objectId": chain["day"], "parentId": chain["month"], "contentAst": _text(iso)}, offset_ms + 2
        )
    )


@pytest.fixture
def store(tmp_path: Path) -> Any:
    from notees_gtk.data.store import LocalStore

    instance = LocalStore(tmp_path / "store.db")
    yield instance
    instance.close()


@pytest.fixture
def world_store(store: Any) -> Any:
    """Two plain nodes created yesterday/today + an "opened" datetime property."""
    today = NOW.date().isoformat()
    last_month = (NOW - timedelta(days=35)).date().isoformat()
    _date_chain(store, today, -3200)
    _date_chain(store, last_month, -3200)
    store.apply_remote(_envelope("object.create", {"objectId": NODE_A, "contentAst": _text("Alpha")}, -24 * 3600 * 1000))
    store.apply_remote(_envelope("object.create", {"objectId": NODE_B, "contentAst": _text("Beta")}, -3600 * 1000))
    store.apply_remote(
        _envelope(
            "propertySchema.create",
            {"propertySchemaId": OPENED, "name": "opened", "type": "datetime"},
            -3000,
        )
    )
    store.apply_remote(
        _envelope(
            "property.set",
            {"objectId": NODE_A, "propertySchemaId": OPENED, "value": {"nodeId": chain_node_ids(today)["day"]}},
            -2000,
        )
    )
    store.apply_remote(
        _envelope(
            "property.set",
            {"objectId": NODE_B, "propertySchemaId": OPENED, "value": {"nodeId": chain_node_ids(last_month)["day"]}},
            -1000,
        )
    )
    return store


class TestPlaceholderExecution:
    def test_after_today_floors_at_midnight_before_today_ceilings_at_end_of_day(self, world_store: Any) -> None:
        from notees_gtk.core.protocol.query_ast import parse_query_ast

        after = run_query(
            world_store._conn,
            parse_query_ast(_ast([{"type": "date", "field": "created", "op": "after", "timestamp": "{today}"}])),
            CompileOptions(workspace_id=WS, now=NOW),
        )
        assert NODE_B in after.ids and NODE_A not in after.ids
        before = run_query(
            world_store._conn,
            parse_query_ast(_ast([{"type": "date", "field": "created", "op": "before", "timestamp": "{today}"}])),
            CompileOptions(workspace_id=WS, now=NOW),
        )
        assert NODE_A in before.ids and NODE_B in before.ids

    def test_a_date_property_eq_today_matches_the_node_dated_today(self, world_store: Any) -> None:
        from notees_gtk.core.protocol.query_ast import parse_query_ast

        result = run_query(
            world_store._conn,
            parse_query_ast(_ast([{"type": "property", "schemaId": OPENED, "op": "eq", "value": "{today}"}])),
            CompileOptions(workspace_id=WS, now=NOW),
        )
        assert list(result.ids) == [NODE_A]

    def test_gte_this_month_keeps_todays_date_and_drops_last_months(self, world_store: Any) -> None:
        from notees_gtk.core.protocol.query_ast import parse_query_ast

        result = run_query(
            world_store._conn,
            parse_query_ast(_ast([{"type": "property", "schemaId": OPENED, "op": "gte", "value": "{this_month}"}])),
            CompileOptions(workspace_id=WS, now=NOW),
        )
        assert list(result.ids) == [NODE_A]
