"""Local SQLite store for the Notees GTK client (relay protocol v3).

Owns the offline-capable client cache: the relay outbox (pending/quarantined
envelopes), the op-id dedupe log, the sync watermark (seq cursor +
``restore_epoch``), and the mirrored node table with the applier semantics
ported from ``packages/store/src/appliers.ts``: row-level last-write-wins by
``(hlc_physical, hlc_logical, actor_id)``, OR-Set class/tag/collection
membership, user-defined class order (``class.reorder``, LWW-by-arrival),
title-is-content (a node's title IS its content — no ``name`` writes; nodes
with document chrome carry text-only content), the Revision-11 render-state
model (``is_class`` identity + ``present_as_main`` render bit; classes are
containers and always roots), m2m class extends with an
applier-maintained transitive closure (cycles fail loud), fractional
child-order positions, property values with tombstones, soft/permanent
deletes with trash retention, per-workspace feature toggles
(``workspace.feature.set`` — LWW rows, F4 ``class.delete`` routing, the
``tasks`` enable family seed-ensure), PG5 per-element value
identity (the row id IS the element id; OR-Set add-wins removes with element
tombstones; the visible-set derivation every read consults), PC4 binding
``active`` (soft-unbind), PC6 date-node-backed qualifiers (normalize-on-write,
read-lenient), and the unset-carrier semantics (unsetting a node-backed
text value trashes the orphaned carrier block), PG4 extends-aware binding
resolution (the diamond rule: own binding → shortest extends-path → earliest
class-assignment HLC, ties by class id; ``bound_by`` names the supplying
ancestor), and PG6 apply-time value validation (one-shape-per-type + scalar
typing + cardinality/precision/filter/existence fail-loud at the property.set
write path).

Thread-safety: the store is constructed on the GTK main thread while the sync
engine runs on worker threads against the same connection. The connection is
therefore opened with ``check_same_thread=False`` and every public method
serializes through a re-entrant lock (see :func:`_synchronized`); private
helpers are only ever called from locked public methods.

Schema notes: the client cache keeps its own table shapes where they match the
derived schema — the ``nodes`` mirror is keyed ``(workspace_id, id)`` (the
server keys ``id`` alone; node ids are uuid7, so the difference is
theoretical) and carries the same column names. Placement invariants the
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
import math
import os
import re
import sqlite3
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any, Concatenate, cast

from notees_gtk.core.protocol.content import (
    parse_content_ast,
    plaintext_excerpt,
    stringify_content_ast,
)
from notees_gtk.core.protocol.dates import day_node_id, parse_date_node_id
from notees_gtk.core.protocol.features import (
    SYSTEM_CLASS_ICONS_TASK,
    TASK_FAMILY_SEED,
    WorkspaceFeature,
    family_class_names,
    feature_for_managed_class,
    gating_features_for_class,
    system_class_uuid,
    task_property_uuid,
)
from notees_gtk.core.protocol.models import RelayEnvelope
from notees_gtk.core.protocol.payloads import validate_payload
from notees_gtk.data.errors import (
    CycleError,
    EnvelopeValidationError,
    MoveGuardError,
    NotFoundError,
    PropertyValueShapeError,
    UnsupportedCarrierError,
)

__all__ = ["EffectiveProperty", "EffectivePropertySchema", "LocalStore", "NodeRow"]

_log = logging.getLogger(__name__)

#: Latest schema version applied to the database (see ``_MIGRATIONS``).
#: v5 adds the tag OR-Set (web schema v5→v6 parity); v6 adds the per-node
#: class order list (web schema v6→v7 parity); v7 is the Revision-11
#: render-state model (web schema v7→v8 parity): the node_type enumeration is
#: replaced by ``is_class`` + ``present_as_main``; v8 adds the per-workspace
#: feature toggle table (web schema v9→v10 parity); v9 is the
#: property-wire batch (web schema v10→v11 parity): PG5 element
#: tombstones + the property_value rebuild (the UNIQUE(node, schema, idx)
#: retires — the row id IS the element id), PC4 ``class_property.active``,
#: and the property_schema date columns PC6 normalizes through.
#: v10 (SCHEMA.md "Number formats"): property_schema gained
#: number_pad / number_decimals / number_rounding (display-only formatting
#: for number schemas).
#: v11: class_property gained ``display`` — the binding's
#: value-display position (NULL/'panel' = the properties section only).
#: v12 (owner review, same day): the render contracts are
#: PROPERTY-level — property_schema gains display / readonly /
#: hide_when_empty (NULL = panel / unset), and class_property is REBUILT
#: without the retired binding columns (readonly/hide_when_empty pre-v3.1.0,
#: display the v11 experiment); ``required`` survives on the row —
#: the owner's per-class exception.
SCHEMA_VERSION = 12

#: Strict pattern every interpolated snapshot identifier must match — closes
#: the quote-breakout surface on names read from the attached snapshot.
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

#: Node tables a snapshot blob may provide. The server's derived schema uses
#: ``node`` (singular); the plural ``nodes`` is the *client* cache's table and
#: must never be accepted (it is how the original restore bug hid in tests).
_SNAPSHOT_TABLES = frozenset({"node"})

#: Snapshot node columns copied verbatim (client cache column → server column).
#: Server derived schema: same names, same polarity (``is_active`` is active on
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
    """Map client cache columns to snapshot SELECT expressions."""
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
        name: Legacy display-name cache, never written by the appliers
            (title-is-content: a node's title IS its content). Remaining
            readers are transition-only; snapshot restores may populate it.
        class_ids: OR-Set class membership projected from ``class_member_set``
            (ordered members first when ``class.reorder`` wrote a user order,
            then any unlisted present members sorted by id).
        tag_ids: OR-Set tag membership projected from ``tag_member_set``
            (sorted by id).
        icon: Emoji/icon string or ``None``.
        color: Preset token or custom ``#RRGGBB`` hex (colors.py grammar), or
            ``None`` (never written / cleared).
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
    """Property schema projection inside an :class:`EffectiveProperty` row.

    Carries the PROPERTY-level render contracts (``display``
    sanitized — ``"bullet"``/``"inline"`` or ``None`` for NULL/"panel";
    ``readonly``/``hide_when_empty`` tri-state flags) — the same for every
    carrier, class-bound or not."""

    id: str
    name: str
    type: str
    multi: bool
    display: str | None = None
    readonly: bool | None = None
    hide_when_empty: bool | None = None


@dataclass(frozen=True)
class EffectiveProperty:
    """One effective ``(schema, idx)`` row for a node — the property panel's
    read surface (SCHEMA.md "Class properties", 2026-09-27; PG5/PC4).

    ``source`` tags authored vs derived; ``bound_by`` is the class supplying
    the binding metadata — the winning class for a default, the
    currently-binding class for an authored row, or ``None`` when no current
    class binds the schema (an authored value whose binding went away stays
    visible, marked unbound). ``element_id`` is the PG5 element identity —
    the property_value row id for an authored row (the address multi-value
    removes target), ``default:{schema}:0`` for a derived default.
    (owner review 2026-10-05): the render contracts ``readonly``/
    ``hide_when_empty``/``display`` are PROPERTY-level — SCHEMA-sourced and
    identical on authored and derived rows, unbound authored values
    included; ``required`` is the per-CLASS exception — it stays
    binding-sourced (``None`` when no active binding wins).
    """

    property_schema_id: str
    idx: int
    element_id: str
    schema: EffectivePropertySchema | None
    value: Any
    metadata: dict[str, Any] | None
    source: str  # "authored" | "default"
    bound_by: str | None
    required: bool | None
    readonly: bool | None
    hide_when_empty: bool | None
    sequence: int | None
    display: str | None = None


def _envelope_ts(env: RelayEnvelope) -> str:
    """Return the envelope timestamp as ISO-8601, falling back to now."""
    return env.timestamp.isoformat() if env.timestamp is not None else _now_iso()


def _class_node_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Map a ``class.*`` payload onto the class-node upsert field names.

    ``color`` keeps wire PRESENCE: the key lands in the result only when the
    payload carried it, so an explicit ``null`` (clear) survives the mapping
    instead of collapsing into "absent".
    """
    fields: dict[str, Any] = {"content_ast": payload.get("contentAst"), "icon": payload.get("icon")}
    if "color" in payload:
        fields["color"] = payload["color"]
    return fields


_UUID_LIKE_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

#: PC6: the reserved qualifier keys that canonicalize as date-node
#: refs on dateQualified schemas (SCHEMA.md "Dates").
_QUALIFIER_KEYS = ("startDate", "endDate")


def _node_ref_of_value(value: Any) -> str | None:
    """The node id a value references, when it is reference-shaped: either the
    canonical ``{"nodeId": …}`` or a legacy bare uuid. Scalar strings that are
    not uuid-shaped return None (they are text, not references)."""
    if isinstance(value, Mapping) and "nodeId" in value:
        node_id = value["nodeId"]
        return node_id if isinstance(node_id, str) and len(node_id) > 0 else None
    if isinstance(value, str) and _UUID_LIKE_RE.match(value):
        return value
    return None


def _normalize_qualifier_metadata(schema: dict[str, Any] | None, metadata: Any) -> Any:
    """PC6 normalize-on-write (SCHEMA.md "Dates"): for a dateQualified
    schema, the reserved qualifier keys canonicalize to date-node refs
    ``{"nodeId": <day chain node>}``. A well-formed ``YYYY-MM-DD`` string is
    the legacy encoding (the pre-PC6 panel wrote input[type=date] values) and
    rewrites to the deterministic day-node id — pure value rewriting, no graph
    side effects; the ref joins the year/month/day chain whenever the chain
    exists and stays existence-lenient until then (the text-carrier precedent).
    Non-date strings, refs, other metadata keys, non-qualified schemas, and
    unknown schemas all ride through untouched."""
    if metadata is None:
        return None
    if not isinstance(metadata, Mapping):
        return metadata
    if schema is None or schema.get("date_qualified") != 1:
        return metadata
    normalized = dict(metadata)
    changed = False
    for key in _QUALIFIER_KEYS:
        value = normalized.get(key)
        if not isinstance(value, str):
            continue
        try:
            day = day_node_id(value)
        except ValueError:
            continue  # not a well-formed ISO date — ride through as authored.
        normalized[key] = {"nodeId": day}
        changed = True
    return normalized if changed else metadata


# --- PG6 apply-time value validation ------------------------------------------
#
# Port of the main repo's packages/store/src/property-values.ts. Write shapes
# by schema type: text = a scalar string OR a carrier reference {"nodeId": …}
# (a legacy bare uuid normalizes to the reference shape); date/object = a node
# reference; date_range = {"start": ref|null, "end": ref|null} — either side
# open. Scalar typing: number = a finite number — a NUMERIC STRING is the
# migrated legacy encoding (live data carries epoch-millis strings) and
# normalizes to a number; boolean/url/email/select = their scalar;
# multi_select = an array of strings. image stays UNCHECKED by design (PG14
# owns the shape — live data carries migrated asset payloads, so any check would
# break replay). `null` means "no value" and bypasses shape validation.

#: SCHEMA.md "Dates": finest granularity a date value may claim.
_DATE_PRECISION_RANK = {"year": 1, "month": 2, "day": 3}


def _jsonish(value: Any) -> str:
    """The JSON rendering an error message quotes (the TS JSON.stringify)."""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _shape_error_detail(type_: str) -> str:
    if type_ == "text":
        return 'a string or a node reference { "nodeId": … }'
    if type_ == "date_range":
        return '{ "start": ref|null, "end": ref|null } of node references'
    return 'a node reference { "nodeId": … }'


def _assert_value_shape_for_type(type_: str, value: Any, op_type: str) -> Any:
    """The PB2 one-shape-per-type gate (SCHEMA.md "Node-backed text
    properties" / "Dates"), invoked by the PG6 validator. Returns the
    normalized value to store (a legacy bare-uuid reference becomes
    ``{"nodeId": …}``); ``None`` bypasses. Raises
    :class:`~notees_gtk.data.errors.PropertyValueShapeError` on mismatch."""
    if value is None:
        return value
    if type_ == "text":
        if isinstance(value, str):
            return value
        ref = _node_ref_of_value(value)
        if ref is not None:
            return {"nodeId": ref}
    elif type_ in ("date", "object"):
        ref = _node_ref_of_value(value)
        if ref is not None:
            return {"nodeId": ref}
    elif type_ == "date_range":
        if isinstance(value, Mapping):
            sides: dict[str, Any] = {}
            well_formed = True
            for key in ("start", "end"):
                if key not in value:
                    well_formed = False
                    break
                side_value = value[key]
                if side_value is None:
                    sides[key] = None
                    continue
                ref = _node_ref_of_value(side_value)
                if ref is None:
                    well_formed = False
                    break
                sides[key] = {"nodeId": ref}
            if well_formed:
                return {"start": sides["start"], "end": sides["end"]}
    else:
        return value  # number/boolean/url/email/select/multi_select/image ride to scalar typing
    raise PropertyValueShapeError(
        f"{op_type}: value for {type_} schema must be {_shape_error_detail(type_)} — got {_jsonish(value)}",
        op_type,
    )


def _scalar_error_detail(type_: str) -> str:
    if type_ == "number":
        return "a finite number"
    if type_ == "boolean":
        return "a boolean"
    if type_ == "multi_select":
        return "an array of strings (option ids)"
    return "a string"


def _assert_scalar_shape_for_type(type_: str, value: Any, op_type: str) -> Any:
    """The PG6 scalar-typing extension, keyed off the schema type. Returns the
    normalized value to store (a numeric string becomes a number)."""
    if value is None:
        return value
    if type_ == "number":
        if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value):
            return value
        # migrated epoch-millis strings (live-data verified): normalize.
        if isinstance(value, str) and value.strip() != "":
            with contextlib.suppress(ValueError):
                parsed = float(value)
                if math.isfinite(parsed):
                    return int(parsed) if parsed.is_integer() else parsed
    elif type_ == "boolean":
        if isinstance(value, bool):
            return value
    elif type_ in ("url", "email", "select"):
        if isinstance(value, str):
            return value
    elif type_ == "multi_select":
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return value
    else:
        # image and any future type: unchecked (see the section header).
        return value
    raise PropertyValueShapeError(
        f"{op_type}: value for {type_} schema must be {_scalar_error_detail(type_)} — got {_jsonish(value)}",
        op_type,
    )


def _is_valid_default_for_type(type_: str, value: Any) -> bool:
    """PC2: a class-binding defaultValue must be typed per the schema type.
    Node-typed schemas (date/date_range/object) accept only JSON null — a
    default that links a node is meaningless. Returns False instead of
    throwing so the read model can drop silently; the write path
    (class.property.set) fails loud. Mirrors isValidDefaultForType."""
    if value is None:
        return True
    if type_ in ("text", "url", "email", "image", "select"):
        return isinstance(value, str)
    if type_ == "number":
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    if type_ == "boolean":
        return isinstance(value, bool)
    if type_ == "multi_select":
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    # date/date_range/object accept only JSON null (handled above); unknown
    # future types ride unchecked.
    return type_ not in ("date", "date_range", "object")


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
            "object.restore": self._apply_object_restore,
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
            "workspace.feature.set": self._apply_workspace_feature_set,
        }
        self._migrate()

    # ------------------------------------------------------------------ schema

    def _migrate(self) -> None:
        """Apply every migration step newer than the stored ``user_version``.

        The canonical aux DDL is applied first on every upgrade (the web
        schema.ts ``migrate`` parity: ``db.exec(schemaSql())`` runs before
        the version-specific steps), so a database missing a table — an
        interrupted upgrade, a handcrafted pre-release DB — converges to the
        full table set before the guarded ALTERs run. Every statement is
        CREATE IF NOT EXISTS / guarded, so the step is a no-op for a
        complete database.
        """
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            with self._conn:
                self._conn.executescript(_CANONICAL_AUX_DDL)
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
                    "property_value_element_tombstone",
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
            self._conn.execute("DELETE FROM workspace_feature WHERE workspace_id = ?", (workspace_id,))

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
                "encrypted payload slot ($e) is reserved for E2EE and cannot be applied yet",
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

    def _parent_id(self, workspace_id: str, node_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT parent_id FROM nodes WHERE workspace_id = ? AND id = ?",
            (workspace_id, node_id),
        ).fetchone()
        return None if row is None else (None if row[0] is None else str(row[0]))

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

        # First create wins for duplicate node ids (INSERT OR IGNORE): a
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
        # Color is nullable on the wire: a present null CLEARS the column, so
        # the write keys off payload PRESENCE, not value (zod
        # ``colorValueSchema.nullish()`` parity — the TS applier gates on
        # ``p.color !== undefined``); a ``is not None`` test here would
        # silently drop the clear.
        if "color" in payload:
            sets.append("color = ?")
            values.append(payload["color"])
        if payload.get("contentAst") is not None:
            # Document-chrome content (class nodes and main-presenting nodes)
            # is text-only; inline blocks keep the rich tokens they were sent.
            flatten = bool(row[3]) or resulting_present_as_main
            tokens: Any = payload["contentAst"] if not flatten else stringify_content_ast(payload["contentAst"])
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
                "property_value_element_tombstone",
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

    def _apply_object_restore(self, env: RelayEnvelope) -> bool:
        """Restore from the trash (lockstep with the TS reference's
        ``applyObjectRestore``). Whole-tree: the subtree that rode THIS trash
        event reactivates; a descendant with its OWN trash row was trashed
        independently and stays trashed (its subtree rides with it). The
        root's trash row is consumed. Corner: a missing parent row (parent
        permanently deleted / legacy data) reparents to the workspace root;
        a present-but-inactive parent is left alone — restoring the parent
        later heals the tree. LWW against ``object.delete`` is log order."""
        op_type = "object.restore"
        object_id = str(env.payload["objectId"])
        row = self._require_node(env.workspace_id, object_id, op_type)
        workspace_id = env.workspace_id
        with self._conn:
            parent_id = row[2]
            if parent_id is not None and self._node_row(workspace_id, str(parent_id)) is None:
                self._conn.execute(
                    "UPDATE nodes SET parent_id = NULL WHERE workspace_id = ? AND id = ?",
                    (workspace_id, object_id),
                )
            ids = self._subtree_ids(workspace_id, object_id)
            has_own_trash = {
                str(r[0])
                for r in self._conn.execute(
                    f"SELECT node_id FROM trash WHERE node_id IN ({', '.join('?' for _ in ids)})",
                    ids,
                ).fetchall()
            }
            to_reactivate: list[str] = []
            for node in ids:
                if node != object_id and node in has_own_trash:
                    continue  # trashed independently — stays trashed
                # Walk up: an own-trash-row ancestor below the root means this
                # id rode a DIFFERENT delete.
                cursor = self._parent_id(workspace_id, node)
                rides_this_delete = True
                while cursor is not None:
                    if cursor == object_id:
                        break
                    if cursor in has_own_trash:
                        rides_this_delete = False
                        break
                    cursor = self._parent_id(workspace_id, cursor)
                if rides_this_delete:
                    to_reactivate.append(node)
            placeholders = ", ".join("?" for _ in to_reactivate)
            self._conn.execute(
                f"UPDATE nodes SET is_active = 1 WHERE workspace_id = ? AND id IN ({placeholders})",
                [workspace_id, *to_reactivate],
            )
            self._conn.execute("DELETE FROM trash WHERE node_id = ?", (object_id,))
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
                      icon, color,
                      is_active, created_at, updated_at, created_by, updated_by,
                      hlc_physical, hlc_logical, actor_id)
                   VALUES (?, ?, 1, 0, NULL, '[]', NULL, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    env.workspace_id,
                    class_id,
                    content,
                    content_plain,
                    fields.get("icon"),
                    fields.get("color"),
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
            if fields.get("icon") is not None:
                sets.append("icon = ?")
                values.append(fields["icon"])
            # Color carries presence semantics: a present null CLEARS the
            # column, so callers put the key only when the payload
            # had it and the write keys off membership, not value (the
            # create-time value already rode the INSERT above — the LWW-gated
            # UPDATE can never beat this envelope's own HLC, so routing
            # create fields through it would silently drop them).
            if "color" in fields:
                sets.append("color = ?")
                values.append(fields["color"])
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
            _class_node_fields(payload),
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
            for column in ("icon", "description"):
                if payload.get(column) is not None:
                    sets.append(f"{column} = ?")
                    values.append(payload[column])
            # Color is nullable on the wire: a present null CLEARS the column,
            # so the write keys off payload PRESENCE, not value (TS parity:
            # ``p.color !== undefined``); a ``is not None`` test would silently
            # drop the clear.
            if "color" in payload:
                sets.append("color = ?")
                values.append(payload["color"])
            if not sets:
                return False
            sets.append("updated_at = ?")
            values.extend((_envelope_ts(env), class_id))
            self._conn.execute(f"UPDATE class SET {', '.join(sets)} WHERE id = ?", values)
        self._upsert_class_node(
            env,
            class_id,
            _class_node_fields(payload),
        )
        return True

    def _apply_class_delete(self, env: RelayEnvelope) -> bool:
        """Class removal.

        F4 routing: a delete addressed at a family BASE class is applied as a
        FEATURE-DISABLE (LWW row + the per-class archival re-derivation), so
        the Features setting is the single archive path for managed classes
        and the lossy plain delete (membership tombstoning below) never runs
        on them. Only the five bases route (the owner's exact mapping);
        family children (book, meeting, birthday, …) keep plain delete
        semantics. The route decision is a pure function of the class id
        (fixed vocabulary), so every replica takes the same branch; the LWW
        row gate keeps the derived state convergent under either delivery
        order.
        """
        class_id = str(env.payload["classId"])
        ts = _envelope_ts(env)
        managed_feature = feature_for_managed_class(class_id)
        if managed_feature is not None:
            wrote = self._lww_write_feature_row(env, managed_feature, False)
            if wrote:
                self._derive_family_class_bits(env.workspace_id, managed_feature)
            return wrote
        with self._conn:
            self._conn.execute("UPDATE class SET active = 0, updated_at = ? WHERE id = ?", (ts, class_id))
            self._conn.execute(
                "UPDATE nodes SET is_active = 0, updated_at = ? WHERE workspace_id = ? AND id = ?",
                (ts, env.workspace_id, class_id),
            )
            # The lossy plain path (non-managed classes): the class leaves
            # every node's class_ids — tombstone the membership pairs and
            # recompute the affected nodes (otherwise pills render dangling
            # ids). The toggle path above NEVER touches membership (F3).
            affected = [
                str(row[0])
                for row in self._conn.execute(
                    "SELECT node_id FROM class_member_set WHERE class_id = ? AND present = 1", (class_id,)
                )
            ]
            self._conn.execute("UPDATE class_member_set SET present = 0 WHERE class_id = ?", (class_id,))
            for node_id in affected:
                self._recompute_class_ids(node_id)
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
        op_type = "class.property.set"
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

        # PC2: defaultValue is typed per the schema type — a
        # wrong-typed default fails loud here instead of deriving silently on
        # every read. Omitted defaultValue (patch keeps the stored one) skips
        # the check; a stored default that drifts out of match (schema
        # delete+recreate with a different type) is dropped defensively at
        # the effective read instead. Unknown schema ids skip validation (no
        # schema FK). A stale write dropped by the row LWW above never
        # reaches the check — the TS parity.
        if "defaultValue" in payload:
            schema_type = self._property_schema_type(schema_id)
            default_value = payload["defaultValue"]
            if schema_type is not None and not _is_valid_default_for_type(schema_type, default_value):
                if schema_type in ("date", "date_range", "object"):
                    detail = "must be null — node-typed defaults are not supported"
                else:
                    detail = f"must be typed {schema_type}"
                raise PropertyValueShapeError(
                    f"{op_type}: defaultValue for {schema_type} schema {detail} — got {_jsonish(default_value)}",
                    op_type,
                )

        # Omitted (absent) fields keep the stored value via COALESCE; explicit
        # false / JSON null are real writes (null default == JSON "null").
        # The binding row carries ONLY the genuinely per-class
        # mechanics (sequence, required, defaultValue, active) — the render
        # contracts (readonly/hideWhenEmpty/display) are PROPERTY-level and
        # live on property_schema.
        def flag(name: str) -> int | None:
            if name not in payload:
                return None  # absent → COALESCE keeps the stored value
            return 1 if payload[name] else 0  # explicit null (falsy) writes 0, as the TS port

        # PC4: the soft-unbind flag rides the row LWW — an inactive
        # binding stops contributing to the effective read (no default, no
        # sequence) while the ROW survives (unlike class.property.unset).
        # Absent payload = keep the stored flag (patch convention).
        active = flag("active")
        sequence = int(payload["sequence"]) if payload.get("sequence") is not None else None
        default_value = json.dumps(payload["defaultValue"]) if "defaultValue" in payload else None
        with self._conn:
            if existing is None:
                self._conn.execute(
                    """INSERT INTO class_property
                         (class_id, property_schema_id, sequence, required, default_value, active,
                          hlc_physical, hlc_logical, actor_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        class_id,
                        schema_id,
                        sequence if sequence is not None else 0,
                        flag("required"),
                        default_value,
                        active if active is not None else 1,
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
                         default_value = COALESCE(?, default_value),
                         active = COALESCE(?, active),
                         hlc_physical = ?, hlc_logical = ?, actor_id = ?
                       WHERE class_id = ? AND property_schema_id = ?""",
                    (
                        sequence,
                        flag("required"),
                        default_value,
                        active,
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
                      date_precision, date_qualified, number_pad, number_decimals, number_rounding,
                      display, readonly, hide_when_empty,
                      active, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     name = excluded.name, type = excluded.type, multi = excluded.multi,
                     scope = excluded.scope, options = excluded.options,
                     target_class_filter = excluded.target_class_filter,
                     date_precision = excluded.date_precision, date_qualified = excluded.date_qualified,
                     number_pad = excluded.number_pad, number_decimals = excluded.number_decimals,
                     number_rounding = excluded.number_rounding,
                     display = excluded.display, readonly = excluded.readonly,
                     hide_when_empty = excluded.hide_when_empty,
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
                    payload.get("datePrecision"),
                    (None if payload.get("dateQualified") is None else (1 if payload.get("dateQualified") else 0)),
                    payload.get("numberPad"),
                    payload.get("numberDecimals"),
                    payload.get("numberRounding"),
                    # Render contracts (PROPERTY-level): absent or
                    # null stores NULL (panel / unset).
                    payload.get("display"),
                    (None if payload.get("readonly") is None else (1 if payload.get("readonly") else 0)),
                    (None if payload.get("hideWhenEmpty") is None else (1 if payload.get("hideWhenEmpty") else 0)),
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
            if payload.get("datePrecision") is not None:
                sets.append("date_precision = ?")
                values.append(payload["datePrecision"])
            if payload.get("dateQualified") is not None:
                sets.append("date_qualified = ?")
                values.append(1 if payload["dateQualified"] else 0)
            # Number formats: key-PRESENCE distinguishes absent (keep) from an
            # explicit null (clear) — the keep-vs-clear contract.
            if "numberPad" in payload:
                sets.append("number_pad = ?")
                values.append(payload["numberPad"])
            if "numberDecimals" in payload:
                sets.append("number_decimals = ?")
                values.append(payload["numberDecimals"])
            if "numberRounding" in payload:
                sets.append("number_rounding = ?")
                values.append(payload["numberRounding"])
            # Render contracts (PROPERTY-level): same key-presence
            # keep-vs-clear contract — absent keeps the stored value, a
            # present null clears back to panel / unset. `required` is NOT
            # here; it stays on the class binding.
            if "display" in payload:
                sets.append("display = ?")
                values.append(payload["display"])
            if "readonly" in payload:
                sets.append("readonly = ?")
                values.append(None if payload["readonly"] is None else (1 if payload["readonly"] else 0))
            if "hideWhenEmpty" in payload:
                sets.append("hide_when_empty = ?")
                values.append(None if payload["hideWhenEmpty"] is None else (1 if payload["hideWhenEmpty"] else 0))
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
    #
    # PG5: the property_value row id IS the element id — writer-minted
    # UUIDv7 for element adds, the deterministic composite node:schema:idx for
    # single-value slots and legacy positional writes (the pre-PG5 id
    # generation — replayed stored logs apply byte-identical). Multi-value
    # slots are an OR-Set of elements: adds never conflict, removes tombstone
    # the element (add-wins: the membership comparator is HLC-only, so on
    # equal HLC the add wins regardless of actor), and the visible-set
    # derivation (:meth:`_visible_property_value_rows`) is consulted by every
    # read. PC6: dateQualified schemas normalize the reserved
    # qualifier keys to date-node refs on write (read-lenient — every reader
    # accepts both shapes). PB2: unsetting a node-backed text value
    # trashes the now-orphaned carrier block under the three guards.

    @staticmethod
    def _property_slot(env: RelayEnvelope) -> tuple[str, str, int, str | None]:
        payload = env.payload
        element_id = payload.get("elementId")
        return (
            str(payload["objectId"]),
            str(payload["propertySchemaId"]),
            int(payload.get("idx") or 0),
            str(element_id) if element_id is not None else None,
        )

    @staticmethod
    def _positional_property_value_id(node_id: str, schema_id: str, idx: int) -> str:
        """The deterministic positional-element id (PG5), the row id for
        single-value slots and legacy positional writes."""
        return f"{node_id}:{schema_id}:{idx}"

    def _property_schema_row(self, schema_id: str) -> dict[str, Any] | None:
        """The schema row the property write path consults (PC6/PG6), or None
        when the schema id is unknown (property.set has no schema FK —
        arbitrary ids store unchecked, the TS parity). Carries every column
        the validators read: type, multi, the target filter, the date
        precision ceiling, and the PC6 qualifier flag."""
        row = self._conn.execute(
            "SELECT id, type, multi, target_class_filter, date_precision, date_qualified"
            " FROM property_schema WHERE id = ?",
            (schema_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": str(row[0]),
            "type": row[1],
            "multi": row[2],
            "target_class_filter": row[3],
            "date_precision": row[4],
            "date_qualified": row[5],
        }

    def _property_schema_type(self, schema_id: str) -> str | None:
        """The schema type a class.property.set defaultValue validates
        against (PC2), or None when the schema id is unknown."""
        row = self._conn.execute("SELECT type FROM property_schema WHERE id = ?", (schema_id,)).fetchone()
        return None if row is None else str(row[0])

    def _assert_ref_target_for_schema(self, workspace_id: str, schema: dict[str, Any], ref: str, op_type: str) -> None:
        """PG6 graph checks for a node-typed ref's target: row existence (any
        liveness — trash is a state, not an absence), the targetClassFilter
        (extends-aware: a carried class satisfies the filter when it equals an
        entry or descends from one through class_hierarchy — the bibliography
        model filters authors by ``agent`` while person/organization EXTEND
        agent), and the datePrecision ceiling for date/date_range refs."""
        target = self._conn.execute(
            "SELECT class_ids FROM nodes WHERE workspace_id = ? AND id = ?", (workspace_id, ref)
        ).fetchone()
        if target is None:
            raise PropertyValueShapeError(f"{op_type}: value references node {ref}, which does not exist", op_type)
        filter_raw = schema.get("target_class_filter")
        if filter_raw is not None:
            try:
                filter_list = json.loads(filter_raw)
            except (TypeError, ValueError):
                filter_list = None
            if isinstance(filter_list, list) and len(filter_list) > 0:
                try:
                    carried_raw = json.loads(target[0]) if target[0] else []
                except (TypeError, ValueError):
                    carried_raw = []
                carried = [str(class_id) for class_id in carried_raw if isinstance(class_id, str)]
                allowed = set(carried)
                if carried:
                    placeholders = ", ".join("?" for _ in carried)
                    for (ancestor,) in self._conn.execute(
                        f"SELECT ancestor_id FROM class_hierarchy WHERE class_id IN ({placeholders})", carried
                    ):
                        allowed.add(str(ancestor))
                if not any(isinstance(class_id, str) and class_id in allowed for class_id in filter_list):
                    raise PropertyValueShapeError(
                        f"{op_type}: value target {ref} does not carry any of the schema's allowed classes",
                        op_type,
                    )
        if schema["type"] in ("date", "date_range"):
            parsed = parse_date_node_id(ref)
            if parsed is not None:
                ceiling = _DATE_PRECISION_RANK.get(
                    schema["date_precision"] if schema["date_precision"] is not None else "day", 3
                )
                precision_rank = _DATE_PRECISION_RANK.get(str(parsed["precision"]), 3)
                if precision_rank > ceiling:
                    declared = schema["date_precision"] if schema["date_precision"] is not None else "day"
                    raise PropertyValueShapeError(
                        f"{op_type}: {parsed['precision']} date ref claims finer granularity"
                        f' than the schema\'s "{declared}" precision',
                        op_type,
                    )

    def _assert_value_for_schema(self, workspace_id: str, schema: dict[str, Any], value: Any, op_type: str) -> Any:
        """PG6: the full apply-time validation for a property.set against a
        KNOWN schema row — shape/scalar typing, then the graph checks
        (existence / class filter / date precision) for the node-typed link
        family. ``None`` means "no value" and bypasses everything. Returns
        the normalized value to store."""
        shaped = _assert_value_shape_for_type(schema["type"], value, op_type)
        typed = _assert_scalar_shape_for_type(schema["type"], shaped, op_type)
        if typed is None:
            return typed
        if schema["type"] in ("date", "object"):
            ref = _node_ref_of_value(typed)
            if ref is not None:
                self._assert_ref_target_for_schema(workspace_id, schema, ref, op_type)
        elif schema["type"] == "date_range":
            for side in (typed.get("start"), typed.get("end")):
                ref = _node_ref_of_value(side)
                if ref is not None:
                    self._assert_ref_target_for_schema(workspace_id, schema, ref, op_type)
        return typed

    def _apply_property_set(self, env: RelayEnvelope) -> bool:
        op_type = "property.set"
        payload = env.payload
        node_id, schema_id, idx, element_id = self._property_slot(env)
        schema = self._property_schema_row(schema_id)
        # PB2/PG6: one-shape-per-type + schema-linked
        # integrity at the write path. The schema row (when known —
        # property.set has no schema FK) types the slot: shape/scalar
        # mismatch, a date ref finer than the schema's precision, a target
        # outside the class filter, and a ref to a nonexistent node all fail
        # loud; a legacy bare-uuid reference or numeric string normalizes to
        # the canonical encoding. Validation runs BEFORE the LWW decision —
        # a stale write that would be dropped still rejects an invalid value,
        # deterministically on every replica. Unknown schema ids store
        # unchecked.
        value = (
            self._assert_value_for_schema(env.workspace_id, schema, payload.get("value"), op_type)
            if schema is not None
            else payload.get("value")
        )
        # PC6 normalize-on-write: a well-formed YYYY-MM-DD string in
        # metadata.startDate/endDate rewrites to the deterministic day-node
        # ref — ONLY on dateQualified schema rows, only those two keys, pure
        # value rewriting (no graph side effects, no existence assertion).
        metadata = _normalize_qualifier_metadata(schema, payload.get("metadata"))
        # PG6 cardinality: a single-value schema takes idx 0 only. Higher
        # slots would write rows no reader derives, so the write is rejected,
        # not parked.
        if schema is not None and not schema["multi"] and idx > 0:
            raise PropertyValueShapeError(
                f"{op_type}: schema {schema_id} is single-value — idx must be 0, got {idx}", op_type
            )
        value_json = json.dumps(value, ensure_ascii=False)
        metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata is not None else None
        with self._conn:
            if element_id is not None:
                dropped = self._property_element_add(
                    env, node_id, schema_id, element_id, idx, value_json, metadata_json
                )
            else:
                dropped = self._property_positional_set(env, node_id, schema_id, idx, value_json, metadata_json)
        return not dropped

    def _property_element_add(
        self,
        env: RelayEnvelope,
        node_id: str,
        schema_id: str,
        element_id: str,
        idx: int,
        value_json: str,
        metadata_json: str | None,
    ) -> bool:
        """PG5 OR-Set element ADD: the row id IS the element id, so adds of
        distinct elements never conflict and a re-issued add revives the
        element unless a strictly-newer (HLC) tombstone stands — add-wins:
        on equal HLC the add proceeds regardless of actor (the classIds >=
        convention generalized to the two-table projection; the stored
        tombstone's full (hlc, actor) tuple is otherwise only used for its
        own LWW upsert). The value/metadata/idx overwrite per element uses
        the full (hlc, actor) tuple, exactly like the pre-PG5 slot LWW."""
        tombstone = self._conn.execute(
            "SELECT hlc_physical, hlc_logical FROM property_value_element_tombstone WHERE element_id = ?",
            (element_id,),
        ).fetchone()
        if tombstone is not None and (
            int(tombstone[0]) > env.hlc.physical
            or (int(tombstone[0]) == env.hlc.physical and int(tombstone[1]) > env.hlc.logical)
        ):
            return True  # a strictly-newer remove wins — the add is dropped.
        existing = self._conn.execute(
            "SELECT hlc_physical, hlc_logical, actor_id FROM property_value WHERE id = ?", (element_id,)
        ).fetchone()
        if existing is None:
            self._conn.execute(
                """INSERT INTO property_value
                     (id, node_id, property_schema_id, value, idx, metadata,
                      hlc_physical, hlc_logical, actor_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    element_id,
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
            return False
        if self._incoming_wins(env, existing[0], existing[1], existing[2]):
            self._conn.execute(
                """UPDATE property_value SET value = ?, metadata = ?, idx = ?,
                      hlc_physical = ?, hlc_logical = ?, actor_id = ?
                   WHERE id = ?""",
                (
                    value_json,
                    metadata_json,
                    idx,
                    env.hlc.physical,
                    env.hlc.logical,
                    env.actor_id,
                    element_id,
                ),
            )
            return False
        # A stale re-add (full tuple <= the live row) leaves the row untouched.
        return True

    def _property_positional_set(
        self,
        env: RelayEnvelope,
        node_id: str,
        schema_id: str,
        idx: int,
        value_json: str,
        metadata_json: str | None,
    ) -> bool:
        """The pre-PG5 positional path (payload WITHOUT elementId): unchanged
        slot LWW, keyed by the deterministic positional row id — concurrent
        element adds may share the idx, but a positional write addresses ONLY
        its own deterministic element, so the address is unambiguous without
        the retired UNIQUE(node, schema, idx)."""
        row_id = self._positional_property_value_id(node_id, schema_id, idx)

        # A tombstone with a winning (>=) (hlc, actor) blocks the write.
        tombstone = self._conn.execute(
            "SELECT hlc_physical, hlc_logical, actor_id FROM property_value_tombstone"
            " WHERE node_id = ? AND property_schema_id = ? AND idx = ?",
            (node_id, schema_id, idx),
        ).fetchone()
        if tombstone is not None and not self._incoming_wins(env, tombstone[0], tombstone[1], tombstone[2]):
            return True

        existing = self._conn.execute(
            "SELECT hlc_physical, hlc_logical, actor_id FROM property_value WHERE id = ?", (row_id,)
        ).fetchone()
        if existing is None:
            self._conn.execute(
                """INSERT INTO property_value
                     (id, node_id, property_schema_id, value, idx, metadata,
                      hlc_physical, hlc_logical, actor_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row_id,
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
            return False
        if self._incoming_wins(env, existing[0], existing[1], existing[2]):
            self._conn.execute(
                """UPDATE property_value SET value = ?, metadata = ?,
                      hlc_physical = ?, hlc_logical = ?, actor_id = ?
                   WHERE id = ?""",
                (
                    value_json,
                    metadata_json,
                    env.hlc.physical,
                    env.hlc.logical,
                    env.actor_id,
                    row_id,
                ),
            )
            return False
        # A stale write (full tuple <= the live row) leaves the row untouched.
        return True

    def _apply_property_unset(self, env: RelayEnvelope) -> bool:
        node_id, schema_id, idx, element_id = self._property_slot(env)
        with self._conn:
            if element_id is not None:
                self._property_element_remove(env, node_id, schema_id, element_id)
            else:
                self._property_positional_unset(env, node_id, schema_id, idx)
        return True

    def _property_element_remove(self, env: RelayEnvelope, node_id: str, schema_id: str, element_id: str) -> None:
        """PG5 OR-Set element REMOVE: records the remove's causality on the
        element tombstone (strictly-greater full (hlc, actor) upsert — on an
        exact tie the earlier add sticks, add-wins) and deletes the live row
        when the remove's HLC is strictly newer than the row's (equal HLC
        keeps the row — the add wins ties). An unset addressed at an element
        that exists under a DIFFERENT (node, schema) is malformed: ignored,
        like a stale write (deterministic on every replica)."""
        existing = self._conn.execute(
            "SELECT node_id, property_schema_id, value, hlc_physical, hlc_logical, actor_id"
            " FROM property_value WHERE id = ?",
            (element_id,),
        ).fetchone()
        if existing is not None and (str(existing[0]) != node_id or str(existing[1]) != schema_id):
            return  # malformed addressing — deterministic no-op.
        self._conn.execute(
            """INSERT INTO property_value_element_tombstone
                 (element_id, node_id, property_schema_id, hlc_physical, hlc_logical, actor_id)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(element_id) DO UPDATE SET
                 node_id = excluded.node_id, property_schema_id = excluded.property_schema_id,
                 hlc_physical = excluded.hlc_physical, hlc_logical = excluded.hlc_logical,
                 actor_id = excluded.actor_id
               WHERE excluded.hlc_physical > hlc_physical
                  OR (excluded.hlc_physical = hlc_physical AND excluded.hlc_logical > hlc_logical)
                  OR (excluded.hlc_physical = hlc_physical AND excluded.hlc_logical = hlc_logical
                      AND excluded.actor_id > COALESCE(actor_id, ''))""",
            (element_id, node_id, schema_id, env.hlc.physical, env.hlc.logical, env.actor_id),
        )
        if existing is None:
            return
        remove_wins_by_hlc = env.hlc.physical > int(existing[3]) or (
            env.hlc.physical == int(existing[3]) and env.hlc.logical > int(existing[4])
        )
        if not remove_wins_by_hlc:
            return  # add-wins ties: the live row stays.
        self._conn.execute("DELETE FROM property_value WHERE id = ?", (element_id,))
        # PB2: unsetting a node-backed text value deletes the
        # carrier block under the same guards as the positional path.
        self._trash_text_carrier_if_orphaned(env.workspace_id, node_id, schema_id, str(existing[2]), _envelope_ts(env))

    def _property_positional_unset(self, env: RelayEnvelope, node_id: str, schema_id: str, idx: int) -> None:
        """The pre-PG5 positional remove (payload WITHOUT elementId) —
        unchanged slot tombstones, now keyed by the deterministic positional
        row id."""
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
        row_id = self._positional_property_value_id(node_id, schema_id, idx)
        existing = self._conn.execute(
            "SELECT value, hlc_physical, hlc_logical, actor_id FROM property_value WHERE id = ?", (row_id,)
        ).fetchone()
        if existing is not None and self._incoming_wins(env, existing[1], existing[2], existing[3]):
            self._conn.execute("DELETE FROM property_value WHERE id = ?", (row_id,))
            # PB2 (SCHEMA.md "Node-backed text properties"): unsetting
            # a node-backed text value deletes the carrier block — trash +
            # retention, consistent with node deletion. Guards: the removed
            # value references a node, the target is an active non-class
            # CHILD of the owner, and no other property_value row (any
            # owner/slot, both stored shapes) still references it. Scalar
            # text values (citekey-style) carry no carrier.
            self._trash_text_carrier_if_orphaned(
                env.workspace_id, node_id, schema_id, str(existing[0]), _envelope_ts(env)
            )

    def _trash_text_carrier_if_orphaned(
        self, workspace_id: str, object_id: str, property_schema_id: str, removed_value_raw: str, timestamp: str
    ) -> None:
        """The carrier-deletion half of property.unset. The value row
        is already deleted; ``removed_value_raw`` is its stored JSON. Trashes
        the now-unreferenced carrier inside the same transaction."""
        schema_row = self._conn.execute(
            "SELECT type FROM property_schema WHERE id = ?", (property_schema_id,)
        ).fetchone()
        if schema_row is None or schema_row[0] != "text":
            return
        try:
            parsed: Any = json.loads(removed_value_raw)
        except (TypeError, ValueError):
            return
        target = _node_ref_of_value(parsed)
        if target is None:
            return
        # Exclusive reference: no other live property_value row (any owner or
        # slot) points at the carrier — both the {nodeId} and the legacy
        # bare-uuid stored shapes.
        still_referenced = self._conn.execute(
            "SELECT 1 FROM property_value WHERE value = ? OR value = ? LIMIT 1",
            (json.dumps({"nodeId": target}, ensure_ascii=False), json.dumps(target, ensure_ascii=False)),
        ).fetchone()
        if still_referenced is not None:
            return
        carrier = self._conn.execute(
            "SELECT parent_id, is_class, is_active FROM nodes WHERE workspace_id = ? AND id = ?",
            (workspace_id, target),
        ).fetchone()
        if carrier is None or carrier[0] != object_id or int(carrier[1]) != 0 or int(carrier[2]) != 1:
            return
        ids = self._subtree_ids(workspace_id, target)
        placeholders = ", ".join("?" for _ in ids)
        self._conn.execute(f"UPDATE nodes SET is_active = 0 WHERE id IN ({placeholders})", ids)
        self._conn.execute(
            "INSERT OR REPLACE INTO trash (node_id, deleted_at, is_permanent) VALUES (?, ?, 0)", (target, timestamp)
        )

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

    # --------------------------------------------------------- workspace.feature.*
    #
    # Per-workspace feature toggles (RESHAPED per owner directive 2026-10-04): LWW by
    # (workspace, feature) on the envelope (hlc, actor) — the winning row
    # lands in ``workspace_feature`` and the applier derives the
    # membership-preserving archival of the family's classes from it.
    # Toggle-off is hide-surfaces-keep-data (F3): the class registry ``active``
    # bit + the class node's ``is_active`` flip; ``class_member_set`` rows are
    # NEVER touched (plain class.delete tombstones every membership pair and
    # is lossy; the toggle must not). An absent row means ENABLED (F2). A
    # class.delete addressed at a family BASE class is ROUTED here (F4,
    # ``_apply_class_delete``). The family sets and gating walk resolve
    # through the STATIC seed map in ``features.py`` — never the store's
    # class_extends table — so replicas converge before the seed envelopes
    # arrive.

    def _lww_write_feature_row(self, env: RelayEnvelope, feature: WorkspaceFeature, enabled: bool) -> bool:
        """LWW-write one feature row. Returns True when the incoming envelope
        won (the row was written); False when a newer (hlc, actor) row already
        stood (the toggle is dropped, exactly like a stale property.set)."""
        existing = self._conn.execute(
            "SELECT hlc_physical, hlc_logical, actor_id FROM workspace_feature WHERE workspace_id = ? AND feature = ?",
            (env.workspace_id, feature),
        ).fetchone()
        if existing is not None and not self._incoming_wins(env, existing[0], existing[1], existing[2]):
            return False
        self._conn.execute(
            """INSERT INTO workspace_feature
                 (workspace_id, feature, enabled, hlc_physical, hlc_logical, actor_id)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(workspace_id, feature) DO UPDATE SET
                 enabled = excluded.enabled, hlc_physical = excluded.hlc_physical,
                 hlc_logical = excluded.hlc_logical, actor_id = excluded.actor_id""",
            (env.workspace_id, feature, 1 if enabled else 0, env.hlc.physical, env.hlc.logical, env.actor_id),
        )
        return True

    def _feature_enabled_now(self, workspace_id: str, feature: WorkspaceFeature) -> bool:
        """The current (winning) feature row's enabled bit — absent = enabled (F2)."""
        row = self._conn.execute(
            "SELECT enabled FROM workspace_feature WHERE workspace_id = ? AND feature = ?", (workspace_id, feature)
        ).fetchone()
        return row is None or int(row[0]) == 1

    def _derive_family_class_bits(self, workspace_id: str, feature: WorkspaceFeature) -> None:
        """Re-derive the archival bits for one family's full class set (base +
        extends-children) from the CURRENT feature rows. Each class's bit is
        the AND of its gating features' current rows (own feature when it is
        a family base, plus every managed ancestor's): re-enabling EVENTS
        does not un-archive a MEETINGS-off meeting, so a blind family-wide
        flip is wrong under the cascade; per-class re-derivation is
        idempotent, membership-preserving, and convergent (pure active-bit
        projection — no causality/timestamp writes: the toggles' HLCs live on
        the workspace_feature rows, and a wall-of-envelope timestamp here
        would diverge under reversed delivery of racing toggles)."""
        for name in family_class_names(feature):
            enabled = all(self._feature_enabled_now(workspace_id, f) for f in gating_features_for_class(name))
            class_id = system_class_uuid(name)
            self._conn.execute("UPDATE class SET active = ? WHERE id = ?", (1 if enabled else 0, class_id))
            # The class NODE flip is what pickers/search/class hubs filter on
            # (node.is_active = 1). Node-row HLC columns stay untouched — the
            # flip is a derived projection of the toggles, not a content write.
            self._conn.execute(
                "UPDATE nodes SET is_active = ? WHERE workspace_id = ? AND id = ? AND is_class = 1",
                (1 if enabled else 0, workspace_id, class_id),
            )

    def _ensure_task_family_rows(self, env: RelayEnvelope) -> None:
        """The ``tasks`` enable path: author the task
        class + the six property schemas + their bindings at the fixed seed
        ids. Purely additive (INSERT OR IGNORE everywhere) so a server-seeded
        family is never clobbered — first writer wins, convergent on the
        single global log. The rows are a deterministic function of the enable
        op, so wipe -> replay stays byte-identical. The rows are inserted
        ACTIVE; the caller normalizes the archival bit afterwards (the family
        re-derivation reads the CURRENT toggle rows on every path)."""
        class_id = system_class_uuid("task")
        title = json.dumps([{"type": "text", "text": "Task"}], ensure_ascii=False)
        ts = _envelope_ts(env)
        self._conn.execute(
            """INSERT OR IGNORE INTO nodes
                 (workspace_id, id, is_class, present_as_main, parent_id, name, class_ids, content, content_plain,
                  icon, color, is_active, created_at, updated_at, created_by, updated_by,
                  hlc_physical, hlc_logical, actor_id)
               VALUES (?, ?, 1, 0, NULL, NULL, '[]', ?, ?, ?, NULL, 1, ?, ?, ?, ?, ?, ?, ?)""",
            (
                env.workspace_id,
                class_id,
                title,
                "Task",
                SYSTEM_CLASS_ICONS_TASK,
                ts,
                ts,
                env.actor_id,
                env.actor_id,
                env.hlc.physical,
                env.hlc.logical,
                env.actor_id,
            ),
        )
        self._conn.execute(
            """INSERT OR IGNORE INTO class
                 (id, workspace_id, name, icon, color, description, active, created_at, updated_at)
               VALUES (?, ?, 'Task', ?, NULL, NULL, 1, ?, ?)""",
            (class_id, env.workspace_id, SYSTEM_CLASS_ICONS_TASK, ts, ts),
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO class_hierarchy (class_id, ancestor_id) VALUES (?, ?)", (class_id, class_id)
        )
        for entry in TASK_FAMILY_SEED:
            schema_id = task_property_uuid(entry.property)
            # The designed options ride verbatim (the seeds.ts manifest row
            # shape): icon/color keys land only when the designed option
            # carries them (zod optional parity).
            designed_options = []
            for option in entry.options:
                designed: dict[str, Any] = {"id": option.id, "label": option.label}
                if option.icon is not None:
                    designed["icon"] = option.icon
                if option.color is not None:
                    designed["color"] = option.color
                designed_options.append(designed)
            self._conn.execute(
                """INSERT OR IGNORE INTO property_schema
                     (id, workspace_id, name, type, multi, scope, options, target_class_filter,
                      date_precision, date_qualified, display, active, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 0, 'class', ?, NULL, NULL, NULL, ?, 1, ?, ?)""",
                (
                    schema_id,
                    env.workspace_id,
                    entry.name,
                    entry.type,
                    json.dumps(designed_options),
                    entry.display,
                    ts,
                    ts,
                ),
            )
            self._conn.execute(
                """INSERT OR IGNORE INTO class_property
                     (class_id, property_schema_id, sequence, required, default_value,
                      hlc_physical, hlc_logical, actor_id)
                   VALUES (?, ?, ?, NULL, NULL, 0, 0, NULL)""",
                (class_id, schema_id, entry.sequence),
            )

    def _apply_workspace_feature_set(self, env: RelayEnvelope) -> bool:
        payload = env.payload
        feature = payload["feature"]
        enabled = bool(payload["enabled"])
        wrote = self._lww_write_feature_row(env, feature, enabled)
        if wrote:
            self._derive_family_class_bits(env.workspace_id, feature)
        # The ensure rides every enable PAYLOAD (not only the LWW winner):
        # both delivery orders of a racing toggle pair must author the
        # identical row set — the family re-derivation below normalizes the
        # archival bits to the CURRENT row state on every path.
        if enabled and feature == "tasks":
            self._ensure_task_family_rows(env)
            self._derive_family_class_bits(env.workspace_id, feature)
        return wrote

    @_synchronized
    def is_feature_enabled(self, workspace_id: str, feature: str) -> bool:
        """One feature's current bit — absent row = enabled (F2)."""
        return self._feature_enabled_now(workspace_id, cast(WorkspaceFeature, feature))

    @_synchronized
    def feature_rows(self, workspace_id: str) -> list[tuple[str, bool, int, int, str | None]]:
        """The winning feature rows: (feature, enabled, hlc_physical, hlc_logical, actor)."""
        rows = self._conn.execute(
            "SELECT feature, enabled, hlc_physical, hlc_logical, actor_id FROM workspace_feature"
            " WHERE workspace_id = ? ORDER BY feature",
            (workspace_id,),
        ).fetchall()
        return [(str(r[0]), bool(r[1]), int(r[2]), int(r[3]), r[4]) for r in rows]

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

    def _visible_property_value_rows(self, node_id: str) -> list[dict[str, Any]]:
        """The PG5 visible set for a node (SCHEMA.md "Multi-value element
        identity"): live property_value rows minus

        - SLOT-tombstoned rows — the legacy positional path: a
          property_value_tombstone (node, schema, idx) with a winning (>=)
          full (hlc, actor) tuple suppresses the row at that idx (the
          pre-PG5 rule, unchanged); and
        - ELEMENT-tombstoned rows — a property_value_element_tombstone whose
          HLC is strictly newer than the row's (add-wins: equal HLC keeps
          the row).

        Every read consults this derivation so a removed element is invisible
        everywhere at once (the effective read is the GTK client's only
        property read surface)."""
        rows = self._conn.execute(
            "SELECT id, node_id, property_schema_id, value, idx, metadata,"
            " hlc_physical, hlc_logical, actor_id FROM property_value WHERE node_id = ?",
            (node_id,),
        ).fetchall()
        slot_tombstones = self._conn.execute(
            "SELECT node_id, property_schema_id, idx, hlc_physical, hlc_logical, actor_id"
            " FROM property_value_tombstone WHERE node_id = ?",
            (node_id,),
        ).fetchall()
        element_tombstones = {
            str(row[0]): (int(row[1]), int(row[2]))
            for row in self._conn.execute(
                "SELECT element_id, hlc_physical, hlc_logical FROM property_value_element_tombstone WHERE node_id = ?",
                (node_id,),
            )
        }
        visible: list[dict[str, Any]] = []
        for row in rows:
            row_map = {
                "id": str(row[0]),
                "node_id": str(row[1]),
                "property_schema_id": str(row[2]),
                "value": row[3],
                "idx": int(row[4]),
                "metadata": row[5],
                "hlc_physical": int(row[6]),
                "hlc_logical": int(row[7]),
                "actor_id": row[8],
            }
            element_tombstone = element_tombstones.get(row_map["id"])
            if element_tombstone is not None and (
                element_tombstone[0] > row_map["hlc_physical"]
                or (element_tombstone[0] == row_map["hlc_physical"] and element_tombstone[1] > row_map["hlc_logical"])
            ):
                continue
            suppressed = False
            for tomb in slot_tombstones:
                if str(tomb[0]) != row_map["node_id"] or str(tomb[1]) != row_map["property_schema_id"]:
                    continue
                if int(tomb[2]) != row_map["idx"]:
                    continue
                # Full-tuple comparison, the legacy slot rule: the tombstone
                # wins on an equal (hlc, actor) tuple.
                if (row_map["hlc_physical"], row_map["hlc_logical"], row_map["actor_id"] or "") <= (
                    int(tomb[3]),
                    int(tomb[4]),
                    tomb[5] or "",
                ):
                    suppressed = True
                    break
            if not suppressed:
                visible.append(row_map)
        return visible

    @_synchronized
    def get_effective_properties(self, node_id: str) -> list[EffectiveProperty]:
        """The effective-values read model (SCHEMA.md "Class properties")::

            effective(node, schema, idx) = authored property_value
                                           ?? winning binding's defaultValue

        Port of ``packages/store/src/effective.ts`` (PG5/PC4).
        Authored rows always win and survive class removal; derived defaults
        are computed HERE and never materialized (the applier writes no
        property_value rows for them). Authored rows come through the PG5
        visible-set derivation (:meth:`_visible_property_value_rows` — slot
        tombstones + element tombstones) and carry their stable element id.
        Binding conflicts across the node's classes resolve per the SCHEMA.md
        diamond rule (PG4): candidates are (class, ancestor) pairs
        discovered by a shortest-path walk (BFS) over ``class_extends`` — own
        binding (distance 0) first, then inherited bindings by shortest
        extends-path, ties by the class's OR-Set membership add HLC (earliest
        first), then class id. ``bound_by`` names the ANCESTOR whose binding
        row supplies the default + metadata (the winning class itself for an
        own binding); with no extends edges this reduces exactly to
        first-class-applied-wins over own bindings. Only ACTIVE binding rows
        are candidates (PC4: an inactive binding stops contributing defaults
        AND binding metadata — required/sequence — while the ROW survives and
        authored values read as unbound, ``bound_by`` None; the render
        contracts readonly/hideWhenEmpty/display are SCHEMA-sourced —
        they ride authored and derived rows alike, unbound values included).
        A stored default that no longer matches the schema type yields no
        default (PC2 read-side). A pure read over derived tables —
        deterministic on every replica, no writes, no clocks.
        """
        authored_rows = self._visible_property_value_rows(node_id)

        # The node's classes in assignment order: OR-Set add HLC ascending
        # (earliest first), ties by class id.
        classes = sorted(
            self._conn.execute(
                "SELECT class_id, hlc_physical, hlc_logical FROM class_member_set WHERE node_id = ? AND present = 1",
                (node_id,),
            ).fetchall(),
            key=lambda row: (int(row[1]), int(row[2]), str(row[0])),
        )

        # Winning binding per schema, extends-aware (SCHEMA.md diamond rule):
        # the winner minimizes (extends-distance, class-assignment HLC,
        # class id); bound_by names the ancestor whose row supplies the
        # binding + default. The class_hierarchy closure carries no distance,
        # so the shortest-path walk uses the edge table.
        extends_adjacency: dict[str, list[str]] = {}
        for edge_row in self._conn.execute("SELECT class_id, parent_class_id FROM class_extends"):
            extends_adjacency.setdefault(str(edge_row[0]), []).append(str(edge_row[1]))
        reach_cache: dict[str, list[tuple[str, int]]] = {}

        def reach_of(class_id: str) -> list[tuple[str, int]]:
            cached = reach_cache.get(class_id)
            if cached is not None:
                return cached
            visited = {class_id}
            queue: list[tuple[str, int]] = [(class_id, 0)]
            reach: list[tuple[str, int]] = []
            cursor = 0
            while cursor < len(queue):
                current, distance = queue[cursor]
                cursor += 1
                reach.append((current, distance))
                for parent_id in extends_adjacency.get(current, ()):
                    if parent_id in visited:
                        continue
                    visited.add(parent_id)
                    queue.append((parent_id, distance + 1))
            reach_cache[class_id] = reach
            return reach

        candidates: dict[str, list[tuple[int, int, int, str, str, tuple[Any, ...]]]] = {}
        for cls in classes:
            class_id = str(cls[0])
            physical, logical = int(cls[1]), int(cls[2])
            for ancestor_id, distance in reach_of(class_id):
                for binding in self._conn.execute(
                    "SELECT property_schema_id, sequence, required, default_value"
                    " FROM class_property WHERE class_id = ? AND active = 1",
                    (ancestor_id,),
                ).fetchall():
                    candidates.setdefault(str(binding[0]), []).append(
                        (distance, physical, logical, class_id, ancestor_id, binding)
                    )
        winner_by_schema: dict[str, tuple[str, tuple[Any, ...]]] = {}
        for schema_id, schema_candidates in candidates.items():
            # The TS comparator orders (distance, assignment HLC, class id);
            # the ancestor id is the final tiebreak so the order is total —
            # a same-class/same-distance tie across two ancestors is
            # pathological and the TS comparator is inconsistent there (see
            # the port notes). Well-defined cases match exactly.
            winning_candidate = min(schema_candidates, key=lambda c: (c[0], c[1], c[2], c[3], c[4]))
            winner_by_schema[schema_id] = (winning_candidate[4], winning_candidate[5])

        def flag(value: Any) -> bool | None:
            return None if value is None else bool(value)

        def display_of(value: Any) -> str | None:
            # Sanitize the stored position (NULL/'panel'/unknown →
            # None, the properties-section default — the effective.ts parity).
            return value if value in ("bullet", "inline") else None

        # Schema rows for everything referenced (authored rows survive schema
        # deletion: the row renders with schema=None). The schema
        # carries the PROPERTY-level render contracts — display (sanitized)
        # and the readonly/hide-when-empty flags.
        schema_ids = {str(row["property_schema_id"]) for row in authored_rows} | set(winner_by_schema)
        schemas: dict[str, EffectivePropertySchema] = {}
        if schema_ids:
            placeholders = ", ".join("?" for _ in schema_ids)
            rows = self._conn.execute(
                f"SELECT id, name, type, multi, display, readonly, hide_when_empty"
                f" FROM property_schema WHERE id IN ({placeholders})",
                tuple(sorted(schema_ids)),
            ).fetchall()
            for row in rows:
                schemas[str(row[0])] = EffectivePropertySchema(
                    id=str(row[0]),
                    name=str(row[1]),
                    type=str(row[2]),
                    multi=bool(row[3]),
                    display=display_of(row[4]),
                    readonly=flag(row[5]),
                    hide_when_empty=flag(row[6]),
                )

        def parse_json(raw: Any) -> Any:
            if not isinstance(raw, str):
                return raw
            try:
                return json.loads(raw)
            except ValueError:
                return raw

        rows_out: dict[str, EffectiveProperty] = {}
        shadowed_default_schemas: set[str] = set()
        for authored in authored_rows:
            schema_id, idx = authored["property_schema_id"], authored["idx"]
            winner = winner_by_schema.get(schema_id)
            schema = schemas.get(schema_id)
            if idx == 0:
                shadowed_default_schemas.add(schema_id)
            # PG5 merge key: rows at the same idx are distinct elements and
            # all surface.
            rows_out[f"{schema_id}:{idx}:{authored['id']}"] = EffectiveProperty(
                property_schema_id=schema_id,
                idx=idx,
                element_id=authored["id"],
                schema=schema,
                value=parse_json(authored["value"]),
                metadata=parse_json(authored["metadata"]) if authored["metadata"] is not None else None,
                source="authored",
                bound_by=winner[0] if winner else None,
                # Required is per-CLASS (the winning binding);
                # readonly/hideWhenEmpty/display are per-PROPERTY (the
                # schema — unbound values included).
                required=flag(winner[1][2]) if winner else None,
                readonly=schema.readonly if schema is not None else None,
                hide_when_empty=schema.hide_when_empty if schema is not None else None,
                sequence=int(winner[1][1]) if winner else None,
                display=schema.display if schema is not None else None,
            )

        for schema_id, (class_id, binding) in winner_by_schema.items():
            if binding[3] is None:
                continue  # bound without a default
            # PC2 read-side: a stored default that no longer matches the
            # schema type (written before validation, or after a
            # delete+recreate changed the type) yields no default rather
            # than a wrong-typed value.
            schema = schemas.get(schema_id)
            if schema is not None and not _is_valid_default_for_type(schema.type, parse_json(binding[3])):
                continue
            if schema_id in shadowed_default_schemas:
                continue  # authored value at idx 0 shadows the default
            rows_out[f"{schema_id}:0:default"] = EffectiveProperty(
                property_schema_id=schema_id,
                idx=0,
                element_id=f"default:{schema_id}:0",
                schema=schema,
                value=parse_json(binding[3]),
                metadata=None,
                source="default",
                bound_by=class_id,
                required=flag(binding[2]),
                readonly=schema.readonly if schema is not None else None,
                hide_when_empty=schema.hide_when_empty if schema is not None else None,
                sequence=int(binding[1]),
                display=schema.display if schema is not None else None,
            )

        # Deterministic presentation order: bound rows by binding sequence,
        # unbound authored rows last; schema name then (idx, element id) as
        # the tiebreak (PG5: concurrent adds may share an idx — the element
        # id orders them identically on every replica).
        def name_of(row: EffectiveProperty) -> str:
            return row.schema.name if row.schema is not None else row.property_schema_id

        return sorted(
            rows_out.values(),
            key=lambda row: (
                row.bound_by is None,
                row.sequence if row.sequence is not None else 2**53 - 1,
                name_of(row),
                row.idx,
                row.element_id,
            ),
        )

    # ---------------------------------------------------------------- snapshots

    @_synchronized
    def restore_snapshot(self, blob: bytes, *, workspace_id: str) -> bool:
        """Restore nodes from a downloaded snapshot blob (serialized derived DB).

        The blob is a serialized copy of the server's derived database,
        whose node table is ``node`` (singular) with the column names
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
                # The server snapshot has no plaintext cache column (the
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


_NODES_DDL = """
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

_AUX_DDL = """
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
    -- SCHEMA.md "Dates": finest granularity a date value may claim
    -- (year|month|day; NULL = day default) and, for node-typed schemas,
    -- whether values may carry date qualifiers (metadata startDate/endDate,
    -- PC6 — the applier normalizes qualifier strings to date-node
    -- refs through these).
    date_precision TEXT,
    date_qualified INTEGER,
    -- SCHEMA.md "Number formats": display-only formatting for number
    -- schemas (values stay exact; these shape render only).
    number_pad INTEGER,
    number_decimals INTEGER,
    number_rounding TEXT,
    -- The render contracts are PROPERTY-level (owner review
    -- 2026-10-05) — display (panel|bullet|inline; NULL = panel) and the
    -- readonly/hide-when-empty tri-state flags, wherever the property
    -- appears (class-bound or not). ('required' deliberately stays on the
    -- class binding — a property may be mandatory for one class, optional
    -- for another.)
    display TEXT,
    readonly INTEGER,
    hide_when_empty INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    updated_at TEXT
);

-- Property values — the LIVE visible rows only (the applier deletes a row
-- when its element's OR-Set remove wins). The row id IS the element id
-- (PG5): writer-minted UUIDv7 for element adds, the deterministic
-- composite 'node:schema:idx' for single-value slots and legacy positional
-- writes. The pre-PG5 UNIQUE(node_id, property_schema_id, idx) is GONE:
-- per-element identity means concurrent adds at the same idx are DISTINCT
-- elements and both stay visible — 'idx' is only a per-element order hint.
CREATE TABLE IF NOT EXISTS property_value (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    property_schema_id TEXT NOT NULL,
    value TEXT NOT NULL,
    idx INTEGER NOT NULL DEFAULT 0,
    metadata TEXT,
    hlc_physical INTEGER NOT NULL DEFAULT 0,
    hlc_logical INTEGER NOT NULL DEFAULT 0,
    actor_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_property_value_node ON property_value (node_id);

-- PG5 OR-Set element tombstones: one row per removed element, carrying the
-- winning remove's causality. An element is VISIBLE iff its live row exists
-- and no tombstone carries a strictly-newer (hlc) remove (add-wins: on
-- equal HLC the add wins regardless of actor). Removes upsert with the
-- strictly-greater full (hlc, actor) tuple, mirroring class.unassign.
CREATE TABLE IF NOT EXISTS property_value_element_tombstone (
    element_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    property_schema_id TEXT NOT NULL,
    hlc_physical INTEGER NOT NULL DEFAULT 0,
    hlc_logical INTEGER NOT NULL DEFAULT 0,
    actor_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_property_value_element_tomb_node
    ON property_value_element_tombstone (node_id);

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
    """Reshape the client cache to the current model (guarded, data-preserving).

    The ``nodes`` mirror is rebuilt at the current column names (``is_active``
    replacing ``archived``, plus ``class_ids``/``content_plain`` and the
    row-LWW columns), the legacy ``node_content_hlc`` watermark is dropped (its
    job is done by the row-LWW columns), and the auxiliary tables
    (child_order, OR-Sets, class registry/extends/closure, property
    registry/values/tombstones, assets, collections, trash) are created.
    The legacy ``node_type`` enumeration maps straight onto the Revision-11
    booleans here (page → presents as main; block/class → not) — the v7
    rebuild is a no-op for this path.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(nodes)")}
    if "is_active" in columns:
        return  # already at the current shape (guarded re-run)
    conn.execute("ALTER TABLE nodes RENAME TO nodes_legacy")
    conn.executescript(_NODES_DDL + _AUX_DDL)
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
-- Class -> property bindings (sequence, required, default), per SCHEMA.md
-- "Class properties — bindings, defaults, aggregation" (2026-09-27).
-- Registry rows authored by class.property.set/unset; LWW on the row by
-- (hlc, actor). Defaults are a DERIVED read model (get_effective_properties),
-- never materialized property_value rows.
-- (owner review 2026-10-05): the row carries ONLY the genuinely
-- per-class mechanics (sequence, required, default_value, active). The
-- render contracts (readonly/hide_when_empty, pre-v3.1.0, and display, the
-- v11 experiment) moved to property_schema — PROPERTY-level; the v12
-- rebuild dropped them here. 'active' (PC4): the soft-unbind flag —
-- an inactive row stops contributing to the effective read (no default, no
-- sequence) while the ROW survives; absent column means active, so existing
-- rows converge with zero migration.
CREATE TABLE IF NOT EXISTS class_property (
    class_id TEXT NOT NULL,
    property_schema_id TEXT NOT NULL,
    sequence INTEGER NOT NULL DEFAULT 0,
    required INTEGER,
    default_value TEXT,
    active INTEGER NOT NULL DEFAULT 1,
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

_WORKSPACE_FEATURE_DDL = """
-- Per-workspace feature toggles: the winning LWW row per
-- (workspace_id, feature); an ABSENT row means enabled (all features
-- default ON — the empty table is the pre-toggle state, so existing
-- workspaces need no migration). The applier derives the membership-
-- preserving archival of the feature's managed system classes from this row
-- (class registry active bit + the class node's is_active; class_member_set
-- rows are never touched).
CREATE TABLE IF NOT EXISTS workspace_feature (
    workspace_id TEXT NOT NULL,
    feature TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    hlc_physical INTEGER NOT NULL DEFAULT 0,
    hlc_logical INTEGER NOT NULL DEFAULT 0,
    actor_id TEXT,
    PRIMARY KEY (workspace_id, feature)
);
"""


def _migrate_v8(conn: sqlite3.Connection) -> None:
    """Per-workspace feature toggles (web schema v9→v10 parity).
    Purely additive — CREATE IF NOT EXISTS is a no-op for fresh databases
    that already ran the DDL; existing databases gain the empty table
    (empty = all features enabled)."""
    conn.executescript(_WORKSPACE_FEATURE_DDL)


def _migrate_v9(conn: sqlite3.Connection) -> None:
    """The property-wire batch (web schema v10→v11 parity): PG5
    element identity + PC4 binding active + the property_schema date
    columns PC6 reads. Every step is guarded so a database whose DDL
    already carries the new shape (fresh chain) is untouched, and a v7
    database upgrades in place."""
    # (1) PC4: class_property gains the soft-unbind flag — absent column
    #     means the pre-PC4 state, which IS active, so the backfill default
    #     is 1.
    LocalStore._add_column_if_missing(conn, "class_property", "active", "INTEGER NOT NULL DEFAULT 1")
    # (2) PC6: property_schema gains the SCHEMA.md "Dates" columns (the
    #     first GTK consumer — web has carried them since schema v4; the
    #     payload keys are stored verbatim from here on).
    LocalStore._add_column_if_missing(conn, "property_schema", "date_precision", "TEXT")
    LocalStore._add_column_if_missing(conn, "property_schema", "date_qualified", "INTEGER")
    # (3) PG5 element tombstone table (CREATE IF NOT EXISTS is the guard).
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS property_value_element_tombstone (
            element_id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            property_schema_id TEXT NOT NULL,
            hlc_physical INTEGER NOT NULL DEFAULT 0,
            hlc_logical INTEGER NOT NULL DEFAULT 0,
            actor_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_property_value_element_tomb_node
            ON property_value_element_tombstone (node_id);
        """
    )
    # (4) property_value: the UNIQUE(node_id, property_schema_id, idx)
    #     constraint is retired (PG5 — per-element identity allows
    #     concurrent adds at the same idx; idx is an order hint only).
    #     SQLite cannot drop a table constraint, so the table is rebuilt
    #     (the v7 node-rebuild precedent): same columns, no UNIQUE, indexes
    #     recreated. Rows copy verbatim — the row id remains the (now
    #     element) id.
    pv_indexes = conn.execute("PRAGMA index_list(property_value)").fetchall()
    if any(str(index[3]) == "u" for index in pv_indexes):
        conn.executescript(
            """
            PRAGMA foreign_keys = OFF;
            CREATE TABLE property_value_v9 (
                id TEXT PRIMARY KEY,
                node_id TEXT NOT NULL,
                property_schema_id TEXT NOT NULL,
                value TEXT NOT NULL,
                idx INTEGER NOT NULL DEFAULT 0,
                metadata TEXT,
                hlc_physical INTEGER NOT NULL DEFAULT 0,
                hlc_logical INTEGER NOT NULL DEFAULT 0,
                actor_id TEXT
            );
            INSERT INTO property_value_v9 (
                id, node_id, property_schema_id, value, idx, metadata,
                hlc_physical, hlc_logical, actor_id
            )
            SELECT id, node_id, property_schema_id, value, idx, metadata,
                   hlc_physical, hlc_logical, actor_id
            FROM property_value;
            DROP TABLE property_value;
            ALTER TABLE property_value_v9 RENAME TO property_value;
            CREATE INDEX IF NOT EXISTS idx_property_value_node ON property_value (node_id);
            PRAGMA foreign_keys = ON;
            """
        )



def _migrate_v10(conn: sqlite3.Connection) -> None:
    """v10 — number display formatting (SCHEMA.md "Number formats")
    lockstep): additive property_schema columns, NULL = unformatted. The
    column guard keeps the ALTER idempotent for databases that already carry
    them (a fresh v10 create)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(property_schema)")}
    if "number_pad" not in cols:
        conn.executescript(
            """
            ALTER TABLE property_schema ADD COLUMN number_pad INTEGER;
            ALTER TABLE property_schema ADD COLUMN number_decimals INTEGER;
            ALTER TABLE property_schema ADD COLUMN number_rounding TEXT;
            """
        )


def _migrate_v11(conn: sqlite3.Connection) -> None:
    """v11 — the binding gains the value-display
    position. Additive guarded column (the ``_add_column_if_missing`` parity
    with PC4's active): the stored NULL default means "panel", so existing
    rows converge with zero backfill and a fresh v11 create is untouched.

    Kept in the chain for databases still below v11: v12 rebuilds the table
    without the column the same open."""
    LocalStore._add_column_if_missing(conn, "class_property", "display", "TEXT")


def _migrate_v12(conn: sqlite3.Connection) -> None:
    """v12 — owner review 2026-10-05: the render contracts move from
    the binding to the property. Two guarded steps, each idempotent for a
    fresh v12 create:

    (1) property_schema gains display + readonly/hide_when_empty
        (NULL = panel / unset). ``required`` deliberately stays on the
        binding (the owner's per-class exception).
    (2) class_property is REBUILT without the retired binding columns
        (readonly/hide_when_empty from the original shape, display from the
        v11 experiment — the v9 property_value rebuild precedent:
        same surviving columns, rows copy verbatim, indexes recreated).
        ``required`` survives on the row.
    """
    LocalStore._add_column_if_missing(conn, "property_schema", "display", "TEXT")
    LocalStore._add_column_if_missing(conn, "property_schema", "readonly", "INTEGER")
    LocalStore._add_column_if_missing(conn, "property_schema", "hide_when_empty", "INTEGER")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(class_property)")}
    if {"readonly", "hide_when_empty", "display"} & columns:
        conn.executescript(
            """
            PRAGMA foreign_keys = OFF;
            CREATE TABLE class_property_v12 (
                class_id TEXT NOT NULL,
                property_schema_id TEXT NOT NULL,
                sequence INTEGER NOT NULL DEFAULT 0,
                required INTEGER,
                default_value TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                hlc_physical INTEGER NOT NULL DEFAULT 0,
                hlc_logical INTEGER NOT NULL DEFAULT 0,
                actor_id TEXT,
                PRIMARY KEY (class_id, property_schema_id)
            );
            INSERT INTO class_property_v12 (
                class_id, property_schema_id, sequence, required, default_value, active,
                hlc_physical, hlc_logical, actor_id
            )
            SELECT class_id, property_schema_id, sequence, required, default_value, active,
                   hlc_physical, hlc_logical, actor_id
            FROM class_property;
            DROP TABLE class_property;
            ALTER TABLE class_property_v12 RENAME TO class_property;
            CREATE INDEX IF NOT EXISTS idx_class_property_class ON class_property (class_id);
            PRAGMA foreign_keys = ON;
            """
        )


#: Canonical auxiliary DDL applied on every upgrade before the versioned
#: steps (the web schema.ts ``migrate`` parity): all CREATE IF NOT EXISTS, so
#: a complete database is untouched and a partial one converges to the full
#: table set. ``_NODES_DDL`` is deliberately excluded — the node table's
#: shape changes ride the v3/v7 rebuilds, never a blind CREATE.
_CANONICAL_AUX_DDL = _AUX_DDL + _CLASS_PROPERTY_DDL + _TAG_MEMBER_SET_DDL + _WORKSPACE_FEATURE_DDL


#: Ordered migration chain; each entry bumps ``PRAGMA user_version`` to its target.
_MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = (
    (1, _migrate_v1),
    (2, _migrate_v2),
    (3, _migrate_v3),
    (4, _migrate_v4),
    (5, _migrate_v5),
    (6, _migrate_v6),
    (7, _migrate_v7),
    (8, _migrate_v8),
    (9, _migrate_v9),
    (10, _migrate_v10),
    (11, _migrate_v11),
    (12, _migrate_v12),
)
