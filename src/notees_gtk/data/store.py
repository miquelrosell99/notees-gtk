"""Local SQLite store for the Notees GTK client (relay protocol v3).

Owns the offline-capable client cache: the relay outbox (pending/quarantined
envelopes), the op-id dedupe log, the sync watermark (seq cursor +
``restore_epoch``), and the mirrored node table with the applier semantics
ported from v2 ``packages/store/src/appliers.ts``: row-level last-write-wins by
``(hlc_physical, hlc_logical, actor_id)``, OR-Set class/tag/collection
membership, user-defined class order (``class.reorder``, LWW-by-arrival),
title-is-content (a node's title IS its content — no ``name`` writes; nodes
with document chrome carry text-only content), the Revision-11 render-state
model (``is_class`` identity + ``present_as_main`` render bit; classes are
containers and always roots), m2m class extends with an
applier-maintained transitive closure (cycles fail loud), fractional
child-order positions, property values with tombstones, and soft/permanent
deletes with trash retention.

Thread-safety: the store is constructed on the GTK main thread while the sync
engine runs on worker threads against the same connection. The connection is
therefore opened with ``check_same_thread=False`` and every public method
serializes through a re-entrant lock (see :func:`_synchronized`); private
helpers are only ever called from locked public methods.

Schema notes: the client cache keeps its own table shapes where they match the
v2 derived schema — the ``nodes`` mirror is keyed ``(workspace_id, id)`` (the
v2 server keys ``id`` alone; node ids are uuid7, so the difference is
theoretical) and carries the same v2 column names. Placement invariants the
server enforces as CHECK constraints (Revision 11: the single
``is_class = 0 OR parent_id IS NULL`` check) are enforced in the applier here
(:class:`~notees_gtk.data.errors.MoveGuardError`), so a corrupt local row
stays repairable via the restore-epoch wipe instead of wedging a table
rebuild.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any, Concatenate

from notees_gtk.core.protocol.content import (
    parse_content_ast,
    plaintext_excerpt,
    stringify_content_ast,
)
from notees_gtk.core.protocol.models import RelayEnvelope
from notees_gtk.core.protocol.payloads import validate_payload
from notees_gtk.data.errors import (
    CycleError,
    EnvelopeValidationError,
    MoveGuardError,
    NotFoundError,
    UnsupportedCarrierError,
)

__all__ = ["EffectiveProperty", "EffectivePropertySchema", "LocalStore", "NodeRow"]

_log = logging.getLogger(__name__)

#: Latest schema version applied to the database (see ``_MIGRATIONS``).
#: v5 adds the tag OR-Set (web schema v5→v6 parity); v6 adds the per-node
#: class order list (web schema v6→v7 parity); v7 is the Revision-11
#: render-state model (web schema v7→v8 parity): the node_type enumeration is
#: replaced by ``is_class`` + ``present_as_main``.
SCHEMA_VERSION = 7

#: Strict pattern every interpolated snapshot identifier must match — closes
#: the quote-breakout surface on names read from the attached snapshot.
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

#: Node tables a snapshot blob may provide. The server's derived schema uses
#: ``node`` (singular); the plural ``nodes`` is the *client* cache's table and
#: must never be accepted (it is how the original restore bug hid in tests).
_SNAPSHOT_TABLES = frozenset({"node"})

#: Snapshot node columns copied verbatim (client cache column → server column).
#: v2 derived schema: same names, same polarity (``is_active`` is active on
#: both sides now); ``updated_at`` falls back to an empty string literal and
#: the LWW columns seed the row baseline when the snapshot carries them.
#: ``tag_ids``/``class_order`` ride along when the snapshot was taken from a
#: schema v6+/v7+ store (absent columns are skipped, the cache defaults win).
#: Revision 11: ``is_class``/``present_as_main`` replace the retired node_type.
_SNAPSHOT_VERBATIM_COLUMNS: dict[str, str] = {
    "id": "id",
    "workspace_id": "workspace_id",
    "is_class": "is_class",
    "present_as_main": "present_as_main",
    "parent_id": "parent_id",
    "class_ids": "class_ids",
    "class_order": "class_order",
    "tag_ids": "tag_ids",
    "name": "name",
    "icon": "icon",
    "color": "color",
    "content": "content",
    "created_at": "created_at",
    "is_active": "is_active",
}


def _quote_ident(name: str) -> str:
    """Quote ``name`` for SQL, rejecting anything outside the strict pattern."""
    if not _IDENT_RE.match(name):
        raise ValueError(f"unsafe snapshot identifier: {name!r}")
    return f'"{name}"'


def _snapshot_select_exprs(remote_columns: set[str]) -> dict[str, str]:
    """Map client cache columns to snapshot SELECT expressions (v2 schema)."""
    exprs: dict[str, str] = {}
    for target, source in _SNAPSHOT_VERBATIM_COLUMNS.items():
        if source in remote_columns:
            exprs[target] = _quote_ident(source)
    if "updated_at" in remote_columns:
        exprs["updated_at"] = f"COALESCE({_quote_ident('updated_at')}, '')"
    else:
        exprs["updated_at"] = "''"
    for column in ("hlc_physical", "hlc_logical", "actor_id"):
        if column in remote_columns:
            exprs[column] = _quote_ident(column)
    return exprs


@dataclass(frozen=True)
class NodeRow:
    """One row of the mirrored node table (render-ready projection).

    Attributes:
        id: Node id (uuid7).
        workspace_id: Workspace the node belongs to.
        parent_id: Parent node id, or ``None`` for roots (legal for any
            non-class node; classes are always roots).
        is_class: Identity marker (Revision 11) — true = class node, always a
            root, renders the ClassView.
        present_as_main: Render bit read only for parented non-class nodes:
            true = the parent's main-children zone + document chrome when
            zoomed; false = inline body + block chrome. Unread for parentless
            nodes (document chrome by the second cascade branch) and classes
            (ClassView by the first branch).
        name: Legacy display-name cache, never written by the v2 appliers
            (title-is-content: a node's title IS its content). Remaining
            readers are transition-only; snapshot restores may populate it.
        class_ids: OR-Set class membership projected from ``class_member_set``
            (ordered members first when ``class.reorder`` wrote a user order,
            then any unlisted present members sorted by id).
        tag_ids: OR-Set tag membership projected from ``tag_member_set``
            (sorted by id).
        icon: Emoji/icon string or ``None``.
        color: Color string or ``None``.
        is_active: False once the node (soft-)deleted; the trash table keeps
            the deletion record.
        content: Serialized flat token array (JSON); ``None`` when the node
            never carried content.
        content_plain: Plaintext derived by the applier (FTS/sidebar excerpt).
    """

    id: str
    workspace_id: str
    parent_id: str | None
    is_class: bool
    present_as_main: bool
    name: str | None
    class_ids: tuple[str, ...]
    tag_ids: tuple[str, ...]
    icon: str | None
    color: str | None
    is_active: bool
    content: str | None
    content_plain: str


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class EffectivePropertySchema:
    """Property schema projection inside an :class:`EffectiveProperty` row."""

    id: str
    name: str
    type: str
    multi: bool


@dataclass(frozen=True)
class EffectiveProperty:
    """One effective ``(schema, idx)`` row for a node — the property panel's
    read surface (SCHEMA.md "Class properties", 2026-09-27).

    ``source`` tags authored vs derived; ``bound_by`` is the class supplying
    the binding metadata — the winning class for a default, the
    currently-binding class for an authored row, or ``None`` when no current
    class binds the schema (an authored value whose binding went away stays
    visible, marked unbound).
    """

    property_schema_id: str
    idx: int
    schema: EffectivePropertySchema | None
    value: Any
    metadata: dict[str, Any] | None
    source: str  # "authored" | "default"
    bound_by: str | None
    required: bool | None
    readonly: bool | None
    hide_when_empty: bool | None
    sequence: int | None


def _envelope_ts(env: RelayEnvelope) -> str:
    """Return the envelope timestamp as ISO-8601, falling back to now."""
    return env.timestamp.isoformat() if env.timestamp is not None else _now_iso()


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
            "object.create": self._apply_object_create,
            "object.update": self._apply_object_update,
            "object.delete": self._apply_object_delete,
            "object.move": self._apply_object_move,
            "class.create": self._apply_class_create,
            "class.update": self._apply_class_update,
            "class.delete": self._apply_class_delete,
            "class.unassign": self._apply_class_unassign,
            "class.reorder": self._apply_class_reorder,
            "tag.unassign": self._apply_tag_unassign,
            "class.setExtends": self._apply_class_set_extends,
            "class.property.set": self._apply_class_property_set,
            "class.property.unset": self._apply_class_property_unset,
            "propertySchema.create": self._apply_property_schema_create,
            "propertySchema.update": self._apply_property_schema_update,
            "propertySchema.delete": self._apply_property_schema_delete,
            "property.set": self._apply_property_set,
            "property.unset": self._apply_property_unset,
            "asset.attach": self._apply_asset_attach,
            "asset.detach": self._apply_asset_detach,
            "collection.member.add": lambda env: self._apply_collection_member(env, present=1, add_wins=True),
            "collection.member.remove": lambda env: self._apply_collection_member(env, present=0, add_wins=False),
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

        The producer-side guard mirrors the server-side payload validation: an
        ``object.update`` carrying no writable field (empty payload beyond
        ``objectId``, or all-``None`` fields) would be rejected with 422 and
        quarantined, so it is skipped here and logged. Payloads of every known
        op type are also validated against the strict wire schemas
        (``core.protocol.payloads``): a retired/renamed key (e.g. the
        title-is-content ``name`` field) or a malformed value fails loud here
        instead of riding the outbox to a certain 422.
        """
        if env.op_type == "object.update":
            writable = [key for key, value in env.payload.items() if key != "objectId" and value is not None]
            if not writable:
                _log.warning("Skipping enqueue of %s: object.update carries no writable field", env.id)
                return
        if env.op_type in self._appliers and "$e" not in env.payload:
            try:
                validate_payload(env.op_type, env.payload)
            except ValueError as exc:
                raise EnvelopeValidationError(f"invalid {env.op_type} payload: {exc}", env.op_type) from exc
        with self._conn:
            self._conn.execute(
                "INSERT INTO relay_outbox (envelope_json, workspace_id, state, created_at) VALUES (?, ?, 'pending', ?)",
                (json.dumps(env.model_dump(mode="json", by_alias=True)), env.workspace_id, _now_iso()),
            )

    @_synchronized
    def pending_outbox(self, workspace_id: str, limit: int = 100) -> list[RelayEnvelope]:
        """Return up to ``limit`` pending envelopes for a workspace, oldest first."""
        rows = self._conn.execute(
            "SELECT envelope_json FROM relay_outbox WHERE workspace_id = ? AND state = 'pending' ORDER BY id LIMIT ?",
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
        node_ids = [
            row[0] for row in self._conn.execute("SELECT id FROM nodes WHERE workspace_id = ?", (workspace_id,))
        ]
        with self._conn:
            if node_ids:
                placeholders = ", ".join("?" for _ in node_ids)
                self._conn.execute(
                    f"DELETE FROM node_child_order WHERE parent_id IN ({placeholders}) OR child_id IN ({placeholders})",
                    [*node_ids, *node_ids],
                )
                for table in (
                    "class_member_set",
                    "tag_member_set",
                    "property_value",
                    "property_value_tombstone",
                    "node_asset",
                ):
                    self._conn.execute(f"DELETE FROM {table} WHERE node_id IN ({placeholders})", node_ids)
                self._conn.execute(
                    f"DELETE FROM collection_member"
                    f" WHERE collection_id IN ({placeholders}) OR object_id IN ({placeholders})",
                    [*node_ids, *node_ids],
                )
                self._conn.execute(f"DELETE FROM trash WHERE node_id IN ({placeholders})", node_ids)
                self._conn.execute(f"DELETE FROM class_property WHERE class_id IN ({placeholders})", node_ids)
            self._conn.execute("DELETE FROM nodes WHERE workspace_id = ?", (workspace_id,))
            self._conn.execute("DELETE FROM relay_outbox WHERE workspace_id = ?", (workspace_id,))
            self._conn.execute("DELETE FROM relay_operations WHERE workspace_id = ?", (workspace_id,))
            self._conn.execute("DELETE FROM sync_watermark WHERE workspace_id = ?", (workspace_id,))

    # ------------------------------------------------------------ remote apply

    @_synchronized
    def apply_remote(self, env: RelayEnvelope) -> bool:
        """Apply one remote envelope to the local cache.

        The envelope id is recorded in ``relay_operations`` first, making the
        apply idempotent across catch-up/live overlap. Known op payloads are
        validated against the strict wire schemas before anything is recorded
        (the web store's ``validateEnvelope``): a deviation raises
        :class:`~notees_gtk.data.errors.EnvelopeValidationError` and nothing
        is written. A guard violation (cycle close, move guard, placement)
        raises the corresponding typed error and rolls the whole apply back —
        including the dedupe record, so the envelope can be retried after a
        wipe/resync. Returns ``True`` when an applier mutated state, ``False``
        for dedupe hits, unknown op types (logged and skipped), and
        LWW/re-create drops.
        """
        applier = self._appliers.get(env.op_type)
        if applier is None:
            _log.warning("Skipping unknown op type %s (envelope %s)", env.op_type, env.id)
            return False
        if "$e" in env.payload:
            raise EnvelopeValidationError(
                "encrypted payload slot ($e) is reserved for M3 E2EE and cannot be applied yet",
                env.op_type,
            )
        try:
            validate_payload(env.op_type, env.payload)
        except ValueError as exc:
            raise EnvelopeValidationError(f"invalid {env.op_type} payload: {exc}", env.op_type) from exc
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO relay_operations (op_id, workspace_id, seq, applied_at) VALUES (?, ?, NULL, ?)",
            (env.id, env.workspace_id, _now_iso()),
        )
        if cursor.rowcount == 0:
            return False
        with self._conn:
            return applier(env)

    # ------------------------------------------------------------- applier utils

    def _node_row(self, workspace_id: str, node_id: str) -> tuple[Any, ...] | None:
        # Column order is the contract: is_class/present_as_main sit at 3/4
        # (the LWW appliers read them positionally) and the row-LWW triple at
        # 12/13/14.
        row: tuple[Any, ...] | None = self._conn.execute(
            "SELECT id, workspace_id, parent_id, is_class, present_as_main, name, class_ids, icon, color,"
            " is_active, content, content_plain, hlc_physical, hlc_logical, actor_id"
            " FROM nodes WHERE workspace_id = ? AND id = ?",
            (workspace_id, node_id),
        ).fetchone()
        return row

    def _require_node(self, workspace_id: str, node_id: str, op_type: str) -> tuple[Any, ...]:
        row = self._node_row(workspace_id, node_id)
        if row is None:
            raise NotFoundError(f"{op_type}: node {node_id} does not exist", op_type)
        return row

    @staticmethod
    def _incoming_wins(
        env: RelayEnvelope, stored_hlc_physical: Any, stored_hlc_logical: Any, stored_actor: Any
    ) -> bool:
        """Row-level LWW: incoming ``(hlc, actor)`` must beat the stored tuple."""
        incoming = (env.hlc.physical, env.hlc.logical, env.actor_id)
        stored = (int(stored_hlc_physical), int(stored_hlc_logical), stored_actor or "")
        return incoming > stored

    def _subtree_ids(self, workspace_id: str, node_id: str) -> list[str]:
        rows = self._conn.execute(
            """WITH RECURSIVE subtree(id) AS (
                 SELECT id FROM nodes WHERE workspace_id = ? AND id = ?
                 UNION ALL
                 SELECT n.id FROM subtree s JOIN nodes n ON n.parent_id = s.id
                 WHERE n.workspace_id = ?
               )
               SELECT id FROM subtree ORDER BY id""",
            (workspace_id, node_id, workspace_id),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def _check_parent_allowed(self, workspace_id: str, parent_id: str, op_type: str) -> tuple[Any, ...]:
        """Shared placement guard for object.create/object.move: the parent
        must exist. Class parents are legal targets (Revision 11, spec I4:
        classes are containers of non-class children); the complementary
        guard — a class node itself may never have a parent — lives in
        ``_apply_object_move`` (the moving side of the tree)."""
        parent = self._node_row(workspace_id, parent_id)
        if parent is None:
            raise NotFoundError(f"{op_type}: parent {parent_id} does not exist", op_type)
        return parent

    # ------------------------------------------------------------------ object.*

    def _apply_object_create(self, env: RelayEnvelope) -> bool:
        op_type = "object.create"
        payload = env.payload
        object_id = str(payload["objectId"])
        parent_id = payload.get("parentId")
        parent_id = str(parent_id) if parent_id is not None else None
        class_ids = [str(class_id) for class_id in (payload.get("classIds") or [])]
        tag_ids = [str(tag_id) for tag_id in (payload.get("tagIds") or [])]
        # Render bit (Revision 11): the payload may carry presentAsMain; the
        # applier defaults it by context — a parentless node presents as main
        # (document chrome by the second cascade branch), a parented one
        # starts inline (block chrome; the "hide from body" gloss is the
        # 0→1 toggle). object.create always makes is_class = 0 nodes; class
        # declaration remains the class.create op.
        present_as_main = payload.get("presentAsMain")
        present_as_main = (1 if parent_id is None else 0) if present_as_main is None else int(bool(present_as_main))
        after_id = payload.get("afterId")
        after_id = str(after_id) if after_id is not None else None
        before_id = payload.get("beforeId")
        before_id = str(before_id) if before_id is not None else None
        ts = _envelope_ts(env)

        # Content flatten invariant (SCHEMA.md): document-chrome nodes
        # (is_class or present_as_main) carry text-only content; an inline
        # block keeps the full rich token stream.
        content_ast = payload.get("contentAst")
        tokens: Any = stringify_content_ast(content_ast) if present_as_main == 1 else (content_ast or [])
        content = json.dumps(tokens, ensure_ascii=False)
        content_plain = plaintext_excerpt(tokens)

        # First create wins for duplicate node ids (v1 INSERT OR IGNORE): a
        # re-create must not touch the TREE — the earlier half-apply added a
        # second child_order row under the new parent while node.parent_id
        # stayed stale, rendering the node under TWO parents. The exceptions
        # are the classIds/tagIds OR-Set seeds below, the convergence carriers
        # for concurrent creates (they cannot move the node).
        for class_id in class_ids:
            self._class_member_upsert(object_id, class_id, env)
        for tag_id in tag_ids:
            self._tag_member_upsert(object_id, tag_id, env)
        if self._node_row(env.workspace_id, object_id) is not None:
            if class_ids:
                self._recompute_class_ids(object_id)
            if tag_ids:
                self._recompute_tag_ids(object_id)
            return False

        if parent_id is not None:
            # Classes are containers (spec I4): a class parent is legal for
            # non-class children — which is all object.create can make.
            self._check_parent_allowed(env.workspace_id, parent_id, op_type)

        with self._conn:
            self._conn.execute(
                """INSERT INTO nodes
                     (workspace_id, id, is_class, present_as_main, parent_id, name, class_ids, content, content_plain,
                      icon, color, is_active, created_at, updated_at, created_by, updated_by,
                      hlc_physical, hlc_logical, actor_id)
                   VALUES (?, ?, 0, ?, ?, NULL, '[]', ?, ?, NULL, NULL, 1, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    env.workspace_id,
                    object_id,
                    present_as_main,
                    parent_id,
                    content,
                    content_plain,
                    ts,
                    ts,
                    env.actor_id,
                    env.actor_id,
                    env.hlc.physical,
                    env.hlc.logical,
                    env.actor_id,
                ),
            )
            self._recompute_class_ids(object_id)
            self._recompute_tag_ids(object_id)
            if parent_id is not None:
                self._conn.execute(
                    "INSERT OR REPLACE INTO node_child_order (parent_id, child_id, position) VALUES (?, ?, ?)",
                    (parent_id, object_id, self._allocate_child_position(parent_id, object_id, after_id, before_id)),
                )
        return True

    def _apply_object_update(self, env: RelayEnvelope) -> bool:
        op_type = "object.update"
        payload = env.payload
        object_id = str(payload["objectId"])
        row = self._require_node(env.workspace_id, object_id, op_type)

        if payload.get("contentDeltaB64") is not None and payload.get("contentAst") is None:
            raise UnsupportedCarrierError(
                f"{op_type}: contentDeltaB64 (canonical CRDT carrier) needs the Yjs port;"
                " reapply with the contentAst readable carrier",
                op_type,
            )

        # Row-level last-write-wins: lower or equal (hlc, actor) writes drop whole.
        if not self._incoming_wins(env, row[12], row[13], row[14]):
            return False

        sets: list[str] = []
        values: list[Any] = []
        # Promotion/demotion (Revision 11) is the presentAsMain toggle: the bit
        # joins the row-level LWW set; a 0→1 flip (promotion) stringifies the
        # rich token stream to text-only in the same op (content flatten
        # invariant), while a 1→0 demotion leaves the (already flattened)
        # content untouched — demotion never un-flattens. On a class row the
        # bit is inert (classes render ClassView regardless); applying it
        # harmlessly keeps the op uniform.
        present_as_main = payload.get("presentAsMain")
        resulting_present_as_main = bool(row[4])
        if present_as_main is not None:
            resulting_present_as_main = bool(present_as_main)
            sets.append("present_as_main = ?")
            values.append(1 if present_as_main else 0)
            if present_as_main and not bool(row[4]):
                flattened = json.dumps(stringify_content_ast(parse_content_ast(row[10])), ensure_ascii=False)
                sets.append("content = ?")
                values.append(flattened)
                sets.append("content_plain = ?")
                values.append(plaintext_excerpt(parse_content_ast(flattened)))
        if payload.get("icon") is not None:
            sets.append("icon = ?")
            values.append(payload["icon"])
        if payload.get("color") is not None:
            sets.append("color = ?")
            values.append(payload["color"])
        if payload.get("contentAst") is not None:
            # Document-chrome content (class nodes and main-presenting nodes)
            # is text-only; inline blocks keep the rich tokens they were sent.
            flatten = bool(row[3]) or resulting_present_as_main
            tokens: Any = (
                payload["contentAst"] if not flatten else stringify_content_ast(payload["contentAst"])
            )
            sets.append("content = ?")
            values.append(json.dumps(tokens, ensure_ascii=False))
            sets.append("content_plain = ?")
            values.append(plaintext_excerpt(tokens))
        sets.append("updated_at = ?")
        values.append(_envelope_ts(env))
        sets.append("updated_by = ?")
        values.append(env.actor_id)
        sets.append("hlc_physical = ?")
        values.append(env.hlc.physical)
        sets.append("hlc_logical = ?")
        values.append(env.hlc.logical)
        sets.append("actor_id = ?")
        values.append(env.actor_id)
        values.extend([env.workspace_id, object_id])
        with self._conn:
            self._conn.execute(f"UPDATE nodes SET {', '.join(sets)} WHERE workspace_id = ? AND id = ?", values)
        return True

    def _apply_object_delete(self, env: RelayEnvelope) -> bool:
        op_type = "object.delete"
        object_id = str(env.payload["objectId"])
        self._require_node(env.workspace_id, object_id, op_type)
        permanent = bool(env.payload.get("permanent"))
        ts = _envelope_ts(env)
        ids = self._subtree_ids(env.workspace_id, object_id)
        placeholders = ", ".join("?" for _ in ids)
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO trash (node_id, deleted_at, is_permanent) VALUES (?, ?, ?)",
                (object_id, ts, 1 if permanent else 0),
            )
            if not permanent:
                # Soft delete: trash the whole subtree (restore is whole-tree),
                # keep the tree rows for restore.
                self._conn.execute(f"UPDATE nodes SET is_active = 0 WHERE id IN ({placeholders})", ids)
                return True
            # Permanent delete: hard-remove the subtree and every derived row.
            self._conn.execute(f"DELETE FROM nodes WHERE id IN ({placeholders})", ids)
            self._conn.execute(
                f"DELETE FROM node_child_order WHERE parent_id IN ({placeholders}) OR child_id IN ({placeholders})",
                [*ids, *ids],
            )
            for table in (
                "class_member_set",
                "tag_member_set",
                "property_value",
                "property_value_tombstone",
                "node_asset",
            ):
                self._conn.execute(f"DELETE FROM {table} WHERE node_id IN ({placeholders})", ids)
            self._conn.execute(
                f"DELETE FROM collection_member"
                f" WHERE collection_id IN ({placeholders}) OR object_id IN ({placeholders})",
                [*ids, *ids],
            )
            self._conn.execute(f"DELETE FROM class_property WHERE class_id IN ({placeholders})", ids)
            self._conn.execute(
                f"DELETE FROM trash WHERE node_id IN ({placeholders}) AND node_id != ?", [*ids, object_id]
            )
        return True

    def _apply_object_move(self, env: RelayEnvelope) -> bool:
        op_type = "object.move"
        payload = env.payload
        object_id = str(payload["objectId"])
        row = self._require_node(env.workspace_id, object_id, op_type)
        parent_id = payload.get("parentId")
        parent_id = str(parent_id) if parent_id is not None else None
        after_id = payload.get("afterId")
        after_id = str(after_id) if after_id is not None else None
        before_id = payload.get("beforeId")
        before_id = str(before_id) if before_id is not None else None

        if parent_id is not None:
            self._check_parent_allowed(env.workspace_id, parent_id, op_type)
            if parent_id in self._subtree_ids(env.workspace_id, object_id):
                raise MoveGuardError(
                    f"{op_type}: cannot move node {object_id} under {parent_id}, which is in its own subtree",
                    op_type,
                )
        if parent_id is not None and bool(row[3]):
            # Classes are always roots (spec I4 makes class nodes containers
            # of non-class children, but the class-under-a-class /
            # class-with-parent shape stays illegal; the DB CHECK would fire
            # anyway, so the guard surfaces it friendly).
            raise MoveGuardError(
                f"{op_type}: node {object_id} is a class; classes are always roots and cannot have a parent",
                op_type,
            )

        # Parent/position are row-level LWW by envelope (hlc, actor): the whole
        # move publishes or drops — a position write never outlives a newer
        # parent write and vice versa. Moves never write the render bit: a
        # parentless non-class node renders with document chrome by the second
        # cascade branch regardless of present_as_main.
        if not self._incoming_wins(env, row[12], row[13], row[14]):
            return False

        with self._conn:
            self._conn.execute(
                """UPDATE nodes SET parent_id = ?, updated_at = ?, updated_by = ?,
                       hlc_physical = ?, hlc_logical = ?, actor_id = ?
                   WHERE workspace_id = ? AND id = ?""",
                (
                    parent_id,
                    _envelope_ts(env),
                    env.actor_id,
                    env.hlc.physical,
                    env.hlc.logical,
                    env.actor_id,
                    env.workspace_id,
                    object_id,
                ),
            )
            # Carry the child_order row with the parent: delete the old one
            # first — without it the node renders under BOTH parents.
            self._conn.execute("DELETE FROM node_child_order WHERE child_id = ?", (object_id,))
            if parent_id is not None:
                self._conn.execute(
                    "INSERT OR REPLACE INTO node_child_order (parent_id, child_id, position) VALUES (?, ?, ?)",
                    (parent_id, object_id, self._allocate_child_position(parent_id, object_id, after_id, before_id)),
                )
        return True

    # ---------------------------------------------------------------- child order

    def _next_child_position(self, parent_id: str) -> str:
        row = self._conn.execute(
            "SELECT position FROM node_child_order WHERE parent_id = ? ORDER BY position DESC LIMIT 1",
            (parent_id,),
        ).fetchone()
        return f"{row[0]}a" if row is not None else "a"

    def _allocate_child_position(
        self, parent_id: str, child_id: str, after_id: str | None, before_id: str | None = None
    ) -> str:
        """Fractional position for ``child_id`` under ``parent_id``, by anchor:

        - ``afterId``: sibling midpoint between afterId's position and the
          next sibling's, append-at-end when afterId is the last sibling;
        - ``beforeId``: sibling midpoint between the previous sibling's
          position and beforeId's — or, when beforeId is the first child,
          one slot below it (midpoint against the empty string: the only
          way to place BEFORE the current first sibling, which the
          afterId-only algebra cannot express);
        - no usable anchor (absent, or not a current sibling): defensive
          plain append.

        When both anchors are present ``afterId`` wins (the TS reference
        never sends both)."""
        if after_id is not None:
            after = self._conn.execute(
                "SELECT position FROM node_child_order WHERE parent_id = ? AND child_id = ?",
                (parent_id, after_id),
            ).fetchone()
            if after is not None:
                next_row = self._conn.execute(
                    """SELECT position FROM node_child_order
                       WHERE parent_id = ? AND child_id != ? AND position > ?
                       ORDER BY position ASC LIMIT 1""",
                    (parent_id, child_id, after[0]),
                ).fetchone()
                if next_row is not None:
                    return _midpoint_between(str(after[0]), str(next_row[0]))
                return self._next_child_position(parent_id)
        if before_id is not None:
            before = self._conn.execute(
                "SELECT position FROM node_child_order WHERE parent_id = ? AND child_id = ?",
                (parent_id, before_id),
            ).fetchone()
            if before is not None:
                prev_row = self._conn.execute(
                    """SELECT position FROM node_child_order
                       WHERE parent_id = ? AND child_id != ? AND position < ?
                       ORDER BY position DESC LIMIT 1""",
                    (parent_id, child_id, before[0]),
                ).fetchone()
                if prev_row is not None:
                    return _midpoint_between(str(prev_row[0]), str(before[0]))
                return _midpoint_between("", str(before[0]))
        return self._next_child_position(parent_id)

    # -------------------------------------------------------------------- class.*

    def _class_member_upsert(self, node_id: str, class_id: str, env: RelayEnvelope) -> None:
        """Seed OR-Set membership from an object.create's classIds (add-wins
        per pair, HLC-gated; concurrent creates on the same id are the
        designed carrier for class membership). The add's comparator is >=
        on the actor tiebreak so an exact-HLC add beats a class.unassign
        remove in either delivery order (the remove's is > — the
        collection_member convention)."""
        self._conn.execute(
            """INSERT INTO class_member_set (node_id, class_id, present, hlc_physical, hlc_logical, actor_id)
               VALUES (?, ?, 1, ?, ?, ?)
               ON CONFLICT(node_id, class_id) DO UPDATE SET
                 present = 1, hlc_physical = excluded.hlc_physical, hlc_logical = excluded.hlc_logical,
                 actor_id = excluded.actor_id
               WHERE excluded.hlc_physical > hlc_physical
                  OR (excluded.hlc_physical = hlc_physical AND excluded.hlc_logical > hlc_logical)
                  OR (excluded.hlc_physical = hlc_physical AND excluded.hlc_logical = hlc_logical
                      AND excluded.actor_id >= COALESCE(actor_id, ''))""",
            (node_id, class_id, env.hlc.physical, env.hlc.logical, env.actor_id),
        )

    def _class_member_remove(self, node_id: str, class_id: str, env: RelayEnvelope) -> None:
        """class.unassign tombstone: strictly-greater gate (including the
        actor tiebreak) — a remove at an equal (hlc, actor) to the standing
        add loses, so ties resolve add-wins regardless of delivery order."""
        self._conn.execute(
            """INSERT INTO class_member_set (node_id, class_id, present, hlc_physical, hlc_logical, actor_id)
               VALUES (?, ?, 0, ?, ?, ?)
               ON CONFLICT(node_id, class_id) DO UPDATE SET
                 present = 0, hlc_physical = excluded.hlc_physical, hlc_logical = excluded.hlc_logical,
                 actor_id = excluded.actor_id
               WHERE excluded.hlc_physical > hlc_physical
                  OR (excluded.hlc_physical = hlc_physical AND excluded.hlc_logical > hlc_logical)
                  OR (excluded.hlc_physical = hlc_physical AND excluded.hlc_logical = hlc_logical
                      AND excluded.actor_id > COALESCE(actor_id, ''))""",
            (node_id, class_id, env.hlc.physical, env.hlc.logical, env.actor_id),
        )

    def _recompute_class_ids(self, node_id: str) -> None:
        """Recompute ``nodes.class_ids`` from the OR-Set's present rows.

        User order (``class_order``, written by class.reorder, LWW-by-arrival)
        wins: ordered members first (filtered to present members), then any
        unlisted present members sorted by id (``recomputeClassIds`` in the
        web appliers)."""
        rows = self._conn.execute(
            "SELECT class_id FROM class_member_set WHERE node_id = ? AND present = 1 ORDER BY class_id",
            (node_id,),
        ).fetchall()
        present = [str(row[0]) for row in rows]
        order_row = self._conn.execute("SELECT class_order FROM nodes WHERE id = ?", (node_id,)).fetchone()
        raw_order = order_row[0] if order_row is not None else None
        try:
            parsed = json.loads(raw_order) if isinstance(raw_order, str) else []
            ordered = [str(item) for item in parsed if isinstance(item, str)]
        except (TypeError, ValueError):
            ordered = []
        present_set = set(present)
        effective = [class_id for class_id in ordered if class_id in present_set]
        effective.extend(class_id for class_id in present if class_id not in ordered)
        self._conn.execute("UPDATE nodes SET class_ids = ? WHERE id = ?", (json.dumps(effective), node_id))

    def _tag_member_upsert(self, node_id: str, tag_id: str, env: RelayEnvelope) -> None:
        """Seed OR-Set tag membership from an object.create's tagIds — the tag
        convergence carrier, mirroring the web ``tagMemberUpsert`` exactly
        (packages/store/src/appliers.ts): strictly-greater actor tiebreak, so
        an exact-(hlc, actor) tie between the add and a tag.unassign resolves
        first-in-log-wins. The relay log is the single global order, so every
        replica converges to the same winner regardless of delivery order
        (the classIds seeding uses >= add-wins — a deliberate web asymmetry)."""
        self._conn.execute(
            """INSERT INTO tag_member_set (node_id, tag_id, present, hlc_physical, hlc_logical, actor_id)
               VALUES (?, ?, 1, ?, ?, ?)
               ON CONFLICT(node_id, tag_id) DO UPDATE SET
                 present = 1, hlc_physical = excluded.hlc_physical, hlc_logical = excluded.hlc_logical,
                 actor_id = excluded.actor_id
               WHERE excluded.hlc_physical > hlc_physical
                  OR (excluded.hlc_physical = hlc_physical AND excluded.hlc_logical > hlc_logical)
                  OR (excluded.hlc_physical = hlc_physical AND excluded.hlc_logical = hlc_logical
                      AND excluded.actor_id > COALESCE(actor_id, ''))""",
            (node_id, tag_id, env.hlc.physical, env.hlc.logical, env.actor_id),
        )

    def _tag_member_remove(self, node_id: str, tag_id: str, env: RelayEnvelope) -> None:
        """tag.unassign tombstone: strictly-greater gate (including the actor
        tiebreak), identical to the class.unassign applier — a remove at an
        equal (hlc, actor) to the standing add loses, so ties resolve
        add-wins regardless of delivery order."""
        self._conn.execute(
            """INSERT INTO tag_member_set (node_id, tag_id, present, hlc_physical, hlc_logical, actor_id)
               VALUES (?, ?, 0, ?, ?, ?)
               ON CONFLICT(node_id, tag_id) DO UPDATE SET
                 present = 0, hlc_physical = excluded.hlc_physical, hlc_logical = excluded.hlc_logical,
                 actor_id = excluded.actor_id
               WHERE excluded.hlc_physical > hlc_physical
                  OR (excluded.hlc_physical = hlc_physical AND excluded.hlc_logical > hlc_logical)
                  OR (excluded.hlc_physical = hlc_physical AND excluded.hlc_logical = hlc_logical
                      AND excluded.actor_id > COALESCE(actor_id, ''))""",
            (node_id, tag_id, env.hlc.physical, env.hlc.logical, env.actor_id),
        )

    def _recompute_tag_ids(self, node_id: str) -> None:
        """Recompute ``nodes.tag_ids`` from the tag OR-Set's present rows."""
        rows = self._conn.execute(
            "SELECT tag_id FROM tag_member_set WHERE node_id = ? AND present = 1 ORDER BY tag_id",
            (node_id,),
        ).fetchall()
        tag_ids = [str(row[0]) for row in rows]
        self._conn.execute("UPDATE nodes SET tag_ids = ? WHERE id = ?", (json.dumps(tag_ids), node_id))

    def _upsert_class_node(
        self,
        env: RelayEnvelope,
        class_id: str,
        fields: dict[str, Any],
    ) -> None:
        """The class node (is_class=1, present_as_main=0) is the structural
        authority for the class_list read model; the registry row carries
        class-only config.

        Title-is-content: the class's title IS its (text-only) content — the
        node's ``content`` flattens the create/update ``contentAst``
        (``stringifyContentAst``), and the retired ``name`` column is never
        written (``NULL``; remaining readers are transition-only). Node
        fields update only when the envelope wins the row-level LWW."""
        ts = _envelope_ts(env)
        has_content = fields.get("content_ast") is not None
        flattened = stringify_content_ast(fields.get("content_ast")) if has_content else []
        content = json.dumps(flattened, ensure_ascii=False)
        content_plain = plaintext_excerpt(flattened)
        with self._conn:
            self._conn.execute(
                """INSERT OR IGNORE INTO nodes
                     (workspace_id, id, is_class, present_as_main, parent_id, class_ids, name, content, content_plain,
                      is_active, created_at, updated_at, created_by, updated_by,
                      hlc_physical, hlc_logical, actor_id)
                   VALUES (?, ?, 1, 0, NULL, '[]', NULL, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    env.workspace_id,
                    class_id,
                    content,
                    content_plain,
                    ts,
                    ts,
                    env.actor_id,
                    env.actor_id,
                    env.hlc.physical,
                    env.hlc.logical,
                    env.actor_id,
                ),
            )
            sets: list[str] = []
            values: list[Any] = []
            if has_content:
                sets.append("content = ?")
                values.append(content)
                sets.append("content_plain = ?")
                values.append(content_plain)
            for column in ("icon", "color"):
                if fields.get(column) is not None:
                    sets.append(f"{column} = ?")
                    values.append(fields[column])
            if not sets:
                return
            sets.extend(("updated_at = ?", "updated_by = ?", "hlc_physical = ?", "hlc_logical = ?", "actor_id = ?"))
            values.extend((ts, env.actor_id, env.hlc.physical, env.hlc.logical, env.actor_id))
            values.extend(
                (
                    env.workspace_id,
                    class_id,
                    env.hlc.physical,
                    env.hlc.physical,
                    env.hlc.logical,
                    env.hlc.physical,
                    env.hlc.logical,
                    env.actor_id,
                )
            )
            self._conn.execute(
                f"""UPDATE nodes SET {", ".join(sets)}
                    WHERE workspace_id = ? AND id = ? AND (
                      ? > hlc_physical
                      OR (? = hlc_physical AND ? > hlc_logical)
                      OR (? = hlc_physical AND ? = hlc_logical AND ? > COALESCE(actor_id, ''))
                    )""",
                values,
            )

    def _apply_class_create(self, env: RelayEnvelope) -> bool:
        payload = env.payload
        class_id = str(payload["classId"])
        ts = _envelope_ts(env)
        # Registry ``name`` is a denormalized cache of the class node's title
        # text (the authority is node.content): the excerpt of the create's
        # (text-only) contentAst.
        title_text = plaintext_excerpt(payload.get("contentAst") or [])
        with self._conn:
            self._conn.execute(
                """INSERT INTO class (id, workspace_id, name, icon, color, description, active, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, NULL, 1, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     name = excluded.name, icon = excluded.icon, color = excluded.color,
                     description = excluded.description, active = 1, updated_at = excluded.updated_at""",
                (class_id, env.workspace_id, title_text, payload.get("icon"), payload.get("color"), ts, ts),
            )
        self._upsert_class_node(
            env,
            class_id,
            {"content_ast": payload.get("contentAst"), "icon": payload.get("icon"), "color": payload.get("color")},
        )
        return True

    def _apply_class_update(self, env: RelayEnvelope) -> bool:
        payload = env.payload
        class_id = str(payload["classId"])
        with self._conn:
            sets: list[str] = []
            values: list[Any] = []
            if payload.get("contentAst") is not None:
                sets.append("name = ?")
                values.append(plaintext_excerpt(payload["contentAst"]))
            for column in ("icon", "color", "description"):
                if payload.get(column) is not None:
                    sets.append(f"{column} = ?")
                    values.append(payload[column])
            if not sets:
                return False
            sets.append("updated_at = ?")
            values.extend((_envelope_ts(env), class_id))
            self._conn.execute(f"UPDATE class SET {', '.join(sets)} WHERE id = ?", values)
        self._upsert_class_node(
            env,
            class_id,
            {"content_ast": payload.get("contentAst"), "icon": payload.get("icon"), "color": payload.get("color")},
        )
        return True

    def _apply_class_delete(self, env: RelayEnvelope) -> bool:
        class_id = str(env.payload["classId"])
        ts = _envelope_ts(env)
        with self._conn:
            self._conn.execute("UPDATE class SET active = 0, updated_at = ? WHERE id = ?", (ts, class_id))
            self._conn.execute(
                "UPDATE nodes SET is_active = 0, updated_at = ? WHERE workspace_id = ? AND id = ?",
                (ts, env.workspace_id, class_id),
            )
        return True

    def _apply_class_unassign(self, env: RelayEnvelope) -> bool:
        """Class membership removal (SCHEMA.md "Class properties" removal
        semantics): an OR-Set tombstone on the (node, class) pair. Bound
        properties with no authored value stop being derived (nothing stored,
        nothing to clean — the effective read model just stops reading the
        class's bindings); authored values survive, marked unbound.

        The remove is gated strictly-greater on (hlc, actor), so a remove
        that loses the HLC race to a newer add (or ties it) is written as
        nothing (the gated upsert no-ops) and membership stands.
        """
        op_type = "class.unassign"
        payload = env.payload
        object_id = str(payload["objectId"])
        class_id = str(payload["classId"])
        self._require_node(env.workspace_id, object_id, op_type)
        with self._conn:
            self._class_member_remove(object_id, class_id, env)
            self._recompute_class_ids(object_id)
        return True

    def _apply_class_reorder(self, env: RelayEnvelope) -> bool:
        """Class ORDER (class.reorder): display-only user ordering,
        LWW-by-arrival — the applier writes ``class_order``
        unconditionally, so it is deterministic per op order and convergent
        replicas agree. The class_ids projection merges: ordered members
        first, then unlisted present members sorted by id
        (``_recompute_class_ids``)."""
        op_type = "class.reorder"
        payload = env.payload
        object_id = str(payload["objectId"])
        self._require_node(env.workspace_id, object_id, op_type)
        class_ids = [str(class_id) for class_id in payload.get("classIds") or []]
        with self._conn:
            self._conn.execute(
                "UPDATE nodes SET class_order = ? WHERE id = ?",
                (json.dumps(class_ids), object_id),
            )
            self._recompute_class_ids(object_id)
        return True

    def _apply_tag_unassign(self, env: RelayEnvelope) -> bool:
        """Tag removal (tag.unassign): the OR-Set remove complement of the
        re-issued object.create add carrier — identical gating to
        class.unassign, own table — then ``tag_ids`` is recomputed from the
        surviving present rows."""
        op_type = "tag.unassign"
        payload = env.payload
        object_id = str(payload["objectId"])
        tag_id = str(payload["tagId"])
        self._require_node(env.workspace_id, object_id, op_type)
        with self._conn:
            self._tag_member_remove(object_id, tag_id, env)
            self._recompute_tag_ids(object_id)
        return True

    def _apply_class_set_extends(self, env: RelayEnvelope) -> bool:
        op_type = "class.setExtends"
        payload = env.payload
        class_id = str(payload["classId"])
        parent_ids = [str(parent) for parent in (payload.get("parentClassIds") or [])]
        self._require_node(env.workspace_id, class_id, op_type)

        for parent_id in parent_ids:
            self._require_node(env.workspace_id, parent_id, op_type)
            if parent_id == class_id:
                raise CycleError(f"{op_type}: class {class_id} cannot extend itself", op_type)
            # A cycle forms when the class is already an ancestor of one of its
            # new parents. The check runs against the pre-write closure, so
            # multi-hop cycles across several parents are covered too.
            cycle = self._conn.execute(
                "SELECT 1 FROM class_hierarchy WHERE class_id = ? AND ancestor_id = ? LIMIT 1",
                (parent_id, class_id),
            ).fetchone()
            if cycle is not None:
                raise CycleError(
                    f"{op_type}: class {class_id} is already an ancestor of {parent_id}; extends would cycle",
                    op_type,
                )

        with self._conn:
            # Replace semantics: the payload array IS the class's full parent
            # set — drop every previous edge, then insert the new ones. An
            # empty array detaches all parents.
            self._conn.execute("DELETE FROM class_extends WHERE class_id = ?", (class_id,))
            for parent_id in parent_ids:
                self._conn.execute(
                    "INSERT OR IGNORE INTO class_extends (class_id, parent_class_id) VALUES (?, ?)",
                    (class_id, parent_id),
                )
            self._conn.execute("UPDATE class SET updated_at = ? WHERE id = ?", (_envelope_ts(env), class_id))
            self._rebuild_class_hierarchy()
        return True

    def _rebuild_class_hierarchy(self) -> None:
        """Full, deterministic rebuild of the class_hierarchy closure from the
        class_extends edge set (m2m). Rows are inserted per class in sorted id
        order with sorted ancestor order, so replay converges to identical
        bytes. Historical cycles terminate via the visited set."""
        classes = [str(row[0]) for row in self._conn.execute("SELECT id FROM class WHERE active = 1 ORDER BY id")]
        edges = self._conn.execute(
            "SELECT class_id, parent_class_id FROM class_extends ORDER BY class_id, parent_class_id"
        ).fetchall()
        parents_by_id: dict[str, list[str]] = {}
        for class_id, parent_id in edges:
            parents_by_id.setdefault(str(class_id), []).append(str(parent_id))
        self._conn.execute("DELETE FROM class_hierarchy")
        for class_id in classes:
            ancestors: set[str] = set()
            visited = {class_id}
            queue = list(parents_by_id.get(class_id, []))
            while queue:
                cursor = queue.pop(0)
                if cursor in visited:
                    continue
                visited.add(cursor)
                ancestors.add(cursor)
                queue.extend(parents_by_id.get(cursor, []))
            self._conn.execute(
                "INSERT OR IGNORE INTO class_hierarchy (class_id, ancestor_id) VALUES (?, ?)", (class_id, class_id)
            )
            for ancestor_id in sorted(ancestors):
                self._conn.execute(
                    "INSERT OR IGNORE INTO class_hierarchy (class_id, ancestor_id) VALUES (?, ?)",
                    (class_id, ancestor_id),
                )

    # ------------------------------------------------------- class.property.*

    def _apply_class_property_set(self, env: RelayEnvelope) -> bool:
        payload = env.payload
        class_id = str(payload["classId"])
        schema_id = str(payload["propertySchemaId"])
        existing = self._conn.execute(
            "SELECT hlc_physical, hlc_logical, actor_id FROM class_property"
            " WHERE class_id = ? AND property_schema_id = ?",
            (class_id, schema_id),
        ).fetchone()
        if existing is not None and not self._incoming_wins(env, existing[0], existing[1], existing[2]):
            return False

        # Omitted (absent) fields keep the stored value via COALESCE; explicit
        # false / JSON null are real writes (null default == JSON "null").
        def flag(name: str) -> int | None:
            if name not in payload:
                return None  # absent → COALESCE keeps the stored value
            return 1 if payload[name] else 0  # explicit null (falsy) writes 0, as the TS port

        sequence = int(payload["sequence"]) if payload.get("sequence") is not None else None
        default_value = json.dumps(payload["defaultValue"]) if "defaultValue" in payload else None
        with self._conn:
            if existing is None:
                self._conn.execute(
                    """INSERT INTO class_property
                         (class_id, property_schema_id, sequence, required, readonly, hide_when_empty,
                          default_value, hlc_physical, hlc_logical, actor_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        class_id,
                        schema_id,
                        sequence if sequence is not None else 0,
                        flag("required"),
                        flag("readonly"),
                        flag("hideWhenEmpty"),
                        default_value,
                        env.hlc.physical,
                        env.hlc.logical,
                        env.actor_id,
                    ),
                )
            else:
                self._conn.execute(
                    """UPDATE class_property SET
                         sequence = COALESCE(?, sequence),
                         required = COALESCE(?, required),
                         readonly = COALESCE(?, readonly),
                         hide_when_empty = COALESCE(?, hide_when_empty),
                         default_value = COALESCE(?, default_value),
                         hlc_physical = ?, hlc_logical = ?, actor_id = ?
                       WHERE class_id = ? AND property_schema_id = ?""",
                    (
                        sequence,
                        flag("required"),
                        flag("readonly"),
                        flag("hideWhenEmpty"),
                        default_value,
                        env.hlc.physical,
                        env.hlc.logical,
                        env.actor_id,
                        class_id,
                        schema_id,
                    ),
                )
        return True

    def _apply_class_property_unset(self, env: RelayEnvelope) -> bool:
        """Binding removal: plain DELETE — a config row, last write wins, no
        tombstone (SCHEMA.md). Authored property_value rows are untouched."""
        payload = env.payload
        with self._conn:
            self._conn.execute(
                "DELETE FROM class_property WHERE class_id = ? AND property_schema_id = ?",
                (str(payload["classId"]), str(payload["propertySchemaId"])),
            )
        return True

    # ------------------------------------------------------------- propertySchema.*

    def _apply_property_schema_create(self, env: RelayEnvelope) -> bool:
        payload = env.payload
        ts = _envelope_ts(env)
        with self._conn:
            self._conn.execute(
                """INSERT INTO property_schema
                     (id, workspace_id, name, type, multi, scope, options, target_class_filter,
                      active, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     name = excluded.name, type = excluded.type, multi = excluded.multi,
                     scope = excluded.scope, options = excluded.options,
                     target_class_filter = excluded.target_class_filter,
                     active = 1, updated_at = excluded.updated_at""",
                (
                    str(payload["propertySchemaId"]),
                    env.workspace_id,
                    payload.get("name"),
                    payload.get("type"),
                    1 if payload.get("multi") else 0,
                    payload.get("scope") or "global",
                    json.dumps(payload.get("options") or []),
                    json.dumps(payload["targetClassFilter"]) if payload.get("targetClassFilter") is not None else None,
                    ts,
                    ts,
                ),
            )
        return True

    def _apply_property_schema_update(self, env: RelayEnvelope) -> bool:
        payload = env.payload
        with self._conn:
            sets: list[str] = []
            values: list[Any] = []
            if payload.get("name") is not None:
                sets.append("name = ?")
                values.append(payload["name"])
            if payload.get("options") is not None:
                sets.append("options = ?")
                values.append(json.dumps(payload["options"]))
            if not sets:
                return False
            sets.append("updated_at = ?")
            values.extend((_envelope_ts(env), str(payload["propertySchemaId"])))
            self._conn.execute(f"UPDATE property_schema SET {', '.join(sets)} WHERE id = ?", values)
        return True

    def _apply_property_schema_delete(self, env: RelayEnvelope) -> bool:
        with self._conn:
            self._conn.execute(
                "UPDATE property_schema SET active = 0, updated_at = ? WHERE id = ?",
                (_envelope_ts(env), str(env.payload["propertySchemaId"])),
            )
        return True

    # ---------------------------------------------------------------- property.*

    @staticmethod
    def _property_slot(env: RelayEnvelope) -> tuple[str, str, int]:
        payload = env.payload
        return (str(payload["objectId"]), str(payload["propertySchemaId"]), int(payload.get("idx") or 0))

    def _apply_property_set(self, env: RelayEnvelope) -> bool:
        payload = env.payload
        node_id, schema_id, idx = self._property_slot(env)

        # A tombstone with a winning (>=) (hlc, actor) blocks the write.
        tombstone = self._conn.execute(
            "SELECT hlc_physical, hlc_logical, actor_id FROM property_value_tombstone"
            " WHERE node_id = ? AND property_schema_id = ? AND idx = ?",
            (node_id, schema_id, idx),
        ).fetchone()
        if tombstone is not None and not self._incoming_wins(env, tombstone[0], tombstone[1], tombstone[2]):
            return False

        existing = self._conn.execute(
            "SELECT hlc_physical, hlc_logical, actor_id FROM property_value"
            " WHERE node_id = ? AND property_schema_id = ? AND idx = ?",
            (node_id, schema_id, idx),
        ).fetchone()
        value_json = json.dumps(payload.get("value"), ensure_ascii=False)
        metadata_json = (
            json.dumps(payload["metadata"], ensure_ascii=False) if payload.get("metadata") is not None else None
        )
        with self._conn:
            if existing is None:
                self._conn.execute(
                    """INSERT INTO property_value
                         (id, node_id, property_schema_id, value, idx, metadata,
                          hlc_physical, hlc_logical, actor_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        f"{node_id}:{schema_id}:{idx}",
                        node_id,
                        schema_id,
                        value_json,
                        idx,
                        metadata_json,
                        env.hlc.physical,
                        env.hlc.logical,
                        env.actor_id,
                    ),
                )
            elif self._incoming_wins(env, existing[0], existing[1], existing[2]):
                self._conn.execute(
                    """UPDATE property_value SET value = ?, metadata = ?,
                          hlc_physical = ?, hlc_logical = ?, actor_id = ?
                       WHERE node_id = ? AND property_schema_id = ? AND idx = ?""",
                    (
                        value_json,
                        metadata_json,
                        env.hlc.physical,
                        env.hlc.logical,
                        env.actor_id,
                        node_id,
                        schema_id,
                        idx,
                    ),
                )
            else:
                return False
        return True

    def _apply_property_unset(self, env: RelayEnvelope) -> bool:
        node_id, schema_id, idx = self._property_slot(env)
        with self._conn:
            # Upsert the tombstone only when the incoming write wins the slot.
            self._conn.execute(
                """INSERT INTO property_value_tombstone
                     (node_id, property_schema_id, idx, hlc_physical, hlc_logical, actor_id)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(node_id, property_schema_id, idx) DO UPDATE SET
                     hlc_physical = excluded.hlc_physical, hlc_logical = excluded.hlc_logical,
                     actor_id = excluded.actor_id
                   WHERE excluded.hlc_physical > hlc_physical
                      OR (excluded.hlc_physical = hlc_physical AND excluded.hlc_logical > hlc_logical)
                      OR (excluded.hlc_physical = hlc_physical AND excluded.hlc_logical = hlc_logical
                          AND excluded.actor_id > COALESCE(actor_id, ''))""",
                (node_id, schema_id, idx, env.hlc.physical, env.hlc.logical, env.actor_id),
            )
            existing = self._conn.execute(
                "SELECT hlc_physical, hlc_logical, actor_id FROM property_value"
                " WHERE node_id = ? AND property_schema_id = ? AND idx = ?",
                (node_id, schema_id, idx),
            ).fetchone()
            if existing is not None and self._incoming_wins(env, existing[0], existing[1], existing[2]):
                self._conn.execute(
                    "DELETE FROM property_value WHERE node_id = ? AND property_schema_id = ? AND idx = ?",
                    (node_id, schema_id, idx),
                )
        return True

    # --------------------------------------------------------------------- asset.*

    def _apply_asset_attach(self, env: RelayEnvelope) -> bool:
        payload = env.payload
        with self._conn:
            self._conn.execute(
                """INSERT INTO node_asset (node_id, asset_id, hash, mime_type, size, original_name, uploaded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(node_id, asset_id) DO UPDATE SET
                     hash = excluded.hash, mime_type = excluded.mime_type, size = excluded.size,
                     original_name = excluded.original_name, uploaded_at = excluded.uploaded_at""",
                (
                    str(payload["objectId"]),
                    str(payload["assetId"]),
                    payload.get("hash"),
                    payload.get("mimeType"),
                    int(payload.get("size") or 0),
                    payload.get("originalName"),
                    _envelope_ts(env),
                ),
            )
        return True

    def _apply_asset_detach(self, env: RelayEnvelope) -> bool:
        payload = env.payload
        with self._conn:
            self._conn.execute(
                "DELETE FROM node_asset WHERE node_id = ? AND asset_id = ?",
                (str(payload["objectId"]), str(payload["assetId"])),
            )
        return True

    # --------------------------------------------------------------- collections

    def _apply_collection_member(self, env: RelayEnvelope, *, present: int, add_wins: bool) -> bool:
        payload = env.payload
        # OR-Set add-wins: an add with an equal (hlc, actor) to a remove still
        # wins, so the remove's WHERE is strictly-greater while the add's is >=.
        actor_comparator = ">=" if add_wins else ">"
        with self._conn:
            self._conn.execute(
                f"""INSERT INTO collection_member
                     (collection_id, object_id, present, hlc_physical, hlc_logical, actor_id)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(collection_id, object_id) DO UPDATE SET
                     present = excluded.present, hlc_physical = excluded.hlc_physical,
                     hlc_logical = excluded.hlc_logical, actor_id = excluded.actor_id
                   WHERE excluded.hlc_physical > hlc_physical
                      OR (excluded.hlc_physical = hlc_physical AND excluded.hlc_logical > hlc_logical)
                      OR (excluded.hlc_physical = hlc_physical AND excluded.hlc_logical = hlc_logical
                          AND excluded.actor_id {actor_comparator} COALESCE(actor_id, ''))""",
                (
                    str(payload["collectionId"]),
                    str(payload["objectId"]),
                    present,
                    env.hlc.physical,
                    env.hlc.logical,
                    env.actor_id,
                ),
            )
        return True

    # -------------------------------------------------------------- node cache

    @_synchronized
    def nodes(self, workspace_id: str, parent_id: str | None = None, include_inactive: bool = False) -> list[NodeRow]:
        """List cached node rows, optionally filtered by parent.

        ``parent_id=None`` applies no parent filter (all nodes in the
        workspace); pass a string to list one parent's children (unordered —
        use :meth:`children` for child-order positions). Soft-deleted nodes are
        excluded unless ``include_inactive`` is set.
        """
        sql = (
            "SELECT id, workspace_id, parent_id, is_class, present_as_main, name, class_ids, tag_ids, icon, color,"
            " is_active, content, content_plain FROM nodes WHERE workspace_id = ?"
        )
        params: list[Any] = [workspace_id]
        if parent_id is not None:
            sql += " AND parent_id = ?"
            params.append(parent_id)
        if not include_inactive:
            sql += " AND is_active = 1"
        sql += " ORDER BY id"
        return [self._to_node_row(row) for row in self._conn.execute(sql, params).fetchall()]

    @_synchronized
    def children(self, workspace_id: str, parent_id: str) -> list[NodeRow]:
        """Direct children in child-order position order (the outliner read)."""
        rows = self._conn.execute(
            """SELECT n.id, n.workspace_id, n.parent_id, n.is_class, n.present_as_main, n.name, n.class_ids, n.tag_ids,
                      n.icon, n.color, n.is_active, n.content, n.content_plain
               FROM nodes n
               JOIN node_child_order o ON o.child_id = n.id
               WHERE n.workspace_id = ? AND o.parent_id = ?
               ORDER BY o.position""",
            (workspace_id, parent_id),
        ).fetchall()
        return [self._to_node_row(row) for row in rows]

    @_synchronized
    def node(self, workspace_id: str, node_id: str) -> NodeRow | None:
        """Return one cached node row, or ``None`` when unknown."""
        row = self._conn.execute(
            "SELECT id, workspace_id, parent_id, is_class, present_as_main, name, class_ids, tag_ids, icon, color,"
            " is_active, content, content_plain FROM nodes WHERE workspace_id = ? AND id = ?",
            (workspace_id, node_id),
        ).fetchone()
        return self._to_node_row(row) if row is not None else None

    @staticmethod
    def _to_node_row(row: tuple[Any, ...]) -> NodeRow:
        raw_class_ids = row[6]
        try:
            class_ids = tuple(str(item) for item in json.loads(raw_class_ids)) if raw_class_ids else ()
        except (TypeError, ValueError):
            class_ids = ()
        raw_tag_ids = row[7]
        try:
            tag_ids = tuple(str(item) for item in json.loads(raw_tag_ids)) if raw_tag_ids else ()
        except (TypeError, ValueError):
            tag_ids = ()
        return NodeRow(
            id=str(row[0]),
            workspace_id=str(row[1]),
            parent_id=row[2],
            is_class=bool(row[3]),
            present_as_main=bool(row[4]),
            name=row[5],
            class_ids=class_ids,
            tag_ids=tag_ids,
            icon=row[8],
            color=row[9],
            is_active=bool(row[10]),
            content=row[11],
            content_plain=str(row[12] or ""),
        )

    # ------------------------------------------------- effective properties

    @_synchronized
    def get_effective_properties(self, node_id: str) -> list[EffectiveProperty]:
        """The effective-values read model (SCHEMA.md "Class properties")::

            effective(node, schema, idx) = authored property_value
                                           ?? winning binding's defaultValue

        Port of v2 ``packages/store/src/effective.ts``. Authored rows always
        win and survive class removal; derived defaults are computed HERE and
        never materialized (the applier writes no property_value rows for
        them). Binding conflicts across the node's classes resolve
        first-class-applied-wins: the class whose OR-Set membership add
        carries the earliest HLC supplies the default AND the binding
        metadata; exact HLC ties break by class id. A pure read over derived
        tables — deterministic on every replica, no writes, no clocks.
        """
        authored_rows = self._conn.execute(
            "SELECT property_schema_id, value, idx, metadata, hlc_physical, hlc_logical, actor_id"
            " FROM property_value WHERE node_id = ?",
            (node_id,),
        ).fetchall()
        tombstones = self._conn.execute(
            "SELECT property_schema_id, idx, hlc_physical, hlc_logical, actor_id"
            " FROM property_value_tombstone WHERE node_id = ?",
            (node_id,),
        ).fetchall()

        def suppressed(row: tuple[Any, ...]) -> bool:
            for tomb in tombstones:
                if tomb[0] != row[0] or tomb[1] != row[2]:
                    continue
                authored_winner = (int(row[4]), int(row[5]), row[6] or "")
                tomb_winner = (int(tomb[2]), int(tomb[3]), tomb[4] or "")
                if authored_winner <= tomb_winner:
                    return True
            return False

        # The node's classes in assignment order: OR-Set add HLC ascending
        # (earliest first), ties by class id.
        classes = sorted(
            self._conn.execute(
                "SELECT class_id, hlc_physical, hlc_logical FROM class_member_set WHERE node_id = ? AND present = 1",
                (node_id,),
            ).fetchall(),
            key=lambda row: (int(row[1]), int(row[2]), str(row[0])),
        )

        # Winning binding per schema: the first class (in assignment order)
        # that binds the schema supplies default + metadata.
        winner_by_schema: dict[str, tuple[str, tuple[Any, ...]]] = {}
        for cls in classes:
            bindings = self._conn.execute(
                "SELECT property_schema_id, sequence, required, readonly, hide_when_empty, default_value"
                " FROM class_property WHERE class_id = ?",
                (str(cls[0]),),
            ).fetchall()
            for binding in bindings:
                if str(binding[0]) not in winner_by_schema:
                    winner_by_schema[str(binding[0])] = (str(cls[0]), binding)

        # Schema rows for everything referenced (authored rows survive schema
        # deletion: the row renders with schema=None).
        schema_ids = {str(row[0]) for row in authored_rows} | set(winner_by_schema)
        schemas: dict[str, EffectivePropertySchema] = {}
        if schema_ids:
            placeholders = ", ".join("?" for _ in schema_ids)
            rows = self._conn.execute(
                f"SELECT id, name, type, multi FROM property_schema WHERE id IN ({placeholders})",
                tuple(sorted(schema_ids)),
            ).fetchall()
            for row in rows:
                schemas[str(row[0])] = EffectivePropertySchema(
                    id=str(row[0]), name=str(row[1]), type=str(row[2]), multi=bool(row[3])
                )

        def flag(value: Any) -> bool | None:
            return None if value is None else bool(value)

        def parse_json(raw: Any) -> Any:
            if not isinstance(raw, str):
                return raw
            try:
                return json.loads(raw)
            except ValueError:
                return raw

        rows_out: dict[str, EffectiveProperty] = {}
        for authored in authored_rows:
            if suppressed(authored):
                continue
            schema_id, idx = str(authored[0]), int(authored[2])
            winner = winner_by_schema.get(schema_id)
            rows_out[f"{schema_id}:{idx}"] = EffectiveProperty(
                property_schema_id=schema_id,
                idx=idx,
                schema=schemas.get(schema_id),
                value=parse_json(authored[1]),
                metadata=parse_json(authored[3]) if authored[3] is not None else None,
                source="authored",
                bound_by=winner[0] if winner else None,
                required=flag(winner[1][2]) if winner else None,
                readonly=flag(winner[1][3]) if winner else None,
                hide_when_empty=flag(winner[1][4]) if winner else None,
                sequence=int(winner[1][1]) if winner else None,
            )

        for schema_id, (class_id, binding) in winner_by_schema.items():
            if binding[5] is None:
                continue  # bound without a default
            key = f"{schema_id}:0"
            if key in rows_out:
                continue  # authored value at idx 0 shadows the default
            rows_out[key] = EffectiveProperty(
                property_schema_id=schema_id,
                idx=0,
                schema=schemas.get(schema_id),
                value=parse_json(binding[5]),
                metadata=None,
                source="default",
                bound_by=class_id,
                required=flag(binding[2]),
                readonly=flag(binding[3]),
                hide_when_empty=flag(binding[4]),
                sequence=int(binding[1]),
            )

        # Deterministic presentation order: bound rows by binding sequence,
        # unbound authored rows last; schema name then idx as the tiebreak.
        def name_of(row: EffectiveProperty) -> str:
            return row.schema.name if row.schema is not None else row.property_schema_id

        return sorted(
            rows_out.values(),
            key=lambda row: (
                row.bound_by is None,
                row.sequence if row.sequence is not None else 2**53 - 1,
                name_of(row),
                row.idx,
            ),
        )

    # ---------------------------------------------------------------- snapshots

    @_synchronized
    def restore_snapshot(self, blob: bytes, *, workspace_id: str) -> bool:
        """Restore nodes from a downloaded snapshot blob (serialized derived DB).

        The blob is a serialized copy of the server's v2 derived database,
        whose node table is ``node`` (singular) with the v2 column names
        (``is_class``/``present_as_main``, ``is_active`` — same polarity as
        the cache, ``class_ids``, and the row-LWW columns
        ``hlc_physical``/``hlc_logical``/``actor_id`` which seed the cache's
        LWW baseline when present). The table is discovered from
        ``snapshot_src.sqlite_master`` but restricted to a small allowlist,
        and every interpolated identifier is validated against a strict
        pattern. Snapshot columns the mapping needs but the blob lacks are
        skipped. On any error, unrecognized schema, or empty column mapping
        the blob is detached and ``False`` is returned with local state
        untouched.
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
            table = self._snapshot_table()
            if table is None:
                _log.warning("Snapshot restore failed: no recognized node table in snapshot")
                return False
            remote_columns = {
                str(row[1]) for row in self._conn.execute(f"PRAGMA snapshot_src.table_info({_quote_ident(table)})")
            }
            select_exprs = _snapshot_select_exprs(remote_columns)
            if not select_exprs:
                _log.warning("Snapshot restore failed: no usable node columns in snapshot")
                return False
            columns = ", ".join(_quote_ident(name) for name in select_exprs)
            select = f"SELECT {', '.join(select_exprs.values())} FROM snapshot_src.{_quote_ident(table)}"
            params: tuple[Any, ...] = ()
            if "workspace_id" in select_exprs:
                select += f" WHERE {_quote_ident('workspace_id')} = ?"
                params = (workspace_id,)
            with self._conn:
                self._conn.execute(f"INSERT OR REPLACE INTO nodes ({columns}) {select}", params)
                # The v2 server snapshot has no plaintext cache column (the
                # server derives FTS text); derive the client cache's excerpt
                # for the restored rows.
                restored = self._conn.execute(
                    "SELECT id, content FROM nodes WHERE workspace_id = ?", (workspace_id,)
                ).fetchall()
                for node_id, content in restored:
                    self._conn.execute(
                        "UPDATE nodes SET content_plain = ? WHERE id = ?",
                        (plaintext_excerpt(parse_content_ast(content)), node_id),
                    )
            return True
        except (sqlite3.Error, ValueError) as exc:
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

    def _snapshot_table(self) -> str | None:
        """Return the snapshot's node table name when it is in the allowlist."""
        rows = self._conn.execute("SELECT name FROM snapshot_src.sqlite_master WHERE type = 'table'").fetchall()
        for (name,) in rows:
            if name in _SNAPSHOT_TABLES and _IDENT_RE.match(str(name)):
                return str(name)
        return None

    # ---------------------------------------------------------------- lifecycle

    @_synchronized
    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()


def _midpoint_between(lo: str, hi: str) -> str:
    """Lexicographic midpoint of two fractional position strings: the shortest
    string strictly greater than ``lo`` and strictly less than ``hi``
    (precondition lo < hi, ASCII). Boundary chars use '`' (one below 'a') as
    the floor and '{' (one above 'z') as the ceil, so distinct positions
    always have room. Deterministic — no random suffix — so
    wipe -> replay -> byte-identical."""
    index = 0
    while index < len(lo) and index < len(hi) and lo[index] == hi[index]:
        index += 1
    prefix = lo[:index]
    lo_rest = lo[index:]
    hi_rest = hi[index:]
    lo_code = ord(lo_rest[0]) if lo_rest else 0x60
    hi_code = ord(hi_rest[0]) if hi_rest else 0x7B
    if lo_code + 1 < hi_code:
        return prefix + chr((lo_code + 1 + hi_code - 1) // 2)
    if not lo_rest:
        # lo is a prefix of hi and hi continues at the lowest digit: squeeze
        # one char below hi's next digit.
        return prefix + chr(hi_code - 1)
    # Adjacent boundary chars: keep lo's digit (which is < hi's) and descend.
    return prefix + lo_rest[0] + _midpoint_between(lo_rest[1:], hi_rest[1:])


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


_NODES_V2_DDL = """
CREATE TABLE IF NOT EXISTS nodes (
    workspace_id TEXT NOT NULL,
    id TEXT NOT NULL,
    -- Revision-11 render-state model (replaces the node_type enumeration):
    -- is_class is the ONLY identity marker — classes are always roots;
    -- present_as_main is the render bit read by the third cascade branch for
    -- parented non-class nodes: 1 = the parent's main-children zone +
    -- document chrome when zoomed, 0 = inline body + block chrome. The bit is
    -- unread for parentless nodes (document chrome by the second branch) and
    -- for classes (ClassView by the first branch).
    is_class INTEGER NOT NULL DEFAULT 0,
    present_as_main INTEGER NOT NULL DEFAULT 0,
    parent_id TEXT,
    name TEXT,
    class_ids TEXT NOT NULL DEFAULT '[]',
    -- User-defined class ORDER (class.reorder, LWW-by-arrival); the
    -- effective class_ids = ordered members first, then unlisted members
    -- sorted by id (schema v6, web v6→v7 parity).
    class_order TEXT NOT NULL DEFAULT '[]',
    -- Tag OR-Set membership projected sorted by id (schema v5, web v5→v6
    -- parity).
    tag_ids TEXT NOT NULL DEFAULT '[]',
    icon TEXT,
    color TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    content TEXT,
    content_plain TEXT NOT NULL DEFAULT '',
    created_at TEXT,
    updated_at TEXT NOT NULL,
    created_by TEXT,
    updated_by TEXT,
    hlc_physical INTEGER NOT NULL DEFAULT 0,
    hlc_logical INTEGER NOT NULL DEFAULT 0,
    actor_id TEXT,
    -- Classes are always roots; every other node may sit anywhere in the
    -- tree, parentless nodes included (they render with document chrome).
    CHECK (is_class = 0 OR parent_id IS NULL),
    PRIMARY KEY (workspace_id, id)
);
"""

_AUX_V2_DDL = """
CREATE TABLE IF NOT EXISTS node_child_order (
    parent_id TEXT NOT NULL,
    child_id TEXT NOT NULL,
    position TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);
CREATE INDEX IF NOT EXISTS idx_node_child_order_parent ON node_child_order (parent_id);

-- OR-Set of class assignments; the applier projects present rows into
-- node.class_ids (add-wins, LWW per (node_id, class_id) by (hlc, actor)).
CREATE TABLE IF NOT EXISTS class_member_set (
    node_id TEXT NOT NULL,
    class_id TEXT NOT NULL,
    present INTEGER NOT NULL,
    hlc_physical INTEGER NOT NULL DEFAULT 0,
    hlc_logical INTEGER NOT NULL DEFAULT 0,
    actor_id TEXT,
    PRIMARY KEY (node_id, class_id)
);
CREATE INDEX IF NOT EXISTS idx_class_member_set_class ON class_member_set (class_id);

-- OR-Set of tag assignments (tags are pages assigned to a page — the same
-- membership semantics as classes, own table, no role overlap). The applier
-- projects present rows into node.tag_ids sorted by id.
CREATE TABLE IF NOT EXISTS tag_member_set (
    node_id TEXT NOT NULL,
    tag_id TEXT NOT NULL,
    present INTEGER NOT NULL,
    hlc_physical INTEGER NOT NULL DEFAULT 0,
    hlc_logical INTEGER NOT NULL DEFAULT 0,
    actor_id TEXT,
    PRIMARY KEY (node_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_tag_member_set_tag ON tag_member_set (tag_id);

-- Direct extends edges (m2m; class.setExtends replaces the full row set).
CREATE TABLE IF NOT EXISTS class_extends (
    class_id TEXT NOT NULL,
    parent_class_id TEXT NOT NULL,
    PRIMARY KEY (class_id, parent_class_id)
);

-- Transitive closure of class extends, applier-maintained, self-row included.
CREATE TABLE IF NOT EXISTS class_hierarchy (
    class_id TEXT NOT NULL,
    ancestor_id TEXT NOT NULL,
    PRIMARY KEY (class_id, ancestor_id)
);

-- Class registry rows (name/icon/color/description); the node row
-- (is_class=1) is the structural authority.
CREATE TABLE IF NOT EXISTS class (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    icon TEXT,
    color TEXT,
    description TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS property_schema (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    multi INTEGER NOT NULL DEFAULT 0,
    scope TEXT NOT NULL DEFAULT 'global',
    options TEXT NOT NULL DEFAULT '[]',
    target_class_filter TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    updated_at TEXT
);

-- Property values: LWW per (node, schema, idx); tombstone wins over live.
CREATE TABLE IF NOT EXISTS property_value (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    property_schema_id TEXT NOT NULL,
    value TEXT NOT NULL,
    idx INTEGER NOT NULL DEFAULT 0,
    metadata TEXT,
    hlc_physical INTEGER NOT NULL DEFAULT 0,
    hlc_logical INTEGER NOT NULL DEFAULT 0,
    actor_id TEXT,
    UNIQUE (node_id, property_schema_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_property_value_node ON property_value (node_id);

CREATE TABLE IF NOT EXISTS property_value_tombstone (
    node_id TEXT NOT NULL,
    property_schema_id TEXT NOT NULL,
    idx INTEGER NOT NULL DEFAULT 0,
    hlc_physical INTEGER NOT NULL DEFAULT 0,
    hlc_logical INTEGER NOT NULL DEFAULT 0,
    actor_id TEXT,
    PRIMARY KEY (node_id, property_schema_id, idx)
);

CREATE TABLE IF NOT EXISTS node_asset (
    node_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    hash TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    original_name TEXT NOT NULL DEFAULT '',
    uploaded_at TEXT,
    PRIMARY KEY (node_id, asset_id)
);

-- OR-Set membership for collection nodes (add-wins per member pair).
CREATE TABLE IF NOT EXISTS collection_member (
    collection_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    present INTEGER NOT NULL,
    hlc_physical INTEGER NOT NULL DEFAULT 0,
    hlc_logical INTEGER NOT NULL DEFAULT 0,
    actor_id TEXT,
    PRIMARY KEY (collection_id, object_id)
);
CREATE INDEX IF NOT EXISTS idx_collection_member_object ON collection_member (object_id);

-- Soft-delete retention; is_permanent distinguishes trash rows recorded for
-- retention cleanup before a hard delete from plain soft deletes.
CREATE TABLE IF NOT EXISTS trash (
    node_id TEXT PRIMARY KEY,
    deleted_at TEXT NOT NULL,
    is_permanent INTEGER NOT NULL DEFAULT 0
);
"""


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """Reshape the client cache to the v2 model (guarded, data-preserving).

    The ``nodes`` mirror is rebuilt at the v2 column names (``is_active``
    replacing ``archived``, plus ``class_ids``/``content_plain`` and the
    row-LWW columns), the v1 ``node_content_hlc`` watermark is dropped (its
    job is done by the row-LWW columns), and the v2 auxiliary tables
    (child_order, OR-Sets, class registry/extends/closure, property
    registry/values/tombstones, assets, collections, trash) are created.
    The legacy ``node_type`` enumeration maps straight onto the Revision-11
    booleans here (page → presents as main; block/class → not) — the v7
    rebuild is a no-op for this path.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(nodes)")}
    if "is_active" in columns:
        return  # already at the v2 shape (guarded re-run)
    conn.execute("ALTER TABLE nodes RENAME TO nodes_legacy")
    conn.executescript(_NODES_V2_DDL + _AUX_V2_DDL)
    legacy = {row[1] for row in conn.execute("PRAGMA table_info(nodes_legacy)")}
    copy_exprs: dict[str, str] = {}
    for column in ("id", "workspace_id", "parent_id", "name", "icon", "color", "content", "updated_at"):
        if column in legacy:
            copy_exprs[column] = _quote_ident(column)
    # Revision-11 render-state model: the legacy node_type enumeration maps
    # to the two booleans (page → is_class 0 + present_as_main 1).
    if "node_type" in legacy:
        copy_exprs["is_class"] = f"CASE WHEN {_quote_ident('node_type')} = 'class' THEN 1 ELSE 0 END"
        copy_exprs["present_as_main"] = f"CASE WHEN {_quote_ident('node_type')} = 'page' THEN 1 ELSE 0 END"
    else:
        copy_exprs.setdefault("is_class", "0")
        copy_exprs.setdefault("present_as_main", "0")
    if "archived" in legacy:
        copy_exprs["is_active"] = f"1 - {_quote_ident('archived')}"
    copy_exprs.setdefault("is_active", "1")
    copy_exprs.setdefault("class_ids", "'[]'")
    copy_exprs.setdefault("content_plain", "''")
    copy_exprs.setdefault("hlc_physical", "0")
    copy_exprs.setdefault("hlc_logical", "0")
    columns_sql = ", ".join(_quote_ident(name) for name in copy_exprs)
    conn.execute(f"INSERT INTO nodes ({columns_sql}) SELECT {', '.join(copy_exprs.values())} FROM nodes_legacy")
    conn.execute("DROP TABLE nodes_legacy")
    conn.execute("DROP TABLE IF EXISTS node_content_hlc")


_CLASS_PROPERTY_DDL = """
-- Class -> property bindings (sequence, flags, default), per SCHEMA.md
-- "Class properties — bindings, defaults, aggregation" (2026-09-27).
-- Registry rows authored by class.property.set/unset; LWW on the row by
-- (hlc, actor). Defaults are a DERIVED read model (get_effective_properties),
-- never materialized property_value rows.
CREATE TABLE IF NOT EXISTS class_property (
    class_id TEXT NOT NULL,
    property_schema_id TEXT NOT NULL,
    sequence INTEGER NOT NULL DEFAULT 0,
    required INTEGER,
    readonly INTEGER,
    hide_when_empty INTEGER,
    default_value TEXT,
    hlc_physical INTEGER NOT NULL DEFAULT 0,
    hlc_logical INTEGER NOT NULL DEFAULT 0,
    actor_id TEXT,
    PRIMARY KEY (class_id, property_schema_id)
);
CREATE INDEX IF NOT EXISTS idx_class_property_class ON class_property (class_id);
"""


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """Add the class_property binding table (CREATE IF NOT EXISTS is the guard;
    idempotent for fresh databases, which run the whole chain 1→4)."""
    conn.executescript(_CLASS_PROPERTY_DDL)


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """Tags (web schema v5→v6 parity): the tag OR-Set table plus the
    ``nodes.tag_ids`` projection column, guarded for upgrade paths whose
    CREATE TABLE already ran at the previous shape."""
    conn.executescript(_TAG_MEMBER_SET_DDL)
    LocalStore._add_column_if_missing(conn, "nodes", "tag_ids", "TEXT NOT NULL DEFAULT '[]'")


def _migrate_v6(conn: sqlite3.Connection) -> None:
    """Class order (web schema v6→v7 parity): the per-node ``class_order``
    list written by class.reorder (LWW-by-arrival); the effective class_ids
    merges ordered members first, then unlisted members sorted by id."""
    LocalStore._add_column_if_missing(conn, "nodes", "class_order", "TEXT NOT NULL DEFAULT '[]'")


def _migrate_v7(conn: sqlite3.Connection) -> None:
    """Revision-11 render-state model (web schema v7→v8 parity): the
    node_type enumeration is replaced by the two booleans. Table rebuild
    (works on old SQLite builds — no DROP COLUMN): nodes_v7 carries
    is_class / present_as_main, the rows map page → (0, 1), block → (0, 0),
    class → (1, 0), and the single placement CHECK keeps classes as roots.
    The old "block needs a parent" rule disappears with the column:
    parentless non-class nodes are legal now (document chrome by the second
    cascade branch)."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(nodes)")}
    if "node_type" not in columns:
        return  # already at the v7 shape (guarded re-run)
    conn.executescript(
        """
        PRAGMA foreign_keys = OFF;
        CREATE TABLE nodes_v7 (
            workspace_id TEXT NOT NULL,
            id TEXT NOT NULL,
            is_class INTEGER NOT NULL DEFAULT 0,
            present_as_main INTEGER NOT NULL DEFAULT 0,
            parent_id TEXT,
            name TEXT,
            class_ids TEXT NOT NULL DEFAULT '[]',
            class_order TEXT NOT NULL DEFAULT '[]',
            tag_ids TEXT NOT NULL DEFAULT '[]',
            icon TEXT,
            color TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            content TEXT,
            content_plain TEXT NOT NULL DEFAULT '',
            created_at TEXT,
            updated_at TEXT NOT NULL,
            created_by TEXT,
            updated_by TEXT,
            hlc_physical INTEGER NOT NULL DEFAULT 0,
            hlc_logical INTEGER NOT NULL DEFAULT 0,
            actor_id TEXT,
            CHECK (is_class = 0 OR parent_id IS NULL),
            PRIMARY KEY (workspace_id, id)
        );
        INSERT INTO nodes_v7 (
            workspace_id, id, is_class, present_as_main, parent_id, name,
            class_ids, class_order, tag_ids, icon, color, is_active, content,
            content_plain, created_at, updated_at, created_by, updated_by,
            hlc_physical, hlc_logical, actor_id
        )
        SELECT workspace_id, id,
            CASE WHEN node_type = 'class' THEN 1 ELSE 0 END,
            CASE WHEN node_type = 'page' THEN 1 ELSE 0 END,
            parent_id, name, class_ids, class_order, tag_ids, icon, color,
            is_active, content, content_plain, created_at, updated_at,
            created_by, updated_by, hlc_physical, hlc_logical, actor_id
        FROM nodes;
        DROP TABLE nodes;
        ALTER TABLE nodes_v7 RENAME TO nodes;
        PRAGMA foreign_keys = ON;
        """
    )


_TAG_MEMBER_SET_DDL = """
-- OR-Set of tag assignments; the applier projects present rows into
-- node.tag_ids sorted by id (web schema v5→v6, 2026-10-01 lockstep).
CREATE TABLE IF NOT EXISTS tag_member_set (
    node_id TEXT NOT NULL,
    tag_id TEXT NOT NULL,
    present INTEGER NOT NULL,
    hlc_physical INTEGER NOT NULL DEFAULT 0,
    hlc_logical INTEGER NOT NULL DEFAULT 0,
    actor_id TEXT,
    PRIMARY KEY (node_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_tag_member_set_tag ON tag_member_set (tag_id);
"""


#: Ordered migration chain; each entry bumps ``PRAGMA user_version`` to its target.
_MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = (
    (1, _migrate_v1),
    (2, _migrate_v2),
    (3, _migrate_v3),
    (4, _migrate_v4),
    (5, _migrate_v5),
    (6, _migrate_v6),
    (7, _migrate_v7),
)
