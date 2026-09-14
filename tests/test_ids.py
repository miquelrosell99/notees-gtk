"""Tests for the in-repo RFC 9562 UUIDv7 generator."""

from __future__ import annotations

import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from notees_gtk.core.protocol.ids import new_uuid7

UUID7_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def test_string_format() -> None:
    for _ in range(1000):
        value = new_uuid7()
        assert UUID7_RE.match(value), value


def test_uuid_round_trip_and_timestamp_near_now() -> None:
    before_ms = time.time_ns() // 1_000_000
    parsed = uuid.UUID(new_uuid7())
    after_ms = time.time_ns() // 1_000_000
    assert parsed.version == 7
    # The 48 most-significant bits hold the Unix epoch milliseconds.
    embedded_ms = parsed.int >> 80
    assert before_ms - 1000 <= embedded_ms <= after_ms + 1000


def test_burst_of_20k_is_distinct_and_monotonic() -> None:
    values = [new_uuid7() for _ in range(20_000)]
    assert len(set(values)) == len(values)
    assert values == sorted(values)


def test_concurrent_generation_is_distinct() -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: new_uuid7(), range(8 * 1000)))
    assert len(set(values)) == len(values)
    for value in values:
        assert UUID7_RE.match(value), value
