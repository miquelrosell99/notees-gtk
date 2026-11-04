"""Query compile-time placeholders — the ``{today}``-style editor-relative
date tokens.

Port of ``packages/query/src/placeholders.ts``. A placeholder is an ordinary
string in the AST (the model accepts any string in the positions that carry
them), resolved to a concrete date at COMPILE time against the injected
``now`` clock, so a saved view authored with ``{today}`` re-evaluates against
the day it runs on.

Supported set (deliberately small): ``{today}``, ``{this_week}`` (ISO week,
Monday start), ``{this_month}``, ``{this_year}``. Positions:

- ``date`` condition after/before timestamps resolve to an ISO-8601 DATETIME
  at the period's LOCAL start (after) or local end (before), expressed in UTC
  with a ``Z`` suffix (``created_at`` is UTC on the wire) — so
  ``date before "{today}"`` includes everything created on the local day,
  whatever the machine's offset;
- property-condition string values resolve to the period's START as a plain
  ``YYYY-MM-DD`` date — the same shape as typing the date by hand.

Anything outside the known set is left verbatim — a literal ``"{banana}"``
value stays a literal (compile never crashes on foreign data).

Local-day semantics: an aware ``now`` converts to the system local zone
(``astimezone()``), a naive ``now`` IS the local wall clock — either way the
boundary is the local calendar day's 00:00 / 23:59:59.999 rendered in UTC,
mirroring the TS ``Date`` local-to-UTC conversion.
"""

from __future__ import annotations

import calendar
import re
from datetime import UTC, datetime, timedelta

__all__ = [
    "QUERY_PLACEHOLDERS",
    "iso_date",
    "local_day_boundary_utc",
    "placeholder_end_date",
    "placeholder_start_date",
    "resolve_day_bounds",
    "resolve_period_bounds",
    "resolve_timestamp_placeholder",
    "resolve_value_placeholder",
]

#: The supported placeholder tokens, in picker order.
QUERY_PLACEHOLDERS: tuple[str, ...] = ("{today}", "{this_week}", "{this_month}", "{this_year}")

#: The token form of the relative periods.
PERIOD_TOKENS: dict[str, str] = {
    "today": "{today}",
    "this_week": "{this_week}",
    "this_month": "{this_month}",
    "this_year": "{this_year}",
}

#: A plain ``YYYY-MM-DD`` date (the day-granular arms recognize it).
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _local(now: datetime) -> datetime:
    """The machine-local view of ``now`` (the TS ``Date`` local-field semantics)."""
    return now if now.tzinfo is None else now.astimezone()


def _js_iso(dt: datetime) -> str:
    """The JS ``Date.toISOString()`` shape: millisecond precision, ``Z``."""
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_date(value: datetime) -> str:
    """``YYYY-MM-DD`` for a datetime's LOCAL calendar day."""
    local = _local(value)
    return f"{local.year:04d}-{local.month:02d}-{local.day:02d}"


def local_day_boundary_utc(iso: str, end: bool) -> str:
    """The UTC instant of a local calendar day's boundary (00:00 local for the
    start, 23:59:59.999 local for the end), as a ``Z``-suffixed ISO-8601
    string comparable lexicographically with the UTC ``created_at`` wire
    format."""
    year, month, day = (int(part) for part in iso.split("-"))
    boundary = datetime(year, month, day, 23, 59, 59, 999000) if end else datetime(year, month, day)
    return _js_iso(boundary)


def _now_or_current(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


def placeholder_start_date(token: str, now: datetime | None = None) -> str | None:
    """The placeholder's period-start date, or ``None`` when the token is not
    one of the supported set."""
    current = _local(_now_or_current(now))
    if token == "{today}":
        return iso_date(current)
    if token == "{this_week}":
        # ISO-8601 week: Monday is day 1 (weekday() 0).
        monday = current - timedelta(days=current.weekday())
        return iso_date(monday)
    if token == "{this_month}":
        return f"{current.year:04d}-{current.month:02d}-01"
    if token == "{this_year}":
        return f"{current.year:04d}-01-01"
    return None


def placeholder_end_date(token: str, now: datetime | None = None) -> str | None:
    """The placeholder's period-end date, or ``None`` when the token is not
    one of the supported set."""
    current = _local(_now_or_current(now))
    if token == "{today}":
        return iso_date(current)
    if token == "{this_week}":
        monday = current - timedelta(days=current.weekday())
        return iso_date(monday + timedelta(days=6))
    if token == "{this_month}":
        last = calendar.monthrange(current.year, current.month)[1]
        return f"{current.year:04d}-{current.month:02d}-{last:02d}"
    if token == "{this_year}":
        return f"{current.year:04d}-12-31"
    return None


def resolve_timestamp_placeholder(value: str, direction: str, now: datetime | None = None) -> str:
    """Resolve a placeholder in a TIMESTAMP position: the period's local day
    start for "after" (inclusive floor), local day end for "before" (inclusive
    ceiling), each expressed in UTC. Non-placeholder input returns as-is.

    Args:
        value: The raw timestamp string.
        direction: ``"after"`` or ``"before"``.
        now: The placeholder clock (tests pin it; default the current instant).
    """
    if direction not in ("after", "before"):
        raise ValueError(f"placeholder: unknown direction {direction!r}")
    resolved = placeholder_start_date(value, now) if direction == "after" else placeholder_end_date(value, now)
    if resolved is None:
        return value
    return local_day_boundary_utc(resolved, direction == "before")


def resolve_value_placeholder(value: str, now: datetime | None = None) -> str:
    """Resolve a placeholder in a PROPERTY-VALUE position: the period start as
    a plain ``YYYY-MM-DD`` date (the shape a hand-typed date would have).
    Non-placeholder input returns as-is."""
    return placeholder_start_date(value, now) or value


def resolve_period_bounds(period: str, now: datetime | None = None) -> tuple[str, str]:
    """The UTC bounds of a relative period: the period's local day-start and
    day-end, each a ``Z``-suffixed ISO-8601 string (the ``within`` arms)."""
    token = PERIOD_TOKENS.get(period, period)
    start = placeholder_start_date(token, now)
    end = placeholder_end_date(token, now)
    if start is None or end is None:
        raise ValueError(f"query: unknown date period {period!r}")
    return (local_day_boundary_utc(start, False), local_day_boundary_utc(end, True))


def resolve_day_bounds(value: str, now: datetime | None = None) -> tuple[str, str]:
    """The DAY bounds behind the day-granular equality (the date condition's
    eq/neq ops): a token resolves to that period's local day; a plain
    ``YYYY-MM-DD`` date to that local day; anything else (a full ISO instant)
    to the instant itself (equality collapses to instant equality)."""
    start = placeholder_start_date(value, now)
    if start is not None:
        end = placeholder_end_date(value, now)
        assert end is not None
        return (local_day_boundary_utc(start, False), local_day_boundary_utc(end, True))
    if _ISO_DATE_RE.match(value):
        return (local_day_boundary_utc(value, False), local_day_boundary_utc(value, True))
    return (value, value)
