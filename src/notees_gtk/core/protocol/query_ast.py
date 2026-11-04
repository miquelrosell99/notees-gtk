"""QueryAST v2 — the canonical, serializable query model (strict parse).

Port of ``packages/protocol/src/query-ast.ts`` (the zod model) to frozen
dataclasses + hand-rolled strict validators. The TS reference is wire-adjacent
protocol: the content grammar's ``query`` token embeds this AST, so the model
lives here in ``core/protocol/`` rather than with the compiler.

Design laws (carried over verbatim from the reference):

- the AST is the source of truth — UI and SQL are projections of it;
- the AST is versioned (``version: 2``) and evolves by versioned extension —
  unknown condition types, families, ops, target modes, or versions FAIL LOUD
  at parse time (zod ``.strict()`` parity: exact key sets, no extra keys, no
  coercion);
- there is NO earlier-version parse path: ``version: 1`` is rejected outright
  (the stored log converges by a one-time in-place rewrite, never dual-parse);
- the ``self`` member target mode names the query's HOSTING PAGE, resolved at
  COMPILE time against the run site's ``self_node_id`` — a compile without it
  fails loud (see :mod:`notees_gtk.core.query.compiler`).

Hand-rolled validators instead of pydantic: zod ``.strict()`` REJECTS wrong
types (``z.literal(2)`` rejects ``"2"``, ``z.enum`` rejects unknown members,
``z.string()`` rejects numbers) while pydantic's default lax mode coerces —
the fail-loud contract needs the zod behavior, so validation is explicit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, NoReturn, cast

__all__ = [
    "Aggregation",
    "AggregationDimension",
    "AggregationMeasure",
    "Child",
    "Condition",
    "ContentCondition",
    "ContentOp",
    "DateCondition",
    "DateOp",
    "DatePeriod",
    "EntireWorkspaceScope",
    "Group",
    "LinkedToScope",
    "MemberCondition",
    "MemberFamily",
    "MemberOp",
    "NameCondition",
    "NameOp",
    "NodeFieldCondition",
    "NodeFieldOp",
    "Not",
    "PagesScope",
    "PropertyCondition",
    "PropertyOp",
    "QueryAst",
    "QueryAstError",
    "QueryTarget",
    "Scope",
    "SelfTarget",
    "IdsTarget",
    "SortSpec",
    "SubtreeScope",
    "TargetRef",
    "parse_query_ast",
]

#: zod ``z.string().uuid()`` is a canonical-shape regex accepting any version
#: (NOT ``uuid.UUID``, which also accepts braces/urn prefixes — zod does not).
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

#: The strict date pattern the ISO-date compiler arms recognize.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class QueryAstError(ValueError):
    """A serialized query AST failed strict validation (zod parse parity)."""


# --- scope ---------------------------------------------------------------------


@dataclass(frozen=True)
class EntireWorkspaceScope:
    """Every active node in the workspace."""


@dataclass(frozen=True)
class PagesScope:
    """Only nodes rendering with document chrome (the page-role predicate)."""


@dataclass(frozen=True)
class SubtreeScope:
    """The page plus every node under it (recursive parent_id walk)."""

    page_id: str


@dataclass(frozen=True)
class LinkedToScope:
    """Roll-up membership: backlinksWithRollup semantics (direct + containment)."""

    node_id: str


Scope = EntireWorkspaceScope | PagesScope | SubtreeScope | LinkedToScope

# --- target reference ------------------------------------------------------------
# The target mode axis: `ids` manual / `query` nested / `self` hosting page.


@dataclass(frozen=True)
class IdsTarget:
    """A manual pick (class family → class ids, tags → tag ids, every other
    family → node ids)."""

    ids: tuple[str, ...]


@dataclass(frozen=True)
class QueryTarget:
    """The target set defined dynamically through a nested group (a saved view
    re-resolves on every run, never baked uuids)."""

    root: Group


@dataclass(frozen=True)
class SelfTarget:
    """The HOSTING PAGE, resolved at compile time against the run site's
    ``self_node_id`` (a self target compiling without it fails loud)."""


TargetRef = IdsTarget | QueryTarget | SelfTarget

# --- group / not -------------------------------------------------------------------


@dataclass(frozen=True)
class Group:
    """A boolean group over children (conditions, nested groups, nots)."""

    logic: Literal["and", "or"]
    children: tuple[Child, ...]


@dataclass(frozen=True)
class Not:
    """The free-form negation wrapper (``not-in``/``not-exists`` are the
    wire-level comparators; this sugars everything else)."""

    child: Condition | Group


# --- conditions ------------------------------------------------------------------

#: The ten node-target families (the family ↔ semantics table lives in
#: ``packages/query/README.md`` — the compiler contract).
MemberFamily = Literal[
    "class",
    "tags",
    "links",
    "referencePath",
    "parent",
    "child",
    "childPath",
    "page",
    "parentPath",
    "root",
]
_MEMBER_FAMILIES = frozenset(
    {"class", "tags", "links", "referencePath", "parent", "child", "childPath", "page", "parentPath", "root"}
)

#: The member ops: `in`/`not-in` ride a target, `exists`/`not-exists` forbid
#: one, class-only `exact` pins a single-id ids target.
MemberOp = Literal["in", "not-in", "exists", "not-exists", "exact"]

#: The plaintext condition ops (contains/fts/eq/neq/starts/ends + folded exists).
ContentOp = Literal["contains", "fts", "eq", "neq", "starts", "ends", "exists"]
_CONTENT_OPS = frozenset({"contains", "fts", "eq", "neq", "starts", "ends", "exists"})

#: The title-condition operators (the ``name:`` DSL field).
NameOp = Literal["eq", "neq", "contains", "exists"]
_NAME_OPS = frozenset({"eq", "neq", "contains", "exists"})

#: The property ops: the value-level set plus the folded between/within.
PropertyOp = Literal["eq", "neq", "contains", "exists", "gt", "gte", "lt", "lte", "between", "within"]
_PROPERTY_OPS = frozenset({"eq", "neq", "contains", "exists", "gt", "gte", "lt", "lte", "between", "within"})

#: The relative periods ("is within today / this week / this month / this year").
DatePeriod = Literal["today", "this_week", "this_month", "this_year"]
_DATE_PERIODS = frozenset({"today", "this_week", "this_month", "this_year"})

#: The base date-comparison ops (eq/neq are DAY-granular, gt..lte instant-based).
DateOp = Literal["eq", "neq", "gt", "gte", "lt", "lte", "after", "before", "between", "within"]
_DATE_OPS = frozenset({"eq", "neq", "gt", "gte", "lt", "lte", "after", "before", "between", "within"})

#: The node wire-field ops (eq/neq against an id; exists the set/unset bit —
#: isClass/isEmbedded ride op ``eq`` with value "true"/"false").
NodeFieldOp = Literal["eq", "neq", "exists"]
_NODE_FIELD_OPS = frozenset({"eq", "neq", "exists"})

_NODE_FIELD_NAMES = frozenset({"coverAsset", "bannerAsset", "aliasedNode", "isClass", "isEmbedded"})


@dataclass(frozen=True)
class MemberCondition:
    """The node-target condition — ONE type over ten families (family × op ×
    target-mode axes made explicit; the parse layer enforces the op/target
    coupling structurally, like the zod union of three strict sub-shapes)."""

    family: MemberFamily
    op: MemberOp
    target: TargetRef | None = None


@dataclass(frozen=True)
class ContentCondition:
    """The plaintext condition over the derived search text (contains/fts/eq/
    neq/starts/ends; ``exists`` reads "the plaintext is non-empty")."""

    op: ContentOp
    value: str | None = None


@dataclass(frozen=True)
class NameCondition:
    """The title condition (title-is-content: the flattened content text)."""

    op: NameOp
    value: str | None = None


@dataclass(frozen=True)
class PropertyCondition:
    """A typed-property condition over the effective-values read model.

    ``value`` carries the comparison bound (eq/neq/contains/gt/gte/lt/lte),
    ``from_``/``to`` the inclusive between range, ``period`` the within
    period. ``include_defaults`` (default true) opts out of the
    bindings-derived-default arm.
    """

    schema_id: str
    op: PropertyOp
    value: Any = None
    from_: Any = None
    to: Any = None
    period: DatePeriod | None = None
    include_defaults: bool | None = None


@dataclass(frozen=True)
class DateCondition:
    """The created/updated facet condition (the explicit ``field`` axis)."""

    field: Literal["created", "updated"]
    op: DateOp
    timestamp: str | None = None
    from_: str | None = None
    to: str | None = None
    period: DatePeriod | None = None


@dataclass(frozen=True)
class NodeFieldCondition:
    """A wire-field predicate over the node-table columns (coverAsset/
    bannerAsset/aliasedNode eq/neq/exists; isClass/isEmbedded ride eq with
    the Revision-12 booleans as "true"/"false")."""

    field: Literal["coverAsset", "bannerAsset", "aliasedNode", "isClass", "isEmbedded"]
    op: NodeFieldOp
    value: str | None = None


Condition = MemberCondition | ContentCondition | NameCondition | PropertyCondition | DateCondition | NodeFieldCondition
Child = Condition | Group | Not

# --- sort / aggregation -------------------------------------------------------------


@dataclass(frozen=True)
class SortSpec:
    """One ORDER BY term (the compiler appends the deterministic id tiebreak)."""

    field: Literal["name", "createdAt", "isClass", "isEmbedded"]
    dir: Literal["asc", "desc"]


@dataclass(frozen=True)
class AggregationDimension:
    """One GROUP BY dimension: class membership (hierarchy-aware), a property's
    effective value at idx 0, or the is_class/is_embedded bit."""

    kind: Literal["class", "property", "isClass", "isEmbedded"]
    id: str | None = None


@dataclass(frozen=True)
class AggregationMeasure:
    """One aggregate measure: count/countDistinct over nodes, or sum/avg/min/
    max over a property's effective value."""

    function: Literal["count", "countDistinct", "sum", "avg", "min", "max"]
    kind: Literal["node", "property"] | None = None
    id: str | None = None


@dataclass(frozen=True)
class Aggregation:
    """The grouped-grid shape: dimensions GROUP BY the filtered set, measures
    aggregate over it."""

    dimensions: tuple[AggregationDimension, ...]
    measures: tuple[AggregationMeasure, ...]


# --- root ----------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryAst:
    """The versioned root: scope + condition tree + optional sort/aggregation.

    ``version`` is the literal 2 — the parse layer rejects every other value
    outright (no backward compatibility; older ASTs converge by log rewrite).
    """

    scope: Scope
    root: Group
    sort: tuple[SortSpec, ...] | None = None
    aggregation: Aggregation | None = None
    version: Literal[2] = 2


# --- strict parse ---------------------------------------------------------------------


def _fail(message: str) -> NoReturn:
    raise QueryAstError(message)


def _require_object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"query AST: expected an object for {what}, got {type(value).__name__}")
    return cast(dict[str, Any], value)


def _check_keys(raw: dict[str, Any], allowed: set[str], what: str, required: set[str] | None = None) -> None:
    extra = set(raw) - allowed
    if extra:
        _fail(f"query AST: unexpected keys {sorted(extra)} on {what} (strict schema)")
    missing = (allowed if required is None else required) - set(raw)
    if missing:
        _fail(f"query AST: missing keys {sorted(missing)} on {what}")


def _require_str(value: Any, what: str) -> str:
    if not isinstance(value, str):
        _fail(f"query AST: expected a string for {what}, got {type(value).__name__}")
    return cast(str, value)


def _require_nonempty_str(value: Any, what: str) -> str:
    text = _require_str(value, what)
    if not text:
        _fail(f"query AST: expected a non-empty string for {what}")
    return text


def _require_uuid(value: Any, what: str) -> str:
    text = _require_str(value, what)
    if _UUID_RE.match(text) is None:
        _fail(f"query AST: expected a uuid for {what}, got {text!r}")
    return text


def _require_bool(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"query AST: expected a boolean for {what}, got {type(value).__name__}")
    return cast(bool, value)


def _require_enum(value: Any, allowed: frozenset[str], what: str) -> str:
    text = _require_str(value, what)
    if text not in allowed:
        _fail(f"query AST: unknown {what} {text!r} (expected one of {sorted(allowed)})")
    return text


def _optional_uuid(raw: dict[str, Any], key: str, what: str) -> str | None:
    if key not in raw:
        return None
    return _require_uuid(raw[key], f"{what}.{key}")


def _parse_scope(raw: Any) -> Scope:
    scope = _require_object(raw, "scope")
    scope_type = _require_enum(scope.get("type"), frozenset({"entire_workspace", "pages", "subtree", "linkedTo"}), "scope type")
    if scope_type == "entire_workspace":
        _check_keys(scope, {"type"}, f"scope {scope_type!r}")
        return EntireWorkspaceScope()
    if scope_type == "pages":
        _check_keys(scope, {"type"}, f"scope {scope_type!r}")
        return PagesScope()
    if scope_type == "subtree":
        _check_keys(scope, {"type", "pageId"}, f"scope {scope_type!r}")
        return SubtreeScope(page_id=_require_uuid(scope.get("pageId"), "scope.pageId"))
    _check_keys(scope, {"type", "nodeId"}, f"scope {scope_type!r}")
    return LinkedToScope(node_id=_require_uuid(scope.get("nodeId"), "scope.nodeId"))


def _parse_target(raw: Any, what: str) -> TargetRef:
    target = _require_object(raw, what)
    mode = _require_enum(target.get("mode"), frozenset({"ids", "query", "self"}), "target mode")
    if mode == "ids":
        _check_keys(target, {"mode", "ids"}, "target ids")
        ids_raw = target.get("ids")
        if not isinstance(ids_raw, list):
            _fail(f"query AST: expected an array for target.ids, got {type(ids_raw).__name__}")
        if not ids_raw:
            _fail("query AST: target.ids requires at least one id")
        return IdsTarget(ids=tuple(_require_uuid(item, "target.ids[]") for item in ids_raw))
    if mode == "query":
        _check_keys(target, {"mode", "root"}, "target query")
        return QueryTarget(root=_parse_group(target.get("root"), "target.root"))
    _check_keys(target, {"mode"}, "target self")
    return SelfTarget()


def _parse_group(raw: Any, what: str) -> Group:
    group = _require_object(raw, what)
    _check_keys(group, {"type", "logic", "children"}, "group")
    if group.get("type") != "group":
        _fail(f"query AST: expected {{'type': 'group'}} for {what}, got {group.get('type')!r}")
    logic = _require_enum(group.get("logic"), frozenset({"and", "or"}), "group logic")
    children_raw = group.get("children")
    if not isinstance(children_raw, list):
        _fail(f"query AST: expected an array for group.children, got {type(children_raw).__name__}")
    return Group(logic=cast(Literal["and", "or"], logic), children=tuple(_parse_child(item) for item in children_raw))


def _parse_child(raw: Any) -> Child:
    node = _require_object(raw, "child")
    child_type = node.get("type")
    if child_type == "group":
        return _parse_group(node, "group child")
    if child_type == "not":
        _check_keys(node, {"type", "child"}, "not")
        inner = _require_object(node.get("child"), "not.child")
        if inner.get("type") in ("group", "not"):
            if inner.get("type") == "not":
                _fail("query AST: a not child must be a condition or a group, not another not")
            return Not(child=_parse_group(inner, "not.child"))
        return Not(child=_parse_condition(inner))
    if not isinstance(child_type, str):
        _fail(f"query AST: child has no discriminating type: {child_type!r}")
    return _parse_condition(node)


def _parse_member(node: dict[str, Any]) -> MemberCondition:
    family = _require_enum(node.get("family"), _MEMBER_FAMILIES, "member family")
    op = _require_enum(node.get("op"), frozenset({"in", "not-in", "exists", "not-exists", "exact"}), "member op")
    has_target = "target" in node
    if op in ("in", "not-in"):
        _check_keys(node, {"type", "family", "op", "target"}, f"member {op}")
        if not has_target:
            _fail(f"query AST: member op {op!r} requires a target")
        return MemberCondition(
            family=cast(MemberFamily, family), op=cast(MemberOp, op), target=_parse_target(node["target"], "member.target")
        )
    if op in ("exists", "not-exists"):
        _check_keys(node, {"type", "family", "op"}, f"member {op}")
        if has_target:
            _fail(f"query AST: member op {op!r} forbids a target")
        return MemberCondition(family=cast(MemberFamily, family), op=cast(MemberOp, op))
    # exact: class family ONLY, a single-id ids tuple (zod z.tuple([uuid])).
    _check_keys(node, {"type", "family", "op", "target"}, "member exact")
    if family != "class":
        _fail("query AST: member op 'exact' is class-family only")
    target = _require_object(node.get("target"), "member exact target")
    _check_keys(target, {"mode", "ids"}, "member exact target")
    if target.get("mode") != "ids":
        _fail("query AST: member op 'exact' requires an ids target")
    ids_raw = target.get("ids")
    if not isinstance(ids_raw, list) or len(ids_raw) != 1:
        _fail("query AST: member op 'exact' requires exactly one target id")
    return MemberCondition(
        family="class",
        op="exact",
        target=IdsTarget(ids=(_require_uuid(ids_raw[0], "member exact target id"),)),
    )


def _optional_value_str(raw: dict[str, Any], what: str) -> str | None:
    """zod ``z.string().min(1).optional()`` parity: absent → None, present →
    a non-empty string."""
    if "value" not in raw:
        return None
    return _require_nonempty_str(raw["value"], f"{what}.value")


def _parse_condition(node: dict[str, Any]) -> Condition:
    condition_type = node.get("type")
    if condition_type == "member":
        return _parse_member(node)
    if condition_type == "content":
        _check_keys(node, {"type", "op", "value"}, "content condition", {"type", "op"})
        op = _require_enum(node.get("op"), _CONTENT_OPS, "content op")
        return ContentCondition(op=cast(ContentOp, op), value=_optional_value_str(node, "content"))
    if condition_type == "name":
        _check_keys(node, {"type", "op", "value"}, "name condition", {"type", "op"})
        op = _require_enum(node.get("op"), _NAME_OPS, "name op")
        return NameCondition(op=cast(NameOp, op), value=_optional_value_str(node, "name"))
    if condition_type == "property":
        _check_keys(
            node,
            {"type", "schemaId", "op", "value", "from", "to", "period", "includeDefaults"},
            "property condition",
            {"type", "schemaId", "op"},
        )
        schema_id = _require_uuid(node.get("schemaId"), "property.schemaId")
        op = _require_enum(node.get("op"), _PROPERTY_OPS, "property op")
        period = node.get("period")
        include_defaults = node.get("includeDefaults")
        return PropertyCondition(
            schema_id=schema_id,
            op=cast(PropertyOp, op),
            value=node.get("value"),
            from_=node.get("from"),
            to=node.get("to"),
            period=cast(DatePeriod | None, _require_enum(period, _DATE_PERIODS, "property period")) if period is not None else None,
            include_defaults=_require_bool(include_defaults, "property.includeDefaults") if include_defaults is not None else None,
        )
    if condition_type == "date":
        _check_keys(
            node,
            {"type", "field", "op", "timestamp", "from", "to", "period"},
            "date condition",
            {"type", "field", "op"},
        )
        field = _require_enum(node.get("field"), frozenset({"created", "updated"}), "date field")
        op = _require_enum(node.get("op"), _DATE_OPS, "date op")
        period = node.get("period")

        def opt_timestamp(key: str) -> str | None:
            if key not in node:
                return None
            return _require_nonempty_str(node[key], f"date.{key}")

        return DateCondition(
            field=cast(Literal["created", "updated"], field),
            op=cast(DateOp, op),
            timestamp=opt_timestamp("timestamp"),
            from_=opt_timestamp("from"),
            to=opt_timestamp("to"),
            period=cast(DatePeriod | None, _require_enum(period, _DATE_PERIODS, "date period")) if period is not None else None,
        )
    if condition_type == "nodeField":
        _check_keys(node, {"type", "field", "op", "value"}, "nodeField condition", {"type", "field", "op"})
        field = _require_enum(node.get("field"), _NODE_FIELD_NAMES, "nodeField field")
        op = _require_enum(node.get("op"), _NODE_FIELD_OPS, "nodeField op")
        # Not a uuid on purpose: isClass/isEmbedded ride "true"/"false".
        return NodeFieldCondition(
            field=cast(Literal["coverAsset", "bannerAsset", "aliasedNode", "isClass", "isEmbedded"], field),
            op=cast(NodeFieldOp, op),
            value=_optional_value_str(node, "nodeField"),
        )
    _fail(f"query AST: unknown condition type {condition_type!r}")


def _parse_sort(raw: Any) -> tuple[SortSpec, ...]:
    if not isinstance(raw, list):
        _fail(f"query AST: expected an array for sort, got {type(raw).__name__}")
    specs: list[SortSpec] = []
    for item in raw:
        spec = _require_object(item, "sort spec")
        _check_keys(spec, {"field", "dir"}, "sort spec")
        field = _require_enum(spec.get("field"), frozenset({"name", "createdAt", "isClass", "isEmbedded"}), "sort field")
        direction = _require_enum(spec.get("dir"), frozenset({"asc", "desc"}), "sort dir")
        specs.append(
            SortSpec(
                field=cast(Literal["name", "createdAt", "isClass", "isEmbedded"], field),
                dir=cast(Literal["asc", "desc"], direction),
            )
        )
    return tuple(specs)


def _parse_dimension(raw: Any) -> AggregationDimension:
    dimension = _require_object(raw, "aggregation dimension")
    kind = _require_enum(dimension.get("kind"), frozenset({"class", "property", "isClass", "isEmbedded"}), "dimension kind")
    if kind in ("class", "property"):
        _check_keys(dimension, {"kind", "id"}, f"dimension {kind!r}")
        return AggregationDimension(
            kind=cast(Literal["class", "property"], kind), id=_require_uuid(dimension.get("id"), f"dimension.{kind}.id")
        )
    _check_keys(dimension, {"kind"}, f"dimension {kind!r}")
    return AggregationDimension(kind=cast(Literal["isClass", "isEmbedded"], kind))


def _parse_measure(raw: Any) -> AggregationMeasure:
    measure = _require_object(raw, "aggregation measure")
    function = _require_enum(
        measure.get("function"), frozenset({"count", "countDistinct", "sum", "avg", "min", "max"}), "measure function"
    )
    kind = measure.get("kind")
    if function in ("count", "countDistinct"):
        _check_keys(measure, {"function", "kind"}, "count measure", {"function"})
        if kind is not None and kind != "node":
            _fail(f"query AST: measure {function!r} only counts nodes (kind must be 'node' or absent)")
        return AggregationMeasure(function=cast(Literal["count", "countDistinct"], function), kind="node" if kind else None)
    _check_keys(measure, {"function", "kind", "id"}, f"{function} measure")
    if kind != "property":
        _fail(f"query AST: measure {function!r} requires kind 'property'")
    return AggregationMeasure(
        function=cast(Literal["sum", "avg", "min", "max"], function),
        kind="property",
        id=_require_uuid(measure.get("id"), "measure.id"),
    )


def _parse_aggregation(raw: Any) -> Aggregation:
    aggregation = _require_object(raw, "aggregation")
    _check_keys(aggregation, {"dimensions", "measures"}, "aggregation")
    dimensions_raw = aggregation.get("dimensions")
    measures_raw = aggregation.get("measures")
    if not isinstance(dimensions_raw, list):
        _fail(f"query AST: expected an array for aggregation.dimensions, got {type(dimensions_raw).__name__}")
    if not isinstance(measures_raw, list):
        _fail(f"query AST: expected an array for aggregation.measures, got {type(measures_raw).__name__}")
    if not measures_raw:
        _fail("query AST: aggregation.measures requires at least one measure")
    return Aggregation(
        dimensions=tuple(_parse_dimension(item) for item in dimensions_raw),
        measures=tuple(_parse_measure(item) for item in measures_raw),
    )


def parse_query_ast(raw: object) -> QueryAst:
    """Parse + validate a serialized AST. Unknown condition types / families /
    ops / target modes / versions and stray keys fail loud (zod ``.strict()``
    parity); there is no earlier-version parse path.

    Args:
        raw: The decoded JSON document (any object).

    Returns:
        The typed :class:`QueryAst`.

    Raises:
        QueryAstError: Any deviation from the strict v2 grammar.
    """
    root = _require_object(raw, "query AST")
    _check_keys(root, {"version", "scope", "root", "sort", "aggregation"}, "query AST", {"version", "scope", "root"})
    version = root.get("version")
    if version != 2:
        _fail(f"query AST: unsupported version {version!r} (only version 2 parses — no dual-parse path)")
    return QueryAst(
        scope=_parse_scope(root.get("scope")),
        root=_parse_group(root.get("root"), "root"),
        sort=_parse_sort(root["sort"]) if "sort" in root else None,
        aggregation=_parse_aggregation(root["aggregation"]) if "aggregation" in root else None,
    )
