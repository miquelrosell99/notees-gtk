"""Condition-builder model tests — the ``filter-block-model.test.ts`` +
``filter-query.test.ts`` matrix ported over the GTK v2 AST, plus the dump
round-trip the builder's commit rides.

Laws under test (the v1 regression this guards): the operator column's
value must always read a member of the family register, and an operator
choice must respect the orthogonality — a membership label flips ONLY the
op axis (in ⇄ not-in) and carries the target VERBATIM (ids list, nested
query, or the self marker — the mode never changes under an operator
flip); the set bits re-materialize exists/not-exists; a mode switch
rebuilds ONLY the target; the boolean rows ride the owner's single-selector
ruling; the prune drops half-typed drafts exactly like the web's
``filterQueryToGroup``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from notees_gtk.core.protocol.query_ast import (
    ContentCondition,
    DateCondition,
    EntireWorkspaceScope,
    Group,
    IdsTarget,
    MemberCondition,
    MemberFamily,
    NameCondition,
    NodeFieldCondition,
    Not,
    PagesScope,
    PropertyCondition,
    QueryTarget,
    SelfTarget,
    dump_query_ast,
)
from notees_gtk.core.query.builder import (
    FAMILY_OPTIONS,
    BoundProperty,
    BuilderFacts,
    ConditionKind,
    NodeCollectionToolbarConfig,
    apply_boolean_value,
    apply_operator,
    apply_property_schema_pick,
    apply_row_value,
    apply_target_mode,
    block_view,
    create_condition,
    draft_or_factory_ast,
    factory_query_ast,
    family_of,
    filter_kind_options_for_config,
    filter_query_to_group,
    insert_child_at,
    operator_of,
    operator_options,
    remove_child_at,
    replace_child_at,
)

ID_A = "0192a000-0000-7000-8000-0000000000a1"
ID_B = "0192a000-0000-7000-8000-0000000000a2"

FACTS = BuilderFacts(
    bound_properties=(
        BoundProperty(id="text", name="Text", type="text"),
        BoundProperty(id="num", name="Num", type="number"),
        BoundProperty(id="date", name="Date", type="datetime"),
        BoundProperty(id="bool", name="Bool", type="boolean"),
        BoundProperty(id="sel", name="Sel", type="select", options=(("s1", "One"), ("s2", "Two"))),
        BoundProperty(id="obj", name="Obj", type="object"),
    )
)

EMPTY_ROOT = Group(logic="and", children=())
SAMPLE_ROOT = Group(logic="and", children=(ContentCondition(op="contains", value="x"),))


def m_in(family: MemberFamily, ids: tuple[str, ...] = ("n1",)) -> MemberCondition:
    return MemberCondition(family=family, op="in", target=IdsTarget(ids=ids))


def m_in_q(family: MemberFamily, root: Group = EMPTY_ROOT) -> MemberCondition:
    return MemberCondition(family=family, op="in", target=QueryTarget(root=root))


def m_self(family: MemberFamily) -> MemberCondition:
    return MemberCondition(family=family, op="in", target=SelfTarget())


def m_exists(family: MemberFamily, negative: bool = False) -> MemberCondition:
    return MemberCondition(family=family, op="not-exists" if negative else "exists")


def m_exact(ids: tuple[str, ...] = ("c1",)) -> MemberCondition:
    return MemberCondition(family="class", op="exact", target=IdsTarget(ids=ids))


MEMBER_FAMILIES: list[MemberFamily] = [
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

#: The family's negative membership label (the register's first negative).
NEGATIVE_LABEL: dict[MemberFamily, str] = {
    "class": "not-contains",
    "tags": "is-not",
    "links": "is-not",
    "referencePath": "is-not-any-of",
    "parent": "is-not",
    "child": "not-contains-any-of",
    "childPath": "not-contains",
    "page": "is-not",
    "parentPath": "not-includes",
    "root": "is-not",
}

#: Representative conditions across every family, every target mode, every op axis.
REPRESENTATIVE: list[Any] = [
    m_in("class", ("c1",)),
    m_in_q("class"),
    m_self("class"),
    m_exists("class"),
    m_exists("class", True),
    m_exact(),
    *[m_in(f) for f in MEMBER_FAMILIES[1:]],
    *[m_in_q(f) for f in MEMBER_FAMILIES[1:]],
    *[m_self(f) for f in MEMBER_FAMILIES[1:]],
    # The set bits ride every family EXCEPT referencePath (no set pair in the
    # register) and root (its set bit is vacuous — every node's root exists).
    *[m_exists(f) for f in MEMBER_FAMILIES[1:] if f not in ("referencePath", "root")],
    *[m_exists(f, True) for f in MEMBER_FAMILIES[1:] if f not in ("referencePath", "root")],
    m_in("referencePath"),
    m_in_q("referencePath"),
    m_self("referencePath"),
    ContentCondition(op="contains", value="x"),
    ContentCondition(op="exists"),
    NameCondition(op="contains", value="x"),
    NodeFieldCondition(field="isClass", op="eq", value="true"),
    NodeFieldCondition(field="isEmbedded", op="eq", value="false"),
    NodeFieldCondition(field="coverAsset", op="exists"),
    NodeFieldCondition(field="bannerAsset", op="exists"),
    NodeFieldCondition(field="aliasedNode", op="exists"),
    DateCondition(field="created", op="eq", timestamp="2026-01-01"),
    DateCondition(field="created", op="between", from_="2026-01-01", to="2026-02-01"),
    DateCondition(field="created", op="within", period="today"),
    DateCondition(field="updated", op="gt", timestamp="2026-01-01"),
    PropertyCondition(schema_id="text", op="contains", value="x"),
    PropertyCondition(schema_id="text", op="exists"),
    PropertyCondition(schema_id="num", op="between", from_=1, to=2),
    PropertyCondition(schema_id="date", op="within", period="today"),
    PropertyCondition(schema_id="bool", op="eq", value=True),
]


# --- operatorOf reads a member of the family register -----------------------------


class TestOperatorOfRegister:
    """The boolean rows (Type/Placement) ride a single value selector — no
    operator column at all (the owner's register ruling). Every OTHER family
    renders an operator that is a member of its offered list, at both
    polarities."""

    NO_OPERATOR_COLUMN: tuple[ConditionKind, ...] = ("type", "placement")

    @pytest.mark.parametrize("negated", [False, True])
    def test_representative_conditions_render_an_offered_operator(self, negated: bool) -> None:
        for condition in REPRESENTATIVE:
            family = family_of(condition)
            offered = [option.value for option in operator_options(family, condition, FACTS)]
            if family in self.NO_OPERATOR_COLUMN:
                assert offered == [], f"{family} must offer no operator column"
                continue
            assert operator_of(condition, negated) in offered, f"{family} (negated={negated})"

    def test_every_family_option_list_is_nonempty_except_the_boolean_rows(self) -> None:
        for option in FAMILY_OPTIONS:
            if option.value in self.NO_OPERATOR_COLUMN:
                continue
            assert len(operator_options(option.value, None, FACTS)) > 0, option.value


# --- the orthogonality law: an operator choice flips ONLY the op axis --------------


class TestOperatorOrthogonality:
    def test_the_negative_label_flips_in_to_not_in_ids_carried_verbatim(self) -> None:
        for family in MEMBER_FAMILIES:
            condition = m_in(family, ("n1", "n2"))
            assert apply_operator(condition, NEGATIVE_LABEL[family]) == replace(condition, op="not-in")

    def test_the_negative_label_on_a_query_mode_row_preserves_the_nested_query(self) -> None:
        for family in MEMBER_FAMILIES:
            condition = m_in_q(family, SAMPLE_ROOT)
            result = apply_operator(condition, NEGATIVE_LABEL[family])
            # Wire-level negation — NOT a group wrapper, the target untouched.
            assert result == replace(condition, op="not-in")

    def test_the_negative_label_on_a_self_mode_row_preserves_the_self_marker(self) -> None:
        for family in MEMBER_FAMILIES:
            condition = m_self(family)
            assert apply_operator(condition, NEGATIVE_LABEL[family]) == replace(condition, op="not-in")

    def test_the_positive_label_flips_not_in_to_in_target_carried_verbatim(self) -> None:
        query = MemberCondition(family="links", op="not-in", target=QueryTarget(root=SAMPLE_ROOT))
        assert apply_operator(query, "is") == replace(query, op="in")

    def test_the_set_bits_rematerialize_exists_not_exists_no_target_at_all(self) -> None:
        query = MemberCondition(family="childPath", op="in", target=QueryTarget(root=SAMPLE_ROOT))
        assert apply_operator(query, "set") == MemberCondition(family="childPath", op="exists")
        assert apply_operator(query, "not-set") == MemberCondition(family="childPath", op="not-exists")

    def test_flipping_back_from_a_set_bit_rematerializes_an_in_over_empty_ids(self) -> None:
        set_row = MemberCondition(family="links", op="exists")
        assert apply_operator(set_row, "is") == MemberCondition(
            family="links", op="in", target=IdsTarget(ids=())
        )
        not_set = MemberCondition(family="links", op="not-exists")
        assert apply_operator(not_set, "is") == MemberCondition(
            family="links", op="not-in", target=IdsTarget(ids=())
        )

    def test_class_is_is_not_bind_the_same_in_op_as_contains(self) -> None:
        condition = m_in("class", ("c1",))
        assert apply_operator(condition, "is") == replace(condition, op="in")
        assert apply_operator(condition, "is-not") == replace(condition, op="not-in")

    def test_the_manual_ids_carry_across_the_membership_flips(self) -> None:
        condition = m_in("childPath", ("n1", "n2"))
        assert apply_operator(condition, "not-contains") == replace(condition, op="not-in")


class TestOperatorOfCanonicalLabels:
    def test_canonical_membership_labels_per_family(self) -> None:
        assert operator_of(m_in("class"), False) == "contains"
        assert operator_of(replace(m_in("class"), op="not-in"), False) == "not-contains"
        assert operator_of(m_in("parent"), False) == "is"
        assert operator_of(replace(m_in("parent"), op="not-in"), False) == "is-not"
        assert operator_of(m_in("parentPath"), False) == "includes"
        assert operator_of(replace(m_in("parentPath"), op="not-in"), False) == "not-includes"

    def test_a_not_wrapped_membership_reads_its_negative_value(self) -> None:
        assert operator_of(m_in("links"), True) == "is-not"

    def test_the_wire_not_exists_reads_not_set_a_not_wrapped_exists_reads_not_set(self) -> None:
        assert operator_of(m_exists("class", True), False) == "not-set"
        assert operator_of(m_exists("class"), True) == "not-set"
        assert operator_of(m_exists("class"), False) == "set"


class TestReportedScenario:
    """The reported v1 scenario at the model level: child path contains /
    does not contain a node matching a subquery — the operator flips must
    never touch the nested query target."""

    def test_child_path_contains_a_node_matching_a_subquery(self) -> None:
        inner: MemberCondition = MemberCondition(
            family="links",
            op="in",
            target=QueryTarget(
                root=Group(
                    logic="and",
                    children=(
                        MemberCondition(family="class", op="in", target=IdsTarget(ids=("fuente",))),
                    ),
                )
            ),
        )
        positive = MemberCondition(
            family="childPath",
            op="in",
            target=QueryTarget(root=Group(logic="and", children=(inner,))),
        )
        assert operator_of(positive, False) == "contains"
        assert apply_operator(positive, "not-contains") == replace(positive, op="not-in")
        negated = apply_operator(positive, "not-contains")
        assert isinstance(negated, MemberCondition)
        assert operator_of(negated, False) == "not-contains"
        assert apply_operator(negated, "contains") == positive


# --- the coupling matrix ------------------------------------------------------------
#
# The two selector columns of every filter row are ORTHOGONAL: an operator
# choice touches only the polarity/op axis (the value payload rides across
# it), and a value write touches only the value axis (the operator read and
# the not-wrap polarity hold). Where the register is intentionally
# single-axis (boolean properties — the value IS the predicate; the
# cover/banner/alias set bits — no value column exists), the intended
# behavior is pinned by a named test so the distinction stays documented.


def row_apply_operator(child: Any, op: str) -> Any:
    """Mirror of the BlockRow's operator-column onChange (FilterBlockBuilder.tsx)."""
    view = block_view(child)
    assert view is not None
    return apply_operator(view.condition, op)


def row_apply_value(child: Any, next_condition: Any) -> Any:
    """Mirror of the BlockRow's value onChange (FilterBlockBuilder.tsx)."""
    return apply_row_value(child, next_condition)


def row_operator_of(child: Any) -> str:
    """The operator read a row renders (blockView unwrap + operatorOf)."""
    view = block_view(child)
    assert view is not None
    return operator_of(view.condition, view.negated)


class TestOperatorNeverMutatesValue:
    def test_member_every_offered_operator_keeps_the_ids_verbatim_or_swaps_to_set(self) -> None:
        for family in MEMBER_FAMILIES:
            condition = m_in(family, ("n1", "n2"))
            for option in operator_options(family, condition, FACTS):
                result = apply_operator(condition, option.value)
                if option.value == "set":
                    assert result == MemberCondition(family=family, op="exists"), f"{family}/{option.value}"
                    continue
                if option.value == "not-set":
                    assert result == MemberCondition(family=family, op="not-exists"), f"{family}/{option.value}"
                    continue
                # A membership label flips only the wire op — the target rides verbatim.
                assert isinstance(result, MemberCondition), f"{family}/{option.value}"
                assert result.family == family, f"{family}/{option.value}"
                assert result.op in ("in", "not-in"), f"{family}/{option.value}"
                assert result.target == condition.target, f"{family}/{option.value}"

    def test_content_the_text_rides_every_text_operator(self) -> None:
        text = ContentCondition(op="contains", value="hello")
        assert apply_operator(text, "is") == ContentCondition(op="eq", value="hello")
        assert apply_operator(text, "is-not") == Not(child=ContentCondition(op="eq", value="hello"))
        assert apply_operator(text, "contains") == text
        assert apply_operator(text, "not-contains") == Not(child=text)
        assert apply_operator(text, "starts") == ContentCondition(op="starts", value="hello")
        assert apply_operator(text, "ends") == ContentCondition(op="ends", value="hello")
        assert apply_operator(text, "fts") == ContentCondition(op="fts", value="hello")
        assert apply_operator(text, "set") == ContentCondition(op="exists")
        assert apply_operator(text, "not-set") == Not(child=ContentCondition(op="exists"))

    def test_name_rows_offer_only_the_working_register(self) -> None:
        title = NameCondition(op="contains", value="hello")
        assert [option.value for option in operator_options("content", title, FACTS)] == [
            "is",
            "is-not",
            "contains",
            "not-contains",
            "set",
            "not-set",
        ]
        assert apply_operator(title, "is") == NameCondition(op="eq", value="hello")
        assert apply_operator(title, "is-not") == Not(child=NameCondition(op="eq", value="hello"))
        assert apply_operator(title, "contains") == title
        assert apply_operator(title, "not-contains") == Not(child=title)
        assert apply_operator(title, "set") == NameCondition(op="exists")
        assert apply_operator(title, "not-set") == Not(child=NameCondition(op="exists"))

    def test_content_rows_keep_the_full_register_incl_starts_ends_fts(self) -> None:
        text = ContentCondition(op="contains", value="hello")
        assert [option.value for option in operator_options("content", text, FACTS)] == [
            "is",
            "is-not",
            "contains",
            "not-contains",
            "starts",
            "ends",
            "fts",
            "set",
            "not-set",
        ]

    def test_property_scalar_the_value_rides_the_flips(self) -> None:
        prop = PropertyCondition(schema_id="text", op="contains", value="abc")
        assert apply_operator(prop, "eq") == PropertyCondition(schema_id="text", op="eq", value="abc")
        assert apply_operator(prop, "neq") == PropertyCondition(schema_id="text", op="neq", value="abc")
        assert apply_operator(prop, "contains") == prop
        assert apply_operator(prop, "not-contains") == Not(child=prop)
        for op in ("gt", "gte", "lt", "lte"):
            assert apply_operator(prop, op) == PropertyCondition(schema_id="text", op=op, value="abc")

    def test_property_boolean_the_is_true_is_false_morphs_set_the_value_and_never_wrap(self) -> None:
        truthy = PropertyCondition(schema_id="bool", op="eq", value=True)
        assert apply_operator(truthy, "is-true") == truthy
        assert apply_operator(truthy, "is-false") == PropertyCondition(schema_id="bool", op="eq", value=False)
        # The value IS the predicate: either choice yields a POSITIVE
        # condition — polarity never rides an is-true/is-false label.
        assert not isinstance(apply_operator(truthy, "is-false"), Not)
        assert not isinstance(apply_operator(replace(truthy, value=False), "is-true"), Not)

    def test_property_bounds_and_period_ride_the_repicks(self) -> None:
        between = PropertyCondition(schema_id="num", op="between", from_=1, to=9)
        assert apply_operator(between, "between") == between
        within = PropertyCondition(schema_id="date", op="within", period="this_week")
        assert apply_operator(within, "within") == within

    def test_date_the_timestamp_rides_the_flips_between_carries_it_into_the_range(self) -> None:
        for field in ("created", "updated"):
            stamp = DateCondition(field=field, op="eq", timestamp="2026-03-01")
            assert apply_operator(stamp, "is") == stamp
            assert apply_operator(stamp, "is-not") == Not(child=DateCondition(field=field, op="eq", timestamp="2026-03-01"))
            for op in ("gt", "gte", "lt", "lte"):
                assert apply_operator(stamp, op) == DateCondition(field=field, op=op, timestamp="2026-03-01")
            assert apply_operator(stamp, "between") == DateCondition(
                field=field, op="between", from_="2026-03-01", to="2026-03-01"
            )
            between = DateCondition(field=field, op="between", from_="2026-01-01", to="2026-02-01")
            assert apply_operator(between, "between") == between
            # The raw-AST ordering arms carry the timestamp too (DSL-parsed rows).
            after = DateCondition(field=field, op="after", timestamp="2026-03-01")
            assert apply_operator(after, "before") == DateCondition(field=field, op="before", timestamp="2026-03-01")

    def test_node_field_set_bits_are_the_condition_no_value_column_exists(self) -> None:
        for field in ("coverAsset", "bannerAsset", "aliasedNode"):
            set_row = NodeFieldCondition(field=field, op="exists")
            assert apply_operator(set_row, "exists") == set_row
            assert apply_operator(set_row, "not-exists") == Not(child=set_row)

    def test_node_field_boolean_an_operator_choice_never_touches_the_value(self) -> None:
        for field in ("isClass", "isEmbedded"):
            for value in ("true", "false"):
                role = NodeFieldCondition(field=field, op="eq", value=value)
                assert apply_operator(role, "is") == role
                assert apply_operator(role, "is-not") == Not(child=role)


class TestValueNeverMutatesOperator:
    def test_member_retargeting_keeps_the_operator_label_at_both_polarities(self) -> None:
        child = m_in("links", ("n1",))
        retargeted = row_apply_value(child, m_in("links", ("n9", "n8")))
        assert row_operator_of(retargeted) == row_operator_of(child)
        negated = Not(child=m_in("links", ("n1",)))
        neg_retargeted = row_apply_value(negated, m_in("links", ("n9",)))
        assert neg_retargeted == Not(child=m_in("links", ("n9",)))
        assert row_operator_of(neg_retargeted) == row_operator_of(negated)

    def test_content_and_name_retyping_keeps_the_operator(self) -> None:
        text = ContentCondition(op="contains", value="hello")
        assert row_operator_of(row_apply_value(text, replace(text, value="world"))) == "contains"
        negated = Not(child=ContentCondition(op="eq", value="hello"))
        result = row_apply_value(negated, ContentCondition(op="eq", value="world"))
        assert row_operator_of(result) == "is-not"
        title = NameCondition(op="eq", value="hello")
        assert row_operator_of(row_apply_value(title, replace(title, value="world"))) == "is"

    def test_property_scalar_retyping_keeps_the_op_at_both_polarities(self) -> None:
        prop = PropertyCondition(schema_id="text", op="gt", value="1")
        assert row_operator_of(row_apply_value(prop, replace(prop, value="9"))) == "gt"
        negated = Not(child=PropertyCondition(schema_id="text", op="contains", value="a"))
        result = row_apply_value(negated, PropertyCondition(schema_id="text", op="contains", value="b"))
        assert row_operator_of(result) == "not-contains"

    def test_property_boolean_re_picking_true_false_moves_the_value_only(self) -> None:
        truthy = PropertyCondition(schema_id="bool", op="eq", value=True)
        assert row_operator_of(truthy) == "set"
        falsy = row_apply_value(truthy, apply_boolean_value(truthy, False))
        assert falsy == PropertyCondition(schema_id="bool", op="eq", value=False)
        assert row_operator_of(falsy) == "set"
        negated = Not(child=truthy)
        neg_falsy = row_apply_value(negated, apply_boolean_value(truthy, False))
        assert row_operator_of(neg_falsy) == "not-set"

    def test_date_retyping_the_timestamp_keeps_the_operator(self) -> None:
        stamp = DateCondition(field="created", op="gt", timestamp="2026-01-01")
        assert row_operator_of(row_apply_value(stamp, replace(stamp, timestamp="2026-06-01"))) == "gt"
        negated = Not(child=DateCondition(field="updated", op="eq", timestamp="2026-01-01"))
        result = row_apply_value(negated, DateCondition(field="updated", op="eq", timestamp="2026-06-01"))
        assert row_operator_of(result) == "is-not"

    def test_node_field_boolean_flipping_keeps_the_operator_read_at_both_polarities(self) -> None:
        for field in ("isClass", "isEmbedded"):
            page = NodeFieldCondition(field=field, op="eq", value="false")
            block = row_apply_value(page, NodeFieldCondition(field=field, op="eq", value="true"))
            assert row_operator_of(block) == row_operator_of(page)
            not_page = Not(child=NodeFieldCondition(field=field, op="eq", value="false"))
            not_block = row_apply_value(not_page, NodeFieldCondition(field=field, op="eq", value="true"))
            assert row_operator_of(not_block) == row_operator_of(not_page)


# --- the not-wrap arms back Exclude (NOT) and host-less rows ------------------------


class TestBooleanNotWrapArms:
    @pytest.mark.parametrize("field", ["isEmbedded", "isClass"])
    def test_is_page_is_not_page_is_not_block_is_block_is_page(self, field: str) -> None:
        def role(value: str) -> NodeFieldCondition:
            return NodeFieldCondition(field=field, op="eq", value=value)

        # is/page (the placement default; "false" = standalone).
        child = role("false")
        assert row_operator_of(child) == "is"
        assert child == role("false")
        # → is-not/page: the operator choice wraps; the value HOLDS.
        child = row_apply_operator(child, "is-not")
        assert child == Not(child=role("false"))
        assert row_operator_of(child) == "is-not"
        # → is-not/block: the value choice flips only the boolean; the wrap HOLDS.
        child = row_apply_value(child, role("true"))
        assert child == Not(child=role("true"))
        assert row_operator_of(child) == "is-not"
        # → is/block: the operator choice unwraps; the value HOLDS.
        child = row_apply_operator(child, "is")
        assert child == role("true")
        assert row_operator_of(child) == "is"
        # → is/page: the value choice flips only the boolean; the polarity HOLDS.
        child = row_apply_value(child, role("false"))
        assert child == role("false")
        assert row_operator_of(child) == "is"


# --- the single-selector boolean register (the owner's ruling) ----------------------


class TestSingleSelectorBooleanRegister:
    def test_type_and_placement_rows_offer_no_operator_column_at_either_value(self) -> None:
        for family in ("type", "placement"):
            field = "isClass" if family == "type" else "isEmbedded"
            for value in ("true", "false"):
                condition = NodeFieldCondition(field=field, op="eq", value=value)
                assert operator_options(family, condition, FACTS) == ()

    def test_structural_role_the_value_selector_alone_reaches_page_and_block(self) -> None:
        page = NodeFieldCondition(field="isEmbedded", op="eq", value="false")
        assert row_operator_of(page) == "is"
        block = row_apply_value(page, NodeFieldCondition(field="isEmbedded", op="eq", value="true"))
        assert block == NodeFieldCondition(field="isEmbedded", op="eq", value="true")
        assert row_operator_of(block) == "is"
        back = row_apply_value(block, NodeFieldCondition(field="isEmbedded", op="eq", value="false"))
        assert back == page
        assert row_operator_of(back) == "is"

    def test_type_the_value_selector_alone_reaches_a_class_and_not_a_class(self) -> None:
        klass = NodeFieldCondition(field="isClass", op="eq", value="true")
        assert row_operator_of(klass) == "is"
        not_class = row_apply_value(klass, NodeFieldCondition(field="isClass", op="eq", value="false"))
        assert not_class == NodeFieldCondition(field="isClass", op="eq", value="false")
        assert row_operator_of(not_class) == "is"

    def test_boolean_property_operator_column_offers_only_the_set_bits(self) -> None:
        truthy = PropertyCondition(schema_id="bool", op="eq", value=True)
        assert [option.value for option in operator_options("property", truthy, FACTS)] == ["set", "not-set"]
        exists = PropertyCondition(schema_id="bool", op="exists")
        assert [option.value for option in operator_options("property", exists, FACTS)] == ["set", "not-set"]
        assert operator_of(truthy, False) == "set"
        assert operator_of(exists, False) == "set"
        assert operator_of(exists, True) == "not-set"

    def test_boolean_property_all_states_reachable_and_stable(self) -> None:
        eq = lambda value: PropertyCondition(schema_id="bool", op="eq", value=value)  # noqa: E731
        exists = PropertyCondition(schema_id="bool", op="exists")

        def pick_value(child: Any, value: bool) -> Any:
            return row_apply_value(child, apply_boolean_value(child, value))

        # is true: the default eq row.
        child = eq(True)
        assert row_operator_of(child) == "set"
        # → is false: the value write moves only the value.
        child = pick_value(child, False)
        assert child == eq(False)
        assert row_operator_of(child) == "set"
        # → is set: the set bit re-materializes exists (the value folds away).
        child = row_apply_operator(child, "set")
        assert child == exists
        assert row_operator_of(child) == "set"
        # → is not set: the negative set bit wraps.
        child = row_apply_operator(child, "not-set")
        assert child == Not(child=exists)
        assert row_operator_of(child) == "not-set"
        # → is set: the positive unwraps.
        child = row_apply_operator(child, "set")
        assert child == exists
        # → is true: the value selector re-materializes the eq row from exists
        # (the set+value fold the ruling defines).
        child = pick_value(child, True)
        assert child == eq(True)
        assert row_operator_of(child) == "set"

    def test_boolean_property_value_write_never_moves_the_set_axis(self) -> None:
        truthy = PropertyCondition(schema_id="bool", op="eq", value=True)
        falsy = row_apply_value(truthy, apply_boolean_value(truthy, False))
        assert falsy == PropertyCondition(schema_id="bool", op="eq", value=False)
        assert row_operator_of(falsy) == "set"


# --- blockView ---------------------------------------------------------------------


class TestBlockView:
    def test_not_around_a_condition_is_a_row(self) -> None:
        view = block_view(Not(child=ContentCondition(op="contains", value="x")))
        assert view is not None
        assert view.negated is True
        assert isinstance(view.condition, ContentCondition)

    def test_not_around_a_group_stays_a_group_wrapper(self) -> None:
        assert block_view(Not(child=EMPTY_ROOT)) is None

    def test_a_group_is_never_a_row(self) -> None:
        assert block_view(EMPTY_ROOT) is None
        assert block_view(ContentCondition(op="contains", value="x")) is not None


# --- the target-mode switch ----------------------------------------------------------


class TestTargetModeSwitch:
    """The mode switch rebuilds ONLY the target — the operator (in / not-in)
    survives the mode change verbatim; a set row re-materializes a
    membership op preserving its polarity; the Manual ids carry where the
    row had them."""

    def test_query_mode_preserves_the_operator(self) -> None:
        for family in MEMBER_FAMILIES:
            for op in ("in", "not-in"):
                condition = MemberCondition(family=family, op=op, target=IdsTarget(ids=("n1",)))
                result = apply_target_mode(condition, "query")
                assert result.op == op
                assert isinstance(result.target, QueryTarget)
                assert result.target.root == EMPTY_ROOT

    def test_self_mode_preserves_the_operator(self) -> None:
        for family in MEMBER_FAMILIES:
            for op in ("in", "not-in"):
                condition = MemberCondition(family=family, op=op, target=IdsTarget(ids=("n1",)))
                result = apply_target_mode(condition, "self")
                assert result.op == op
                assert isinstance(result.target, SelfTarget)

    def test_manual_mode_carries_the_current_ids(self) -> None:
        condition = m_in("links", ("n1", "n2"))
        result = apply_target_mode(condition, "manual")
        assert result == condition

    def test_a_set_row_rematerializes_membership_preserving_polarity(self) -> None:
        set_row = MemberCondition(family="links", op="exists")
        assert apply_target_mode(set_row, "manual") == MemberCondition(
            family="links", op="in", target=IdsTarget(ids=())
        )
        not_set = MemberCondition(family="links", op="not-exists")
        assert apply_target_mode(not_set, "self") == MemberCondition(family="links", op="not-in", target=SelfTarget())

    def test_the_mode_switch_leaves_a_query_target_root_verbatim(self) -> None:
        condition = m_in_q("childPath", SAMPLE_ROOT)
        result = apply_target_mode(condition, "query")
        assert result.target == condition.target

    def test_a_non_member_condition_ignores_the_switch(self) -> None:
        content = ContentCondition(op="contains", value="x")
        assert apply_target_mode(content, "self") == content


# --- the property-schema pick --------------------------------------------------------


class TestPropertySchemaPick:
    def test_a_re_pick_normalizes_the_row_back_to_the_plain_shape(self) -> None:
        picked = apply_property_schema_pick("num", "number")
        assert picked == PropertyCondition(schema_id="num", op="eq", value="")

    def test_a_boolean_schema_seeds_the_truth_value(self) -> None:
        picked = apply_property_schema_pick("bool", "boolean")
        assert picked == PropertyCondition(schema_id="bool", op="eq", value=True)

    def test_an_unknown_schema_type_seeds_a_blank_value(self) -> None:
        picked = apply_property_schema_pick("text", None)
        assert picked == PropertyCondition(schema_id="text", op="eq", value="")


# --- createCondition / the add-menu register -----------------------------------------


class TestCreateCondition:
    def test_create_condition_seeds_a_half_typed_default_per_family(self) -> None:
        assert create_condition("class") == m_in("class", ())
        assert create_condition("type") == NodeFieldCondition(field="isClass", op="eq", value="true")
        assert create_condition("placement") == NodeFieldCondition(field="isEmbedded", op="eq", value="false")
        assert create_condition("content") == ContentCondition(op="contains", value="")
        assert create_condition("property") == PropertyCondition(schema_id="", op="eq", value="")
        for family in ("tags", "links", "referencePath", "parent", "child", "childPath", "page", "parentPath", "root"):
            assert create_condition(family) == m_in(family, ())  # type: ignore[arg-type]
        assert create_condition("cover") == NodeFieldCondition(field="coverAsset", op="exists")
        assert create_condition("banner") == NodeFieldCondition(field="bannerAsset", op="exists")
        assert create_condition("alias") == NodeFieldCondition(field="aliasedNode", op="exists")
        assert create_condition("created") == DateCondition(field="created", op="after", timestamp="")
        assert create_condition("edited") == DateCondition(field="updated", op="after", timestamp="")


class TestAddMenuRegister:
    def test_the_register_offers_the_families_plus_the_group_constructors_in_order(self) -> None:
        assert [option.value for option in filter_kind_options_for_config()] == [
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
            "group-and",
            "group-or",
            "not",
        ]

    def test_config_gates_the_class_property_date_kinds_only(self) -> None:
        values = [
            option.value
            for option in filter_kind_options_for_config(
                NodeCollectionToolbarConfig(klass=False, properties=False, date_range=False)
            )
        ]
        assert "class" not in values
        assert "property" not in values
        assert "created" not in values
        assert "edited" not in values
        assert "content" in values
        assert "group-and" in values
        assert "not" in values


# --- the draft → composed group prune ------------------------------------------------


def g(logic: str, *children: Any) -> Group:
    return Group(logic=logic, children=children)  # type: ignore[arg-type]


class TestPrune:
    def test_returns_none_for_the_untouched_query(self) -> None:
        assert filter_query_to_group(EMPTY_ROOT) is None

    def test_drops_half_typed_conditions(self) -> None:
        draft = g(
            "and",
            ContentCondition(op="contains", value="   "),
            m_in("class", ()),
            PropertyCondition(schema_id="", op="eq", value="42"),
            DateCondition(field="created", op="after", timestamp=""),
            DateCondition(field="created", op="before", timestamp="  "),
            NodeFieldCondition(field="isClass", op="eq", value="true"),
        )
        assert filter_query_to_group(draft) == g("and", NodeFieldCondition(field="isClass", op="eq", value="true"))

    def test_drops_half_typed_memberships_keeps_the_set_bits_and_picked_rows(self) -> None:
        draft = g(
            "and",
            m_in("page", ()),
            m_in_q("page"),
            m_in_q("page", g("and", m_in("class", ()))),
            m_in("parentPath", ()),
            m_in("root", ()),
            m_in("tags", ()),
            m_in("links", ()),
            m_in("childPath", ()),
            DateCondition(field="created", op="eq", timestamp="  "),
            DateCondition(field="created", op="between", from_="2026-10-01", to=""),
            PropertyCondition(schema_id="", op="between", from_=1, to=5),
            PropertyCondition(schema_id=ID_A, op="between", from_=None, to=5),
            m_exists("class"),
            ContentCondition(op="exists"),
            m_exists("tags"),
            m_exists("links"),
            m_exists("parent"),
            m_exists("page"),
            m_exact((ID_A,)),
            m_in("class", (ID_B,)),
            m_in("page", (ID_A,)),
            m_in_q("page", g("and", m_exact((ID_A,)))),
            m_in("tags", (ID_B,)),
            DateCondition(field="created", op="within", period="today"),
        )
        assert filter_query_to_group(draft) == g(
            "and",
            m_exists("class"),
            ContentCondition(op="exists"),
            m_exists("tags"),
            m_exists("links"),
            m_exists("parent"),
            m_exists("page"),
            m_exact((ID_A,)),
            m_in("class", (ID_B,)),
            m_in("page", (ID_A,)),
            m_in_q("page", g("and", m_exact((ID_A,)))),
            m_in("tags", (ID_B,)),
            DateCondition(field="created", op="within", period="today"),
        )

    def test_drops_half_typed_query_targets_and_blank_timestamps_to_nothing(self) -> None:
        draft = g(
            "and",
            m_in_q("links"),
            m_in_q("childPath", g("and", m_in("class", ()))),
            m_in_q("class"),
            m_in_q("tags", g("and", m_in("tags", ()))),
            DateCondition(field="updated", op="after", timestamp=" "),
            DateCondition(field="updated", op="before", timestamp=""),
            m_in("links", ()),
        )
        assert filter_query_to_group(draft) is None

    def test_keeps_a_query_target_whose_nested_group_has_a_surviving_condition(self) -> None:
        draft = g(
            "and",
            m_in_q(
                "links",
                g("and", NodeFieldCondition(field="isClass", op="eq", value="false"), PropertyCondition(schema_id="", op="eq")),
            ),
            m_in_q("class", g("and", m_in("class", (ID_A,)))),
        )
        assert filter_query_to_group(draft) == g(
            "and",
            m_in_q("links", g("and", NodeFieldCondition(field="isClass", op="eq", value="false"))),
            m_in_q("class", g("and", m_in("class", (ID_A,)))),
        )

    def test_keeps_fully_specified_conditions_verbatim_and_preserves_the_root_logic(self) -> None:
        draft = g(
            "or",
            m_in("class", (ID_A,)),
            PropertyCondition(schema_id=ID_A, op="gte", value="5"),
            DateCondition(field="created", op="after", timestamp="2026-01-01"),
            NodeFieldCondition(field="coverAsset", op="exists"),
        )
        assert filter_query_to_group(draft) == draft

    def test_recurses_into_nested_groups_drops_emptied_ones_and_drops_a_not_whose_child_pruned(self) -> None:
        draft = g(
            "and",
            g("or", ContentCondition(op="contains", value=""), m_in("class", (ID_B,))),
            Not(child=m_in("class", ())),
            Not(child=g("and", ContentCondition(op="contains", value="draft"))),
        )
        assert filter_query_to_group(draft) == g(
            "and",
            g("or", m_in("class", (ID_B,))),
            Not(child=g("and", ContentCondition(op="contains", value="draft"))),
        )

    def test_the_self_target_survives_the_prune(self) -> None:
        draft = g("and", m_self("links"))
        assert filter_query_to_group(draft) == draft


# --- draftOrFactoryAst ------------------------------------------------------------


class TestDraftOrFactory:
    def test_an_empty_draft_commits_the_factory_match_all(self) -> None:
        ast = draft_or_factory_ast(EMPTY_ROOT)
        assert ast == factory_query_ast()
        assert ast.scope == EntireWorkspaceScope()
        assert ast.root == EMPTY_ROOT

    def test_the_scope_rides_the_commit_verbatim(self) -> None:
        draft = g("and", m_in("class", (ID_A,)))
        ast = draft_or_factory_ast(draft, PagesScope())
        assert ast.scope == PagesScope()
        assert ast.root == draft

    def test_a_fully_pruned_draft_still_commits_a_parseable_ast(self) -> None:
        ast = draft_or_factory_ast(g("and", m_in("class", ())), PagesScope())
        assert ast == factory_query_ast(PagesScope())
        assert dump_query_ast(ast)["root"] == {"type": "group", "logic": "and", "children": []}


# --- the immutable draft-tree edit verbs ---------------------------------------------


class TestDraftTreeVerbs:
    def test_replace_child_at_replaces_nested_paths(self) -> None:
        draft = g("and", g("or", m_in("class", ()), m_in("links", ())))
        replaced = replace_child_at(draft, (0, 1), m_in("links", ("n9",)))
        assert replaced == g("and", g("or", m_in("class", ()), m_in("links", ("n9",))))
        assert draft == g("and", g("or", m_in("class", ()), m_in("links", ())))  # immutable

    def test_replace_child_at_fails_loud_on_bad_paths(self) -> None:
        draft = g("and", ContentCondition(op="contains", value="x"))
        with pytest.raises(IndexError):
            replace_child_at(draft, (5,), ContentCondition(op="contains", value="y"))
        with pytest.raises(ValueError):
            replace_child_at(draft, (), ContentCondition(op="contains", value="y"))
        with pytest.raises(ValueError):
            replace_child_at(draft, (0, 0), ContentCondition(op="contains", value="y"))

    def test_insert_and_remove_child_at(self) -> None:
        draft = g("and", m_in("class", (ID_A,)))
        inserted = insert_child_at(draft, 1, m_in("links", (ID_B,)))
        assert inserted == g("and", m_in("class", (ID_A,)), m_in("links", (ID_B,)))
        removed = remove_child_at(inserted, 0)
        assert removed == g("and", m_in("links", (ID_B,)))
        with pytest.raises(IndexError):
            remove_child_at(draft, 3)
        with pytest.raises(IndexError):
            insert_child_at(draft, 5, m_in("links", ()))


# ------------------------------------------------------------------ UI smoke


def test_ui_builder_imports_on_gtk_host() -> None:
    """Smoke test: the builder chrome must import on a machine with PyGObject.

    Skipped on headless boxes without GTK; kept here so ``uv run pytest`` on a
    GTK host (Arch + libadwaita) exercises the import path at least once. All
    behavior above rides the headless model — the chrome is widgets only.
    """
    pytest.importorskip("gi")
    from notees_gtk.ui import query_builder as _query_builder  # noqa: F401
    from notees_gtk.ui import query_section as _query_section  # noqa: F401
    from notees_gtk.ui import query_tokens as _query_tokens  # noqa: F401
