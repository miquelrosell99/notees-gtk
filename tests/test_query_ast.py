"""QueryAST v2 strict-parse tests — the rejection/acceptance matrix ported
from ``packages/protocol/test/query-ast-v2.test.ts``.

Laws under test: ``version: 1`` is rejected outright (no backward
compatibility — the stored log converges by a one-time rewrite, not by
dual-parse); every schema is ``.strict()`` (exact key sets, no coercion,
fail loud); unknown condition types / families / ops / target modes fail at
parse time; the member op/target coupling is enforced structurally.
"""

from __future__ import annotations

from typing import Any

import pytest

from notees_gtk.core.protocol.query_ast import (
    ContentCondition,
    IdsTarget,
    MemberCondition,
    Not,
    QueryAstError,
    QueryTarget,
    SelfTarget,
    SubtreeScope,
    parse_query_ast,
)

ID_A = "11111111-1111-4111-8111-111111111111"
ID_B = "22222222-2222-4222-8222-222222222222"
ID_C = "33333333-3333-4333-8333-333333333333"

EMPTY_GROUP = {"type": "group", "logic": "and", "children": []}

def wrap(condition: Any) -> dict[str, Any]:
    return {
        "version": 2,
        "scope": {"type": "pages"},
        "root": {"type": "group", "logic": "and", "children": [condition]},
    }


class TestRootSchema:
    def test_accepts_version_two_and_rejects_version_one_outright(self) -> None:
        assert parse_query_ast(wrap({"type": "content", "op": "exists"})).version == 2
        with pytest.raises(QueryAstError):
            parse_query_ast({**wrap({"type": "contentSet"}), "version": 1})

    def test_rejects_newer_versions_and_extra_keys(self) -> None:
        base = {"scope": {"type": "pages"}, "root": EMPTY_GROUP}
        with pytest.raises(QueryAstError):
            parse_query_ast({**base, "version": 3})
        with pytest.raises(QueryAstError):
            parse_query_ast({**base, "version": 2, "extra": True})

    def test_parses_a_full_ast_with_sort_and_aggregation(self) -> None:
        ast = parse_query_ast(
            {
                "version": 2,
                "scope": {"type": "subtree", "pageId": ID_A},
                "root": {
                    "type": "group",
                    "logic": "or",
                    "children": [
                        {"type": "content", "op": "contains", "value": "hello"},
                        {"type": "not", "child": {"type": "name", "op": "eq", "value": "draft"}},
                    ],
                },
                "sort": [{"field": "name", "dir": "asc"}],
                "aggregation": {"dimensions": [{"kind": "isClass"}], "measures": [{"function": "count"}]},
            }
        )
        assert isinstance(ast.scope, SubtreeScope)
        assert ast.sort is not None and ast.sort[0].field == "name"
        assert ast.aggregation is not None and ast.aggregation.measures[0].function == "count"
        assert isinstance(ast.root.children[1], Not)

    def test_rejects_malformed_uuids_in_id_positions(self) -> None:
        with pytest.raises(QueryAstError):
            parse_query_ast(
                {
                    "version": 2,
                    "scope": {"type": "subtree", "pageId": "not-a-uuid"},
                    "root": EMPTY_GROUP,
                }
            )
        with pytest.raises(QueryAstError):
            parse_query_ast(
                wrap({"type": "member", "family": "class", "op": "in", "target": {"mode": "ids", "ids": ["not-a-uuid"]}})
            )

    def test_rejects_non_object_and_snake_case(self) -> None:
        with pytest.raises(QueryAstError):
            parse_query_ast([1, 2, 3])  # type: ignore[arg-type]
        with pytest.raises(QueryAstError):
            parse_query_ast({"version": 2, "scope": {"type": "pages"}, "root": {"type": "group", "logic": "and", "children": [], "child_nodes": []}})


FAMILIES = ["class", "tags", "links", "referencePath", "parent", "child", "childPath", "page", "parentPath", "root"]


class TestMemberCondition:
    def test_round_trips_in_not_in_over_all_target_modes_for_every_family(self) -> None:
        targets = [
            {"mode": "ids", "ids": [ID_A, ID_B]},
            {"mode": "query", "root": EMPTY_GROUP},
            {"mode": "self"},
        ]
        for family in FAMILIES:
            for op in ("in", "not-in"):
                for target in targets:
                    condition = {"type": "member", "family": family, "op": op, "target": target}
                    parsed = parse_query_ast(wrap(condition))
                    member = parsed.root.children[0]
                    assert isinstance(member, MemberCondition)
                    assert member.family == family

    def test_round_trips_exists_not_exists_with_no_target_for_every_family(self) -> None:
        for family in FAMILIES:
            for op in ("exists", "not-exists"):
                parsed = parse_query_ast(wrap({"type": "member", "family": family, "op": op}))
                member = parsed.root.children[0]
                assert isinstance(member, MemberCondition)
                assert member.family == family and member.op == op and member.target is None

    def test_accepts_class_exact_with_a_single_class_id(self) -> None:
        parsed = parse_query_ast(
            wrap({"type": "member", "family": "class", "op": "exact", "target": {"mode": "ids", "ids": [ID_A]}})
        )
        member = parsed.root.children[0]
        assert isinstance(member, MemberCondition)
        assert isinstance(member.target, IdsTarget)
        assert member.target.ids == (ID_A,)

    def test_requires_a_target_for_in_not_in(self) -> None:
        with pytest.raises(QueryAstError):
            parse_query_ast(wrap({"type": "member", "family": "class", "op": "in"}))
        with pytest.raises(QueryAstError):
            parse_query_ast(wrap({"type": "member", "family": "tags", "op": "not-in"}))

    def test_rejects_a_target_on_exists_not_exists(self) -> None:
        with pytest.raises(QueryAstError):
            parse_query_ast(wrap({"type": "member", "family": "class", "op": "exists", "target": {"mode": "self"}}))
        with pytest.raises(QueryAstError):
            parse_query_ast(
                wrap({"type": "member", "family": "links", "op": "not-exists", "target": {"mode": "ids", "ids": [ID_A]}})
            )

    def test_rejects_exact_on_non_class_families(self) -> None:
        for family in [item for item in FAMILIES if item != "class"]:
            with pytest.raises(QueryAstError):
                parse_query_ast(
                    wrap({"type": "member", "family": family, "op": "exact", "target": {"mode": "ids", "ids": [ID_A]}})
                )

    def test_rejects_exact_with_zero_or_multiple_ids_or_a_non_ids_target(self) -> None:
        with pytest.raises(QueryAstError):
            parse_query_ast(
                wrap({"type": "member", "family": "class", "op": "exact", "target": {"mode": "ids", "ids": [ID_A, ID_B]}})
            )
        with pytest.raises(QueryAstError):
            parse_query_ast(wrap({"type": "member", "family": "class", "op": "exact", "target": {"mode": "ids", "ids": []}}))
        with pytest.raises(QueryAstError):
            parse_query_ast(wrap({"type": "member", "family": "class", "op": "exact", "target": {"mode": "self"}}))

    def test_rejects_unknown_family_op_target_mode(self) -> None:
        with pytest.raises(QueryAstError):
            parse_query_ast(
                wrap({"type": "member", "family": "siblings", "op": "in", "target": {"mode": "ids", "ids": [ID_A]}})
            )
        with pytest.raises(QueryAstError):
            parse_query_ast(
                wrap({"type": "member", "family": "class", "op": "overlaps", "target": {"mode": "ids", "ids": [ID_A]}})
            )
        with pytest.raises(QueryAstError):
            parse_query_ast(wrap({"type": "member", "family": "class", "op": "in", "target": {"mode": "every"}}))
        with pytest.raises(QueryAstError):
            parse_query_ast(
                wrap({"type": "member", "family": "class", "op": "in", "target": {"mode": "ids", "ids": ["not-a-uuid"]}})
            )

    def test_ids_target_requires_at_least_one_id(self) -> None:
        with pytest.raises(QueryAstError):
            parse_query_ast(wrap({"type": "member", "family": "class", "op": "in", "target": {"mode": "ids", "ids": []}}))

    def test_round_trips_nested_query_targets_with_conditions_inside(self) -> None:
        parsed = parse_query_ast(
            wrap(
                {
                    "type": "member",
                    "family": "links",
                    "op": "in",
                    "target": {
                        "mode": "query",
                        "root": {
                            "type": "group",
                            "logic": "and",
                            "children": [
                                {"type": "member", "family": "class", "op": "in", "target": {"mode": "ids", "ids": [ID_C]}}
                            ],
                        },
                    },
                }
            )
        )
        member = parsed.root.children[0]
        assert isinstance(member, MemberCondition)
        assert isinstance(member.target, QueryTarget)
        assert isinstance(member.target.root.children[0], MemberCondition)

    def test_self_target_parses_for_every_family(self) -> None:
        for family in FAMILIES:
            parsed = parse_query_ast(wrap({"type": "member", "family": family, "op": "in", "target": {"mode": "self"}}))
            member = parsed.root.children[0]
            assert isinstance(member, MemberCondition)
            assert isinstance(member.target, SelfTarget)


class TestValueConditions:
    def test_content_ops_plus_folded_exists(self) -> None:
        for op in ("contains", "fts", "eq", "neq", "starts", "ends", "exists"):
            parsed = parse_query_ast(wrap({"type": "content", "op": op}))
            condition = parsed.root.children[0]
            assert isinstance(condition, ContentCondition)
            assert condition.op == op
        assert parse_query_ast(wrap({"type": "content", "op": "contains", "value": "x"})).root.children[0].value == "x"
        assert parse_query_ast(wrap({"type": "content", "op": "contains"})).root.children[0].value is None
        with pytest.raises(QueryAstError):
            parse_query_ast(wrap({"type": "content", "op": "matches"}))
        with pytest.raises(QueryAstError):
            parse_query_ast(wrap({"type": "content", "op": "exists", "value": 4}))  # type: ignore[dict-item]

    def test_content_value_must_be_non_empty_when_present(self) -> None:
        with pytest.raises(QueryAstError):
            parse_query_ast(wrap({"type": "content", "op": "contains", "value": ""}))

    def test_name_unchanged_operator_set(self) -> None:
        for op in ("eq", "neq", "contains", "exists"):
            assert parse_query_ast(wrap({"type": "name", "op": op})).root.children[0].op == op
            assert parse_query_ast(wrap({"type": "name", "op": op, "value": "x"})).root.children[0].value == "x"
        with pytest.raises(QueryAstError):
            parse_query_ast(wrap({"type": "name", "op": "starts", "value": "x"}))

    def test_property_base_ops_plus_folded_between_within(self) -> None:
        for op in ("eq", "neq", "contains", "exists", "gt", "gte", "lt", "lte", "between", "within"):
            parsed = parse_query_ast(wrap({"type": "property", "schemaId": ID_A, "op": op}))
            assert parsed.root.children[0].op == op
        full = parse_query_ast(
            wrap({"type": "property", "schemaId": ID_A, "op": "between", "from": 1, "to": 10, "includeDefaults": False})
        )
        assert full.root.children[0].from_ == 1 and full.root.children[0].to == 10
        assert full.root.children[0].include_defaults is False
        within = parse_query_ast(wrap({"type": "property", "schemaId": ID_A, "op": "within", "period": "this_week"}))
        assert within.root.children[0].period == "this_week"
        with pytest.raises(QueryAstError):
            parse_query_ast(wrap({"type": "property", "schemaId": ID_A, "op": "overlaps"}))
        with pytest.raises(QueryAstError):
            parse_query_ast(wrap({"type": "property", "schemaId": "nope", "op": "eq"}))

    def test_date_created_updated_fields_over_the_full_op_set(self) -> None:
        ops = ["eq", "neq", "gt", "gte", "lt", "lte", "after", "before", "between", "within"]
        for field in ("created", "updated"):
            for op in ops:
                parsed = parse_query_ast(
                    wrap({"type": "date", "field": field, "op": op, "timestamp": "2026-01-01"})
                )
                assert parsed.root.children[0].field == field
            assert parse_query_ast(
                wrap({"type": "date", "field": field, "op": "between", "from": "a", "to": "b"})
            ).root.children[0].from_ == "a"
            assert parse_query_ast(
                wrap({"type": "date", "field": field, "op": "within", "period": "today"})
            ).root.children[0].period == "today"
            with pytest.raises(QueryAstError):
                parse_query_ast(wrap({"type": "date", "field": field, "op": "since"}))
        with pytest.raises(QueryAstError):
            parse_query_ast(wrap({"type": "date", "field": "deleted", "op": "eq", "timestamp": "x"}))

    def test_node_field_the_five_wire_fields(self) -> None:
        for field in ("coverAsset", "bannerAsset", "aliasedNode"):
            for op in ("eq", "neq", "exists"):
                parsed = parse_query_ast(wrap({"type": "nodeField", "field": field, "op": op}))
                assert parsed.root.children[0].field == field
                parsed = parse_query_ast(wrap({"type": "nodeField", "field": field, "op": op, "value": ID_A}))
                assert parsed.root.children[0].value == ID_A
        for field in ("isClass", "isEmbedded"):
            for value in ("true", "false"):
                parsed = parse_query_ast(wrap({"type": "nodeField", "field": field, "op": "eq", "value": value}))
                assert parsed.root.children[0].value == value
        with pytest.raises(QueryAstError):
            parse_query_ast(wrap({"type": "nodeField", "field": "icon", "op": "eq"}))
        with pytest.raises(QueryAstError):
            parse_query_ast(wrap({"type": "nodeField", "field": "isClass", "op": "gt", "value": "true"}))


class TestStrictness:
    def test_rejects_unknown_condition_types_and_stray_keys(self) -> None:
        with pytest.raises(QueryAstError):
            parse_query_ast(wrap({"type": "class", "classId": ID_A}))
        with pytest.raises(QueryAstError):
            parse_query_ast(wrap({"type": "content", "op": "contains", "value": "x", "extra": 1}))
        with pytest.raises(QueryAstError):
            parse_query_ast(
                wrap({"type": "member", "family": "class", "op": "in", "target": {"mode": "ids", "ids": [ID_A], "also": True}})
            )

    def test_rejects_malformed_aggregations(self) -> None:
        base = {"version": 2, "scope": {"type": "pages"}, "root": EMPTY_GROUP}
        with pytest.raises(QueryAstError):
            parse_query_ast({**base, "aggregation": {"dimensions": [{"kind": "wobble"}], "measures": [{"function": "count"}]}})
        with pytest.raises(QueryAstError):
            parse_query_ast({**base, "aggregation": {"dimensions": [], "measures": [{"function": "sum"}]}})
        with pytest.raises(QueryAstError):
            parse_query_ast({**base, "aggregation": {"dimensions": [], "measures": []}})
        with pytest.raises(QueryAstError):
            parse_query_ast(
                {**base, "aggregation": {"dimensions": [], "measures": [{"function": "count", "kind": "property", "id": ID_A}]}}
            )
        with pytest.raises(QueryAstError):
            parse_query_ast(
                {**base, "aggregation": {"dimensions": [{"kind": "isClass", "id": ID_A}], "measures": [{"function": "count"}]}}
            )

    def test_accepts_a_well_formed_aggregation(self) -> None:
        parsed = parse_query_ast(
            {
                "version": 2,
                "scope": {"type": "pages"},
                "root": EMPTY_GROUP,
                "aggregation": {
                    "dimensions": [
                        {"kind": "isClass"},
                        {"kind": "isEmbedded"},
                        {"kind": "class", "id": ID_A},
                        {"kind": "property", "id": ID_B},
                    ],
                    "measures": [
                        {"function": "count"},
                        {"function": "countDistinct", "kind": "node"},
                        {"function": "sum", "kind": "property", "id": ID_C},
                    ],
                },
            }
        )
        assert parsed.aggregation is not None
        assert len(parsed.aggregation.dimensions) == 4
        assert len(parsed.aggregation.measures) == 3

    def test_rejects_a_not_wrapping_a_not(self) -> None:
        with pytest.raises(QueryAstError):
            parse_query_ast(
                wrap({"type": "not", "child": {"type": "not", "child": {"type": "content", "op": "exists"}}})
            )
