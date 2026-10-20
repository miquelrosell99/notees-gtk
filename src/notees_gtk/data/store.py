"""Local SQLite store for the Notees GTK client (relay protocol v3).

Owns the offline-capable client cache: the relay outbox (pending/quarantined
envelopes), the op-id dedupe log, the sync watermark (seq cursor +
``restore_epoch``), and the mirrored node table with the applier semantics
ported from ``packages/store/src/appliers.ts``: row-level last-write-wins by
``(hlc_physical, hlc_logical, actor_id)``, OR-Set class/tag/collection
membership, user-defined class order (``class.reorder``, LWW-by-arrival),
title-is-content (a node's title IS its content — no ``name`` writes; nodes
with document chrome carry text-only content), the Revision-11 render-state
model (``is_class`` identity + ``is_embedded`` embedding flag; classes are
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
write path), the M27 wire node fields (``object.update`` maps
``coverAssetId``/``bannerAssetId``/``aliasedNodeId`` presence-writes /
present-null-clears onto the derived node columns — the icon/color
convention; the alias target gets the M12 write-time cycle check and
:meth:`resolve_alias` is the cycle-safe chain walker) plus the 2026-10-09
page-subtitle field (``description`` — plain text, max 512 chars, the same
nullish convention, web schema v18 parity), the 2026-10-07
title-flatten ruling (class rows stay text-only; every other node keeps the
rich token stream on content writes — create-as-main and promotion remain
the lossy boundaries), the class conversion capability (``class.create``
on an existing node DECLARES it a class: the identity bit flips, a parented
node is cut to a root, the render bit clears, absent registry fields
preserve), and the M38 ``asset`` property type (node-typed values validated
as asset-node references — the filter is implicit in the type).

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
from notees_gtk.core.protocol.dates import (
    date_chain_class_id,
    date_chain_parent_id,
    day_node_id,
    is_valid_time_of_day,
    parse_date_node_id,
)
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
#: replaced by ``is_class`` + ``present_as_main`` (Revision 12 renames + inverts it to ``is_embedded``); v8 adds the per-workspace
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
#: v12: the render contracts are
#: PROPERTY-level — property_schema gains display / readonly /
#: hide_when_empty (NULL = panel / unset), and class_property is REBUILT
#: without the retired binding columns (readonly/hide_when_empty pre-v3.1.0,
#: display the v11 experiment); ``required`` survives on the row —
#: the owner's per-class exception.
#: v13 (the M27/M12 lockstep — web schema v15→v16
#: parity): the wire node fields land as three nullable node columns
#: (``cover_asset_id`` / ``banner_asset_id`` / ``aliased_node_id``) mapped
#: by object.update (presence writes, present-null clears — the color
#: convention); object.update of ``aliasedNodeId`` runs the write-time
#: alias-cycle check (the extends-DAG precedent) and the store gains the
#: ``resolve_alias`` chain walker.
#: v14 (web schema v17→v18 parity): the page-subtitle
#: wire node field — ``description``, the Capacities header precedent,
#: plain text max 512 chars — lands as a nullable node column mapped by
#: object.update (presence writes, present-null clears — the color
#: convention); object.create carries none.
#: v15 (web schema v21 parity): the Revision-12
#: embedding model — ``present_as_main`` renamed to ``is_embedded`` WITH
#: POLARITY INVERSION (the nodes table rebuild).
#: v16 (web schema v22→v24 parity):
#: the per-block list style lands as a nullable ``list_style`` node column
#: (the listStyle wire node field; NULL = bullet); the property_schema
#: table REBUILDS (the v12 precedent) — the display-only ``scope`` column
#: retires (the locality authority is the new ``scope_class_ids`` JSON
#: column, NULL = global) and the schema gains ``icon`` +
#: ``validation_rules``.
#: v17 (the property-definition surfaces batch —
#: web schema v24→v25 parity): `required` retires outright and
#: ``default_value`` MOVES from class_property to property_schema (one
#: global default per property, the Logseq shape). The binding table
#: REBUILDS without both columns (SQLite cannot drop a column on old
#: builds); the schema gains ``default_value`` TEXT additively (NULL = no
#: default). The moved values arrive from the live log rewrite (the
#: monorepo's migrate-drop-binding-required.mts, Path B — appended
#: propertySchema.update compensation per affected schema) + the epoch
#: bump, so the backfill is deliberately NULL.
SCHEMA_VERSION = 17

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
#: Revision 12: ``is_class``/``is_embedded`` (the Revision-11 ``present_as_main``
#: retired and inverted) replace the retired node_type.
_SNAPSHOT_VERBATIM_COLUMNS: dict[str, str] = {
    "id": "id",
    "workspace_id": "workspace_id",
    "is_class": "is_class",
    "is_embedded": "is_embedded",
    "parent_id": "parent_id",
    "class_ids": "class_ids",
    "class_order": "class_order",
    "tag_ids": "tag_ids",
    "name": "name",
    "icon": "icon",
    "color": "color",
    # Wire node fields (web schema v16): ride along when the snapshot was
    # taken from a post-M27 store; older snapshots lack the columns and the
    # mapping skips them (the cache default NULL wins). ``description``
    # (web schema v18, the page subtitle) rides the same way.
    "cover_asset_id": "cover_asset_id",
    "banner_asset_id": "banner_asset_id",
    "aliased_node_id": "aliased_node_id",
    "description": "description",
    # Per-block list style (web schema v23): rides along when the snapshot
    # was taken from a post-list-style store; older snapshots lack the
    # column and the mapping skips them (the cache default NULL wins).
    "list_style": "list_style",
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
        is_embedded: Embedding flag read only for parented non-class nodes:
            true = the node's content belongs to the parent's body (the
            Block role, the DEFAULT for parented nodes); false = a standalone
            structural element under the parent (the Page role — the
            main-children zone + document chrome when zoomed). Unread for
            parentless nodes (document chrome by the second cascade branch)
            and classes (ClassView by the first branch).
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
        cover_asset_id: The asset node behind the page cover (the M27 wire
            node field), or ``None`` — set/cleared by object.update only.
        banner_asset_id: The asset node behind the page banner, or ``None``.
        aliased_node_id: The main page this node is an alias of (many-to-one
            FROM the alias), or ``None``.
        description: The page subtitle in the core page chrome (the wire node
            field, web schema v18 parity) — plain text, max 512 chars, or
            ``None`` (never written / cleared); set/cleared by object.update
            only.
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
    is_embedded: bool
    name: str | None
    class_ids: tuple[str, ...]
    tag_ids: tuple[str, ...]
    icon: str | None
    color: str | None
    cover_asset_id: str | None
    banner_asset_id: str | None
    aliased_node_id: str | None
    description: str | None
    # Per-block list style (the listStyle wire node field): "numbered" |
    # "alpha" | "roman", or ``None`` = the bullet default.
    list_style: str | None
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
    carrier, class-bound or not. The R3 locality authority rides too:
    ``scope_class_ids`` (None = global — eligibility walks the node's class
    closure against this list), the ``icon`` (None = none), and the R2
    ``validation_rules`` (None = none)."""

    id: str
    name: str
    type: str
    multi: bool
    display: str | None = None
    readonly: bool | None = None
    hide_when_empty: bool | None = None
    icon: str | None = None
    scope_class_ids: tuple[str, ...] | None = None
    validation_rules: dict[str, Any] | None = None


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
   the render contracts ``readonly``/
    ``hide_when_empty``/``display`` are PROPERTY-level — SCHEMA-sourced and
    identical on authored and derived rows, unbound authored values
    included. ``required`` retired outright from
    the binding — there is no required field on effective rows anymore.
    """

    property_schema_id: str
    idx: int
    element_id: str
    schema: EffectivePropertySchema | None
    value: Any
    metadata: dict[str, Any] | None
    source: str  # "authored" | "default"
    bound_by: str | None
    readonly: bool | None
    hide_when_empty: bool | None
    sequence: int | None
    display: str | None = None
    # R3 eligibility: False when the schema's
    # scopeClassIds excludes the node (its class closure misses the list) —
    # the row survives (authored values always survive; narrowing a scope is
    # never retroactive) but callers know it is out of scope; derived
    # defaults never materialize for ineligible nodes (R4), so only authored
    # rows can carry eligible=False. Schema-less rows (schema deleted) read
    # True.
    eligible: bool = True


def _content_class_link_ids(tokens: Any) -> list[str]:
    """Content-asserted membership (the no-divergence law): every ``class_link`` token in a stored content stream IS a
    classing of its node — the object appliers apply the OR-set add, so any
    writer converges. Deep walk (tokens nest in quote/heading children)."""
    found: list[str] = []
    seen: set[str] = set()

    def walk(ts: Any) -> None:
        if not isinstance(ts, list):
            return
        for token in ts:
            if not isinstance(token, dict):
                continue
            if token.get("type") == "class_link":
                class_id = token.get("classId")
                if isinstance(class_id, str) and class_id not in seen:
                    seen.add(class_id)
                    found.append(class_id)
            children = token.get("children")
            if isinstance(children, list):
                walk(children)

    walk(tokens)
    return found


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
#: refs on dateQualified schemas (SCHEMA.md "Datetime").
_QUALIFIER_KEYS = ("startDate", "endDate")

#: M12: the alias-chain walker's depth cap (chains are user-built and tiny;
#: the cap bounds pathological data — on exhaustion the furthest node is the
#: best-effort terminal).
_ALIAS_CHAIN_DEPTH_CAP = 32

#: The object.update fields whose explicit null is a real write (the CLEAR)
#: rather than a missing value: the nullish schema fields (the color
#: precedent + the M27 wire node fields + ``description``, the page
#: subtitle). The outbox writable-field guard keys off this set — the
#: server's refine is key-PRESENCE-based, so a nullish-only update is a
#: legitimate envelope, while a null on any other field (icon/contentAst
#: are optional, not nullish) would 422.
_NULLISH_OBJECT_UPDATE_FIELDS = frozenset(
    {"color", "coverAssetId", "bannerAssetId", "aliasedNodeId", "description", "listStyle"}
)


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
    """PC6 normalize-on-write (SCHEMA.md "Datetime"): for a dateQualified
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
# (a legacy bare uuid normalizes to the reference shape); datetime = the
# unified date value union (SCHEMA.md "Datetime") — a POINT
# {"nodeId": …, "time"?: "HH:MM"} (a bare-uuid string, the legacy date
# encoding, normalizes to the reference shape) OR a RANGE
# {"start": slot|null, "end": slot|null} — either side open (a legacy
# date_range value rides untouched); each present slot is
# {"nodeId": …, "time"?: "HH:MM"} (legacy bare-uuid sides normalized). A
# value carrying BOTH nodeId and start/end keys is rejected outright; a
# "time" must match HH:MM (24h, minute precision) and ride a DAY-precision
# date-node ref. object/asset = a node reference. Scalar typing: number =
# a finite number — a NUMERIC STRING is the migrated legacy encoding (live
# data carries epoch-millis strings) and normalizes to a number; boolean/url/
# email/select = their scalar; multi_select = an array of strings. image
# stays UNCHECKED by design (PG14 owns the shape — live data carries migrated
# asset payloads, so any check would break replay). `null` means "no value"
# and bypasses shape validation.

#: SCHEMA.md "Datetime": finest granularity a date value may claim.
_DATE_PRECISION_RANK = {"year": 1, "month": 2, "day": 3}

#: The datetime-slot sentinel: a range side ABSENT from the payload (distinct
#: from an explicit JSON null — an open side). property-values.ts `undefined`.
_MISSING: Any = object()


def _jsonish(value: Any) -> str:
    """The JSON rendering an error message quotes (the TS JSON.stringify)."""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _shape_error_detail(type_: str) -> str:
    if type_ == "text":
        return 'a string or a node reference { "nodeId": … }'
    if type_ == "datetime":
        return (
            'a datetime point { "nodeId": …, "time"?: "HH:MM" } or range '
            '{ "start": slot|null, "end": slot|null } — "time" requires a day-precision date anchor'
        )
    return 'a node reference { "nodeId": … }'


def _datetime_slot(value: Any) -> dict[str, Any] | None | Any:
    """One anchored datetime endpoint (property-values.ts `slot`): the chain
    node plus an optional wall-clock time. Returns the normalized slot dict
    (``{"nodeId": …}`` / ``{"nodeId": …, "time": …}``), ``None`` for an open
    side, or :data:`_MISSING` when the side is absent or malformed. A legacy
    bare-uuid string side normalizes to ``{"nodeId"}``. ``time`` must be a
    well-formed ``HH:MM`` and ride a DAY-precision date-node anchor — a
    year/month ref (or a non-date id) has no wall-clock time."""
    if value is _MISSING:
        return _MISSING
    if value is None:
        return None
    ref = _node_ref_of_value(value)
    if ref is None:
        return _MISSING
    out: dict[str, Any] = {"nodeId": ref}
    if isinstance(value, Mapping) and "time" in value:
        time = value["time"]
        if not is_valid_time_of_day(time):
            return _MISSING
        parsed = parse_date_node_id(ref)
        if parsed is None or parsed["precision"] != "day":
            return _MISSING
        out["time"] = time
    return out


def _assert_value_shape_for_type(type_: str, value: Any, op_type: str) -> Any:
    """The PB2 one-shape-per-type gate (SCHEMA.md "Node-backed text
    properties" / "Datetime"), invoked by the PG6 validator. Returns the
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
    elif type_ in ("object", "asset"):
        ref = _node_ref_of_value(value)
        if ref is not None:
            return {"nodeId": ref}
    elif type_ == "datetime":
        # The unified date union (SCHEMA.md "Datetime"): a point
        # { "nodeId": …, "time"?: "HH:MM" } or a range { "start", "end" } of
        # slots ({ "nodeId": …, "time"?: "HH:MM" }), either side open. Legacy
        # `date` values (bare {nodeId} points, bare-uuid strings) and legacy
        # `date_range` values (bare-ref ranges) are legal members — no stored
        # value ever needs rewriting.
        if isinstance(value, str):
            # Legacy bare-uuid point (the archived date encoding) normalizes.
            if _UUID_LIKE_RE.match(value):
                return {"nodeId": value}
        elif isinstance(value, Mapping):
            point_key = "nodeId" in value
            range_key = "start" in value or "end" in value
            # A value carrying BOTH shapes is rejected outright (fail loud).
            if not (point_key and range_key):
                if point_key:
                    out = _datetime_slot(value)
                    if out is not _MISSING and out is not None:
                        return out
                elif range_key:
                    start = _datetime_slot(value.get("start", _MISSING))
                    end = _datetime_slot(value.get("end", _MISSING))
                    if start is not _MISSING and end is not _MISSING:
                        return {"start": start, "end": end}
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
        # image, asset (shape-normalized above), and any future type:
        # unchecked (see the section header).
        return value
    raise PropertyValueShapeError(
        f"{op_type}: value for {type_} schema must be {_scalar_error_detail(type_)} — got {_jsonish(value)}",
        op_type,
    )


def _is_valid_default_for_type(type_: str, value: Any) -> bool:
    """PC2: a property SCHEMA defaultValue (moved
    from the class binding to the schema) must be typed per the schema type.
    Node-typed schemas (datetime/object/asset) accept only JSON null —
    a default that links a node is meaningless. Returns False instead of
    throwing so the read model can drop silently; the write path
    (propertySchema.create/update) fails loud. Mirrors isValidDefaultForType."""
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
    # datetime/object/asset accept only JSON null (handled above);
    # unknown future types ride unchecked.
    return type_ not in ("datetime", "object", "asset")


def _json_array_of_row(raw: Any) -> list[str] | None:
    """A stored JSON array column as a string array (NULL/malformed → None —
    a corrupt column reads as "no list", never a crash; the effective.ts
    ``parseJsonArray`` parity)."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list):
        return None
    return [str(item) for item in parsed if isinstance(item, str)]


def _validation_rules_of_row(raw: Any) -> dict[str, Any] | None:
    """The stored validation_rules column parsed to the rule object (None on
    NULL or unparseable — a corrupt column reads as "no rules", never a
    crash; the property-values.ts ``validationRulesOfRow`` parity)."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None or not isinstance(parsed, dict):
        return None
    rules: dict[str, Any] = {}
    minimum = parsed.get("min")
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and math.isfinite(minimum):
        rules["min"] = minimum
    maximum = parsed.get("max")
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and math.isfinite(maximum):
        rules["max"] = maximum
    pattern = parsed.get("pattern")
    if isinstance(pattern, str):
        rules["pattern"] = pattern
    return rules


def _assert_validation_rules_applicable(schema_type: str, rules: dict[str, Any], op_type: str) -> None:
    """R2 applicability: the update payload is a
    patch carrying no type, so validationRules applicability is enforced
    against the schema's STORED type — min/max only on ``number`` schemas,
    pattern only on ``text``/``url``/``email``. (min <= max is a parse-time
    rule.) Fail loud — the rules would be provably inert or wrong-typed."""
    if (rules.get("min") is not None or rules.get("max") is not None) and schema_type != "number":
        raise PropertyValueShapeError(
            f"{op_type}: validationRules min/max apply to number schemas only"
            f" — schema type is {schema_type}",
            op_type,
        )
    if rules.get("pattern") is not None and schema_type not in ("text", "url", "email"):
        raise PropertyValueShapeError(
            f"{op_type}: validationRules pattern applies to text/url/email schemas only"
            f" — schema type is {schema_type}",
            op_type,
        )


def _assert_validation_pattern_compilable(rules: dict[str, Any], op_type: str) -> None:
    """A pattern that does not compile would throw at value-check time (an
    applier crash is never acceptable) — the rule write itself fails loud
    instead. The pattern is stored as authored (no anchoring rewrite: the
    value check adds the full-string anchors around the source)."""
    pattern = rules.get("pattern")
    if pattern is None:
        return
    try:
        re.compile(pattern)
    except re.error:
        raise PropertyValueShapeError(
            f"{op_type}: validationRules pattern is not a compilable regex — got {_jsonish(pattern)}",
            op_type,
        ) from None


def _assert_validation_rules_for_value(schema: dict[str, Any], typed: Any, op_type: str) -> None:
    """The stored validationRules applied to a NORMALIZED value (the R2 value
    enforcement — SCHEMA.md "Class properties"):
    min/max bound a number schema's value (the legacy numeric string already
    normalized to a finite number upstream), pattern full-string-tests a
    text/url/email schema's SCALAR strings (a text carrier ref {nodeId} is
    not a string and rides unchecked — the shape family owns refs). A multi
    batch is a property.set per element (PG5), so every element is validated
    at its own write and one bad element rejects its op while the surviving
    elements stay. Types outside applicability never reach here — rule
    writes on them are rejected at the schema-update path. A stored pattern
    that does not compile is corrupt config: fail loud
    (PropertyValueShapeError), never an applier crash."""
    rules = _validation_rules_of_row(schema.get("validation_rules"))
    if rules is None:
        return
    schema_type = str(schema.get("type"))
    if schema_type == "number":
        if not isinstance(typed, (int, float)) or isinstance(typed, bool):
            return  # the shape check owns non-numbers.
        minimum = rules.get("min")
        if minimum is not None and typed < minimum:
            raise PropertyValueShapeError(
                f"{op_type}: value {_jsonish(typed)} is below the schema's validationRules min {minimum}",
                op_type,
            )
        maximum = rules.get("max")
        if maximum is not None and typed > maximum:
            raise PropertyValueShapeError(
                f"{op_type}: value {_jsonish(typed)} is above the schema's validationRules max {maximum}",
                op_type,
            )
        return
    if schema_type in ("text", "url", "email"):
        pattern = rules.get("pattern")
        if pattern is None or not isinstance(typed, str):
            return
        try:
            compiled = re.compile(f"^(?:{pattern})$")
        except re.error:
            raise PropertyValueShapeError(
                f"{op_type}: schema {schema.get('id')} carries a validationRules pattern that is not"
                " a compilable regex — stored config is corrupt (rewrite the schema's rules"
                " via propertySchema.update)",
                op_type,
            ) from None
        if compiled.match(typed) is None:
            raise PropertyValueShapeError(
                f"{op_type}: value {_jsonish(typed)} does not match the schema's validationRules pattern"
                f" {_jsonish(pattern)} (full-string match)",
                op_type,
            )


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
            "trash.empty": self._apply_trash_empty,
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
        ``objectId``, or only nulls on non-nullish fields) would be rejected
        with 422 and quarantined, so it is skipped here and logged. A null on
        a NULLISH field (``color``, the M27 wire node fields, and
        ``description`` — an explicit clear the server's presence-based
        refine accepts) IS writable.
        Payloads of every known op type are also validated against the strict
        wire schemas (``core.protocol.payloads``): a retired/renamed key
        (e.g. the title-is-content ``name`` field) or a malformed value fails
        loud here instead of riding the outbox to a certain 422.
        """
        if env.op_type == "object.update":
            writable = [
                key
                for key, value in env.payload.items()
                if key != "objectId" and (value is not None or key in _NULLISH_OBJECT_UPDATE_FIELDS)
            ]
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
        # Column order is the contract: is_class/is_embedded sit at 3/4
        # (the LWW appliers read them positionally) and the row-LWW triple at
        # 12/13/14; the wire node fields ride at 15/16/17, the page subtitle
        # at 18 and the per-block list style at 19 (appended, so every
        # existing position is stable).
        row: tuple[Any, ...] | None = self._conn.execute(
            "SELECT id, workspace_id, parent_id, is_class, is_embedded, name, class_ids, icon, color,"
            " is_active, content, content_plain, hlc_physical, hlc_logical, actor_id,"
            " cover_asset_id, banner_asset_id, aliased_node_id, description, list_style"
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

    def _is_page_row(self, row: tuple[Any, ...]) -> bool:
        """True when the node row renders with document chrome under the
        recursive parent-page rule (carried into
        Revision 12): a class never does (ClassView), a parentless non-class
        row always does (the flag is unread), and a parented row does exactly
        when ``is_embedded = 0`` (the standalone claim) AND its parent also
        renders with document chrome. Cycles are impossible (subtree moves
        are guarded); a missing parent row is a defensive "not a page"."""
        if bool(row[3]):
            return False
        if row[2] is None:
            return True
        if int(row[4]) != 0:
            return False
        parent = self._node_row(str(row[1]), str(row[2]))
        if parent is None:
            return False
        return self._is_page_row(parent)

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

    def _assert_alias_acyclic(self, workspace_id: str, node_id: str, target_id: str, op_type: str) -> None:
        """M12 write-time alias-cycle validation (the extends-DAG precedent):
        ``object.update {aliasedNodeId: T}`` on node N must not close an alias
        cycle. The would-be chain is N → T → T's target → … — walk it from T;
        a revisit of any visited node (including N itself — the 1-edge
        self-alias) means the write would create a cycle, so it fails loud
        and is NEVER applied. Clearing (null) cannot create a cycle and
        skips the check. Chains without a cycle terminate (finite graph);
        the visited set makes the walk exact."""
        visited = {node_id}
        current = target_id
        while True:
            if current in visited:
                raise CycleError(
                    f"{op_type}: aliasing {node_id} → {target_id} would close an alias cycle at {current}",
                    op_type,
                )
            visited.add(current)
            row = self._conn.execute(
                "SELECT aliased_node_id FROM nodes WHERE workspace_id = ? AND id = ?", (workspace_id, current)
            ).fetchone()
            if row is None or row[0] is None:
                return
            current = str(row[0])

    # ------------------------------------------------------------------ object.*

    def _apply_object_create(self, env: RelayEnvelope) -> bool:
        op_type = "object.create"
        payload = env.payload
        object_id = str(payload["objectId"])
        parent_id = payload.get("parentId")
        parent_id = str(parent_id) if parent_id is not None else None
        class_ids = [str(class_id) for class_id in (payload.get("classIds") or [])]
        tag_ids = [str(tag_id) for tag_id in (payload.get("tagIds") or [])]
        is_embedded_pre = payload.get("isEmbedded")
        is_embedded_pre = (0 if parent_id is None else 1) if is_embedded_pre is None else int(bool(is_embedded_pre))
        # Date-chain class lock (SCHEMA.md "Datetime"): a date node's class
        # set is FIXED to its precision's system class. The re-issued
        # object.create is the OR-Set add carrier (class.assign rides it,
        # also on an existing id), so the guard runs BEFORE the membership
        # seeding below and the re-create early return.
        # Content-asserted membership: an inline block keeps the rich stream,
        # so its class_link tokens class the node; a document-chrome node's
        # content flattens below (text-only — nothing to assert).
        link_class_ids = (
            []
            if is_embedded_pre == 0
            else _content_class_link_ids(payload.get("contentAst"))
        )
        locked_date_class = date_chain_class_id(object_id)
        if locked_date_class is not None:
            for class_id in [*class_ids, *link_class_ids]:
                if class_id != locked_date_class:
                    raise MoveGuardError(
                        f"{op_type}: node {object_id} is a date-chain node;"
                        f" it can only carry the {locked_date_class} system class",
                        op_type,
                    )
        # Embedding flag (Revision 12): the payload may carry isEmbedded; the
        # applier defaults it by context — a parentless node is standalone
        # (document chrome by the second cascade branch; the flag is stored
        # but unread), a parented one starts embedded (its content belongs to
        # the parent's body — block chrome; the promotion boundary is the
        # 1→0 toggle). object.create always makes is_class = 0 nodes; class
        # declaration remains the class.create op.
        is_embedded = payload.get("isEmbedded")
        is_embedded = (0 if parent_id is None else 1) if is_embedded is None else int(bool(is_embedded))
        after_id = payload.get("afterId")
        after_id = str(after_id) if after_id is not None else None
        before_id = payload.get("beforeId")
        before_id = str(before_id) if before_id is not None else None
        ts = _envelope_ts(env)

        # Content flatten invariant (SCHEMA.md): document-chrome nodes
        # (is_class or is_embedded = 0) carry text-only content; an embedded
        # block keeps the full rich token stream.
        content_ast = payload.get("contentAst")
        tokens: Any = stringify_content_ast(content_ast) if is_embedded == 0 else (content_ast or [])
        content = json.dumps(tokens, ensure_ascii=False)
        content_plain = plaintext_excerpt(tokens)

        # First create wins for duplicate node ids (INSERT OR IGNORE): a
        # re-create must not touch the TREE — the earlier half-apply added a
        # second child_order row under the new parent while node.parent_id
        # stayed stale, rendering the node under TWO parents. The exceptions
        # are the classIds/tagIds OR-Set seeds below, the convergence carriers
        # for concurrent creates (they cannot move the node).
        for class_id in [*class_ids, *link_class_ids]:
            self._class_member_upsert(object_id, class_id, env)
        for tag_id in tag_ids:
            self._tag_member_upsert(object_id, tag_id, env)
        if self._node_row(env.workspace_id, object_id) is not None:
            if class_ids or link_class_ids:
                self._recompute_class_ids(object_id)
            if tag_ids:
                self._recompute_tag_ids(object_id)
            return False

        if parent_id is not None:
            # Classes are containers (spec I4): a class parent is legal for
            # non-class children — which is all object.create can make.
            parent = self._check_parent_allowed(env.workspace_id, parent_id, op_type)
            # Parent-page condition (Revision 12):
            # the standalone role is only effective under a page parent —
            # claiming it under a class or an embedded block establishes an
            # illegal parented state and fails loud. (A parented default is
            # embedded = 1, so is_embedded == 0 here is always an explicit
            # claim.)
            if is_embedded == 0 and not self._is_page_row(parent):
                raise MoveGuardError(
                    f"{op_type}: is_embedded = 0 (standalone) requires the parent to render"
                    " with document chrome (the parent is a class or an embedded block)",
                    op_type,
                )
        # Date-chain parenting lock (SCHEMA.md "Datetime", same rule as
        # object.move, with one birth-time exception): a date id may only be
        # created AT its deterministic chain parent (year → root, month →
        # year, day → month). Claiming any other parent is establishing an
        # illegal parented state and fails loud; a parentless create is the
        # standalone shape the client chain self-heal adopts, so it stays
        # legal.
        if (
            parent_id is not None
            and parse_date_node_id(object_id) is not None
            and parent_id != date_chain_parent_id(object_id)
        ):
            raise MoveGuardError(
                f"{op_type}: node {object_id} is a date-chain node;"
                " it can only be created at its deterministic chain parent",
                op_type,
            )

        with self._conn:
            self._conn.execute(
                """INSERT INTO nodes
                     (workspace_id, id, is_class, is_embedded, parent_id, name, class_ids, content, content_plain,
                      icon, color, list_style, is_active, created_at, updated_at, created_by, updated_by,
                      hlc_physical, hlc_logical, actor_id)
                   VALUES (?, ?, 0, ?, ?, NULL, '[]', ?, ?, NULL, NULL, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    env.workspace_id,
                    object_id,
                    is_embedded,
                    parent_id,
                    content,
                    content_plain,
                    payload.get("listStyle"),
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
        # Promotion/demotion (Revision 12) is the isEmbedded toggle: the flag
        # joins the row-level LWW set; a 1→0 flip (promotion to the Page
        # role) stringifies the rich token stream to text-only in the same op
        # (the lossy boundary the UI warns about), while a 0→1 demotion
        # leaves the content untouched — demotion never un-flattens. On a
        # class row the flag is inert (classes render ClassView regardless);
        # applying it harmlessly keeps the op uniform. Promotion (the
        # standalone claim) is gated by the parent-page condition; demotion
        # of a parented page is gated on its standalone children being gone.
        is_embedded = payload.get("isEmbedded")
        if is_embedded is not None:
            if is_embedded is False and row[2] is not None:
                parent = self._node_row(env.workspace_id, str(row[2]))
                if parent is None or not self._is_page_row(parent):
                    raise MoveGuardError(
                        f"{op_type}: is_embedded = 0 (standalone) requires the parent to render"
                        " with document chrome (the parent is a class or an embedded block)",
                        op_type,
                    )
            if is_embedded is True and bool(row[4]) is False and row[2] is not None:
                standalone_child = self._conn.execute(
                    "SELECT 1 FROM nodes WHERE parent_id = ? AND is_embedded = 0 LIMIT 1",
                    (object_id,),
                ).fetchone()
                if standalone_child is not None:
                    raise MoveGuardError(
                        f"{op_type}: cannot demote node {object_id} while it carries"
                        " standalone (is_embedded = 0) children (demote or move them first)",
                        op_type,
                    )
            sets.append("is_embedded = ?")
            values.append(1 if is_embedded else 0)
            if is_embedded is False and bool(row[4]):
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
        # Wire node fields (the icon/color precedent): presence writes,
        # present-null clears (SQL NULL) — the payload schema's nullable
        # fields make absence and clear distinguishable, exactly like
        # ``color``. They ride the row-level LWW with the rest of the
        # update. Cover/banner map without validating (asset existence is a
        # read/client-layer concern); the alias target DOES validate —
        # see _assert_alias_acyclic (the extends-DAG precedent: structural
        # invariants are write-time impossible).
        if "coverAssetId" in payload:
            sets.append("cover_asset_id = ?")
            cover_asset_id = payload["coverAssetId"]
            values.append(str(cover_asset_id) if cover_asset_id is not None else None)
        if "bannerAssetId" in payload:
            sets.append("banner_asset_id = ?")
            banner_asset_id = payload["bannerAssetId"]
            values.append(str(banner_asset_id) if banner_asset_id is not None else None)
        if "aliasedNodeId" in payload:
            aliased_node_id = payload["aliasedNodeId"]
            if aliased_node_id is not None:
                self._assert_alias_acyclic(env.workspace_id, object_id, str(aliased_node_id), op_type)
            sets.append("aliased_node_id = ?")
            values.append(str(aliased_node_id) if aliased_node_id is not None else None)
        # The page subtitle (web schema v18 parity): same nullish
        # convention — presence writes, present-null clears (SQL NULL),
        # absence preserves; plain text, max 512 chars enforced by the
        # payload schema above the applier.
        if "description" in payload:
            sets.append("description = ?")
            values.append(payload["description"])
        # Per-block list style (the listStyle wire node field, web schema
        # v22→v23 parity): presence writes, present-null clears back to the
        # bullet default — the same keep/clear contract as `color`.
        if "listStyle" in payload:
            sets.append("list_style = ?")
            values.append(payload["listStyle"])
        if payload.get("contentAst") is not None:
            # Class content stays text-only; every other node keeps the rich
            # token stream it was sent. A page's own content may carry inline
            # tokens (mentions, external links) — the header title edits it
            # with the full block editor — while display-name derivation
            # still flattens to text for labels (SCHEMA.md title-is-content).
            # Create-as-main and the promotion stringify above remain the
            # lossy boundaries (packages/store/src/appliers.ts parity, the
            # 2026-10-07 title-flatten ruling).
            flatten = bool(row[3])
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
        # Content-asserted membership: the class_link tokens about to be
        # stored class the node (any writer converges). Class rows flatten
        # (never carry links); date-chain nodes' class set is fixed — a
        # foreign link there is the same illegal state as a foreign classId
        # on create. The adds land after the write, inside one transaction.
        asserted_class_ids: list[str] = []
        if payload.get("contentAst") is not None:
            flatten = bool(row[3])
            asserted_class_ids = [] if flatten else _content_class_link_ids(payload["contentAst"])
            locked_date_class = date_chain_class_id(object_id)
            if locked_date_class is not None and any(
                cid != locked_date_class for cid in asserted_class_ids
            ):
                raise MoveGuardError(
                    f"{op_type}: node {object_id} is a date-chain node;"
                    f" it can only carry the {locked_date_class} system class",
                    op_type,
                )
        with self._conn:
            self._conn.execute(f"UPDATE nodes SET {', '.join(sets)} WHERE workspace_id = ? AND id = ?", values)
            for class_id in asserted_class_ids:
                self._class_member_upsert(object_id, class_id, env)
            if asserted_class_ids:
                self._recompute_class_ids(object_id)
        return True

    def _sweep_class_membership(self, class_id: str, purge: bool) -> None:
        """A deleted class leaves every node's class_ids (the no-divergence
        law's deletion arm): ``purge`` deletes the membership rows outright
        (permanent delete / trash.empty), otherwise the pairs are tombstoned
        (trash — restore brings the class node back; membership is
        re-assertable from content). Lockstep with the TS reference's
        ``sweepClassMembership`` (packages/store/src/appliers.ts)."""
        members = [
            str(r[0])
            for r in self._conn.execute(
                "SELECT node_id FROM class_member_set WHERE class_id = ? AND present = 1",
                (class_id,),
            ).fetchall()
        ]
        if purge:
            self._conn.execute("DELETE FROM class_member_set WHERE class_id = ?", (class_id,))
        else:
            self._conn.execute(
                "UPDATE class_member_set SET present = 0 WHERE class_id = ?", (class_id,)
            )
        for node_id in members:
            self._recompute_class_ids(node_id)

    def _apply_object_delete(self, env: RelayEnvelope) -> bool:
        op_type = "object.delete"
        object_id = str(env.payload["objectId"])
        self._require_node(env.workspace_id, object_id, op_type)
        permanent = bool(env.payload.get("permanent"))
        ts = _envelope_ts(env)
        deleted_row = self._node_row(env.workspace_id, object_id)
        deleted_is_class = deleted_row is not None and bool(deleted_row[3])
        if not permanent:
            ids = self._subtree_ids(env.workspace_id, object_id)
            placeholders = ", ".join("?" for _ in ids)
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO trash (node_id, deleted_at, is_permanent) VALUES (?, ?, 0)",
                    (object_id, ts),
                )
                # Soft delete: trash the whole subtree (restore is whole-tree),
                # keep the tree rows for restore.
                self._conn.execute(f"UPDATE nodes SET is_active = 0 WHERE id IN ({placeholders})", ids)
                if deleted_is_class:
                    self._sweep_class_membership(object_id, purge=False)
            return True
        with self._conn:
            self._permanently_delete_subtree(env.workspace_id, object_id, ts)
            if deleted_is_class:
                self._sweep_class_membership(object_id, purge=True)
        return True

    def _permanently_delete_subtree(self, workspace_id: str, object_id: str, ts: str) -> None:
        """Hard-delete a subtree and record the root's permanent retention row
 the shared body of ``object.delete {permanent: true}`` and
        ``trash.empty`` (lockstep with the TS reference's
        ``permanentlyDeleteSubtree``, ``packages/store/src/appliers.ts``).
        Every descendant table owned by the subtree is purged; the root's
        ``is_permanent = 1`` trash row survives with ``deleted_at = ts``."""
        ids = self._subtree_ids(workspace_id, object_id)
        placeholders = ", ".join("?" for _ in ids)
        self._conn.execute(
            "INSERT OR REPLACE INTO trash (node_id, deleted_at, is_permanent) VALUES (?, ?, 1)",
            (object_id, ts),
        )
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

    def _apply_trash_empty(self, env: RelayEnvelope) -> bool:
        """Empty the trash (lockstep with the TS reference's
        ``applyTrashEmpty``, ``packages/store/src/appliers.ts``):
        permanently delete every node carrying a non-permanent trash row.
        The root set is snapshotted at apply time, ordered by node_id for
        determinism, and each root runs the same subtree purge as
        ``object.delete {permanent: true}``. A root whose node row is gone
        (consumed by an earlier iteration's subtree, or a legacy dangling
        trash row) is swept, never fatal. Replay is idempotent: an empty
        trash table no-ops.

        Concurrency is plain log order, not special-cased: an
        ``object.restore`` landing BEFORE this op removed the node from the
        apply-time set (it survives); one landing after is LWW against the
        permanent tombstones like any delete pair."""
        ts = _envelope_ts(env)
        workspace_id = env.workspace_id
        roots = [
            str(row[0])
            for row in self._conn.execute(
                "SELECT node_id FROM trash WHERE is_permanent = 0 ORDER BY node_id"
            ).fetchall()
        ]
        with self._conn:
            for node_id in roots:
                root_row = self._node_row(workspace_id, node_id)
                if root_row is None:
                    # Consumed by an earlier iteration's subtree purge, or a
                    # legacy dangling trash row with no node row: sweep the
                    # stale row and skip — replay never fails on it.
                    self._conn.execute("DELETE FROM trash WHERE node_id = ?", (node_id,))
                    continue
                root_was_class = bool(root_row[3])
                self._permanently_delete_subtree(workspace_id, node_id, ts)
                if root_was_class:
                    self._sweep_class_membership(node_id, purge=True)
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
        # Parent-page condition (Revision 12): a
        # standalone node (is_embedded = 0) renders with document chrome only
        # under a page parent, so moving one under a class or an embedded
        # block establishes an illegal parented state and fails loud (moves
        # never write the flag — the alternative, silently clearing it, would
        # rewrite render state behind the user's back).
        if parent_id is not None and int(row[4]) == 0:
            parent = self._node_row(env.workspace_id, parent_id)
            if parent is None or not self._is_page_row(parent):
                raise MoveGuardError(
                    f"{op_type}: node {object_id} is standalone (is_embedded = 0);"
                    " it can only move under a parent that renders with document chrome",
                    op_type,
                )
        # Date-chain parenting lock (SCHEMA.md "Datetime"): a date node may
        # ONLY live under its deterministic chain parent (year → root,
        # month → year, day → month). The chain self-heal re-issues exactly
        # this move, so the chain-correct parent stays legal; any other
        # target is a reparenting attempt and fails loud.
        if parse_date_node_id(object_id) is not None and parent_id != date_chain_parent_id(object_id):
            raise MoveGuardError(
                f"{op_type}: node {object_id} is a date-chain node;"
                " its parent is locked to the deterministic chain parent",
                op_type,
            )

        # Parent/position are row-level LWW by envelope (hlc, actor): the whole
        # move publishes or drops — a position write never outlives a newer
        # parent write and vice versa. Moves never write the render bit: a
        # parentless non-class node renders with document chrome by the second
        # cascade branch regardless of is_embedded (unread there).
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
        """The class node (is_class=1, is_embedded=0) is the structural
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
                     (workspace_id, id, is_class, is_embedded, parent_id, class_ids, name, content, content_plain,
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
        # Date-chain class lock (SCHEMA.md "Datetime"): a date node is a
        # journal entry, never a class — declaring it one (fresh or the
        # conversion flip) would sever its identity.
        if parse_date_node_id(class_id) is not None:
            raise MoveGuardError(
                f"class.create: node {class_id} is a date-chain node; it can never be declared a class",
                "class.create",
            )
        ts = _envelope_ts(env)
        # Registry ``name`` is a denormalized cache of the class node's title
        # text (NOT NULL — the empty string is the absent sentinel; the
        # authority is node.content). A conversion (class.create on an
        # existing node) carries no contentAst: absent fields PRESERVE the
        # stored registry values on re-declaration (COALESCE around a
        # NULLIF sentinel), they never wipe. The fresh INSERT's empty-name
        # case is backfilled from the node's existing title below
        # (title-is-content: the node row is the authority).
        title_text = plaintext_excerpt(payload["contentAst"]) if payload.get("contentAst") is not None else None
        with self._conn:
            self._conn.execute(
                """INSERT INTO class (id, workspace_id, name, icon, color, description, active, created_at, updated_at)
                   VALUES (?, ?, COALESCE(?, ''), ?, ?, NULL, 1, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     name = COALESCE(NULLIF(excluded.name, ''), class.name),
                     icon = COALESCE(excluded.icon, class.icon),
                     color = COALESCE(excluded.color, class.color),
                     description = class.description, active = 1, updated_at = excluded.updated_at""",
                (class_id, env.workspace_id, title_text, payload.get("icon"), payload.get("color"), ts, ts),
            )
            if title_text is None:
                # Title-less declaration (the conversion path — a bare
                # classId payload): adopt the node's current title into the
                # fresh registry cache (parse_content_ast never raises —
                # unparseable content reads as a single text token).
                node_row = self._conn.execute(
                    "SELECT content FROM nodes WHERE workspace_id = ? AND id = ?", (env.workspace_id, class_id)
                ).fetchone()
                derived = plaintext_excerpt(parse_content_ast(node_row[0] if node_row is not None else None))
                self._conn.execute("UPDATE class SET name = ? WHERE id = ? AND name = ''", (derived, class_id))
        self._upsert_class_node(
            env,
            class_id,
            _class_node_fields(payload),
        )
        # Conversion (retiring the seeded `class`
        # meta class): class.create on an EXISTING node DECLARES that node a
        # class — the node row was INSERT OR IGNOREed above, which never
        # flips the identity bit. The flip is the whole capability:
        # is_class = 1, classes-are-roots (the parent edge + its
        # child-order row drop), the render bit cleared. The node's
        # title/icon/color stay LWW-gated above (an absent payload field
        # preserves them — conversion carries no content unless sent).
        # Membership and the node's own children are untouched (classes are
        # containers). Applied unconditionally: declaration is structural,
        # not a row-field race (the extends-DAG precedent, object.delete's
        # bit flips).
        with self._conn:
            converted = self._conn.execute(
                "UPDATE nodes SET is_class = 1, is_embedded = 0, parent_id = NULL"
                " WHERE workspace_id = ? AND id = ? AND is_class = 0",
                (env.workspace_id, class_id),
            )
            if converted.rowcount > 0:
                self._conn.execute("DELETE FROM node_child_order WHERE child_id = ?", (class_id,))
            # Hierarchy self-row: the `class` query condition matches via
            # the closure, so every class needs (id, id) even before any
            # setExtends runs (the TS applyClassCreate parity).
            self._conn.execute(
                "INSERT OR IGNORE INTO class_hierarchy (class_id, ancestor_id) VALUES (?, ?)", (class_id, class_id)
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
        # Date-chain class lock (SCHEMA.md "Datetime"): the precision's
        # system class IS the date node's identity — removing it is illegal.
        # Unassigning a FOREIGN class stays legal: it cleans up a legacy
        # violation rather than freezing one.
        locked_date_class = date_chain_class_id(object_id)
        if locked_date_class is not None and class_id == locked_date_class:
            raise MoveGuardError(
                f"{op_type}: node {object_id} is a date-chain node;"
                f" its {locked_date_class} system class cannot be removed",
                op_type,
            )
        # Content-reference lock (the no-divergence law): the stored content
        # still carries an inline class_link to this class — unassigning
        # would strand a live reference. The removal gesture strips the
        # links FIRST; a raw class.unassign refuses rather than silently
        # diverging — loud failure, never applied.
        node_row = self._node_row(env.workspace_id, object_id)
        if node_row is not None and class_id in _content_class_link_ids(
            parse_content_ast(node_row[10])
        ):
            raise MoveGuardError(
                f"{op_type}: node {object_id} content references class {class_id}"
                " inline — remove the inline links first",
                op_type,
            )
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
        """Binding upsert — the genuinely per-class mechanics ONLY
        (sequence + the active soft-unbind flag; row LWW by envelope HLC).

        (the property-definition surfaces batch):
        ``required`` and ``defaultValue`` left the binding entirely —
        ``required`` retired outright, ``defaultValue`` moved to the
        property schema (one global default per property, the Logseq shape;
        the strict payload schema rejects both keys like any retired key).
        On update the payload PATCHES the row — omitted fields keep their
        existing values (COALESCE; explicit false is a real write).
        """
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

        # R5: dead config is rejected — a binding
        # on a schema whose scope EXCLUDES the class is provably inert (the
        # effective read suppresses its default under R4), so the write fails
        # loud. The class's own extends closure counts: a class scoped to an
        # ANCESTOR of the binding class still binds (subclass instances are
        # eligible via the ancestor). Schema-less writes (unknown schema id)
        # stay unchecked.
        schema_row = self._property_schema_row(schema_id)
        if schema_row is not None:
            scope = schema_row.get("scope_class_ids")
            if not self._class_closure_intersects_scope([class_id], scope):
                raise MoveGuardError(
                    f"{op_type}: schema {schema_id} is scoped to classes that exclude {class_id} —"
                    " the binding would be inert (scope is the sole locality authority)",
                    op_type,
                )

        # Omitted (absent) fields keep the stored value via COALESCE; an
        # explicit false is a real write. The binding row carries ONLY
        # sequence + active — the render contracts (readonly/hideWhenEmpty/
        # display) are PROPERTY-level and live on property_schema; the
        # default value lives there too.
        active = None if "active" not in payload else (1 if payload["active"] else 0)
        sequence = int(payload["sequence"]) if payload.get("sequence") is not None else None
        with self._conn:
            if existing is None:
                self._conn.execute(
                    """INSERT INTO class_property
                         (class_id, property_schema_id, sequence, active,
                          hlc_physical, hlc_logical, actor_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        class_id,
                        schema_id,
                        sequence if sequence is not None else 0,
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
                         active = COALESCE(?, active),
                         hlc_physical = ?, hlc_logical = ?, actor_id = ?
                       WHERE class_id = ? AND property_schema_id = ?""",
                    (
                        sequence,
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
        """Schema upsert (INSERT ... ON CONFLICT id — delete+recreate wins).

        PC2 (moved from the binding): the default
        is typed per the schema's (payload) type — a wrong-typed default
        fails loud here instead of deriving silently on every read.
        Node-typed schemas (datetime/object/asset) accept only JSON null.
        Absent = no default (NULL column).
        """
        payload = env.payload
        op_type = "propertySchema.create"
        default_value = payload.get("defaultValue")
        if default_value is not None and not _is_valid_default_for_type(str(payload["type"]), default_value):
            schema_type = str(payload["type"])
            if schema_type in ("datetime", "object", "asset"):
                detail = "must be null — node-typed defaults are not supported"
            else:
                detail = f"must be typed {schema_type}"
            raise PropertyValueShapeError(
                f"{op_type}: defaultValue for {schema_type} schema {detail} — got {_jsonish(default_value)}",
                op_type,
            )
        ts = _envelope_ts(env)
        with self._conn:
            self._conn.execute(
                """INSERT INTO property_schema
                     (id, workspace_id, name, type, multi, icon, scope_class_ids, options, target_class_filter,
                      date_precision, date_qualified, number_pad, number_decimals, number_rounding,
                      display, readonly, hide_when_empty, default_value,
                      active, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     name = excluded.name, type = excluded.type, multi = excluded.multi,
                     icon = excluded.icon, scope_class_ids = excluded.scope_class_ids,
                     options = excluded.options,
                     target_class_filter = excluded.target_class_filter,
                     date_precision = excluded.date_precision, date_qualified = excluded.date_qualified,
                     number_pad = excluded.number_pad, number_decimals = excluded.number_decimals,
                     number_rounding = excluded.number_rounding,
                     display = excluded.display, readonly = excluded.readonly,
                     hide_when_empty = excluded.hide_when_empty,
                     default_value = excluded.default_value,
                     active = 1, updated_at = excluded.updated_at""",
                (
                    str(payload["propertySchemaId"]),
                    env.workspace_id,
                    payload.get("name"),
                    payload.get("type"),
                    1 if payload.get("multi") else 0,
                    payload.get("icon"),
                    (json.dumps([str(c) for c in payload["scopeClassIds"]]) if payload.get("scopeClassIds") is not None else None),
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
                    # The schema's ONE global default: JSON-encoded; NULL = no default (absent or
                    # an explicit JSON null both store NULL, the TS parity).
                    json.dumps(default_value) if default_value is not None else None,
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
            # icon / scope / validation rules: the keep/clear contract (absent
            # keeps the stored value, present-null clears back to
            # NULL/global/none) — the same key-presence shape as the number
            # formats below.
            if "icon" in payload:
                sets.append("icon = ?")
                values.append(payload["icon"])
            if "scopeClassIds" in payload:
                sets.append("scope_class_ids = ?")
                scope = payload["scopeClassIds"]
                values.append(json.dumps([str(c) for c in scope]) if scope is not None else None)
            if "targetClassFilter" in payload:
                sets.append("target_class_filter = ?")
                target_filter = payload["targetClassFilter"]
                values.append(json.dumps([str(c) for c in target_filter]) if target_filter is not None else None)
            if "multi" in payload:
                # The column is NOT NULL — "clear" is the single-value
                # default (0), not SQL NULL.
                sets.append("multi = ?")
                values.append(0 if payload["multi"] is None else (1 if payload["multi"] else 0))
            if "validationRules" in payload:
                rules = payload["validationRules"]
                if rules is not None:
                    schema_type = self._property_schema_type(str(payload["propertySchemaId"]))
                    # A schema-less update (unknown id — no schema FK here
                    # either) stores unchecked, exactly like property.set; a
                    # known schema enforces.
                    if schema_type is not None:
                        _assert_validation_rules_applicable(schema_type, rules, "propertySchema.update")
                    # Applicable to every write carrying a pattern: a stored
                    # pattern that throws at value-check time would be an
                    # applier crash.
                    _assert_validation_pattern_compilable(rules, "propertySchema.update")
                sets.append("validation_rules = ?")
                values.append(json.dumps(rules) if rules is not None else None)
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
            # The default value (schema-level):
            # absent keeps, present-null clears. PC2 runs against the
            # schema's STORED type, exactly like validationRules
            # applicability (a schema-less update stores unchecked).
            if "defaultValue" in payload:
                default_value = payload["defaultValue"]
                if default_value is not None:
                    schema_type = self._property_schema_type(str(payload["propertySchemaId"]))
                    if schema_type is not None and not _is_valid_default_for_type(schema_type, default_value):
                        if schema_type in ("datetime", "object", "asset"):
                            detail = "must be null — node-typed defaults are not supported"
                        else:
                            detail = f"must be typed {schema_type}"
                        raise PropertyValueShapeError(
                            f"propertySchema.update: defaultValue for {schema_type} schema"
                            f" {detail} — got {_jsonish(default_value)}",
                            "propertySchema.update",
                        )
                sets.append("default_value = ?")
                values.append(json.dumps(default_value) if default_value is not None else None)
            # Render contracts (PROPERTY-level): same key-presence
            # keep-vs-clear contract — absent keeps the stored value, a
            # present null clears back to panel / unset. `required` retired
            # from the binding; the default
            # lives here.
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
        precision ceiling, the PC6 qualifier flag, the R3 scope list and the
        R2 validation rules."""
        row = self._conn.execute(
            "SELECT id, type, multi, target_class_filter, date_precision, date_qualified,"
            " scope_class_ids, validation_rules"
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
            "scope_class_ids": _json_array_of_row(row[6]),
            "validation_rules": row[7],
        }

    def _class_closure_intersects_scope(self, carried_class_ids: list[str], scope_class_ids: list[str] | None) -> bool:
        """R3/R5 eligibility: the scope list vs a
        candidate class set's EXTENDS-AWARE closure — carried classes plus
        their class_hierarchy ancestors (the same closure walk
        _assert_ref_target_for_schema uses for targetClassFilter, and the
        effective read's reach_of for bindings). An empty scope list never
        occurs (the wire rejects it); None means global."""
        if scope_class_ids is None or len(scope_class_ids) == 0:
            return True  # global (or the defensive no-stored-empty-list).
        allowed = set(carried_class_ids)
        if carried_class_ids:
            placeholders = ", ".join("?" for _ in carried_class_ids)
            for (ancestor,) in self._conn.execute(
                f"SELECT ancestor_id FROM class_hierarchy WHERE class_id IN ({placeholders})", carried_class_ids
            ):
                allowed.add(str(ancestor))
        return any(class_id in allowed for class_id in scope_class_ids)

    def _node_eligible_for_schema(self, node_class_ids_raw: Any, scope_class_ids: list[str] | None) -> bool:
        """R3: a node is eligible for a schema when
        the schema is global or the node's class closure (its classes +
        extends ancestors) intersects the scope list. The node's classes
        come from the derived class_ids projection (already
        assignment-order; eligibility is a pure set test)."""
        if scope_class_ids is None:
            return True
        carried = _json_array_of_row(node_class_ids_raw) or []
        return self._class_closure_intersects_scope(carried, scope_class_ids)

    def _property_schema_type(self, schema_id: str) -> str | None:
        """The schema's STORED type the propertySchema.update validationRules
        applicability and defaultValue PC2 check validate against, or None when the schema id is unknown (a
        schema-less write stores unchecked)."""
        row = self._conn.execute("SELECT type FROM property_schema WHERE id = ?", (schema_id,)).fetchone()
        return None if row is None else str(row[0])

    def _assert_ref_target_for_schema(self, workspace_id: str, schema: dict[str, Any], ref: str, op_type: str) -> None:
        """PG6 graph checks for a node-typed ref's target: row existence (any
        liveness — trash is a state, not an absence), the targetClassFilter
        (extends-aware: a carried class satisfies the filter when it equals an
        entry or descends from one through class_hierarchy — the bibliography
        model filters authors by ``agent`` while person/organization EXTEND
        agent), and the datePrecision ceiling for datetime refs. M38:
        an asset-typed schema's filter is IMPLICIT — the type IS the filter
        (the asset class); an explicit targetClassFilter on an asset schema
        is redundant and ignored."""
        target = self._conn.execute(
            "SELECT class_ids FROM nodes WHERE workspace_id = ? AND id = ?", (workspace_id, ref)
        ).fetchone()
        if target is None:
            raise PropertyValueShapeError(f"{op_type}: value references node {ref}, which does not exist", op_type)
        if schema["type"] == "asset":
            filter_list: Any = [system_class_uuid("asset")]
        else:
            filter_list = None
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
        if schema["type"] == "datetime":
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
        KNOWN schema row — shape/scalar typing, the stored validationRules
        (R2 value enforcement), then the graph checks (existence / class
        filter / date precision) for the node-typed link family. ``None``
        means "no value" and bypasses everything. Returns the normalized
        value to store."""
        shaped = _assert_value_shape_for_type(schema["type"], value, op_type)
        typed = _assert_scalar_shape_for_type(schema["type"], shaped, op_type)
        if typed is None:
            return typed
        _assert_validation_rules_for_value(schema, typed, op_type)
        if schema["type"] in ("object", "asset"):
            ref = _node_ref_of_value(typed)
            if ref is not None:
                self._assert_ref_target_for_schema(workspace_id, schema, ref, op_type)
        elif schema["type"] == "datetime":
            # The unified union: a point runs the existence/filter/precision
            # checks on its ref; a range runs them on EACH non-null slot (the
            # date_range parity — either end missing fails, open sides skip).
            # A timed value on a coarser-than-day ceiling fails here too: its
            # day-precision ref claims finer granularity than the schema's.
            record = typed if isinstance(typed, Mapping) else {}
            sides = (record.get("start"), record.get("end")) if ("start" in record or "end" in record) else (typed,)
            for side in sides:
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
        # R3: the write fails loud when the target
        # node is INELIGIBLE for the schema — scopeClassIds set and the
        # node's class closure (its classes + extends ancestors) misses the
        # list. Schema-less writes (unknown schema id) stay unchecked,
        # exactly like the shape checks below. Narrowing a scope later never
        # orphans history: pre-existing authored rows survive in the log and
        # the effective read returns them tagged eligible=false — enforcement
        # is never retroactive. A schema carrying validationRules validates
        # the value before the write too (R2 value enforcement — min/max on
        # numbers, pattern full-string on text/url/email scalars, each multi
        # element at its own add).
        schema = self._property_schema_row(schema_id)
        if schema is not None:
            scope = schema.get("scope_class_ids")
            if scope is not None:
                node_row = self._conn.execute(
                    "SELECT class_ids FROM nodes WHERE workspace_id = ? AND id = ?",
                    (env.workspace_id, node_id),
                ).fetchone()
                carried = node_row[0] if node_row is not None else None
                if not self._node_eligible_for_schema(carried, scope):
                    raise MoveGuardError(
                        f"{op_type}: node {node_id} is not eligible for schema {schema_id}"
                        " (its classes — extends-aware — do not intersect the schema's scopeClassIds)",
                        op_type,
                    )
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
    # Per-workspace feature toggles (RESHAPED): LWW by
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
                 (workspace_id, id, is_class, is_embedded, parent_id, name, class_ids, content, content_plain,
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
                     (id, workspace_id, name, type, multi, icon, scope_class_ids, options, target_class_filter,
                      date_precision, date_qualified, display, active, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 0, NULL, ?, ?, NULL, NULL, NULL, ?, 1, ?, ?)""",
                (
                    schema_id,
                    env.workspace_id,
                    entry.name,
                    entry.type,
                    # Global scope (the seed-scope
                    # audit — the whole task family is reusable, Status
                    # explicitly so): NULL scope_class_ids; the family binds
                    # the task class below (bindings are per-class config,
                    # zero availability semantics).
                    None,
                    json.dumps(designed_options),
                    entry.display,
                    ts,
                    ts,
                ),
            )
            self._conn.execute(
                """INSERT OR IGNORE INTO class_property
                     (class_id, property_schema_id, sequence,
                      hlc_physical, hlc_logical, actor_id)
                   VALUES (?, ?, ?, 0, 0, NULL)""",
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
            "SELECT id, workspace_id, parent_id, is_class, is_embedded, name, class_ids, tag_ids, icon, color,"
            " is_active, content, content_plain, cover_asset_id, banner_asset_id, aliased_node_id, description, list_style"
            " FROM nodes WHERE workspace_id = ?"
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
            """SELECT n.id, n.workspace_id, n.parent_id, n.is_class, n.is_embedded, n.name, n.class_ids, n.tag_ids,
                      n.icon, n.color, n.is_active, n.content, n.content_plain,
                      n.cover_asset_id, n.banner_asset_id, n.aliased_node_id, n.description, n.list_style
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
            "SELECT id, workspace_id, parent_id, is_class, is_embedded, name, class_ids, tag_ids, icon, color,"
            " is_active, content, content_plain, cover_asset_id, banner_asset_id, aliased_node_id, description, list_style"
            " FROM nodes WHERE workspace_id = ? AND id = ?",
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
            is_embedded=bool(row[4]),
            name=row[5],
            class_ids=class_ids,
            tag_ids=tag_ids,
            icon=row[8],
            color=row[9],
            is_active=bool(row[10]),
            content=row[11],
            content_plain=str(row[12] or ""),
            cover_asset_id=row[13],
            banner_asset_id=row[14],
            aliased_node_id=row[15],
            description=row[16],
            list_style=row[17],
        )

    @_synchronized
    def resolve_alias(self, workspace_id: str, node_id: str) -> str:
        """The terminal of a node-alias chain: follow ``aliased_node_id``
        links to the final main node (M12 — the wire-field read helper
        behind the alias semantics; the read-path repointing that consumes
        it is a follow-on slice). Cycle-safe by construction: a revisit
        yields the STARTING id unchanged (the SCHEMA.md navigation ruling —
        a cyclic alias is no alias), and a generous depth cap bounds
        pathological chains to the furthest node reached. Unset/unstored
        rows are their own terminal."""
        visited: set[str] = set()
        current = node_id
        for _ in range(_ALIAS_CHAIN_DEPTH_CAP):
            if current in visited:
                return node_id  # cycle — the id unchanged
            visited.add(current)
            row = self._conn.execute(
                "SELECT aliased_node_id FROM nodes WHERE workspace_id = ? AND id = ?", (workspace_id, current)
            ).fetchone()
            if row is None or row[0] is None:
                return current
            current = str(row[0])
        return current  # depth cap — best-effort terminal

    @_synchronized
    def alias_nodes_of(self, workspace_id: str, node_id: str) -> list[str]:
        """Every node whose alias-terminal is ``node_id`` — the reverse read
        of :meth:`resolve_alias` (the monorepo ``Store.aliasNodesOf`` port):
        the recursive reverse-walk over ``aliased_node_id``, ``node_id``
        itself excluded, LIVE rows only, id order. Drives the page chrome's
        Aliases section."""
        rows = self._conn.execute(
            """WITH RECURSIVE alias_set(id) AS (
                 SELECT ?
                 UNION
                 SELECT n.id FROM nodes n JOIN alias_set a ON n.aliased_node_id = a.id
                 WHERE n.workspace_id = ? AND n.is_active = 1
               )
               SELECT id FROM alias_set WHERE id != ? ORDER BY id""",
            (node_id, workspace_id, node_id),
        ).fetchall()
        return [str(row[0]) for row in rows]

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
                                           ?? the SCHEMA's defaultValue

        Port of ``packages/store/src/effective.ts`` (PG5/PC4).
        Authored rows always win and survive class removal; derived defaults
        are computed HERE and never materialized (the applier writes no
        property_value rows for them). The default is SCHEMA-level (one global default per property, the Logseq
        shape): a winning ACTIVE binding still decides WHICH properties a
        class's nodes carry (and supplies sequence/boundBy), but the default
        VALUE comes from the schema row. Authored rows come through the PG5
        visible-set derivation (:meth:`_visible_property_value_rows` — slot
        tombstones + element tombstones) and carry their stable element id.
        Binding conflicts across the node's classes resolve per the SCHEMA.md
        diamond rule (PG4): candidates are (class, ancestor) pairs
        discovered by a shortest-path walk (BFS) over ``class_extends`` — own
        binding (distance 0) first, then inherited bindings by shortest
        extends-path, ties by the class's OR-Set membership add HLC (earliest
        first), then class id. ``bound_by`` names the ANCESTOR whose binding
        row supplies the metadata (the winning class itself for an own
        binding); with no extends edges this reduces exactly to
        first-class-applied-wins over own bindings. Only ACTIVE binding rows
        are candidates (PC4: an inactive binding stops contributing defaults
        AND binding metadata — sequence — while the ROW survives and
        authored values read as unbound, ``bound_by`` None; the render
        contracts readonly/hideWhenEmpty/display are SCHEMA-sourced —
        they ride authored and derived rows alike, unbound values included).
        A stored default that no longer matches the schema type yields no
        default (PC2 read-side). R3/R4: every row
        carries the eligibility tag — the schema's scopeClassIds (None =
        global) against the node's extends-aware class closure; an
        ineligible node reads neither the property nor a derived default,
        while authored rows survive scope narrowing tagged
        ``eligible=False`` (never retroactive).
        A pure read over derived tables —
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
                    "SELECT property_schema_id, sequence"
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

        def parse_json(raw: Any) -> Any:
            if not isinstance(raw, str):
                return raw
            try:
                return json.loads(raw)
            except ValueError:
                return raw

        # Schema rows for everything referenced (authored rows survive schema
        # deletion: the row renders with schema=None). The schema
        # carries the PROPERTY-level render contracts — display (sanitized)
        # and the readonly/hide-when-empty flags — and the ONE global default
        # (the value moved from the binding row to
        # the schema).
        schema_ids = {str(row["property_schema_id"]) for row in authored_rows} | set(winner_by_schema)
        schemas: dict[str, EffectivePropertySchema] = {}
        schema_default: dict[str, Any] = {}
        if schema_ids:
            placeholders = ", ".join("?" for _ in schema_ids)
            rows = self._conn.execute(
                f"SELECT id, name, type, multi, display, readonly, hide_when_empty,"
                f" icon, scope_class_ids, validation_rules, default_value"
                f" FROM property_schema WHERE id IN ({placeholders})",
                tuple(sorted(schema_ids)),
            ).fetchall()
            for row in rows:
                if row[10] is not None:
                    schema_default[str(row[0])] = parse_json(row[10])
                schemas[str(row[0])] = EffectivePropertySchema(
                    id=str(row[0]),
                    name=str(row[1]),
                    type=str(row[2]),
                    multi=bool(row[3]),
                    display=display_of(row[4]),
                    readonly=flag(row[5]),
                    hide_when_empty=flag(row[6]),
                    icon=row[7],
                    scope_class_ids=(tuple(scope) if (scope := _json_array_of_row(row[8])) is not None else None),
                    validation_rules=_validation_rules_of_row(row[9]),
                )

        # 4b. R3 eligibility: the node's
        # EXTENDS-AWARE class closure (its classes + all ancestors via
        # reach_of — the same walk binding resolution uses) against each
        # schema's scopeClassIds. A schema is eligible for the node when
        # global (None list) or the closure intersects the list. The closure
        # set is computed once.
        node_class_closure: set[str] = set()
        for cls in classes:
            for ancestor_id, _distance in reach_of(str(cls[0])):
                node_class_closure.add(ancestor_id)

        def eligible_of(schema_id: str, schema: EffectivePropertySchema | None) -> bool:
            if schema is None:
                return True  # schema-less rows (schema deleted) read True.
            scope = schema.scope_class_ids
            return scope is None or any(class_id in node_class_closure for class_id in scope)

        rows_out: dict[str, EffectiveProperty] = {}
        shadowed_default_schemas: set[str] = set()
        for authored in authored_rows:
            schema_id, idx = authored["property_schema_id"], authored["idx"]
            winner = winner_by_schema.get(schema_id)
            schema = schemas.get(schema_id)
            if idx == 0:
                shadowed_default_schemas.add(schema_id)
            # PG5 merge key: rows at the same idx are distinct elements and
            # all surface. Readonly/hideWhenEmpty/display are per-PROPERTY
            # (the schema — unbound values included); sequence is per-CLASS
            # (the winning binding).
            rows_out[f"{schema_id}:{idx}:{authored['id']}"] = EffectiveProperty(
                property_schema_id=schema_id,
                idx=idx,
                element_id=authored["id"],
                schema=schema,
                value=parse_json(authored["value"]),
                metadata=parse_json(authored["metadata"]) if authored["metadata"] is not None else None,
                source="authored",
                bound_by=winner[0] if winner else None,
                readonly=schema.readonly if schema is not None else None,
                hide_when_empty=schema.hide_when_empty if schema is not None else None,
                sequence=int(winner[1][1]) if winner else None,
                display=schema.display if schema is not None else None,
                # Authored rows SURVIVE scope narrowing (never retroactive) —
                # the read tags them; callers decide how an ineligible row
                # renders.
                eligible=eligible_of(schema_id, schema),
            )

        for schema_id, (class_id, binding) in winner_by_schema.items():
            # The default VALUE is schema-level:
            # the winning binding decides the property is present (and
            # supplies sequence/boundBy), the schema row supplies the value.
            if schema_id not in schema_default:
                continue  # schema without a default
            default_value = schema_default[schema_id]
            # R4: an ineligible node shows NEITHER
            # the property NOR a derived default from an out-of-scope
            # binding — a stray binding must not render a value for a
            # property the node can't have.
            schema = schemas.get(schema_id)
            if not eligible_of(schema_id, schema):
                continue
            # PC2 read-side: a stored default that no longer matches the
            # schema type (written before validation, or after a
            # delete+recreate changed the type) yields no default rather
            # than a wrong-typed value.
            if schema is not None and not _is_valid_default_for_type(schema.type, default_value):
                continue
            if schema_id in shadowed_default_schemas:
                continue  # authored value at idx 0 shadows the default
            rows_out[f"{schema_id}:0:default"] = EffectiveProperty(
                property_schema_id=schema_id,
                idx=0,
                element_id=f"default:{schema_id}:0",
                schema=schema,
                value=default_value,
                metadata=None,
                source="default",
                bound_by=class_id,
                readonly=schema.readonly if schema is not None else None,
                hide_when_empty=schema.hide_when_empty if schema is not None else None,
                sequence=int(binding[1]),
                display=schema.display if schema is not None else None,
                eligible=True,
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
        (``is_class``/``is_embedded``, ``is_active`` — same polarity as
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
    -- Revision-12 embedding model (replaces the node_type enumeration and
    -- Revision 11's present_as_main render bit, renamed + inverted):
    -- is_class is the ONLY identity marker — classes are always roots;
    -- is_embedded is the embedding flag read by the third cascade branch for
    -- parented non-class nodes: 1 (the DEFAULT) = the node's content belongs
    -- to the parent's body — the Block role; 0 = a standalone structural
    -- element — the Page role (main-children zone + document chrome when
    -- zoomed). The flag is unread for parentless nodes (document chrome by
    -- the second branch) and for classes (ClassView by the first branch).
    is_class INTEGER NOT NULL DEFAULT 0,
    is_embedded INTEGER NOT NULL DEFAULT 1,
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
    -- Wire node fields (the icon/color precedent — the
    -- M27 lockstep, web schema v15→v16 parity): platform-fixed node
    -- fundamentals set via object.update (never object.create) — the asset
    -- node behind the page cover/banner chrome and the main page a node
    -- alias points at (many-to-one FROM the alias). NULL = unset;
    -- present-null on the wire CLEARS. The alias target additionally gets
    -- the write-time cycle check (see _assert_alias_acyclic).
    cover_asset_id TEXT,
    banner_asset_id TEXT,
    aliased_node_id TEXT,
    -- Page subtitle in the core page chrome (web schema
    -- v17→v18 parity — the Capacities header precedent): plain text,
    -- max 512 chars, object.update-only. NULL = unset; present-null CLEARS.
    description TEXT,
    -- Per-block list style (web schema v22→v23 parity —
    -- the listStyle wire node field): the bullet dot's replacement —
    -- "numbered" | "alpha" | "roman". NULL = bullet (the default); the
    -- NUMBER is never stored — clients derive it at render time from the
    -- contiguous run of same-style siblings.
    list_style TEXT,
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
    -- The schema's icon: an MDI icon name; NULL = none.
    icon TEXT,
    -- The SOLE locality authority (R1; web schema
    -- v24 parity): a JSON array of class ids, NULL = GLOBAL (any node may
    -- carry the property). A non-empty list is LOCAL to those classes and
    -- their extends-ancestors (eligibility walks class_hierarchy, the same
    -- closure binding resolution uses). RETIRES the display-only ``scope``
    -- TEXT column (rebuilt away at v16; the live log rewrite is the
    -- monorepo's migrate-property-scope.mts). Bindings (class_property
    -- rows) carry ZERO availability semantics — scope says who MAY carry
    -- the property; a binding says what a class DOES WITH it.
    scope_class_ids TEXT,
    -- Value validation rules, JSON {min?, max?, pattern?};
    -- NULL = none. min/max apply to number schemas, pattern to
    -- text/url/email (apply-time enforcement against the stored type).
    validation_rules TEXT,
    options TEXT NOT NULL DEFAULT '[]',
    target_class_filter TEXT,
    -- SCHEMA.md "Datetime": finest granularity a date value may claim
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
    -- The property's DEFAULT VALUE (web schema v25
    -- parity — moved from the class binding to the schema, the Logseq
    -- shape): ONE global default per property. JSON-encoded; NULL = no
    -- default. Typed per the schema type (PC2, validated at apply —
    -- node-typed schemas (datetime/object/asset) accept only JSON null, "a
    -- default that links a node is meaningless").
    default_value TEXT,
    -- The render contracts are PROPERTY-level — display (panel|bullet|inline; NULL = panel) and the
    -- readonly/hide-when-empty tri-state flags, wherever the property
    -- appears (class-bound or not). ('required' retired outright under the
    -- it was the binding-level exception.)
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
    # Revision-12 embedding model: the legacy node_type enumeration maps to
    # the two booleans directly in the canonical shape (page → is_class 0 +
    # is_embedded 0; block → (0, 1); class → (1, 0) — class rows converge to
    # 0, matching the v15 ladder's inversion).
    if "node_type" in legacy:
        copy_exprs["is_class"] = f"CASE WHEN {_quote_ident('node_type')} = 'class' THEN 1 ELSE 0 END"
        copy_exprs["is_embedded"] = f"CASE WHEN {_quote_ident('node_type')} = 'block' THEN 1 ELSE 0 END"
    else:
        copy_exprs.setdefault("is_class", "0")
        copy_exprs.setdefault("is_embedded", "1")
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
-- Class -> property bindings (sequence + active only), per SCHEMA.md
-- "Class properties — bindings, defaults, aggregation" (2026-09-27) and the
-- (the property-definition surfaces batch).
-- Registry rows authored by class.property.set/unset; LWW on the row by
-- (hlc, actor). Defaults are a DERIVED read model (get_effective_properties),
-- never materialized property_value rows — and the default VALUE is
-- SCHEMA-level (one global default per property, the Logseq shape), read
-- off the property_schema row.
-- the row carries ONLY the genuinely per-class
-- mechanics (sequence + active). 'required' retired outright (its only
-- render effect — hiding the icon-button's "None" row — was mild
-- enforcement, contradicting the render-contract law) and 'default_value'
-- MOVED to property_schema; the strict payload schema rejects both keys
-- like any retired key. 'active' (PC4): the soft-unbind flag — an inactive
-- row stops contributing to the effective read (no default, no sequence)
-- while the ROW survives; absent column means active, so existing rows
-- converge with zero migration.
CREATE TABLE IF NOT EXISTS class_property (
    class_id TEXT NOT NULL,
    property_schema_id TEXT NOT NULL,
    sequence INTEGER NOT NULL DEFAULT 0,
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
            is_embedded INTEGER NOT NULL DEFAULT 1,
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
            workspace_id, id, is_class, is_embedded, parent_id, name,
            class_ids, class_order, tag_ids, icon, color, is_active, content,
            content_plain, created_at, updated_at, created_by, updated_by,
            hlc_physical, hlc_logical, actor_id
        )
        SELECT workspace_id, id,
            CASE WHEN node_type = 'class' THEN 1 ELSE 0 END,
            CASE WHEN node_type = 'block' THEN 1 ELSE 0 END,
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
    without the column the same open. The guard also requires the pre-v12
    row shape (a ``required`` column): fresh chains create the current
    binding shape, where the v11 experiment never
    existed — adding ``display`` there would only trip v12's rebuild into
    copying retired columns."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(class_property)")}
    if "required" in columns:
        LocalStore._add_column_if_missing(conn, "class_property", "display", "TEXT")


def _migrate_v12(conn: sqlite3.Connection) -> None:
    """v12: the render contracts move from
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


def _migrate_v13(conn: sqlite3.Connection) -> None:
    """v13 — the wire node fields (the M27 lockstep; web
    schema v15→v16 parity): the cover/banner asset refs and the node-alias
    target land as three nullable node columns mapped by object.update.
    Purely additive — the single column guard keeps the ALTERs idempotent
    for fresh v13 creates (CREATE TABLE IF NOT EXISTS never alters), the
    v10 migration precedent."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(nodes)")}
    if "cover_asset_id" not in columns:
        conn.executescript(
            """
            ALTER TABLE nodes ADD COLUMN cover_asset_id TEXT;
            ALTER TABLE nodes ADD COLUMN banner_asset_id TEXT;
            ALTER TABLE nodes ADD COLUMN aliased_node_id TEXT;
            """
        )


def _migrate_v14(conn: sqlite3.Connection) -> None:
    """v14 — the page-subtitle wire node field (web
    schema v17→v18 parity): ``description`` lands as a nullable node column
    mapped by object.update. Purely additive — the column guard keeps the
    ALTER idempotent for fresh v14 creates (CREATE TABLE IF NOT EXISTS never
    alters), the v10/v13 precedent."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(nodes)")}
    if "description" not in columns:
        conn.execute("ALTER TABLE nodes ADD COLUMN description TEXT")


def _migrate_v15(conn: sqlite3.Connection) -> None:
    """v15 — the Revision-12 embedding model (web schema v21 parity): ``present_as_main`` is renamed to ``is_embedded`` WITH
    POLARITY INVERSION (is_embedded = 1 - present_as_main: the old
    main-zone bit 1 becomes the standalone claim 0, the old inline-body 0
    becomes the embedded default 1; class rows converge to 0 so the ladder
    and the appliers' canonical writes meet). Table rebuild (the v12
    precedent); guarded on the old column so the step is a no-op for
    databases that already carry is_embedded (idempotent)."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(nodes)")}
    if "present_as_main" not in columns:
        return
    conn.executescript(
        """
        PRAGMA foreign_keys = OFF;
        CREATE TABLE nodes_v15 (
            workspace_id TEXT NOT NULL,
            id TEXT NOT NULL,
            is_class INTEGER NOT NULL DEFAULT 0,
            is_embedded INTEGER NOT NULL DEFAULT 1,
            parent_id TEXT,
            name TEXT,
            class_ids TEXT NOT NULL DEFAULT '[]',
            class_order TEXT NOT NULL DEFAULT '[]',
            tag_ids TEXT NOT NULL DEFAULT '[]',
            content TEXT,
            content_plain TEXT NOT NULL DEFAULT '',
            icon TEXT,
            color TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT,
            updated_at TEXT,
            created_by TEXT,
            updated_by TEXT,
            hlc_physical INTEGER NOT NULL DEFAULT 0,
            hlc_logical INTEGER NOT NULL DEFAULT 0,
            actor_id TEXT,
            cover_asset_id TEXT,
            banner_asset_id TEXT,
            aliased_node_id TEXT,
            description TEXT,
            PRIMARY KEY (workspace_id, id)
        );
        INSERT INTO nodes_v15 (
            workspace_id, id, is_class, is_embedded, parent_id, name,
            class_ids, class_order, tag_ids, content, content_plain, icon, color,
            is_active, created_at, updated_at, created_by, updated_by,
            hlc_physical, hlc_logical, actor_id,
            cover_asset_id, banner_asset_id, aliased_node_id, description
        )
        SELECT workspace_id, id, is_class,
            CASE WHEN is_class = 1 THEN 0
                 WHEN present_as_main = 1 THEN 0
                 ELSE 1 END,
            parent_id, name, class_ids, class_order, tag_ids, content, content_plain,
            icon, color, is_active, created_at, updated_at, created_by, updated_by,
            hlc_physical, hlc_logical, actor_id,
            cover_asset_id, banner_asset_id, aliased_node_id, description
        FROM nodes;
        DROP TABLE nodes;
        ALTER TABLE nodes_v15 RENAME TO nodes;
        PRAGMA foreign_keys = ON;
        """
    )


def _migrate_v16(conn: sqlite3.Connection) -> None:
    """v16 — two guarded blocks (web schema v22→v24 parity):

    1. The per-block list style: ``list_style`` lands as
       a nullable node column mapped by object.create/update. Purely
       additive — the column guard keeps the ALTER idempotent for fresh
       v16 creates (the v14 precedent), and it runs after every node-table
       rebuild above so pre-v15 databases arrive at the current shape
       through the same path.
    2. The property-scope batch: the
       property_schema table REBUILDS (the v12 precedent — SQLite cannot
       drop a column on old builds): the display-only ``scope`` TEXT column
       retires (the locality authority is the new ``scope_class_ids`` JSON
       column, NULL = global), and the schema gains ``icon`` +
       ``validation_rules`` (both NULL = unset). Rows copy through with
       the new columns NULL — the live log rewrite (the monorepo's
       migrate-property-scope.mts, Path B) sets scope_class_ids from the
       retired enum and the epoch bump makes every replica wipe + resync,
       so converged bytes never depend on this backfill. Guarded on the old
       column so the step is a no-op for databases that already carry the
       new shape (idempotent)."""
    LocalStore._add_column_if_missing(conn, "nodes", "list_style", "TEXT")
    schema_columns = {row[1] for row in conn.execute("PRAGMA table_info(property_schema)")}
    if "scope" not in schema_columns:
        # Fresh v16 creates (and databases already carrying the new shape)
        # still gain the columns additively when missing.
        LocalStore._add_column_if_missing(conn, "property_schema", "icon", "TEXT")
        LocalStore._add_column_if_missing(conn, "property_schema", "scope_class_ids", "TEXT")
        LocalStore._add_column_if_missing(conn, "property_schema", "validation_rules", "TEXT")
        return
    conn.executescript(
        """
        PRAGMA foreign_keys = OFF;
        CREATE TABLE property_schema_v16 (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            multi INTEGER NOT NULL DEFAULT 0,
            icon TEXT,
            scope_class_ids TEXT,
            validation_rules TEXT,
            options TEXT NOT NULL DEFAULT '[]',
            target_class_filter TEXT,
            date_precision TEXT,
            date_qualified INTEGER,
            number_pad INTEGER,
            number_decimals INTEGER,
            number_rounding TEXT,
            display TEXT,
            readonly INTEGER,
            hide_when_empty INTEGER,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT,
            updated_at TEXT
        );
        INSERT INTO property_schema_v16 (
            id, workspace_id, name, type, multi, icon, scope_class_ids, validation_rules,
            options, target_class_filter, date_precision, date_qualified,
            number_pad, number_decimals, number_rounding,
            display, readonly, hide_when_empty, active, created_at, updated_at
        )
        SELECT id, workspace_id, name, type, multi, NULL, NULL, NULL,
               options, target_class_filter, date_precision, date_qualified,
               number_pad, number_decimals, number_rounding,
               display, readonly, hide_when_empty, active, created_at, updated_at
        FROM property_schema;
        DROP TABLE property_schema;
        ALTER TABLE property_schema_v16 RENAME TO property_schema;
        PRAGMA foreign_keys = ON;
        """
    )


def _migrate_v17(conn: sqlite3.Connection) -> None:
    """v17 — the binding-flags v14 precedent again (the property-definition surfaces batch; web schema v24→v25
    parity): `required` retires outright and `default_value` MOVES from
    class_property to property_schema (one global default per property, the
    Logseq shape). The binding table REBUILDS without both columns (SQLite
    cannot drop a column on old builds); the schema gains ``default_value``
    additively (NULL = no default). The moved values arrive from the live
    log rewrite (the monorepo's migrate-drop-binding-required.mts, Path B —
    appended propertySchema.update compensation per affected schema) + the
    epoch bump, so this backfill is deliberately NULL. Guarded on the old
    columns so the step is a no-op for databases already carrying the new
    shape (idempotent).
    """
    binding_columns = {row[1] for row in conn.execute("PRAGMA table_info(class_property)")}
    if "required" in binding_columns or "default_value" in binding_columns:
        conn.executescript(
            """
            PRAGMA foreign_keys = OFF;
            CREATE TABLE class_property_v17 (
                class_id TEXT NOT NULL,
                property_schema_id TEXT NOT NULL,
                sequence INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                hlc_physical INTEGER NOT NULL DEFAULT 0,
                hlc_logical INTEGER NOT NULL DEFAULT 0,
                actor_id TEXT,
                PRIMARY KEY (class_id, property_schema_id)
            );
            INSERT INTO class_property_v17 (
                class_id, property_schema_id, sequence, active, hlc_physical, hlc_logical, actor_id
            )
            SELECT class_id, property_schema_id, sequence, active, hlc_physical, hlc_logical, actor_id
            FROM class_property;
            DROP TABLE class_property;
            ALTER TABLE class_property_v17 RENAME TO class_property;
            CREATE INDEX IF NOT EXISTS idx_class_property_class ON class_property (class_id);
            PRAGMA foreign_keys = ON;
            """
        )
    LocalStore._add_column_if_missing(conn, "property_schema", "default_value", "TEXT")


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
    (13, _migrate_v13),
    (14, _migrate_v14),
    (15, _migrate_v15),
    (16, _migrate_v16),
    (17, _migrate_v17),
)
