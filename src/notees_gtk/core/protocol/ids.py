"""UUIDv7 generation (RFC 9562) using only the standard library.

UUIDv7 encodes the Unix epoch milliseconds in the most-significant bits, so
generated identifiers are roughly sortable by creation time. Within the same
millisecond, ``rand_a`` is a fixed-length monotonically incrementing counter
(RFC 9562 §5.2, Method 1) so generated UUIDs stay strictly increasing; when
the counter would overflow, generation spins until the wall clock advances to
the next millisecond. Generation is thread-safe: all shared state is guarded
by a module-level lock.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid

__all__ = ["new_uuid7"]

_RAND_A_MAX = 0xFFF

_LOCK = threading.Lock()
_LAST_MS = 0
_LAST_RAND_A = 0


def _now_ms() -> int:
    """Return the current Unix epoch time in milliseconds."""
    return time.time_ns() // 1_000_000


def new_uuid7() -> str:
    """Return a new UUIDv7 as a hyphenated lowercase hex string.

    Layout: 48-bit big-endian Unix epoch milliseconds, version nibble ``7``,
    12-bit ``rand_a`` (monotonic counter within a millisecond), variant bits
    ``10``, and 62 random ``rand_b`` bits.

    Returns:
        The string form of a fresh UUIDv7.
    """
    global _LAST_MS, _LAST_RAND_A
    with _LOCK:
        ms = _now_ms()
        if ms <= _LAST_MS:
            rand_a = _LAST_RAND_A + 1
            if rand_a > _RAND_A_MAX:
                while (ms := _now_ms()) <= _LAST_MS:
                    pass
                rand_a = secrets.randbits(12)
        else:
            rand_a = secrets.randbits(12)
        _LAST_MS = ms
        _LAST_RAND_A = rand_a
        value = (ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | secrets.randbits(62)
    return str(uuid.UUID(int=value))
