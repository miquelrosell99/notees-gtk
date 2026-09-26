"""Shared test fixtures and helpers for notees-gtk."""

from __future__ import annotations

import json
import sqlite3
import threading
import typing
from datetime import datetime
from typing import Any

#: Verbatim copy of the server derived schema's ``node`` table
#: (``v2/packages/store/src/schema.ts`` in the Notees monorepo). A snapshot
#: blob is a serialized derived database, so snapshot fakes MUST be built from
#: the real DDL — inventing a ``nodes``-plural table here once hid a restore
#: bug that only surfaces against a real server.
SERVER_NODE_DDL = """
CREATE TABLE IF NOT EXISTS node (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    node_type TEXT NOT NULL DEFAULT 'block'
        CHECK (node_type IN ('page', 'block', 'class')),
    parent_id TEXT REFERENCES node(id),
    class_ids TEXT NOT NULL DEFAULT '[]',
    name TEXT,
    content TEXT NOT NULL DEFAULT '[]',
    icon TEXT,
    color TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    updated_at TEXT,
    created_by TEXT,
    updated_by TEXT,
    -- Server-only: used for last-write-wins merges.
    hlc_physical INTEGER NOT NULL DEFAULT 0,
    hlc_logical INTEGER NOT NULL DEFAULT 0,
    actor_id TEXT,
    CHECK (node_type <> 'block' OR parent_id IS NOT NULL),
    CHECK (node_type <> 'class' OR parent_id IS NULL)
)
"""


def make_server_snapshot(rows: list[dict[str, Any]]) -> bytes:
    """Serialize a fake server snapshot DB built from the real derived DDL.

    Args:
        rows: ``node`` rows as column→value dicts. ``kind`` is required by
            the real CHECK constraint; omitted columns take the server defaults.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(SERVER_NODE_DDL)
    for row in rows:
        cols = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        conn.execute(f"INSERT INTO node ({cols}) VALUES ({placeholders})", tuple(row.values()))
    blob = conn.serialize()
    conn.close()
    return blob


def normalize_json(value: typing.Any, key: str | None = None) -> typing.Any:
    """Reduce a decoded JSON doc to comparable values.

    ``timestamp`` strings become timezone-aware datetimes so comparisons are
    instant-based: pydantic serializes ``...06.400Z`` as ``...06.400000Z`` and
    trims ``.000`` — same moment, different bytes.
    """
    if isinstance(value, dict):
        return {k: normalize_json(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize_json(v) for v in value]
    if key == "timestamp" and isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


# ------------------------------------------------------------ realtime stub


def wait_until(predicate: typing.Callable[[], bool], timeout: float = 5.0, interval: float = 0.02) -> bool:
    """Poll ``predicate`` until true or the deadline; returns the final value."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class WsRelayStub:
    """Scripted in-process relay for realtime client/engine tests: a
    ``websockets`` sync server on a background thread pushes frames into the
    client and records the frames (and close codes) the client sends."""

    def __init__(self, on_connect: typing.Callable[[typing.Any, int], None] | None = None) -> None:
        self._on_connect = on_connect
        self.connections = 0
        self.close_codes: list[int | None] = []
        self.received: list[dict[str, typing.Any]] = []
        self._conns: set[typing.Any] = set()
        self._lock = threading.Lock()
        self._server = None
        self._thread: threading.Thread | None = None
        self.port = 0

    def __enter__(self) -> WsRelayStub:
        from websockets.sync.server import serve

        self._server = serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        assert self._server is not None
        self._server.shutdown()

    def _handler(self, conn: typing.Any) -> None:
        with self._lock:
            self.connections += 1
            index = self.connections
            self._conns.add(conn)
        try:
            if self._on_connect is not None:
                self._on_connect(conn, index)
            for message in conn:  # Receive loop ends when the client closes.
                try:
                    decoded = json.loads(message)
                except ValueError:
                    decoded = {"malformed": message}
                with self._lock:
                    self.received.append(decoded)
        finally:
            with self._lock:
                self._conns.discard(conn)
                self.close_codes.append(conn.close_code)

    def send_all(self, frame: dict[str, typing.Any] | str) -> None:
        payload = frame if isinstance(frame, str) else json.dumps(frame)
        with self._lock:
            targets = list(self._conns)
        for conn in targets:
            conn.send(payload)

    def close_all(self, code: int = 1011) -> None:
        """Abnormally close every current connection (client must reconnect)."""
        with self._lock:
            targets = list(self._conns)
        for conn in targets:
            conn.close(code=code)

    def connection_count(self) -> int:
        with self._lock:
            return self.connections
