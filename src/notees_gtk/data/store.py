"""Local SQLite store for the Notees GTK client.

Owns the offline-capable client cache: the relay outbox (pending/quarantined
envelopes), the op-id dedupe log, the sync watermark (seq cursor +
``restore_epoch``), and the mirrored node table. Mirrors the mobile client's
``AppDatabase`` contract: idempotent ``PRAGMA user_version`` migrations where
every schema change is guarded (``CREATE TABLE IF NOT EXISTS`` /
``_add_column_if_missing``), WAL journaling, and last-write-wins node content
gated through ``node_content_hlc``.

Thread-safety: the store is constructed on the GTK main thread while the sync
engine runs on worker threads against the same connection. The connection is
therefore opened with ``check_same_thread=False`` and every public method
serializes through a re-entrant lock (see :func:`_synchronized`); private
helpers are only ever called from locked public methods.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import tempfile
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any, Concatenate

from notees_gtk.core.protocol.clock import Hlc, compare_hlc
from notees_gtk.core.protocol.models import RelayEnvelope

__all__ = ["LocalStore", "NodeRow"]

_log = logging.getLogger(__name__)

#: Latest schema version applied to the database (see ``_MIGRATIONS``).
SCHEMA_VERSION = 2

#: Client ``nodes`` columns that are NOT NULL with no DEFAULT while nullable on
#: the server (server ``node.updated_at`` is plain TEXT); coalesced to '' when
#: restoring snapshots so real server blobs never fail the intersection insert.
_NOT_NULL_NO_DEFAULT = frozenset({"updated_at"})


@dataclass(frozen=True)
class NodeRow:
    """One row of the mirrored node table (render-ready projection).

    Attributes:
        id: Node id (uuid7).
        workspace_id: Workspace the node belongs to.
        parent_id: Parent node id, or ``None`` for roots.
        node_type: Node kind from ``node.create`` (``page``/``block``/...).
        name: Placeholder for future search integration; always ``''`` today.
        icon: Emoji/icon string or ``None``.
        color: Color string or ``None``.
        archived: Whether the node is archived (hidden from default listings).
        content: Raw content mirror string (JSON AST or plaintext); unwrapped
            on render by the UI layer.
    """

    id: str
    workspace_id: str
    parent_id: str | None
    node_type: str
    name: str
    icon: str | None
    color: str | None
    archived: bool
    content: str | None


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def _envelope_ts(env: RelayEnvelope) -> str:
    """Return the envelope timestamp as ISO-8601, falling back to now."""
    return env.timestamp.isoformat() if env.timestamp is not None else _now_iso()


def _content_to_string(value: Any) -> str | None:
    """Normalize a ``content`` payload carrier to its stored string form.

    Strings are the serialized AST JSON (or bare plaintext) and are stored
    verbatim; legacy AST lists/dicts are serialized to JSON. ``None`` means
    "no content carried".
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _synchronized[**P, R](method: Callable[Concatenate[LocalStore, P], R]) -> Callable[Concatenate[LocalStore, P], R]:
    """Run a :class:`LocalStore` public method while holding the instance lock.

    The single SQLite connection is shared between the GTK main thread and
    worker threads (``check_same_thread=False``); this decorator serializes
    every public entry point. The lock is re-entrant, so locked methods may
    call each other (and private helpers) freely.
    """

    @wraps(method)
    def wrapper(self: LocalStore, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class LocalStore:
    """SQLite-backed local cache and relay outbox.

    Args:
        path: Filesystem path of the database file.

    All migrations are idempotent: opening a fresh database applies the full
    chain, and re-opening (or downgrading ``user_version``) never fails with
    "duplicate column name" / "table already exists". The instance is safe to
    share across threads: the connection is opened with
    ``check_same_thread=False`` and every public method serializes through a
    re-entrant lock.
    """

    def __init__(self, path: str | Path) -> None:
        """Open (creating if needed) the database and apply pending migrations."""
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._appliers: dict[str, Callable[[RelayEnvelope], bool]] = {
            "node.create": self._apply_create,
            "node.delete": self._apply_delete,
            "node.move": self._apply_move,
            "node.updateContent": self._apply_update_content,
            "node.updateIcon": self._apply_update_icon,
            "node.updateColor": self._apply_update_color,
            "node.archive": self._apply_archive,
            "node.restore": self._apply_restore,
        }
        self._migrate()

    # ------------------------------------------------------------------ schema

    def _migrate(self) -> None:
        """Apply every migration step newer than the stored ``user_version``."""
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        for target, step in _MIGRATIONS:
            if version < target:
                with self._conn:
                    step(self._conn)
                    self._conn.execute(f"PRAGMA user_version = {target}")
                version = target

    @staticmethod
    def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        """Add ``column`` to ``table`` unless it already exists.

        Every column-adding migration goes through this guard: upgrade paths
        from versions predating the column create the table at its current
        shape, so an unguarded ``ALTER TABLE ... ADD COLUMN`` would fail with
        "duplicate column name" on exactly those paths (mobile gotcha).
        """
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    # ------------------------------------------------------------------ outbox

    @_synchronized
    def enqueue(self, env: RelayEnvelope) -> None:
        """Append an envelope to the relay outbox for later submission.

        The null-content guard mirrors the mobile producer: a
        ``node.updateContent`` without ``content`` would be rejected by the
        server with 422 and quarantined, so it is skipped here and logged.
        """
        if env.op_type == "node.updateContent" and env.payload.get("content") is None:
            _log.warning("Skipping enqueue of %s: node.updateContent carries no content", env.id)
            return
        with self._conn:
            self._conn.execute(
                "INSERT INTO relay_outbox (envelope_json, workspace_id, state, created_at)"
                " VALUES (?, ?, 'pending', ?)",
                (json.dumps(env.model_dump(mode="json", by_alias=True)), env.workspace_id, _now_iso()),
            )

    @_synchronized
    def pending_outbox(self, workspace_id: str, limit: int = 100) -> list[RelayEnvelope]:
        """Return up to ``limit`` pending envelopes for a workspace, oldest first."""
        rows = self._conn.execute(
            "SELECT envelope_json FROM relay_outbox"
            " WHERE workspace_id = ? AND state = 'pending' ORDER BY id LIMIT ?",
            (workspace_id, limit),
        ).fetchall()
        return [RelayEnvelope.model_validate(json.loads(raw)) for (raw,) in rows]

    @_synchronized
    def mark_outbox_sent(self, ids: Iterable[str]) -> None:
        """Remove outbox rows whose envelope id is in ``ids`` (whole-chunk ack)."""
        row_ids = self._matching_outbox_rows(ids)
        if not row_ids:
            return
        placeholders = ", ".join("?" for _ in row_ids)
        with self._conn:
            self._conn.execute(f"DELETE FROM relay_outbox WHERE id IN ({placeholders})", row_ids)

    @_synchronized
    def quarantine_outbox(self, ids: Iterable[str], reason: str) -> None:
        """Park outbox rows whose envelope id is in ``ids`` as ``quarantined``."""
        row_ids = self._matching_outbox_rows(ids)
        if not row_ids:
            return
        placeholders = ", ".join("?" for _ in row_ids)
        with self._conn:
            self._conn.execute(
                f"UPDATE relay_outbox SET state = 'quarantined', quarantine_reason = ? WHERE id IN ({placeholders})",
                [reason, *row_ids],
            )

    def _matching_outbox_rows(self, ids: Iterable[str]) -> list[int]:
        """Resolve envelope ids to pending outbox row ids (envelope ids are uuid7-unique)."""
        wanted = set(ids)
        if not wanted:
            return []
        rows = self._conn.execute("SELECT id, envelope_json FROM relay_outbox WHERE state = 'pending'").fetchall()
        return [row_id for row_id, raw in rows if json.loads(raw).get("id") in wanted]

    # -------------------------------------------------------------- sync state

    @_synchronized
    def cursor(self, workspace_id: str) -> int:
        """Return the persisted catch-up seq cursor (0 when never synced)."""
        row = self._conn.execute(
            "SELECT cursor_seq FROM sync_watermark WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
        return int(row[0]) if row is not None else 0

    @_synchronized
    def set_cursor(self, workspace_id: str, seq: int) -> None:
        """Persist the catch-up seq cursor for a workspace."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO sync_watermark (workspace_id, cursor_seq, restore_epoch) VALUES (?, ?, 0)"
                " ON CONFLICT(workspace_id) DO UPDATE SET cursor_seq = excluded.cursor_seq",
                (workspace_id, seq),
            )

    @_synchronized
    def stored_restore_epoch(self, workspace_id: str) -> int:
        """Return the persisted server ``restore_epoch`` (0 when never synced)."""
        row = self._conn.execute(
            "SELECT restore_epoch FROM sync_watermark WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
        return int(row[0]) if row is not None else 0

    @_synchronized
    def set_restore_epoch(self, workspace_id: str, epoch: int) -> None:
        """Persist the server ``restore_epoch`` for a workspace."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO sync_watermark (workspace_id, cursor_seq, restore_epoch) VALUES (?, 0, ?)"
                " ON CONFLICT(workspace_id) DO UPDATE SET restore_epoch = excluded.restore_epoch",
                (workspace_id, epoch),
            )

    @_synchronized
    def wipe(self, workspace_id: str) -> None:
        """Delete every piece of local state for a workspace (all tables)."""
        with self._conn:
            self._conn.execute(
                "DELETE FROM node_content_hlc"
                " WHERE node_id IN (SELECT id FROM nodes WHERE workspace_id = ?)",
                (workspace_id,),
            )
            self._conn.execute("DELETE FROM nodes WHERE workspace_id = ?", (workspace_id,))
            self._conn.execute("DELETE FROM relay_outbox WHERE workspace_id = ?", (workspace_id,))
            self._conn.execute("DELETE FROM relay_operations WHERE workspace_id = ?", (workspace_id,))
            self._conn.execute("DELETE FROM sync_watermark WHERE workspace_id = ?", (workspace_id,))

    # ------------------------------------------------------------ remote apply

    @_synchronized
    def apply_remote(self, env: RelayEnvelope) -> bool:
        """Apply one remote envelope to the local cache.

        The envelope id is recorded in ``relay_operations`` first, making the
        apply idempotent across catch-up/live overlap. Returns ``True`` when a
        known applier mutated (or explicitly consumed) the envelope,
        ``False`` for dedupe hits, unknown op types (logged and skipped), and
        LWW/null-content skips.
        """
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO relay_operations (op_id, workspace_id, seq, applied_at)"
            " VALUES (?, ?, NULL, ?)",
            (env.id, env.workspace_id, _now_iso()),
        )
        if cursor.rowcount == 0:
            return False
        applier = self._appliers.get(env.op_type)
        if applier is None:
            _log.warning("Skipping unknown op type %s (envelope %s)", env.op_type, env.id)
            self._conn.commit()
            return False
        with self._conn:
            return applier(env)

    def _apply_create(self, env: RelayEnvelope) -> bool:
        payload = env.payload
        node_id = str(payload["nodeId"])
        content = _content_to_string(payload.get("content"))
        write_content = content is not None and self._content_hlc_allows(node_id, env.hlc)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO nodes
                    (workspace_id, id, parent_id, node_type, name, icon, color, archived, content, updated_at)
                VALUES (?, ?, ?, ?, '', ?, ?, 0, ?, ?)
                ON CONFLICT(workspace_id, id) DO UPDATE SET
                    parent_id = excluded.parent_id,
                    node_type = excluded.node_type,
                    icon = excluded.icon,
                    color = excluded.color,
                    content = COALESCE(excluded.content, nodes.content),
                    updated_at = excluded.updated_at
                """,
                (
                    env.workspace_id,
                    node_id,
                    payload.get("parentId"),
                    str(payload.get("kind") or ""),
                    payload.get("icon"),
                    payload.get("color"),
                    content if write_content else None,
                    _envelope_ts(env),
                ),
            )
            if write_content:
                self._set_content_hlc(node_id, env.hlc)
        return True

    def _apply_delete(self, env: RelayEnvelope) -> bool:
        node_id = str(env.payload["nodeId"])
        with self._conn:
            self._conn.execute("DELETE FROM nodes WHERE workspace_id = ? AND id = ?", (env.workspace_id, node_id))
            self._conn.execute("DELETE FROM node_content_hlc WHERE node_id = ?", (node_id,))
        return True

    def _apply_move(self, env: RelayEnvelope) -> bool:
        with self._conn:
            self._conn.execute(
                "UPDATE nodes SET parent_id = ? WHERE workspace_id = ? AND id = ?",
                (env.payload.get("newParentId"), env.workspace_id, str(env.payload["nodeId"])),
            )
        return True

    def _apply_update_icon(self, env: RelayEnvelope) -> bool:
        with self._conn:
            self._conn.execute(
                "UPDATE nodes SET icon = ? WHERE workspace_id = ? AND id = ?",
                (env.payload.get("icon"), env.workspace_id, str(env.payload["nodeId"])),
            )
        return True

    def _apply_update_color(self, env: RelayEnvelope) -> bool:
        with self._conn:
            self._conn.execute(
                "UPDATE nodes SET color = ? WHERE workspace_id = ? AND id = ?",
                (env.payload.get("color"), env.workspace_id, str(env.payload["nodeId"])),
            )
        return True

    def _apply_archive(self, env: RelayEnvelope) -> bool:
        with self._conn:
            self._conn.execute(
                "UPDATE nodes SET archived = 1 WHERE workspace_id = ? AND id = ?",
                (env.workspace_id, str(env.payload["nodeId"])),
            )
        return True

    def _apply_restore(self, env: RelayEnvelope) -> bool:
        with self._conn:
            self._conn.execute(
                "UPDATE nodes SET archived = 0 WHERE workspace_id = ? AND id = ?",
                (env.workspace_id, str(env.payload["nodeId"])),
            )
        return True

    def _apply_update_content(self, env: RelayEnvelope) -> bool:
        payload = env.payload
        node_id = str(payload["nodeId"])
        content = _content_to_string(payload.get("content"))
        if content is None:
            # The server rejects these on submit (422); be lenient on apply.
            return False
        if not self._content_hlc_allows(node_id, env.hlc):
            return False
        with self._conn:
            self._conn.execute(
                "UPDATE nodes SET content = ?, updated_at = ? WHERE workspace_id = ? AND id = ?",
                (content, _envelope_ts(env), env.workspace_id, node_id),
            )
            self._set_content_hlc(node_id, env.hlc)
        return True

    def _content_hlc(self, node_id: str) -> Hlc | None:
        row = self._conn.execute(
            "SELECT physical, logical FROM node_content_hlc WHERE node_id = ?", (node_id,)
        ).fetchone()
        return Hlc(physical=int(row[0]), logical=int(row[1])) if row is not None else None

    def _content_hlc_allows(self, node_id: str, incoming: Hlc) -> bool:
        """Gate content writes last-write-wins: incoming must beat the stored HLC."""
        stored = self._content_hlc(node_id)
        return stored is None or compare_hlc(incoming, stored) > 0

    def _set_content_hlc(self, node_id: str, hlc: Hlc) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO node_content_hlc (node_id, physical, logical) VALUES (?, ?, ?)",
            (node_id, hlc.physical, hlc.logical),
        )

    # -------------------------------------------------------------- node cache

    @_synchronized
    def nodes(self, workspace_id: str, parent_id: str | None = None, include_archived: bool = False) -> list[NodeRow]:
        """List cached node rows, optionally filtered by parent.

        ``parent_id=None`` applies no parent filter (all nodes in the
        workspace); pass a string to list one parent's children. Archived
        nodes are excluded unless ``include_archived`` is set.
        """
        sql = (
            "SELECT id, workspace_id, parent_id, node_type, name, icon, color, archived, content"
            " FROM nodes WHERE workspace_id = ?"
        )
        params: list[Any] = [workspace_id]
        if parent_id is not None:
            sql += " AND parent_id = ?"
            params.append(parent_id)
        if not include_archived:
            sql += " AND archived = 0"
        return [self._to_node_row(row) for row in self._conn.execute(sql, params).fetchall()]

    @_synchronized
    def node(self, workspace_id: str, node_id: str) -> NodeRow | None:
        """Return one cached node row, or ``None`` when unknown."""
        row = self._conn.execute(
            "SELECT id, workspace_id, parent_id, node_type, name, icon, color, archived, content"
            " FROM nodes WHERE workspace_id = ? AND id = ?",
            (workspace_id, node_id),
        ).fetchone()
        return self._to_node_row(row) if row is not None else None

    @staticmethod
    def _to_node_row(row: tuple[Any, ...]) -> NodeRow:
        return NodeRow(
            id=str(row[0]),
            workspace_id=str(row[1]),
            parent_id=row[2],
            node_type=str(row[3]),
            name=str(row[4]),
            icon=row[5],
            color=row[6],
            archived=bool(row[7]),
            content=row[8],
        )

    # ---------------------------------------------------------------- snapshots

    @_synchronized
    def restore_snapshot(self, blob: bytes, *, workspace_id: str) -> bool:
        """Restore nodes from a downloaded snapshot blob (serialized derived DB).

        The blob is attached as a temporary SQLite database and only columns
        present in *both* schemas are copied (``INSERT OR REPLACE``) — the
        server derived schema has more columns than the client cache. The
        server's ``updated_at`` is nullable while the client cache is not, so
        that column is coalesced to an empty string. On any SQLite error or
        zero overlapping columns the blob is detached and ``False`` is
        returned with local state untouched.
        """
        fd, path = tempfile.mkstemp(prefix="notees-snapshot-", suffix=".db")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(blob)
        except OSError as exc:
            _log.warning("Snapshot restore failed: cannot stage blob: %s", exc)
            os.unlink(path)
            return False
        attached = False
        try:
            self._conn.execute("ATTACH DATABASE ? AS snapshot_src", (path,))
            attached = True
            remote_columns = {row[1] for row in self._conn.execute("PRAGMA snapshot_src.table_info(nodes)")}
            local_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(nodes)")}
            common = sorted(remote_columns & local_columns)
            if not common:
                _log.warning("Snapshot restore failed: no overlapping node columns")
                return False
            columns = ", ".join(f'"{name}"' for name in common)
            select_exprs = ", ".join(
                f"COALESCE(\"{name}\", '')" if name in _NOT_NULL_NO_DEFAULT else f'"{name}"' for name in common
            )
            select = f"SELECT {select_exprs} FROM snapshot_src.nodes"
            params: tuple[Any, ...] = ()
            if "workspace_id" in common:
                select += ' WHERE "workspace_id" = ?'
                params = (workspace_id,)
            with self._conn:
                self._conn.execute(f"INSERT OR REPLACE INTO nodes ({columns}) {select}", params)
            return True
        except sqlite3.Error as exc:
            _log.warning("Snapshot restore failed: %s", exc)
            return False
        finally:
            if attached:
                try:
                    self._conn.execute("DETACH DATABASE snapshot_src")
                except sqlite3.Error:
                    _log.debug("snapshot_src detach failed", exc_info=True)
            with contextlib.suppress(OSError):
                os.unlink(path)

    # ---------------------------------------------------------------- lifecycle

    @_synchronized
    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """Create the initial schema at its current shape (idempotent)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS relay_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            envelope_json TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            quarantine_reason TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS relay_operations (
            op_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            seq INTEGER,
            applied_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sync_watermark (
            workspace_id TEXT PRIMARY KEY,
            cursor_seq INTEGER NOT NULL DEFAULT 0,
            restore_epoch INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS nodes (
            workspace_id TEXT NOT NULL,
            id TEXT NOT NULL,
            parent_id TEXT,
            node_type TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            icon TEXT,
            color TEXT,
            archived INTEGER NOT NULL DEFAULT 0,
            content TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (workspace_id, id)
        );
        CREATE TABLE IF NOT EXISTS node_content_hlc (
            node_id TEXT PRIMARY KEY,
            physical INTEGER NOT NULL,
            logical INTEGER NOT NULL
        );
        """
    )


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Add the outbox ``attempts`` retry counter (guarded ALTER)."""
    LocalStore._add_column_if_missing(conn, "relay_outbox", "attempts", "INTEGER NOT NULL DEFAULT 0")


#: Ordered migration chain; each entry bumps ``PRAGMA user_version`` to its target.
_MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = (
    (1, _migrate_v1),
    (2, _migrate_v2),
)
