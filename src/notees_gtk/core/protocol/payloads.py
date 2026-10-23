"""Strict op payload schemas and builders — the zod ``.strict()`` parity layer.

Mirrors ``packages/protocol/src/op-types.ts`` (``OP_PAYLOAD_SCHEMAS``):
every known op type has a pydantic model with ``extra="forbid"`` (an unknown
or renamed wire key fails validation instead of drifting silently), uuid
fields are format-checked, and the ``object.update`` refines ride along
(at least one writable field; exactly one content carrier). This is the
client-side half of the relay's 422 ``validation_failed`` gate: producers
(``new_envelope``, the store outbox) validate before an envelope can leave
the client, and the local appliers validate again at apply time, mirroring
the web store's ``validateEnvelope``.

Revision 11 (render-state model): ``object.create``/``object.update`` dropped
the ``nodeType`` enumeration and gained the optional ``presentAsMain`` render
bit — the retired ``nodeType`` key is rejected outright by these strict
schemas (no wire compat of any kind; the stored log is rewritten in place by
the one-time migration script).

Color grammar: node/class ``color`` is a preset
token or a custom ``#RRGGBB`` hex (``colors.py``, the ``colors.ts`` parity
module), never the retired ``var(--color-preset-*)`` encoding, and payloads
accept an explicit ``null`` clear.

Protocol batch (LOCKSTEP-PENDING wave 2026-10-04): the
workspace-feature op, the ``code_block``/``hr`` grammar (validated by
``content.py``'s strict token validators — the payload layer carries
contentAst as ``list[Any]`` exactly like the zod ``z.array(z.unknown())``),
the ``embed_ref.view`` field, and the property-wire batch — PG5 per-element
value ids (optional ``elementId`` on ``property.set``/``property.unset``;
the row id IS the element id in the store), PC4 ``class.property.set``
``active`` (omitted = keep), PC6 date-node-backed qualifiers (normalize-on-
write in the store applier). Retired feature ids and retired encodings are
rejected outright — no wire compat.

Property display batch:
select option records gained the optional ``icon`` (MDI camelCase name,
max 64; the option record is deliberately NON-strict — additive keys strip
instead of rejecting, so icon-carrying envelopes sync through pre-batch
parsers). The value-display position and the render contracts
(``display``/``readonly``/``hideWhenEmpty``) are PROPERTY-level and live on
``propertySchema.create/update`` (nullable-optional; update-side absent
keeps, null clears) — the binding-level ``display`` on
``class.property.set`` was withdrawn the same day.

Builders are the write-side conveniences (web parity: ``WorkspaceClient``
``createObject``/``createClass``/``reorderClasses``). Title-is-content
(SCHEMA.md, 2026-10-01): no op payload carries a ``name`` — the builders
keep an optional ``name`` parameter that wraps into a single text token
(``[{"type": "text", "text": name}]``) when no explicit ``content_ast`` is
given; when both are given, ``content_ast`` wins and ``name`` is dropped.

Wire-node-fields + asset-type batch (the M27/M12/M38
lockstep): ``object.update`` gains the optional nullable ``coverAssetId``
/ ``bannerAssetId`` / ``aliasedNodeId`` node fields (presence writes,
present-null clears — the ``color`` convention; ``object.create`` carries
none), and the property-schema type enum gains ``asset`` — a node-typed
value whose target must carry the asset class (the filter is implicit in
the type).

Page-subtitle batch (web schema v17→v18 parity): the
wire node fields gain ``description`` — the page subtitle in the core page
chrome (the Capacities header precedent), plain text max 512 chars,
``object.update``-only with the same presence-writes / present-null-clears
semantics.

Trash batch: ``trash.empty`` — the strict empty payload
(the apply-time trash table is the target set; any key is rejected), the
bulk-permanent-delete companion to ``object.delete {permanent: true}``.

List-style batch (web store v22→v23 parity):
``object.create``/``object.update`` gain the optional nullable
``listStyle`` ("numbered"|"alpha"|"roman" — absent/null = bullet; a
present null on update CLEARS, the icon/color precedent).

Property-scope batch:
``propertySchema.create`` RETIRES the display-only ``scope`` enum
(rejected outright by the strict schema) and gains ``icon`` +
``scopeClassIds`` (the sole locality authority — absent/null = global, the
empty array rejected); ``propertySchema.update`` gains ``icon`` /
``scopeClassIds`` / ``targetClassFilter`` / ``multi`` / ``validationRules``
(``{min?, max?, pattern?}`` strict, nullable, keep-vs-clear; min <= max
parse-time). The retired-key fixtures re-encoded.

Binding-defaults batch (the property-definition
surfaces batch): ``class.property.set`` is now STRICTLY
``{classId, propertySchemaId, sequence?, active?}`` — ``required`` retires
outright (its only render effect hid the icon-button's "None" row, mild
enforcement contradicting the render contract) and ``defaultValue`` MOVES
to the property schema: ``propertySchema.create``/``propertySchema.update``
gain it (any JSON value, nullable-optional; update-side absent keeps, null
clears). ONE global default per property, the Logseq shape; PC2 types it
per the schema type at apply (node-typed schemas accept only JSON null).
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from notees_gtk.core.protocol.colors import ColorValue

__all__ = [
    "PAYLOAD_SCHEMAS",
    "build_class_create",
    "build_class_reorder",
    "build_class_unassign",
    "build_class_update",
    "build_external_ref_delete",
    "build_external_ref_set",
    "build_object_create",
    "build_object_delete",
    "build_object_move",
    "build_object_restore",
    "build_object_update",
    "build_tag_unassign",
    "build_trash_empty",
    "build_workspace_feature_set",
    "payload_schema_for",
    "validate_payload",
]

_PROPERTY_TYPE = Literal[
    "text",
    "number",
    "boolean",
    # The unified date property type (SCHEMA.md
    # "Datetime"): the retired `date`/`date_range` types rewrite to
    # `datetime` in live logs (the monorepo's migrate-unified-datetime.mts,
    # Path B); strict schemas reject the retired values outright. A value is
    # a point {nodeId, time?} or a range {start: slot|null, end: slot|null}
    # (slot = {nodeId, time?}) anchored to the year/month/day node chain.
    "datetime",
    "url",
    "email",
    "select",
    "multi_select",
    "object",
    "image",
    # M38: an asset reference — a node-typed value
    # ({nodeId}) whose target MUST carry the asset class; the filter is
    # IMPLICIT in the type (an explicit targetClassFilter is redundant and
    # ignored on asset schemas). The attachments property (…0011) is the
    # first asset-typed schema (retyped from object).
    "asset",
]
_DATE_PRECISION = Literal["year", "month", "day"]
#: Per-block list style (the listStyle wire node field —
#: op-types.ts ``LIST_STYLES``): replaces the bullet dot with a derived
#: marker. The NUMBER is never stored — clients derive it at render time
#: from the contiguous run of same-style siblings. Absent/null = the bullet
#: default; present-null on object.update CLEARS back to the bullet
#: (the icon/color precedent).
_LIST_STYLE = Literal["numbered", "alpha", "roman"]
_NUMBER_ROUNDING = Literal["round", "floor", "ceil", "truncate"]
# The binding's value-display position — where a select/multi_select
# (or boolean) value renders on a block row. A render contract only.
_DISPLAY_POSITION = Literal["panel", "bullet", "inline"]


class _Strict(BaseModel):
    """Base for every payload model: strict keys, camelCase wire names as-is."""

    model_config = ConfigDict(extra="forbid")


class ObjectCreatePayload(_Strict):
    object_id: UUID = Field(alias="objectId")
    # Embedding flag (Revision 12): whether the node's content belongs to its
    # parent's body — true = embedded (the Block role, the DEFAULT for
    # parented nodes), false = a standalone structural element under the
    # parent (the Page role). Unread for parentless nodes (document chrome by
    # the second cascade branch) and classes (ClassView). Optional — the
    # applier defaults it by context: false (standalone) when parentless,
    # true (embedded) otherwise. The retired nodeType and presentAsMain keys
    # are rejected outright by this strict schema.
    is_embedded: bool | None = Field(default=None, alias="isEmbedded")
    class_ids: list[UUID] = Field(default_factory=list, alias="classIds")
    tag_ids: list[UUID] = Field(default_factory=list, alias="tagIds")
    content_ast: list[Any] | None = Field(default=None, alias="contentAst")
    parent_id: UUID | None = Field(default=None, alias="parentId")
    # Initial sibling placement (fractional child order — see object.move):
    # `afterId`/`beforeId` anchor the node next to that current sibling;
    # omit both to append at the end. At most one is meaningful; when both
    # are present `afterId` wins (the TS reference never sends both).
    after_id: UUID | None = Field(default=None, alias="afterId")
    before_id: UUID | None = Field(default=None, alias="beforeId")
    # Initial list style; absent/null = bullet (the default).
    list_style: _LIST_STYLE | None = Field(default=None, alias="listStyle")


class ObjectUpdatePayload(_Strict):
    object_id: UUID = Field(alias="objectId")
    # Embedding-flag toggle (Revision 12): promotion/demotion flips isEmbedded
    # true/false — promotion is the FALSE claim (the Page role; it stringifies
    # the content in the same op, 1→0; demotion never un-flattens). The
    # retired nodeType and presentAsMain keys are rejected outright by this
    # strict schema.
    is_embedded: bool | None = Field(default=None, alias="isEmbedded")
    icon: str | None = Field(default=None, max_length=64)
    # Preset token (`sky`) or custom `#RRGGBB` hex (colors.py grammar);
    # null CLEARS the node's color.
    color: ColorValue = Field(default=None)
    # Archival state (the classes-gallery batch):
    # `false` ARCHIVES the node, `true` UNARCHIVES it — a reversible STATE,
    # distinct from the trash table (deletion: object.delete/object.restore).
    # Platform-fixed, cardinality-1 node fundamental (the icon/color
    # precedent; the class registry `active` is a projection of the class
    # node's bit). Presence writes — absent = no write; the applier refuses
    # an explicit null (zod `z.boolean().optional()` parity — null is not a
    # valid archival write). Class rows mirror the bit to the registry
    # projection (the feature-derivation pattern).
    active: StrictBool | None = Field(default=None)
    # Wire node fields (the icon/color precedent):
    # platform-fixed node fundamentals that core chrome or navigation
    # reads/writes — an asset node for the page cover, an asset node for the
    # page banner, and the main page a node alias points at (many-to-one FROM
    # the alias: a node aliases at most one node). Set via object.update only
    # (object.create carries none); `null` CLEARS. Presence writes, null
    # clears — the applier distinguishes absence (no write) from present-null
    # (SQL NULL), exactly like `color`. Reference integrity (asset existence,
    # alias-chain cycle validation) is a client/read-layer concern — the
    # applier maps the fields; the alias target DOES get a write-time
    # cycle check (the extends-DAG precedent). See SCHEMA.md "Node structure"
    # and "Node aliases" (op-types.ts parity).
    cover_asset_id: UUID | None = Field(default=None, alias="coverAssetId")
    banner_asset_id: UUID | None = Field(default=None, alias="bannerAssetId")
    aliased_node_id: UUID | None = Field(default=None, alias="aliasedNodeId")
    # Page subtitle in the core page chrome (the Capacities header precedent —
    # the same wire-node-field convention): platform-fixed,
    # cardinality-1, plain text, max 512 chars; never a class-bound property.
    # Presence writes, present-null clears, absence preserves.
    description: str | None = Field(default=None, max_length=512)
    # List style; null CLEARS back to the bullet default (like `color`).
    list_style: _LIST_STYLE | None = Field(default=None, alias="listStyle")
    content_delta_b64: str | None = Field(default=None, alias="contentDeltaB64")
    content_ast: list[Any] | None = Field(default=None, alias="contentAst")

    @model_validator(mode="after")
    def _check_writable_and_single_carrier(self) -> ObjectUpdatePayload:
        provided = self.model_fields_set - {"object_id"}
        if not provided:
            raise ValueError("object.update requires at least one field")
        if self.content_delta_b64 is not None and self.content_ast is not None:
            raise ValueError("exactly one content carrier per update")
        return self

    @model_validator(mode="after")
    def _check_active_not_null(self) -> ObjectUpdatePayload:
        # zod `z.boolean().optional()` parity: the archival bit is presence-
        # only — an explicit null is rejected outright, never coerced.
        if "active" in self.model_fields_set and self.active is None:
            raise ValueError("object.update 'active' must be a boolean when present")
        return self


class ObjectDeletePayload(_Strict):
    object_id: UUID = Field(alias="objectId")
    permanent: bool = False


class ObjectRestorePayload(_Strict):
    object_id: UUID = Field(alias="objectId")


class TrashEmptyPayload(_Strict):
    """The ``trash.empty`` payload: a strict empty object — any key is
    rejected outright (zod ``z.object({}).strict()`` parity,
    ``packages/protocol/src/op-types.ts``). The op's target set is evaluated
    at APPLY time against the trash table, never an author-time id list, so
    the payload carries nothing."""


class ObjectMovePayload(_Strict):
    object_id: UUID = Field(alias="objectId")
    parent_id: UUID | None = Field(alias="parentId")
    after_id: UUID | None = Field(default=None, alias="afterId")
    # `beforeId` places the node immediately before that sibling — the
    # first-child placement that afterId-only fractional ordering cannot
    # express. At most one anchor is meaningful; when both are present
    # `afterId` wins.
    before_id: UUID | None = Field(default=None, alias="beforeId")


class ClassCreatePayload(_Strict):
    class_id: UUID = Field(alias="classId")
    content_ast: list[Any] | None = Field(default=None, alias="contentAst")
    icon: str | None = Field(default=None, max_length=64)
    # Preset token or `#RRGGBB` hex (colors.py grammar); null = no color.
    color: ColorValue = Field(default=None)
    description: str | None = Field(default=None, max_length=4096)


class ClassUpdatePayload(_Strict):
    class_id: UUID = Field(alias="classId")
    content_ast: list[Any] | None = Field(default=None, alias="contentAst")
    icon: str | None = Field(default=None, max_length=64)
    # Preset token or `#RRGGBB` hex (colors.py grammar); null clears —
    # the schema now accepts what the catalog always documented.
    color: ColorValue = Field(default=None)
    description: str | None = Field(default=None, max_length=4096)


class ClassUnassignPayload(_Strict):
    object_id: UUID = Field(alias="objectId")
    class_id: UUID = Field(alias="classId")


class ClassReorderPayload(_Strict):
    object_id: UUID = Field(alias="objectId")
    class_ids: list[UUID] = Field(alias="classIds")


class TagUnassignPayload(_Strict):
    object_id: UUID = Field(alias="objectId")
    tag_id: UUID = Field(alias="tagId")


class ClassSetExtendsPayload(_Strict):
    class_id: UUID = Field(alias="classId")
    parent_class_ids: list[UUID] = Field(alias="parentClassIds")


class ClassPropertySetPayload(_Strict):
    """The class → property-schema binding upsert — the genuinely PER-CLASS
    mechanic left: the class's soft-unbind (``active``).

    (the property-definition surfaces batch):
    ``required`` and ``defaultValue`` RETIRE from the binding — ``required``
    is gone outright (its only render effect was mild enforcement,
    contradicting the render-contract law: suggestion only, never
    enforcement) and ``defaultValue`` MOVED to the property schema
    (``propertySchema.create/update``) — ONE global default per property,
    the Logseq shape. The strict schema rejects both keys like any retired
    key. A binding answers "does THIS class list the property"; a schema
    answers "what the property is, how it behaves, and what value it starts
    with everywhere".

   ``sequence`` RETIRES too — class properties
    render as a PURE ALPHABETICAL list everywhere (by property name, at read
    time), so panel order no longer exists to configure. The strict schema
    rejects the key outright; live logs rewrite in place via the monorepo's
    scripts/migrate-drop-binding-sequence.mts.
    """

    class_id: UUID = Field(alias="classId")
    property_schema_id: UUID = Field(alias="propertySchemaId")
    # PC4: the soft-unbind flag — an inactive binding stops
    # contributing to the effective read while the ROW survives. Omitted =
    # keep the stored flag (the patch convention). zod parity: an explicit
    # JSON null is NOT ``undefined`` — the strict schema rejects it.
    active: bool | None = Field(default=None)

    @model_validator(mode="after")
    def _check_active_not_null(self) -> ClassPropertySetPayload:
        if "active" in self.model_fields_set and self.active is None:
            raise ValueError("class.property.set 'active' must be a boolean when present")
        return self


class ClassPropertyUnsetPayload(_Strict):
    class_id: UUID = Field(alias="classId")
    property_schema_id: UUID = Field(alias="propertySchemaId")


class _OptionEntry(BaseModel):
    """A select/multi_select option record (PG16): ``{id, label}``
    plus optional decoration (``color`` in the grammar, ``icon`` — an
    MDI camelCase name, max 64 chars, absent/null = no icon).

    Deliberately NON-strict (the zod ``propertySchemaOptionSchema`` is a
    plain object, not ``.strict()``): web clients send additive keys inside
    options, and unknown keys must STRIP, never reject — a pre-batch parser
    syncs icon-carrying envelopes through (its store drops the decoration;
    wipe → replay restores it). The store appliers serialize the raw
    ``payload["options"]`` verbatim, so a validated icon rides into the
    options JSON automatically.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    label: str
    icon: str | None = Field(default=None, max_length=64)


class PropertySchemaCreatePayload(_Strict):
    property_schema_id: UUID = Field(alias="propertySchemaId")
    name: str = Field(min_length=1, max_length=256)
    type: _PROPERTY_TYPE
    multi: bool = False
    # The schema's icon (the property-definition surfaces
    # batch): an OPTIONAL MDI icon name (camelCase @mdi/js convention,
    # max 64 chars — the same string shape as node/class/option icons);
    # absent/null = no icon.
    icon: str | None = Field(default=None, max_length=64)
    # The SOLE locality authority (R1): absent/null
    # = GLOBAL (any node may carry the property); a non-empty UUID list =
    # LOCAL to those classes AND their extends-ancestors (eligibility walks
    # class_hierarchy the same way binding resolution does). The EMPTY array
    # is rejected — exactly one spelling of "global". This field RETIRES the
    # display-only ``scope: "global"|"class"|"object"`` enum (rejected
    # outright by this strict schema like any retired key; live logs rewrite
    # in place via the monorepo's migrate-property-scope.mts). Bindings
    # (class_property rows) carry ZERO availability semantics — scope says
    # who MAY carry the property; a binding says what a class DOES WITH it.
    scope_class_ids: list[UUID] | None = Field(default=None, alias="scopeClassIds")
    options: list[_OptionEntry] | None = None
    target_class_filter: list[UUID] | None = Field(default=None, alias="targetClassFilter")
    date_precision: _DATE_PRECISION | None = Field(default=None, alias="datePrecision")
    date_qualified: bool | None = Field(default=None, alias="dateQualified")
    # SCHEMA.md "Number formats": display-only formatting for number schemas
    # (values stay exact; these shape render only). Lockstep with the TS
    # reference: additive-optional, nullable.
    number_pad: int | None = Field(default=None, alias="numberPad", ge=1, le=20)
    number_decimals: int | None = Field(default=None, alias="numberDecimals", ge=0, le=10)
    number_rounding: _NUMBER_ROUNDING | None = Field(default=None, alias="numberRounding")
    # The property's DEFAULT VALUE (moved from the
    # class binding to the schema, the Logseq shape): ONE global default per
    # property, not per class. Typed per the schema type (PC2, validated at
    # apply — node-typed schemas (datetime/object/asset) accept only JSON
    # null, "a default that links a node is meaningless"). Absent = no
    # default (NULL column). Editing a default in the UI writes an authored
    # property.set on the node, which from then on shadows the default.
    default_value: Any = Field(default=None, alias="defaultValue")
    # The render contracts are PROPERTY-level —
    # a property displays/behaves the same everywhere it
    # appears, whatever class binds it (or none). ``display`` is the
    # value-display position ("panel" (absent/null) keeps the value in the
    # properties section only; "bullet" renders it as an icon button next to
    # the block bullet; "inline" before the block content — the
    # icon_visibility port); ``hideWhenEmpty`` is the tri-state render flag.
    # All nullable-optional (absent or null stores NULL).
    # ``required`` is NOT here — it retired outright (its only render effect was mild enforcement); the default
    # lives HERE, not on the binding. ``readonly`` is NOT here either — it
    # retired outright (never user
    # configuration; clients derive it from the system property list,
    # ``is_readonly_property`` in features.py); the strict schema rejects
    # the key like any retired key.
    display: _DISPLAY_POSITION | None = Field(default=None)
    hide_when_empty: bool | None = Field(default=None, alias="hideWhenEmpty")

    @model_validator(mode="after")
    def _check_scope_class_ids_not_empty(self) -> PropertySchemaCreatePayload:
        # The empty array is rejected (the zod ``.min(1)`` parity) — exactly
        # one spelling of "global".
        if self.scope_class_ids is not None and len(self.scope_class_ids) == 0:
            raise ValueError("propertySchema.create 'scopeClassIds' must be a non-empty list when present")
        return self


class _ValidationRules(_Strict):
    """Value validation rules (v1 parity — op-types.ts
    ``validationRulesSchema``): ``{min?, max?, pattern?}``, all optional.
    min/max apply to number schemas, pattern to text/url/email; the
    applicability check runs at APPLY time against the schema's stored type
    and ``min <= max`` is a parse-time rule."""

    min: float | None = None
    max: float | None = None
    pattern: str | None = None

    @model_validator(mode="after")
    def _check_min_le_max(self) -> _ValidationRules:
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("validationRules: min must be <= max when both are present")
        return self


class PropertySchemaUpdatePayload(_Strict):
    property_schema_id: UUID = Field(alias="propertySchemaId")
    name: str | None = Field(default=None, min_length=1, max_length=256)
    # The keep-vs-clear contract: absent keeps the stored
    # value, a present null clears back to NULL/global/none — the same shape
    # as the number formats below (the applier distinguishes absence from
    # null by key presence, the zod ``!== undefined`` parity).
    icon: str | None = Field(default=None, max_length=64)
    scope_class_ids: list[UUID] | None = Field(default=None, alias="scopeClassIds")
    # Node-typed (m2o/m2m) target classes; absent keeps, null clears the
    # filter (an empty array means "no filter").
    target_class_filter: list[UUID] | None = Field(default=None, alias="targetClassFilter")
    # Values single/multiple; absent keeps, null clears back to single.
    multi: bool | None = Field(default=None)
    # Value validation rules: absent keeps the stored rules, null clears
    # them. Applicability is enforced at APPLY time against the schema's
    # stored type; values are validated against the stored rules at
    # property.set (integrity, fail loud, SCHEMA.md "Properties").
    validation_rules: _ValidationRules | None = Field(default=None, alias="validationRules")
    options: list[_OptionEntry] | None = None
    date_precision: _DATE_PRECISION | None = Field(default=None, alias="datePrecision")
    date_qualified: bool | None = Field(default=None, alias="dateQualified")
    # Absent keeps the stored value; explicit null clears it (the keep-vs-clear
    # contract — the applier distinguishes absence from null by key presence).
    number_pad: int | None = Field(default=None, alias="numberPad", ge=1, le=20)
    number_decimals: int | None = Field(default=None, alias="numberDecimals", ge=0, le=10)
    number_rounding: _NUMBER_ROUNDING | None = Field(default=None, alias="numberRounding")
    # The default value (create-side doc above): absent keeps the stored
    # value, present-null clears it (the same keep/clear contract as the
    # number formats). Applicability + the PC2 type-check run at apply time
    # against the schema's STORED type, exactly like validationRules — the
    # update carries no type; a schema-less write (unknown id) stores
    # unchecked.
    default_value: Any = Field(default=None, alias="defaultValue")
    # Render contracts (PROPERTY-level): same absent-keeps /
    # null-clears contract as the number formats — `required` retired from
    # the binding outright; the default
    # lives here. `readonly` retired outright 2026-10-21 (derived from the
    # system list, not stored) — rejected like any retired key.
    display: _DISPLAY_POSITION | None = Field(default=None)
    hide_when_empty: bool | None = Field(default=None, alias="hideWhenEmpty")

    @model_validator(mode="after")
    def _check_scope_class_ids_not_empty(self) -> PropertySchemaUpdatePayload:
        # Same rejection as create (the zod ``.min(1)`` parity).
        if self.scope_class_ids is not None and len(self.scope_class_ids) == 0:
            raise ValueError("propertySchema.update 'scopeClassIds' must be a non-empty list when present")
        return self


class PropertySchemaDeletePayload(_Strict):
    property_schema_id: UUID = Field(alias="propertySchemaId")


class PropertySetPayload(_Strict):
    object_id: UUID = Field(alias="objectId")
    property_schema_id: UUID = Field(alias="propertySchemaId")
    value: Any
    # PG5 element id: the OR-Set add carrier for multi-value slots —
    # the property_value row id IS the element id. Absent = the legacy
    # positional carrier (addresses the deterministic positional element at
    # ``idx``). zod parity: an explicit null is rejected, not "absent".
    element_id: UUID | None = Field(default=None, alias="elementId")
    idx: int = Field(default=0, ge=0)
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check_element_id_not_null(self) -> PropertySetPayload:
        if "element_id" in self.model_fields_set and self.element_id is None:
            raise ValueError("property.set 'elementId' must be a uuid when present")
        return self


class PropertyUnsetPayload(_Strict):
    object_id: UUID = Field(alias="objectId")
    property_schema_id: UUID = Field(alias="propertySchemaId")
    # PG5 element id — OR-Set remove of that element (add-wins tombstone).
    # Absent = legacy positional remove of the slot's deterministic
    # positional element at ``idx``.
    element_id: UUID | None = Field(default=None, alias="elementId")
    idx: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _check_element_id_not_null(self) -> PropertyUnsetPayload:
        if "element_id" in self.model_fields_set and self.element_id is None:
            raise ValueError("property.unset 'elementId' must be a uuid when present")
        return self


class AssetAttachPayload(_Strict):
    object_id: UUID = Field(alias="objectId")
    asset_id: UUID = Field(alias="assetId")
    hash: str = Field(min_length=64, max_length=64)
    mime_type: str = Field(min_length=1, alias="mimeType")
    size: int = Field(ge=0)
    original_name: str = Field(min_length=1, max_length=1024, alias="originalName")


class AssetDetachPayload(_Strict):
    object_id: UUID = Field(alias="objectId")
    asset_id: UUID = Field(alias="assetId")


class CollectionMemberAddPayload(_Strict):
    collection_id: UUID = Field(alias="collectionId")
    object_id: UUID = Field(alias="objectId")


class CollectionMemberRemovePayload(_Strict):
    collection_id: UUID = Field(alias="collectionId")
    object_id: UUID = Field(alias="objectId")


class WorkspaceFeatureSetPayload(_Strict):
    """Per-workspace feature toggle (RESHAPED).

    ``feature`` is fixed protocol vocabulary — the five core class families
    (tasks=task, events=event, meetings=meeting, sources=source,
    persons=person) plus ``journal`` (the
    surfaces-only toggle: the journal nav view + Today-page management; the
    date classes stay always-on structural seeds and it derives no class
    bits). The retired pre-reshape ids (journals/readItLater/library/
    people/collections) are rejected outright by this strict schema — no
    wire compat. LWW by envelope HLC on (workspace,
    feature); the derived ``workspace_feature`` table stores the winning row
    and an ABSENT row means enabled (F2).

    The optional ``settings`` record (op-types.ts ``settings:
    z.record(z.unknown()).optional()`` — the Sources
    library's citekey config et al.) is the per-feature configuration blob:
    record-shaped, feature-scoped vocabulary — the protocol pins only "a JSON
    object". It rides the same whole-row LWW: a payload WITHOUT ``settings``
    stores NULL and reads back absent (distinct from the empty record).
    """

    feature: Literal["tasks", "events", "meetings", "sources", "persons", "journal"]
    enabled: bool
    # zod parity: an explicit null is rejected, not "absent" (the
    # PropertySetPayload elementId precedent).
    settings: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check_settings_not_null(self) -> WorkspaceFeatureSetPayload:
        if "settings" in self.model_fields_set and self.settings is None:
            raise ValueError("workspace.feature.set 'settings' must be a record when present")
        return self


#: The external-ref key cap (the ``EXTERNAL_REF_MAX_LENGTH`` op-types.ts
#: parity): ``ref`` is the caller-namespaced ``<provider>:<kind>:<externalId>``
#: key — opaque to the protocol — capped at 512 chars; empty/oversize fail loud.
EXTERNAL_REF_MAX_LENGTH = 512


class ExternalRefSetPayload(_Strict):
    """External-id mapping write (the importer/
    sync-plugin substrate, SCHEMA.md "External-id mappings").

    One LIVE mapping per ``ref``, LWW by envelope ``(hlc, actor)`` on the
    ``(workspace, ref)`` key — a re-set MOVES the mapping and racing sets
    converge on the higher tuple. ``nodeId`` is a bare uuid with NO
    referential integrity (the ``property_value`` precedent): the mapping may
    name a node another device has not synced yet.
    """

    ref: str = Field(min_length=1, max_length=EXTERNAL_REF_MAX_LENGTH)
    node_id: UUID = Field(alias="nodeId")


class ExternalRefDeletePayload(_Strict):
    """The LWW delete side of the ref key — a tombstone per ``(workspace,
    ref)`` lands even for an UNMAPPED ref, killing a stale concurrent set
    delivered after the winning delete on every replica."""

    ref: str = Field(min_length=1, max_length=EXTERNAL_REF_MAX_LENGTH)


#: Registry mirroring ``OP_PAYLOAD_SCHEMAS`` (op-types.ts) one-for-one.
PAYLOAD_SCHEMAS: dict[str, type[BaseModel]] = {
    "object.create": ObjectCreatePayload,
    "object.update": ObjectUpdatePayload,
    "object.delete": ObjectDeletePayload,
    "object.restore": ObjectRestorePayload,
    "trash.empty": TrashEmptyPayload,
    "object.move": ObjectMovePayload,
    "class.create": ClassCreatePayload,
    "class.update": ClassUpdatePayload,
    "class.unassign": ClassUnassignPayload,
    "class.reorder": ClassReorderPayload,
    "tag.unassign": TagUnassignPayload,
    "class.setExtends": ClassSetExtendsPayload,
    "class.property.set": ClassPropertySetPayload,
    "class.property.unset": ClassPropertyUnsetPayload,
    "propertySchema.create": PropertySchemaCreatePayload,
    "propertySchema.update": PropertySchemaUpdatePayload,
    "propertySchema.delete": PropertySchemaDeletePayload,
    "property.set": PropertySetPayload,
    "property.unset": PropertyUnsetPayload,
    "asset.attach": AssetAttachPayload,
    "asset.detach": AssetDetachPayload,
    "collection.member.add": CollectionMemberAddPayload,
    "collection.member.remove": CollectionMemberRemovePayload,
    "workspace.feature.set": WorkspaceFeatureSetPayload,
    "external_ref.set": ExternalRefSetPayload,
    "external_ref.delete": ExternalRefDeletePayload,
}


def payload_schema_for(op_type: str) -> type[BaseModel] | None:
    """Return the strict payload model for ``op_type`` (``None`` when unknown)."""
    return PAYLOAD_SCHEMAS.get(op_type)


def validate_payload(op_type: str, payload: dict[str, Any]) -> None:
    """Validate ``payload`` against the strict schema for ``op_type``.

    Raises:
        ValueError: Unknown op type, or the payload deviates from the wire
            schema (extra keys — e.g. a retired ``name`` field —, bad uuid
            shapes, violated refines). The message names the deviation.
    """
    schema = PAYLOAD_SCHEMAS.get(op_type)
    if schema is None:
        raise ValueError(f"unknown opType: {op_type}")
    schema.model_validate(payload)


# --------------------------------------------------------------------- builders

_UNSET: Any = object()


def _validated(op_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Gate a builder's output through the strict schema (fail loud)."""
    validate_payload(op_type, payload)
    return payload


def build_object_create(
    object_id: str,
    *,
    is_embedded: bool | None = None,
    class_ids: list[str] | None = None,
    tag_ids: list[str] | None = None,
    name: str | None = None,
    content_ast: list[Any] | None = None,
    parent_id: str | None | object = _UNSET,
    after_id: str | None = None,
    before_id: str | None = None,
    list_style: str | None = None,
) -> dict[str, Any]:
    """Build an ``object.create`` payload (web ``WorkspaceClient.createObject``).

    Title-is-content: the protocol has no object ``name``. The ``name``
    convenience becomes the node's initial text content (a single text
    token) when no explicit ``content_ast`` is given; when both are given,
    ``content_ast`` wins and ``name`` is dropped. ``is_embedded`` is the
    Revision-12 embedding flag; omit it and the applier defaults it by context
    (false/standalone when parentless, true/embedded otherwise).
    """
    payload: dict[str, Any] = {"objectId": object_id}
    if is_embedded is not None:
        payload["isEmbedded"] = is_embedded
    if class_ids is not None:
        payload["classIds"] = list(class_ids)
    if tag_ids is not None:
        payload["tagIds"] = list(tag_ids)
    initial_text = [{"type": "text", "text": name}] if name is not None and content_ast is None else None
    if initial_text is not None:
        payload["contentAst"] = initial_text
    if content_ast is not None:
        payload["contentAst"] = content_ast
    if parent_id is not _UNSET:
        payload["parentId"] = parent_id
    if after_id is not None:
        payload["afterId"] = after_id
    if before_id is not None:
        payload["beforeId"] = before_id
    if list_style is not None:
        payload["listStyle"] = list_style
    return _validated("object.create", payload)


def build_object_update(
    object_id: str,
    *,
    is_embedded: bool | None = None,
    content_ast: list[Any] | None = None,
    content_delta_b64: str | None = None,
    icon: str | None = None,
    color: str | None | object = _UNSET,
    cover_asset_id: str | None | object = _UNSET,
    banner_asset_id: str | None | object = _UNSET,
    aliased_node_id: str | None | object = _UNSET,
    description: str | None | object = _UNSET,
    list_style: str | None | object = _UNSET,
    active: bool | None = None,
) -> dict[str, Any]:
    """Build an ``object.update`` payload (at least one field required).

    ``color=None`` sends an explicit null that CLEARS the node's color
    (the UI's "No color"); omitting ``color`` leaves it untouched. The wire
    node fields (``cover_asset_id`` / ``banner_asset_id`` /
    ``aliased_node_id`` / ``description``) follow the same convention:
    ``None`` sends an explicit null that CLEARS the column, omitting leaves
    it untouched. ``list_style`` rides the same nullable contract. The
    archival bit is presence-only: ``active=False`` archives, ``active=True``
    unarchives, omitting leaves the bit untouched (an explicit null is
    rejected by the strict schema).
    """
    payload: dict[str, Any] = {"objectId": object_id}
    if is_embedded is not None:
        payload["isEmbedded"] = is_embedded
    if content_ast is not None:
        payload["contentAst"] = content_ast
    if content_delta_b64 is not None:
        payload["contentDeltaB64"] = content_delta_b64
    if icon is not None:
        payload["icon"] = icon
    if color is not _UNSET:
        payload["color"] = color
    if cover_asset_id is not _UNSET:
        payload["coverAssetId"] = cover_asset_id
    if banner_asset_id is not _UNSET:
        payload["bannerAssetId"] = banner_asset_id
    if aliased_node_id is not _UNSET:
        payload["aliasedNodeId"] = aliased_node_id
    if description is not _UNSET:
        payload["description"] = description
    if list_style is not _UNSET:
        payload["listStyle"] = list_style
    if active is not None:
        payload["active"] = active
    return _validated("object.update", payload)


def build_object_delete(object_id: str, *, permanent: bool = False) -> dict[str, Any]:
    """Build an ``object.delete`` payload (soft delete unless ``permanent``)."""
    return _validated("object.delete", {"objectId": object_id, "permanent": permanent})


def build_object_restore(object_id: str) -> dict[str, Any]:
    """Build an ``object.restore`` payload (whole-tree trash restore)."""
    return _validated("object.restore", {"objectId": object_id})


def build_trash_empty() -> dict[str, Any]:
    """Build a ``trash.empty`` payload — strictly ``{}`` (the apply-time trash
    table is the target set; nothing rides the wire)."""
    return _validated("trash.empty", {})


def build_object_move(
    object_id: str,
    parent_id: str | None,
    *,
    after_id: str | None = None,
    before_id: str | None = None,
) -> dict[str, Any]:
    """Build an ``object.move`` payload (``parent_id`` None = workspace root)."""
    payload: dict[str, Any] = {"objectId": object_id, "parentId": parent_id}
    if after_id is not None:
        payload["afterId"] = after_id
    if before_id is not None:
        payload["beforeId"] = before_id
    return _validated("object.move", payload)


def build_class_create(
    class_id: str,
    *,
    name: str | None = None,
    content_ast: list[Any] | None = None,
    icon: str | None = None,
    color: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Build a ``class.create`` payload (web ``WorkspaceClient.createClass``).

    The class's title IS its (text-only) content: the ``name`` convenience
    wraps into a single text token unless an explicit ``content_ast`` wins.
    """
    payload: dict[str, Any] = {"classId": class_id}
    if name is not None and content_ast is None:
        payload["contentAst"] = [{"type": "text", "text": name}]
    if content_ast is not None:
        payload["contentAst"] = content_ast
    if icon is not None:
        payload["icon"] = icon
    if color is not None:
        payload["color"] = color
    if description is not None:
        payload["description"] = description
    return _validated("class.create", payload)


def build_class_update(
    class_id: str,
    *,
    name: str | None = None,
    content_ast: list[Any] | None = None,
    icon: str | None = None,
    color: str | None | object = _UNSET,
    description: str | None = None,
) -> dict[str, Any]:
    """Build a ``class.update`` payload (same name convenience as create).

    ``color=None`` sends an explicit null that CLEARS the class color;
    omitting ``color`` leaves it untouched.
    """
    payload: dict[str, Any] = {"classId": class_id}
    if name is not None and content_ast is None:
        payload["contentAst"] = [{"type": "text", "text": name}]
    if content_ast is not None:
        payload["contentAst"] = content_ast
    if icon is not None:
        payload["icon"] = icon
    if color is not _UNSET:
        payload["color"] = color
    if description is not None:
        payload["description"] = description
    return _validated("class.update", payload)


def build_class_unassign(object_id: str, class_id: str) -> dict[str, Any]:
    """Build a ``class.unassign`` payload (OR-Set remove complement)."""
    return _validated("class.unassign", {"objectId": object_id, "classId": class_id})


def build_class_reorder(object_id: str, class_ids: list[str]) -> dict[str, Any]:
    """Build a ``class.reorder`` payload (web ``WorkspaceClient.reorderClasses``).

    Display-only user order, LWW-by-arrival: the full ordered member list;
    the applier keeps ordered members first and appends any unlisted present
    members sorted by id.
    """
    return _validated("class.reorder", {"objectId": object_id, "classIds": list(class_ids)})


def build_tag_unassign(object_id: str, tag_id: str) -> dict[str, Any]:
    """Build a ``tag.unassign`` payload (OR-Set remove, own table)."""
    return _validated("tag.unassign", {"objectId": object_id, "tagId": tag_id})


def build_workspace_feature_set(feature: str, enabled: bool) -> dict[str, Any]:
    """Build a ``workspace.feature.set`` payload (the Features tab).

    ``feature`` is one of the five core class families or ``journal`` (the
    ``WORKSPACE_FEATURES`` enum in ``features.py`` — the journal entry gates
    the journal surfaces only, no class bits); the strict schema rejects the
    retired pre-reshape ids (journals/readItLater/library/people/collections)
    outright. LWW by HLC on (workspace, feature); an absent derived row reads
    enabled (F2).
    """
    return _validated("workspace.feature.set", {"feature": feature, "enabled": enabled})


def build_external_ref_set(ref: str, node_id: str) -> dict[str, Any]:
    """Build an ``external_ref.set`` payload (importer/sync-plugin id mapping).

    ``ref`` is the caller-namespaced ``<provider>:<kind>:<externalId>`` key
    (opaque to the protocol, 1..512 chars); ``node_id`` is the mapped node —
    no referential integrity on the wire (the ``property_value`` precedent).
    One LIVE mapping per ref: LWW by envelope ``(hlc, actor)``, a re-set moves.
    """
    return _validated("external_ref.set", {"ref": ref, "nodeId": node_id})


def build_external_ref_delete(ref: str) -> dict[str, Any]:
    """Build an ``external_ref.delete`` payload — the LWW delete side; the
    derived tombstone lands even when the ref is unmapped."""
    return _validated("external_ref.delete", {"ref": ref})
