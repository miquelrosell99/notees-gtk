"""Shared test fixtures and helpers for notees-gtk."""

from __future__ import annotations

import sqlite3
from typing import Any

#: Verbatim copy of the server derived schema's ``node`` table
#: (``app/core/derived/schema.py`` in the Notees backend). A snapshot blob is a
#: serialized derived database, so snapshot fakes MUST be built from the real
#: DDL — inventing a ``nodes``-plural table here once hid a restore bug that
#: only surfaces against a real server.
SERVER_NODE_DDL = """
CREATE TABLE IF NOT EXISTS node (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('page', 'block')),
    class_ids TEXT NOT NULL DEFAULT '[]',
    parent_id TEXT,
    content TEXT NOT NULL DEFAULT '[]',
    icon TEXT,
    color TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    updated_at TEXT,
    created_by TEXT,
    updated_by TEXT,
    -- Server-only: used for last-write-wins content merging.
    hlc_physical INTEGER NOT NULL DEFAULT 0,
    hlc_logical INTEGER NOT NULL DEFAULT 0
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
