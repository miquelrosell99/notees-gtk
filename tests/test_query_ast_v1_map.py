"""The pure v1→v2 query-AST mapping — the ``map-query-ast-v1-v2.ts`` port's
parity battery (mirrors the monorepo's ``map-query-ast-v1-v2.test.ts``
intent): every member-spec kind, the folded condition types, the ten date
types, not/group recursion, pass-through fields, the fail-loud paths (with
the tree path), and strict-parseability of a representative mapped tree.
"""

from __future__ import annotations

from typing import Any

import pytest

from notees_gtk.core.protocol.query_ast import parse_query_ast
from notees_gtk.core.query.ast_v1_map import (
    QueryAstV1MapError,
    is_query_ast_v1,
    map_query_ast_v1_to_v2,
)

V1_ENVELOPE: dict[str, Any] = {
    "version": 1,
    "scope": {"type": "entire_workspace"},
    "root": {"type": "group", "logic": "and", "children": []},
}


def mapped_root(children: Any) -> dict[str, Any]:
    envelope = dict(V1_ENVELOPE)
    envelope["root"] = {"type": "group", "logic": "and", "children": children}
    result = map_query_ast_v1_to_v2(envelope)
    assert isinstance(result, dict)
    return result["root"]


class TestGuard:
    def test_is_query_ast_v1(self) -> None:
        assert is_query_ast_v1(V1_ENVELOPE) is True
        assert is_query_ast_v1({**V1_ENVELOPE, "version": 2}) is False
        assert is_query_ast_v1({"version": 1}) is False
        assert is_query_ast_v1("nope") is False
        rootless = dict(V1_ENVELOPE)
        del rootless["root"]
        assert is_query_ast_v1(rootless) is False

    def test_v2_input_fails_loud(self) -> None:
        with pytest.raises(QueryAstV1MapError, match=r"expected version 1"):
            map_query_ast_v1_to_v2({**V1_ENVELOPE, "version": 2})


class TestMemberSpecs:
    def test_one(self) -> None:
        assert mapped_root([{"type": "class", "classId": "c1"}])["children"] == [
            {"type": "member", "family": "class", "op": "in", "target": {"mode": "ids", "ids": ["c1"]}}
        ]

    def test_many(self) -> None:
        assert mapped_root([{"type": "tagAny", "tagIds": ["t1", "t2"]}])["children"] == [
            {"type": "member", "family": "tags", "op": "in", "target": {"mode": "ids", "ids": ["t1", "t2"]}}
        ]

    def test_query(self) -> None:
        root = mapped_root(
            [
                {
                    "type": "linkedToQuery",
                    "root": {"type": "group", "logic": "or", "children": [{"type": "name", "op": "eq", "value": "x"}]},
                }
            ]
        )
        (child,) = root["children"]
        assert child["target"]["mode"] == "query"
        assert child["target"]["root"]["logic"] == "or"

    def test_self(self) -> None:
        assert mapped_root([{"type": "childOfSelf"}])["children"] == [
            {"type": "member", "family": "child", "op": "in", "target": {"mode": "self"}}
        ]

    def test_set(self) -> None:
        assert mapped_root([{"type": "linkSet"}])["children"] == [
            {"type": "member", "family": "links", "op": "exists"}
        ]

    def test_exact(self) -> None:
        assert mapped_root([{"type": "classExact", "classId": "c1"}])["children"] == [
            {"type": "member", "family": "class", "op": "exact", "target": {"mode": "ids", "ids": ["c1"]}}
        ]

    def test_missing_id_fails_loud_with_path(self) -> None:
        with pytest.raises(QueryAstV1MapError, match=r"classId.*\$\.root\.children\[0\]"):
            mapped_root([{"type": "class"}])


class TestFoldedConditions:
    def test_content_and_content_set(self) -> None:
        children = mapped_root(
            [{"type": "content", "op": "contains", "value": "x"}, {"type": "contentSet"}]
        )["children"]
        assert children == [
            {"type": "content", "op": "contains", "value": "x"},
            {"type": "content", "op": "exists"},
        ]

    def test_property_variants(self) -> None:
        children = mapped_root(
            [
                {"type": "property", "schemaId": "s", "op": "eq", "value": 3, "includeDefaults": True},
                {"type": "propertyBetween", "schemaId": "s", "from": 1, "to": 9},
                {"type": "propertyWithin", "schemaId": "s", "period": "this_week"},
            ]
        )["children"]
        assert children[0] == {
            "type": "property",
            "schemaId": "s",
            "op": "eq",
            "value": 3,
            "includeDefaults": True,
        }
        assert children[1] == {"type": "property", "schemaId": "s", "op": "between", "from": 1, "to": 9}
        assert children[2] == {"type": "property", "schemaId": "s", "op": "within", "period": "this_week"}

    def test_boolean_fields(self) -> None:
        children = mapped_root([{"type": "isClass", "isClass": True}, {"type": "isEmbedded", "isEmbedded": False}])[
            "children"
        ]
        assert children == [
            {"type": "nodeField", "field": "isClass", "op": "eq", "value": "true"},
            {"type": "nodeField", "field": "isEmbedded", "op": "eq", "value": "false"},
        ]

    def test_boolean_field_requires_boolean(self) -> None:
        with pytest.raises(QueryAstV1MapError, match=r"expected a boolean"):
            mapped_root([{"type": "isClass", "isClass": "yes"}])

    def test_wire_node_fields(self) -> None:
        (child,) = mapped_root([{"type": "coverAsset", "op": "eq", "value": "id"}])["children"]
        assert child == {"type": "nodeField", "field": "coverAsset", "op": "eq", "value": "id"}


class TestDateTypes:
    def test_all_ten(self) -> None:
        cases = {
            "createdAfter": ("created", "after"),
            "createdBefore": ("created", "before"),
            "createdCmp": ("created", "cmp"),
            "createdBetween": ("created", "between"),
            "createdWithin": ("created", "within"),
            "updatedAfter": ("updated", "after"),
            "updatedBefore": ("updated", "before"),
            "updatedCmp": ("updated", "cmp"),
            "updatedBetween": ("updated", "between"),
            "updatedWithin": ("updated", "within"),
        }
        for v1_type, (field, op) in cases.items():
            condition: dict[str, Any] = {"type": v1_type}
            if op == "between":
                condition.update({"from": "2026-01-01T00:00:00Z", "to": "2026-02-01T00:00:00Z"})
            elif op == "within":
                condition["period"] = "today"
            elif op == "cmp":
                condition.update({"op": "gt", "timestamp": "2026-01-01T00:00:00Z"})
            else:
                condition["timestamp"] = "2026-01-01T00:00:00Z"
            (child,) = mapped_root([condition])["children"]
            assert child["type"] == "date"
            assert child["field"] == field
            # The mapped op is the v1 op VALUE for cmp (gt here), the folded
            # op name for the between/within/after/before hats.
            assert child["op"] == ("gt" if op == "cmp" else op)


class TestTree:
    def test_not_wraps_condition_and_group(self) -> None:
        children = mapped_root(
            [
                {"type": "not", "child": {"type": "name", "op": "eq", "value": "x"}},
                {
                    "type": "not",
                    "child": {"type": "group", "logic": "or", "children": [{"type": "contentSet"}]},
                },
            ]
        )["children"]
        assert children[0] == {
            "type": "not",
            "child": {"type": "name", "op": "eq", "value": "x"},
        }
        assert children[1] == {
            "type": "not",
            "child": {
                "type": "group",
                "logic": "or",
                "children": [{"type": "content", "op": "exists"}],
            },
        }

    def test_nested_groups_and_logic_validation(self) -> None:
        root = mapped_root(
            [
                {
                    "type": "group",
                    "logic": "or",
                    "children": [{"type": "group", "logic": "and", "children": [{"type": "contentSet"}]}],
                }
            ]
        )
        assert root["children"][0]["logic"] == "or"
        with pytest.raises(QueryAstV1MapError, match=r"expected 'and' \| 'or'"):
            mapped_root([{"type": "group", "logic": "xor", "children": []}])

    def test_scope_sort_aggregation_pass_through(self) -> None:
        envelope = {
            "version": 1,
            "scope": {"type": "subtree", "rootId": "r"},
            "root": {"type": "group", "logic": "and", "children": []},
            "sort": [{"field": "name", "dir": "asc"}],
            "aggregation": {"dimensions": [{"type": "member", "family": "class", "op": "in", "target": {"mode": "ids", "ids": ["c"]}}]},
        }
        mapped = map_query_ast_v1_to_v2(envelope)
        assert mapped["scope"] == {"type": "subtree", "rootId": "r"}
        assert mapped["sort"] == [{"field": "name", "dir": "asc"}]
        assert "aggregation" in mapped

    def test_unknown_condition_fails_loud_with_path(self) -> None:
        with pytest.raises(QueryAstV1MapError, match=r"unknown v1 condition type.*children\[1\]"):
            mapped_root([{"type": "contentSet"}, {"type": "warp"}])

    def test_mapped_tree_strict_parses(self) -> None:
        class_id = "01920000-0000-7000-8000-000000000001"
        envelope = {
            "version": 1,
            "scope": {"type": "entire_workspace"},
            "root": {
                "type": "group",
                "logic": "and",
                "children": [
                    {"type": "class", "classId": class_id},
                    {"type": "not", "child": {"type": "createdBefore", "timestamp": "2026-01-01T00:00:00Z"}},
                    {"type": "isEmbedded", "isEmbedded": False},
                ],
            },
        }
        mapped = map_query_ast_v1_to_v2(envelope)
        parsed = parse_query_ast(mapped)  # the rewritten envelope is a strict v2 AST
        assert len(parsed.root.children) == 3
