"""The condition-builder model — the Odoo register over the query-AST v2 draft.

Port of ``apps/web/src/features/collection/filterBlockModel.ts`` (the
condition model) + the prune/create semantics of
``apps/web/src/features/collection/filterQuery.ts`` + the row-chrome morphs
inlined in ``FilterBlockBuilder.tsx`` (the target-mode switch, the
property-schema pick, the row value write). The structured builder is the
query-builder block UI over the v2 AST; this module is that UI's entire
behavioral contract, headless by design (the core/data law).

The register: every filter row reads ``[Family][Operator][Value][Mode]`` —
the family a condition belongs to (:func:`family_of`), the operator
column's options per family (:func:`operator_options`, adapting to the
selected property schema), the operator's current value + negation
(:func:`operator_of`), and the operator-application morphs
(:func:`apply_operator` — the picked ids / values / timestamps carry
across them).

The orthogonality law (the v1 regression this guards): an operator choice
touches ONLY the op/polarity axis — the member families flip ``in`` ⇄
``not-in`` carrying the target VERBATIM (an ids list, a nested query, or
the self marker — the mode survives every flip); the set bits
re-materialize ``exists`` / ``not-exists`` (no target at all); the
content/property/date negatives, the isClass/isEmbedded polarity, and the
wire-field "is not set" sugar as a NOT-wrap. A mode switch
(:func:`apply_target_mode`) rebuilds ONLY the target — the operator
survives verbatim. The boolean rows (type/placement, boolean properties)
ride the owner's single-selector ruling: the wire booleans are
non-nullable, so is/is-not is redundant with the value — the value column
IS the predicate.

Drafts may carry half-typed rows (a class condition with no class picked,
a content condition with a blank value); :func:`filter_query_to_group`
prunes those into the schema-valid composed root, returning ``None`` when
nothing survives — the strict wire parser rejects the half-typed shapes,
so the prune is the draft → wire boundary. The GTK draft edits frozen
dataclasses (:mod:`notees_gtk.core.protocol.query_ast`) instead of loose
JSON — morphs construct new instances, and the immutable-tree helpers
(:func:`replace_child_at` / :func:`insert_child_at` /
:func:`remove_child_at`) are the chrome's edit verbs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal, cast

from notees_gtk.core.protocol.query_ast import (
    Child,
    Condition,
    ContentCondition,
    ContentOp,
    DateCondition,
    DateOp,
    EntireWorkspaceScope,
    Group,
    IdsTarget,
    MemberCondition,
    MemberFamily,
    NameCondition,
    NodeFieldCondition,
    Not,
    PropertyCondition,
    PropertyOp,
    QueryAst,
    QueryTarget,
    Scope,
    SelfTarget,
    TargetRef,
)

__all__ = [
    "AddMenuEntry",
    "BlockView",
    "BoundProperty",
    "BuilderFacts",
    "ConditionKind",
    "EMPTY_FACTS",
    "FAMILY_LABELS",
    "FAMILY_OPTIONS",
    "FILTER_KIND_OPTIONS",
    "FilterKindOption",
    "NodeCollectionToolbarConfig",
    "OperatorOption",
    "TargetMode",
    "apply_boolean_value",
    "apply_operator",
    "apply_property_schema_pick",
    "apply_row_value",
    "apply_target_mode",
    "block_view",
    "create_condition",
    "draft_or_factory_ast",
    "factory_query_ast",
    "family_of",
    "filter_kind_options_for_config",
    "filter_query_to_group",
    "insert_child_at",
    "operator_of",
    "operator_options",
    "prune_child",
    "remove_child_at",
    "replace_child_at",
]

#: The block FAMILIES (the v2 add-menu register as single-word types — the
#: operator column carries the is/is-not/contains/etc variation), plus the
#: group/not constructors. Port of ``ConditionKind`` + ``AddMenuEntry``.
ConditionKind = Literal[
    "class",
    "type",
    "placement",
    "content",
    "property",
    "tags",
    "links",
    "referencePath",
    "parent",
    "child",
    "childPath",
    "page",
    "parentPath",
    "root",
    "cover",
    "banner",
    "alias",
    "created",
    "edited",
]
AddMenuEntry = Literal[ConditionKind, "group-and", "group-or", "not"]

#: The member target modes (the three-way switch: Manual picked nodes /
#: Query nested / Self the hosting page).
TargetMode = Literal["manual", "query", "self"]


# --- the block -----------------------------------------------------------------


@dataclass(frozen=True)
class BlockView:
    """A block's view of a child: a single condition + whether a NOT wraps it.

    The content/property/date negatives, the isClass/isEmbedded polarity,
    and the wire-field "is not set" are sugar over a not-wrap, so the draft
    AST carries them verbatim and the row renders them as an operator
    choice. NOT-around-a-GROUP stays a group wrapper (never a row) —
    :func:`block_view` returns ``None`` for groups and not-around-groups.
    """

    negated: bool
    condition: Condition


def block_view(child: Child) -> BlockView | None:
    """The row view of one draft child (``blockView`` port)."""
    if isinstance(child, Not):
        if isinstance(child.child, Group):
            return None
        return BlockView(negated=True, condition=child.child)
    if isinstance(child, Group):
        return None
    return BlockView(negated=False, condition=child)


def family_of(condition: Condition) -> ConditionKind:
    """The family a condition belongs to (the TYPE column's value).

    The v2 member condition maps to its wire family; ``name`` joins
    ``content`` (the title register — the builder never authors it, but
    DSL-parsed ASTs can carry it in).
    """
    if isinstance(condition, MemberCondition):
        return "class" if condition.family == "class" else condition.family  # type: ignore[return-value]
    if isinstance(condition, (ContentCondition, NameCondition)):
        return "content"
    if isinstance(condition, PropertyCondition):
        return "property"
    if isinstance(condition, DateCondition):
        return "created" if condition.field == "created" else "edited"
    # nodeField
    if condition.field == "coverAsset":
        return "cover"
    if condition.field == "bannerAsset":
        return "banner"
    if condition.field == "aliasedNode":
        return "alias"
    return "type" if condition.field == "isClass" else "placement"


# --- the register ----------------------------------------------------------------


@dataclass(frozen=True)
class OperatorOption:
    """One operator-column entry (value + label)."""

    value: str
    label: str


@dataclass(frozen=True)
class _MemberRegister:
    """The membership register per member family: the operator column's
    positive and negative labels and whether the family offers the set
    bits. Both positive labels map onto the SAME v2 op ("in" — v2 has no
    single/list split on the wire); the canonical label is what
    :func:`operator_of` reads back (the other is an offered alias that
    keeps the condition verbatim)."""

    positives: tuple[str, ...]
    canonical: str
    negatives: tuple[str, ...]
    set: bool


#: ``MEMBER_REGISTER`` verbatim (filterBlockModel.ts).
_MEMBER_REGISTER: dict[MemberFamily, _MemberRegister] = {
    "class": _MemberRegister(("is", "contains"), "contains", ("not-contains", "is-not"), True),
    "tags": _MemberRegister(("is",), "is", ("is-not",), True),
    "links": _MemberRegister(("is",), "is", ("is-not",), True),
    "referencePath": _MemberRegister(("is-any-of",), "is-any-of", ("is-not-any-of",), False),
    "parent": _MemberRegister(("is",), "is", ("is-not",), True),
    "child": _MemberRegister(("contains-any-of",), "contains-any-of", ("not-contains-any-of",), True),
    "childPath": _MemberRegister(("contains",), "contains", ("not-contains",), True),
    "page": _MemberRegister(("is",), "is", ("is-not",), True),
    "parentPath": _MemberRegister(("includes",), "includes", ("not-includes",), True),
    "root": _MemberRegister(("is",), "is", ("is-not",), False),
}


@dataclass(frozen=True)
class BoundProperty:
    """One bound property the operator/value columns adapt to (the
    ``BuilderFacts.boundProperties`` record, GTK subset: the type-aware
    operator list and the select options need ``type``/``options`` only —
    the picker-source enrichment the web's ``useBuilderFacts`` derives from
    class bindings is the chrome's store read, not the model's)."""

    id: str
    name: str
    type: str
    options: tuple[tuple[str, str], ...] = ()  # (option id, label) pairs, select schemas only


@dataclass(frozen=True)
class BuilderFacts:
    """The picker/operator facts the register adapts to (the
    ``BuilderFacts`` port — the model consumes ``bound_properties`` only)."""

    bound_properties: tuple[BoundProperty, ...] = ()


#: The resting facts (no property schemas known — the property operator
#: list falls back to the text register, exactly the web's unknown-schema arm).
EMPTY_FACTS = BuilderFacts()


@dataclass(frozen=True)
class NodeCollectionToolbarConfig:
    """The per-section facet configuration (``NodeCollectionToolbarConfig``
    port) — every facet defaults on; the facets gate which condition kinds
    the add menu offers (the other kinds are always offered)."""

    text: bool | None = None
    klass: bool | None = None
    properties: bool | None = None
    date_range: bool | None = None


def _set_bits() -> tuple[OperatorOption, OperatorOption]:
    return (OperatorOption("set", "is set"), OperatorOption("not-set", "is not set"))


def operator_options(
    family: ConditionKind,
    condition: Condition | None = None,
    facts: BuilderFacts = EMPTY_FACTS,
) -> tuple[OperatorOption, ...]:
    """The operator column's options per family (the Odoo operator sets).

    The property family's list adapts to the SELECTED schema's type; the
    edited family carries no set pair (created_at/updated_at are written at
    create time — the options would lie); the boolean rows (type/placement)
    offer NO operator column at all (the owner's single-selector ruling).
    """
    if family == "class":
        # The class set: BOTH class operators ride the extends-aware
        # closure membership — "is fuente" matches a node classed book when
        # book extends fuente. is / is not and contains / does not contain
        # are the SAME v2 wire op ("in" over the picked ids — the
        # single/list split was a wire artifact); is set / is not set test
        # the set's emptiness. (Exact set equality — the class member's
        # "exact" op — is legacy-wire-only: stored ASTs still
        # parse/evaluate it, the UI no longer binds it.)
        return (
            OperatorOption("is", "is"),
            OperatorOption("is-not", "is not"),
            OperatorOption("contains", "contains"),
            OperatorOption("not-contains", "does not contain"),
            OperatorOption("set", "is set"),
            OperatorOption("not-set", "is not set"),
        )
    if family == "content":
        if isinstance(condition, NameCondition):
            # A name row (the title register — enters via DSL parse): only
            # the ops the name morph implements. starts/ends/fts are
            # content-only arms — offering them on a title row was a dead
            # choice (applyTextOperator silently kept the condition).
            return (
                OperatorOption("is", "is"),
                OperatorOption("is-not", "is not"),
                OperatorOption("contains", "contains"),
                OperatorOption("not-contains", "does not contain"),
                *_set_bits(),
            )
        # The text operator set: exact/equality, substring, edge-anchored,
        # full-text, and the emptiness bit.
        return (
            OperatorOption("is", "is"),
            OperatorOption("is-not", "is not"),
            OperatorOption("contains", "contains"),
            OperatorOption("not-contains", "does not contain"),
            OperatorOption("starts", "starts with"),
            OperatorOption("ends", "ends with"),
            OperatorOption("fts", "full-text"),
            *_set_bits(),
        )
    if family in ("tags", "links"):
        # The link/tag families (the multi-value register): contains/does
        # not contain bind the LIST (ANY-of membership over the picked nodes).
        return (
            OperatorOption("is", "contains"),
            OperatorOption("is-not", "does not contain"),
            *_set_bits(),
        )
    if family == "referencePath":
        # The link-path rollup (containment folded in) over the picked
        # nodes / the subquery's matches / the host.
        return (
            OperatorOption("is-any-of", "contains"),
            OperatorOption("is-not-any-of", "does not contain"),
        )
    if family == "parent":
        # The DIRECT PARENT is one node — is/is not bind the picked list
        # (the single-slot value control carries 0/1 elements), is set/is
        # not set the has-a-parent bit.
        return (
            OperatorOption("is", "is"),
            OperatorOption("is-not", "is not"),
            *_set_bits(),
        )
    if family == "child":
        # A DIRECT child is one of the picked nodes.
        return (
            OperatorOption("contains-any-of", "contains"),
            OperatorOption("not-contains-any-of", "does not contain"),
            *_set_bits(),
        )
    if family == "childPath":
        # The subtree contains any of the picked nodes anywhere down their tree.
        return (
            OperatorOption("contains", "contains"),
            OperatorOption("not-contains", "does not contain"),
            *_set_bits(),
        )
    if family in ("type", "placement"):
        # The boolean rows ride a SINGLE value selector (the owner's
        # register ruling): the wire booleans are non-nullable, so
        # is/is-not is redundant with the value ("is not block" ≡ "is
        # page"). No operator column — the builder skips the dropdown when
        # the list is empty. Negatives stay reachable via group-level
        # Exclude (NOT).
        return ()
    if family == "page":
        # The CONTAINING PAGE is one node — is/is not bind the picked list,
        # is set/is not set the lives-inside-a-page bit.
        return (
            OperatorOption("is", "is"),
            OperatorOption("is-not", "is not"),
            *_set_bits(),
        )
    if family == "parentPath":
        # The ancestor chain contains any of the picked nodes (or the
        # subquery's matches / the host page).
        return (
            OperatorOption("includes", "contains"),
            OperatorOption("not-includes", "does not contain"),
            *_set_bits(),
        )
    if family == "root":
        # The workspace-root ancestor is one node — is/is not bind the
        # picked list. The set bit is vacuous — every node's root exists (a
        # parentless row is its own root).
        return (
            OperatorOption("is", "is"),
            OperatorOption("is-not", "is not"),
        )
    if family == "property":
        return property_operator_options(condition, facts)
    if family in ("cover", "banner", "alias"):
        return (
            OperatorOption("exists", "is set"),
            OperatorOption("not-exists", "is not set"),
        )
    # created / edited — the date operator set (Odoo's date list):
    # day-granular equality, the instant comparisons, the inclusive range,
    # the relative period. The set bits are deliberately absent —
    # created_at/updated_at are written at create time, so every row reads
    # "set" (a lying control).
    return (
        OperatorOption("is", "is"),
        OperatorOption("is-not", "is not"),
        OperatorOption("gt", ">"),
        OperatorOption("gte", "≥"),
        OperatorOption("lt", "<"),
        OperatorOption("lte", "≤"),
        OperatorOption("between", "is between"),
        OperatorOption("within", "is within"),
    )


def property_operator_options(
    condition: Condition | None = None,
    facts: BuilderFacts = EMPTY_FACTS,
) -> tuple[OperatorOption, ...]:
    """The property family's operator list, adapted to the selected
    schema's type (``propertyOperatorOptions`` port)."""
    schema_id = condition.schema_id if isinstance(condition, PropertyCondition) else ""
    schema = next((prop for prop in facts.bound_properties if prop.id == schema_id), None)
    set_bits = _set_bits()
    if schema is not None and schema.type in ("number", "integer"):
        # Odoo's numeric list: =/!=/>/>=/</<=, the range, the substring ops.
        return (
            OperatorOption("eq", "="),
            OperatorOption("neq", "≠"),
            OperatorOption("gt", ">"),
            OperatorOption("gte", "≥"),
            OperatorOption("lt", "<"),
            OperatorOption("lte", "≤"),
            OperatorOption("between", "is between"),
            OperatorOption("contains", "contains"),
            OperatorOption("not-contains", "does not contain"),
            *set_bits,
        )
    if schema is not None and schema.type == "datetime":
        # The date list: the comparisons, the range, the relative period.
        return (
            OperatorOption("eq", "="),
            OperatorOption("neq", "≠"),
            OperatorOption("gt", ">"),
            OperatorOption("gte", "≥"),
            OperatorOption("lt", "<"),
            OperatorOption("lte", "≤"),
            OperatorOption("between", "is between"),
            OperatorOption("within", "is within"),
            *set_bits,
        )
    if schema is not None and schema.type == "select":
        return (
            OperatorOption("eq", "is"),
            OperatorOption("neq", "is not"),
            *set_bits,
        )
    if schema is not None and schema.type == "boolean":
        # The value column is the true/false predicate; the operator column
        # carries ONLY the set bits (is-true ≡ is set + true — the ruling's
        # fold). The is-true/is-false morphs survive on applyOperator for
        # Raw-AST round-trips, unoffered here.
        return set_bits
    if schema is not None and schema.type == "object":
        # Node-typed: the value control is the anchored node picker.
        return (
            OperatorOption("eq", "is"),
            OperatorOption("neq", "is not"),
            *set_bits,
        )
    # Text and the rest: the text set.
    return (
        OperatorOption("eq", "is"),
        OperatorOption("neq", "is not"),
        OperatorOption("contains", "contains"),
        OperatorOption("not-contains", "does not contain"),
        *set_bits,
    )


def operator_of(condition: Condition, negated: bool) -> str:
    """The operator column's current value for a condition (+ its negation)."""
    if isinstance(condition, MemberCondition):
        register = _MEMBER_REGISTER[condition.family]
        if condition.op == "exact":
            # Legacy-wire-only (exact set equality): rows still render their "is".
            return "is-not" if negated else "is"
        if condition.op == "exists":
            return "not-set" if negated else "set"
        if condition.op == "not-exists":
            return "set" if negated else "not-set"
        # in / not-in read the family's canonical membership label (the
        # single/list register collapsed onto the one wire op).
        if condition.op == "not-in":
            return register.canonical if negated else register.negatives[0]
        return register.negatives[0] if negated else register.canonical
    if isinstance(condition, (ContentCondition, NameCondition)):
        if condition.op == "exists":
            return "not-set" if negated else "set"
        if condition.op == "fts":
            return "fts"
        if condition.op == "eq":
            return "is-not" if negated else "is"
        if condition.op == "starts":
            return "starts"
        if condition.op == "ends":
            return "ends"
        return "not-contains" if negated else "contains"
    if isinstance(condition, PropertyCondition):
        if condition.op == "exists":
            return "not-set" if negated else "set"
        if condition.op == "between":
            return "between"
        if condition.op == "within":
            return "within"
        # The boolean register folds the truth value into the VALUE column
        # (is-true ≡ set + true): the operator column reads the set axis only.
        if condition.op == "eq" and isinstance(condition.value, bool):
            return "not-set" if negated else "set"
        # The scalar arms read the polarity too: a not-wrapped eq/contains
        # row renders its negative label (the content family's contract).
        if condition.op == "eq":
            return "neq" if negated else "eq"
        if condition.op == "contains":
            return "not-contains" if negated else "contains"
        return condition.op
    if isinstance(condition, DateCondition):
        if condition.op == "eq":
            return "is-not" if negated else "is"
        if condition.op == "neq":
            return "is" if negated else "is-not"
        return condition.op
    # nodeField
    if condition.field in ("isClass", "isEmbedded"):
        # The boolean axis rides the VALUE; polarity rides the not-wrap —
        # the operator column reads (and applies) ONLY the polarity, like
        # the content/property/date negatives and the set-bit arm below.
        return "is-not" if negated else "is"
    return "not-exists" if negated else "exists"


# --- the operator morphs ----------------------------------------------------------


def _apply_member_operator(condition: MemberCondition, op: str) -> Child:
    """The member morph — the orthogonality law: an operator choice touches
    ONLY the op axis. Positive labels → ``in``, negative labels →
    ``not-in``, each carrying the target VERBATIM (an ids list, a nested
    query, or the self marker — the mode survives every flip). The set bits
    re-materialize the condition as ``exists`` / ``not-exists`` (the set
    shapes carry no target); flipping back to a membership label
    re-materializes an ``in`` over an empty ids pick."""
    register = _MEMBER_REGISTER[condition.family]
    carried_target: TargetRef | None = None
    if condition.op in ("in", "not-in", "exact"):
        carried_target = condition.target
    target = carried_target if carried_target is not None else IdsTarget(ids=())
    in_op: Literal["in", "not-in"] = "not-in" if condition.op == "not-exists" else "in"
    if op in register.positives:
        return replace(condition, op=in_op, target=target)
    if op in register.negatives:
        return replace(condition, op="not-in", target=target)
    if op == "set":
        return MemberCondition(family=condition.family, op="exists")
    if op == "not-set":
        return MemberCondition(family=condition.family, op="not-exists")
    return condition


def _apply_text_operator(condition: ContentCondition | NameCondition, op: str) -> Child:
    """The content/name morph: the text operator set + the emptiness bit."""
    carried = condition.value if condition.value is not None else ""

    def text(text_op: str, value: str) -> Condition:
        if isinstance(condition, ContentCondition):
            return ContentCondition(op=cast(ContentOp, text_op), value=value)
        # The title register carries only eq/neq/contains/exists — the
        # edge/fts arms are guarded into "contains" here (unreachable for
        # name rows).
        return NameCondition(op="eq" if text_op == "eq" else "contains", value=value)

    if op == "is":
        return text("eq", carried)
    if op == "is-not":
        return Not(child=text("eq", carried))
    if op == "contains":
        return text("contains", carried)
    if op == "not-contains":
        return Not(child=text("contains", carried))
    if op == "starts":
        return text("starts", carried) if isinstance(condition, ContentCondition) else condition
    if op == "ends":
        return text("ends", carried) if isinstance(condition, ContentCondition) else condition
    if op == "fts":
        return text("fts", carried) if isinstance(condition, ContentCondition) else condition
    if op == "set":
        return ContentCondition(op="exists") if isinstance(condition, ContentCondition) else NameCondition(op="exists")
    if op == "not-set":
        inner: Condition = (
            ContentCondition(op="exists") if isinstance(condition, ContentCondition) else NameCondition(op="exists")
        )
        return Not(child=inner)
    return condition


def _apply_date_operator(condition: DateCondition, op: str) -> Child:
    """The date morph (created / edited): the field rides the condition (the
    family only names WHICH date column); comparisons carry the timestamp,
    the range carries it into ``from``. The set bits are vacuous —
    created_at/updated_at are written at create time — and keep the
    condition."""
    carried = condition.timestamp if condition.timestamp is not None else (condition.from_ or "")

    def cmp(date_op: str, timestamp: str) -> Condition:
        return DateCondition(field=condition.field, op=cast(DateOp, date_op), timestamp=timestamp)

    if op == "is":
        return cmp("eq", carried)
    if op == "is-not":
        return Not(child=cmp("eq", carried))
    if op in ("gt", "gte", "lt", "lte"):
        return cmp(op, carried)
    if op == "after":
        return DateCondition(field=condition.field, op="after", timestamp=carried)
    if op == "before":
        return DateCondition(field=condition.field, op="before", timestamp=carried)
    if op == "between":
        return DateCondition(
            field=condition.field,
            op="between",
            from_=carried,
            to=condition.to if condition.to is not None else carried,
        )
    if op == "within":
        return DateCondition(field=condition.field, op="within", period=condition.period or "today")
    # set / not-set: vacuous (every row reads "set") — no honest control to offer.
    return condition


def _apply_property_operator(condition: PropertyCondition, op: str) -> Child:
    """The property morph: the op carries the value across; between keeps
    the bounds (an eq value carries into the range); within keeps the period."""

    def value(prop_op: str, v: object) -> Condition:
        next_condition = PropertyCondition(schema_id=condition.schema_id, op=cast(PropertyOp, prop_op))
        # The carried value rides the re-pick (absent, not None, when the
        # source shape carried none — the TS `value: undefined` arm).
        return next_condition if v is None else replace(next_condition, value=v)

    carried = condition.value
    if op == "eq":
        return value("eq", carried)
    if op == "neq":
        return value("neq", carried)
    if op == "contains":
        return value("contains", carried)
    if op == "not-contains":
        return Not(child=value("contains", carried))
    if op in ("gt", "gte", "lt", "lte"):
        return value(op, carried)
    if op == "is-true":
        return value("eq", True)
    if op == "is-false":
        return value("eq", False)
    if op == "between":
        # The bounds ride the re-pick: an eq value carries into the range
        # (the date arm's contract); a between row keeps both bounds verbatim.
        from_ = carried if carried is not None else condition.from_
        to = condition.to if condition.to is not None else carried
        return PropertyCondition(schema_id=condition.schema_id, op="between", from_=from_, to=to)
    if op == "within":
        return PropertyCondition(schema_id=condition.schema_id, op="within", period=condition.period or "today")
    if op == "set":
        return PropertyCondition(schema_id=condition.schema_id, op="exists")
    if op == "not-set":
        return Not(child=PropertyCondition(schema_id=condition.schema_id, op="exists"))
    return condition


def apply_boolean_value(condition: PropertyCondition, value: bool) -> PropertyCondition:
    """The boolean property row's value-selector morph (the ruling's
    set+value fold): picking true/false re-materializes the positive eq row —
    from an ``exists`` row as much as from an eq row. The value IS the
    predicate; the row-level not-wrap re-applies when the row renders
    negated (Exclude)."""
    return PropertyCondition(schema_id=condition.schema_id, op="eq", value=value)


def apply_operator(condition: Condition, op: str) -> Child:
    """Apply an operator choice to a condition, returning the new child
    (not-wrapped where the register sugars the negative that way — the
    content/property/date families, the isClass/isEmbedded polarity, and
    the wire fields). The member families flip the wire op instead (in ⇄
    not-in with the target verbatim; the set bits re-materialize
    exists/not-exists). The orthogonality law: an operator choice NEVER
    touches the family's value payload (picked ids / text / value /
    boolean / bounds / timestamps ride across the morphs)."""
    if isinstance(condition, MemberCondition):
        return _apply_member_operator(condition, op)
    if isinstance(condition, (ContentCondition, NameCondition)):
        return _apply_text_operator(condition, op)
    if isinstance(condition, DateCondition):
        return _apply_date_operator(condition, op)
    if isinstance(condition, PropertyCondition):
        return _apply_property_operator(condition, op)
    # nodeField
    if condition.field in ("isClass", "isEmbedded"):
        # The orthogonality law: the operator choice touches ONLY the
        # polarity — "is-not" wraps (the group-level not sugar, like the
        # cover/banner/alias arm below), "is" returns the positive row. The
        # boolean VALUE is the independent other axis and never moves here.
        return Not(child=condition) if op == "is-not" else condition
    return Not(child=condition) if op == "not-exists" else condition


def apply_row_value(child: Child, next_condition: Condition) -> Child:
    """The row's VALUE write (the BlockRow's value onChange): writes the new
    condition while the row's not-wrap polarity HOLDS — a negated row
    re-wraps, a positive row takes the condition verbatim."""
    view = block_view(child)
    if view is None:
        raise ValueError("apply_row_value: not a single-condition block row")
    return Not(child=next_condition) if view.negated else next_condition


def apply_target_mode(condition: Condition, mode: TargetMode) -> Condition:
    """The member row's target-mode switch (the SelectionButton onChange in
    FilterBlockBuilder.tsx): rebuilds ONLY the target — the operator (in /
    not-in) survives the mode change verbatim. A set row (no target) first
    re-materializes a membership op, preserving its polarity (not-exists →
    not-in); the current ids carry into Manual where the row had them. A
    row already in Query mode keeps its nested root verbatim (the web only
    fires the switch on an actual change, so its rebuild-from-empty is
    unreachable there — the GTK model makes the unreachable case safe
    rather than discarding a typed nested query)."""
    if not isinstance(condition, MemberCondition):
        return condition
    carried_ids: tuple[str, ...] = ()
    if condition.op in ("in", "not-in") and isinstance(condition.target, IdsTarget):
        carried_ids = condition.target.ids
    in_op: Literal["in", "not-in"] = "not-in" if condition.op in ("not-in", "not-exists") else "in"
    if mode == "query":
        root = condition.target.root if isinstance(condition.target, QueryTarget) else Group(logic="and", children=())
        return MemberCondition(family=condition.family, op=in_op, target=QueryTarget(root=root))
    if mode == "self":
        return MemberCondition(family=condition.family, op=in_op, target=SelfTarget())
    return MemberCondition(family=condition.family, op=in_op, target=IdsTarget(ids=carried_ids))


def apply_property_schema_pick(schema_id: str, schema_type: str | None) -> PropertyCondition:
    """The property family's row-identity morph (PropertySchemaPick): picking
    a different property resets the value (it was typed for the old type)
    and normalizes the condition back to the plain property shape. A
    boolean schema seeds the truth value — the register ruling: the value
    column IS the predicate (is-true ≡ is set + true), so a freshly picked
    boolean property reads "is true", never a blank half-typed value."""
    return PropertyCondition(
        schema_id=schema_id,
        op="eq",
        value=True if schema_type == "boolean" else "",
    )


# --- the draft → composed group prune ---------------------------------------------


def _prune_member(child: MemberCondition) -> MemberCondition | None:
    """The member condition's prune: the set bits (exists / not-exists) are
    always fully specified and survive; an ids target with an empty pick
    drops (the half-typed row); a query target drops when its nested group
    pruned to empty; the self target survives (the host resolves at run
    time)."""
    if child.op in ("exists", "not-exists"):
        # The set bits are always fully specified and survive.
        return child
    if child.op == "exact":
        # exact pins a single-id ids target (parse-guaranteed) — the empty
        # string sentinel drops, anything else survives.
        return child if isinstance(child.target, IdsTarget) and bool(child.target.ids) else None
    assert child.target is not None  # in/not-in parse-guaranteed
    if isinstance(child.target, IdsTarget):
        return None if not child.target.ids else child
    if isinstance(child.target, SelfTarget):
        return child
    nested = prune_child(child.target.root)
    # The nested prune of a group root yields a group or nothing (a query
    # target's root is a group by grammar); an emptied one drops the row.
    if not isinstance(nested, Group) or not nested.children:
        return None
    return replace(child, target=QueryTarget(root=nested))


def prune_child(child: Child) -> Child | None:
    """Prune one draft child into its composed form: half-typed conditions
    drop (content with a blank value, a member with an empty ids pick,
    property with an empty schemaId or a range missing a bound, date
    comparisons with a blank timestamp), nested groups recurse (emptied
    ones drop), a not drops when its child pruned away, and a member with a
    query target drops when its nested group pruned to empty. Anything fully
    specified — the set bits, the self target, the node-field predicates,
    date within — survives verbatim."""
    if isinstance(child, Group):
        children = tuple(nested for item in child.children if (nested := prune_child(item)) is not None)
        return None if not children else replace(child, children=children)
    if isinstance(child, Not):
        pruned = prune_child(child.child)
        # A not's child is a condition or a group by grammar, so the prune
        # yields one again (never a not) — the cast pins what the grammar
        # guarantees, like the TS `pruned as Condition | Group`.
        return None if pruned is None else Not(child=cast("Condition | Group", pruned))
    if isinstance(child, (ContentCondition, NameCondition)):
        return None if child.op != "exists" and not (child.value or "").strip() else child
    if isinstance(child, MemberCondition):
        return _prune_member(child)
    if isinstance(child, PropertyCondition):
        if child.schema_id == "":
            return None
        if child.op == "between":
            return None if child.from_ is None or child.to is None else child
        return child
    if isinstance(child, DateCondition):
        if child.op == "between":
            return None if not (child.from_ or "").strip() or not (child.to or "").strip() else child
        if child.op == "within":
            return child
        return None if not (child.timestamp or "").strip() else child
    # nodeField — always fully specified.
    return child


def filter_query_to_group(group: Group) -> Group | None:
    """Compose the structured draft into the schema-valid root group
    evaluation consumes, returning ``None`` when nothing survives (the
    caller treats ``None`` as "no structured query" — the factory
    match-all)."""
    children = tuple(child for item in group.children if (child := prune_child(item)) is not None)
    if not children:
        return None
    return Group(logic=group.logic, children=children)


def factory_query_ast(scope: Scope | None = None) -> QueryAst:
    """The factory AST a fresh query starts from when the draft is empty
    (``FACTORY_QUERY_AST`` port): the unspecified match-all — the section
    renders "Set a query to see results." for it."""
    return QueryAst(scope=scope if scope is not None else EntireWorkspaceScope(), root=Group(logic="and", children=()))


def draft_or_factory_ast(draft: Group, scope: Scope | None = None) -> QueryAst:
    """The AST the builder commits: the composed draft verbatim, falling
    back to the factory AST when the draft pruned to nothing
    (``draftOrFactoryAst`` port — the token always carries a parseable
    AST; an emptied builder is the unspecified query, never an absent one).
    The factory root is always the AND match-all, verbatim the web."""
    group = filter_query_to_group(draft)
    return QueryAst(
        scope=scope if scope is not None else EntireWorkspaceScope(),
        root=group if group is not None else Group(logic="and", children=()),
    )


# --- the add-menu registry --------------------------------------------------------


@dataclass(frozen=True)
class FilterKindOption:
    """One add-menu entry — a condition family or a group/not constructor
    (the ``FilterKindOption`` port; the web's mdi icon names are
    web-chrome detail and stay unported — the GTK menu renders labels)."""

    value: AddMenuEntry
    label: str
    description: str


#: The add-menu register (the v2 ``FILTER_KIND_OPTIONS``, 1:1 over the v2
#: AST — single-word types, operators live in the block's operator column).
#: Group constructors come last, the established order.
FILTER_KIND_OPTIONS: tuple[FilterKindOption, ...] = (
    FilterKindOption("class", "Class", "Filter by node class"),
    FilterKindOption("type", "Type", "A class or not a class"),
    FilterKindOption("placement", "Structural role", "Page (standalone) or Block (embedded in body)"),
    FilterKindOption("content", "Content", "Filter by text content"),
    FilterKindOption("property", "Property", "Filter by property value"),
    FilterKindOption("tags", "Tags", "Filter by assigned tags (any page)"),
    FilterKindOption("links", "Links", "Nodes that link to a node"),
    FilterKindOption("referencePath", "Link path", "The node or its ancestors reference these nodes — or the node sits inside them"),
    FilterKindOption("parent", "Parent", "The direct parent is any of these nodes"),
    FilterKindOption("child", "Child", "Contains any of these nodes as direct children"),
    FilterKindOption("childPath", "Child path", "Contains any of these nodes anywhere down their tree"),
    FilterKindOption("page", "Page", "The containing page is any of these nodes"),
    FilterKindOption("parentPath", "Parent path", "An ancestor anywhere up the parent chain is any of these nodes"),
    FilterKindOption("root", "Root", "The top-level parent (workspace-root child) is any of these nodes"),
    FilterKindOption("cover", "Cover", "Cover is set or not"),
    FilterKindOption("banner", "Banner", "Banner is set or not"),
    FilterKindOption("alias", "Alias", "Alias of a main page, set or not"),
    FilterKindOption("created", "Created", "Created after / before a date"),
    FilterKindOption("edited", "Edited", "Edited after / before a date"),
    FilterKindOption("group-and", "All of (AND)", "Match all nested conditions"),
    FilterKindOption("group-or", "Any of (OR)", "Match any nested condition"),
    FilterKindOption("not", "Exclude (NOT)", "Exclude matching nodes"),
)

#: The family entries only (the add-menu minus the group constructors).
FAMILY_OPTIONS: tuple[FilterKindOption, ...] = tuple(
    option for option in FILTER_KIND_OPTIONS if option.value not in ("group-and", "group-or", "not")
)

FAMILY_LABELS: dict[ConditionKind, str] = {cast(ConditionKind, option.value): option.label for option in FAMILY_OPTIONS}


def filter_kind_options_for_config(config: NodeCollectionToolbarConfig | None = None) -> tuple[FilterKindOption, ...]:
    """The add-menu entries a config offers — the gated kinds (class/
    property/created/edited) plus everything else always offered."""
    show_class = config.klass if config is not None and config.klass is not None else True
    show_properties = config.properties if config is not None and config.properties is not None else True
    show_date_range = config.date_range if config is not None and config.date_range is not None else True

    def offered(option: FilterKindOption) -> bool:
        if option.value == "class":
            return show_class
        if option.value == "property":
            return show_properties
        if option.value in ("created", "edited"):
            return show_date_range
        return True

    return tuple(option for option in FILTER_KIND_OPTIONS if offered(option))


def create_condition(kind: ConditionKind) -> Condition:
    """A fresh, half-typed condition per family — the block the menu appends."""
    if kind == "class":
        # The default CLASS row rides the MULTI-VALUE membership register:
        # "Class contains Day, Week" — closure-aware ANY-of over the picked
        # list — is the intent nearly always; the exact single-pill reading
        # ("is") is the deliberate narrow operator. Both bind the v2 wire op
        # "in" over an ids target.
        return MemberCondition(family="class", op="in", target=IdsTarget(ids=()))
    if kind == "type":
        return NodeFieldCondition(field="isClass", op="eq", value="true")
    if kind == "placement":
        return NodeFieldCondition(field="isEmbedded", op="eq", value="false")
    if kind == "content":
        return ContentCondition(op="contains", value="")
    if kind == "property":
        return PropertyCondition(schema_id="", op="eq", value="")
    if kind in ("tags", "links", "referencePath", "parent", "child", "childPath", "page", "parentPath", "root"):
        # The node-target families bind the LIST by default: "Tags is
        # […]", "Links is […]" — ANY-of membership over the picked ids (the
        # Manual target mode; the mode switch re-targets to a nested query
        # or the hosting page).
        return MemberCondition(family=kind, op="in", target=IdsTarget(ids=()))  # type: ignore[arg-type]
    if kind == "cover":
        return NodeFieldCondition(field="coverAsset", op="exists")
    if kind == "banner":
        return NodeFieldCondition(field="bannerAsset", op="exists")
    if kind == "alias":
        return NodeFieldCondition(field="aliasedNode", op="exists")
    if kind == "created":
        return DateCondition(field="created", op="after", timestamp="")
    return DateCondition(field="updated", op="after", timestamp="")


# --- the immutable draft-tree edit verbs ------------------------------------------


def _child_at(group: Group, index: int) -> Child:
    if not 0 <= index < len(group.children):
        raise IndexError(f"builder draft: child index {index} out of range ({len(group.children)} children)")
    return group.children[index]


def replace_child_at(group: Group, path: Sequence[int], child: Child) -> Group:
    """Return ``group`` with the child at ``path`` replaced (the chrome's
    edit verb over the frozen draft tree — a React-style immutable update).

    ``path`` is the index chain from the root group to the child; every
    intermediate node must be a :class:`Group` (a nested query target is
    replaced through its row's condition, see the chrome's group-box
    closures)."""
    if not path:
        raise ValueError("builder draft: replace path must not be empty")
    index, rest = path[0], tuple(path[1:])
    current = _child_at(group, index)
    if rest:
        if not isinstance(current, Group):
            raise ValueError("builder draft: replace path descends through a non-group child")
        return replace(group, children=group.children[:index] + (replace_child_at(current, rest, child),) + group.children[index + 1 :])
    return replace(group, children=group.children[:index] + (child,) + group.children[index + 1 :])


def insert_child_at(group: Group, index: int, child: Child) -> Group:
    """Return ``group`` with ``child`` inserted at ``index`` (append at
    ``len(children)``)."""
    if not 0 <= index <= len(group.children):
        raise IndexError(f"builder draft: insert index {index} out of range ({len(group.children)} children)")
    return replace(group, children=group.children[:index] + (child,) + group.children[index:])


def remove_child_at(group: Group, index: int) -> Group:
    """Return ``group`` with the child at ``index`` removed."""
    _child_at(group, index)
    return replace(group, children=group.children[:index] + group.children[index + 1 :])
