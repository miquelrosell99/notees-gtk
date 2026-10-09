"""Deterministic date-node ids — the scheme ported from
``app/domain/entities/constants.py`` (``generate_day_uuid`` and siblings),
mirroring ``packages/domain/src/dates.ts``.

A date is a node, not a string (SCHEMA.md "Datetime"): every ISO date maps to a
year / month / day node chain with ids content-addressed from the date, so
chain creation is an idempotent no-op on re-create and migrated data locks
step with the server. Layout (FIXED — never regenerate):

- day    ``00000000-0000-0000-00dd-YYYYMMDD0000``
- month  ``00000000-0000-0000-00aa-YYYYMM000000``
- year   ``00000000-0000-0000-00bb-YYYY00000000``

Amendment 2026-10-09 (the unified-datetime change): time-of-day rides the
property VALUE beside the day-node anchor — a datetime slot is
``{nodeId, time?: "HH:MM"}`` (full-day = no ``time``, the default). The id
layout itself is unchanged and stays FIXED; only the value vocabulary around
it grew (:func:`is_valid_time_of_day`).

PC6 consumes :func:`day_node_id`: the property applier normalizes a
well-formed ``YYYY-MM-DD`` qualifier string to the deterministic day-node
ref, so the stored shape canonicalizes without any graph side effects.
"""

from __future__ import annotations

import re
from datetime import date

__all__ = [
    "DATE_UUID_MAX_YEAR",
    "DATE_UUID_MIN_YEAR",
    "TIME_OF_DAY_PATTERN",
    "chain_node_ids",
    "date_node_id",
    "day_node_id",
    "is_valid_time_of_day",
    "month_node_id",
    "parse_date_node_id",
    "parse_iso_date",
    "year_node_id",
]

#: ``parse_date_uuid`` acceptance window (1900..2200 inclusive).
DATE_UUID_MIN_YEAR = 1900
DATE_UUID_MAX_YEAR = 2200

DAY_PREFIX = "00000000-0000-0000-00dd-"
MONTH_PREFIX = "00000000-0000-0000-00aa-"
YEAR_PREFIX = "00000000-0000-0000-00bb-"

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

#: Datetime property values (unified-datetime, 2026-10-09 — SCHEMA.md
#: "Datetime"): 24h wall-clock ``HH:MM``, minute precision, no timezone (the
#: no-timezone law stands — times are local wall-clock, never an offset or a
#: zone). ``packages/domain/src/dates.ts`` ``TIME_OF_DAY_PATTERN`` parity.
TIME_OF_DAY_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def is_valid_time_of_day(value: object) -> bool:
    """Defensive acceptance: any input, true only for a well-formed ``HH:MM``
    (``packages/domain/src/dates.ts`` ``isValidTimeOfDay`` parity)."""
    return isinstance(value, str) and TIME_OF_DAY_PATTERN.match(value) is not None


def parse_iso_date(iso_date: str) -> date:
    """Strict ``YYYY-MM-DD`` parse with real-calendar validation (leap years
    included). Datetime strings are rejected: date-node ids address whole
    days; time-of-day rides the property value (``time: "HH:MM"`` on the
    slot), never an ISO string. Fail loud — a malformed date must never
    silently produce a node id."""
    match = _ISO_DATE_RE.match(iso_date.strip())
    if match is None:
        raise ValueError(f"invalid ISO date: {iso_date!r} (expected YYYY-MM-DD)")
    year, month, day = int(match[1]), int(match[2]), int(match[3])
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ValueError(f"invalid ISO date: {iso_date!r} (no such calendar day)") from exc


def year_node_id(iso_date: str) -> str:
    """``00000000-0000-0000-00bb-YYYY00000000`` (``generate_year_uuid``)."""
    return f"{YEAR_PREFIX}{parse_iso_date(iso_date).year:04d}00000000"


def month_node_id(iso_date: str) -> str:
    """``00000000-0000-0000-00aa-YYYYMM000000`` (``generate_month_uuid``)."""
    parsed = parse_iso_date(iso_date)
    return f"{MONTH_PREFIX}{parsed.year:04d}{parsed.month:02d}000000"


def day_node_id(iso_date: str) -> str:
    """``00000000-0000-0000-00dd-YYYYMMDD0000`` (``generate_day_uuid``)."""
    parsed = parse_iso_date(iso_date)
    return f"{DAY_PREFIX}{parsed.year:04d}{parsed.month:02d}{parsed.day:02d}0000"


def date_node_id(iso_date: str, precision: str) -> str:
    """The node id a date property value links at the schema's precision."""
    if precision == "year":
        return year_node_id(iso_date)
    if precision == "month":
        return month_node_id(iso_date)
    return day_node_id(iso_date)


def chain_node_ids(iso_date: str) -> dict[str, str]:
    """The full chain for a date: year (root) → month (under year) → day."""
    return {"year": year_node_id(iso_date), "month": month_node_id(iso_date), "day": day_node_id(iso_date)}


def parse_date_node_id(node_id: str) -> dict[str, int | str] | None:
    """``parse_date_uuid`` port: extract precision + date components from a
    date-node id, or ``None`` when the id is not a date UUID (or falls outside
    the 1900..2200 window). Round-trips with the generators above."""
    if not isinstance(node_id, str) or len(node_id) != 36:
        return None
    data = node_id[24:]  # trailing 12-digit payload

    def read(start: int, end: int) -> int | None:
        slice_ = data[start:end]
        return int(slice_) if slice_.isdigit() else None

    if node_id.startswith(DAY_PREFIX):
        year, month, day = read(0, 4), read(4, 6), read(6, 8)
        if (
            year is not None
            and month is not None
            and day is not None
            and DATE_UUID_MIN_YEAR <= year <= DATE_UUID_MAX_YEAR
            and 1 <= month <= 12
            and 1 <= day <= 31
        ):
            return {"precision": "day", "year": year, "month": month, "day": day}
        return None
    if node_id.startswith(MONTH_PREFIX):
        year, month = read(0, 4), read(4, 6)
        if (
            year is not None
            and month is not None
            and DATE_UUID_MIN_YEAR <= year <= DATE_UUID_MAX_YEAR
            and 1 <= month <= 12
        ):
            return {"precision": "month", "year": year, "month": month, "day": 1}
        return None
    if node_id.startswith(YEAR_PREFIX):
        year = read(0, 4)
        if year is not None and DATE_UUID_MIN_YEAR <= year <= DATE_UUID_MAX_YEAR:
            return {"precision": "year", "year": year, "month": 1, "day": 1}
        return None
    return None
