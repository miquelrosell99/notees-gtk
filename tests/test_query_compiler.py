"""Compiler tests: golden SQL + full param vectors for the v2 grammar over
the GTK mirror — the mechanical ``node``→``nodes`` + workspace-predicate
transform of the TS goldens (``packages/query/test/query.test.ts`` "compile:
SQL shape" + "v2 member grammar pins" + "aggregation schema" blocks).

Every golden pins BOTH the SQL text (or its load-bearing fragments, the TS
convention) and the positional param vector (R3 of the plan: pushes happen in
SQL text order — the params are as much the contract as the text).
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from notees_gtk.core.protocol.query_ast import parse_query_ast
from notees_gtk.core.query.compiler import (
    CompileOptions,
    QueryCompileError,
    build_match_expression,
    compile,
    compile_aggregate,
)

WS = "0192a000-0000-7000-8000-000000000001"
FRANCE = "0192a000-0000-7000-8000-000000000101"
PARIS = "0192a000-0000-7000-8000-000000000102"
LONE = "0192a000-0000-7000-8000-000000000103"
PLACE = "0192a000-0000-7000-8000-000000000201"
CITY = "0192a000-0000-7000-8000-000000000202"
PRIORITY = "0192a000-0000-7000-8000-000000000301"
RATING = "0192a000-0000-7000-8000-000000000302"
YEAR = "0192a000-0000-7000-8000-000000000303"
OPENED = "0192a000-0000-7000-8000-000000000304"

OPT = CompileOptions(workspace_id=WS)

ENTIRE = {"type": "entire_workspace"}
PAGES = {"type": "pages"}


def ast(scope: dict[str, Any], children: list[Any], sort: list[Any] | None = None) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "version": 2,
        "scope": scope,
        "root": {"type": "group", "logic": "and", "children": children},
    }
    if sort is not None:
        raw["sort"] = sort
    return raw


def all_in(scope: dict[str, Any]) -> dict[str, Any]:
    return ast(scope, [])


def m_in(family: str, ids: list[str]) -> dict[str, Any]:
    return {"type": "member", "family": family, "op": "in", "target": {"mode": "ids", "ids": ids}}


def m_in_q(family: str, children: list[Any]) -> dict[str, Any]:
    return {
        "type": "member",
        "family": family,
        "op": "in",
        "target": {"mode": "query", "root": {"type": "group", "logic": "and", "children": children}},
    }


def m_self(family: str) -> dict[str, Any]:
    return {"type": "member", "family": family, "op": "in", "target": {"mode": "self"}}


def m_exists(family: str) -> dict[str, Any]:
    return {"type": "member", "family": family, "op": "exists"}


def nf(field: str, op: str, value: str | None = None) -> dict[str, Any]:
    condition: dict[str, Any] = {"type": "nodeField", "field": field, "op": op}
    if value is not None:
        condition["value"] = value
    return condition


def compiled(raw: dict[str, Any], options: CompileOptions = OPT) -> Any:
    return compile(parse_query_ast(raw), options)


class TestSqlShape:
    def test_entire_workspace_scope_no_scope_clause_deterministic_columns_and_order(self) -> None:
        result = compiled(all_in(ENTIRE))
        assert result.sql == (
            "SELECT n.*\n"
            "FROM nodes n\n"
            "WHERE n.workspace_id = ? AND n.is_active = 1\n"
            "ORDER BY n.id ASC"
        )
        assert result.params == (WS,)

    def test_pages_scope_filters_to_the_document_chrome_predicate(self) -> None:
        result = compiled(all_in(PAGES))
        assert "n.is_class = 0 AND (n.parent_id IS NULL OR n.is_embedded = 0)" in result.sql
        # The parentlessRole demote conjunct.
        assert "(n.parent_id IS NOT NULL OR NOT EXISTS (SELECT 1 FROM class_hierarchy ch" in result.sql
        assert "c.parentless_role = 'block'" in result.sql
        assert "distance" not in result.sql

    def test_subtree_scope_joins_a_recursive_cte_and_exposes_distance(self) -> None:
        result = compiled(all_in({"type": "subtree", "pageId": FRANCE}))
        assert "WITH RECURSIVE sub(id, distance) AS" in result.sql
        assert "JOIN (WITH RECURSIVE sub(id, distance) AS" in result.sql
        assert "SELECT n.*, sc.distance AS distance" in result.sql
        # D1: anchor seed + the recursive arm both carry the workspace param.
        assert result.params == (WS, FRANCE, WS, WS)

    def test_linked_to_scope_uses_the_rollup_cte(self) -> None:
        result = compiled(all_in({"type": "linkedTo", "nodeId": PARIS}))
        assert "hits AS (" in result.sql
        assert "e.target_id IN (SELECT id FROM anchors)" in result.sql
        assert "sub.distance > 0 AND e.target_id NOT IN (SELECT id FROM sub)" in result.sql
        assert "MIN(distance) AS distance" in result.sql
        assert result.params == (WS, PARIS, WS, WS, WS, WS)

    def test_member_class_in_ids_probes_class_hierarchy(self) -> None:
        result = compiled(ast(ENTIRE, [m_in("class", [PLACE])]))
        assert "json_each(n.class_ids)" in result.sql
        assert "value IN (SELECT class_id FROM class_hierarchy WHERE ancestor_id IN (?))" in result.sql
        assert result.params == (WS, PLACE)

    def test_member_in_query_targets_nest_the_group_subquery(self) -> None:
        class_sql = compiled(ast(ENTIRE, [m_in_q("class", [m_in("class", [PLACE])])]))
        assert "value IN (SELECT class_id FROM class_hierarchy WHERE ancestor_id IN " in class_sql.sql
        assert "IN (SELECT m.id FROM nodes m WHERE m.is_active = 1 AND m.workspace_id = ? AND (" in class_sql.sql
        assert class_sql.params == (WS, WS, PLACE)
        tag_sql = compiled(ast(ENTIRE, [m_in_q("tags", [nf("isClass", "eq", "true")])]))
        assert (
            "EXISTS (\n  SELECT 1 FROM json_each(n.tag_ids)\n  WHERE value IN "
            "(SELECT m.id FROM nodes m WHERE m.is_active = 1 AND m.workspace_id = ? AND (" in tag_sql.sql
        )
        assert tag_sql.params == (WS, WS, 1)

    def test_member_in_query_with_an_empty_group_degrades_to_the_never_matching_subquery(self) -> None:
        result = compiled(
            ast(ENTIRE, [m_in_q("class", [{"type": "group", "logic": "and", "children": []}])])
        )
        assert "SELECT m.id FROM nodes m WHERE m.is_active = 0 AND m.workspace_id = ?" in result.sql
        assert result.params == (WS, WS)

    def test_node_field_and_date_after_before_are_parameterized(self) -> None:
        result = compiled(
            ast(
                ENTIRE,
                [
                    nf("isClass", "eq", "false"),
                    nf("isEmbedded", "eq", "false"),
                    {"type": "date", "field": "created", "op": "after", "timestamp": "2026-09-24T12:00:00.000Z"},
                    {"type": "date", "field": "created", "op": "before", "timestamp": "2026-09-25T12:00:00.000Z"},
                ],
            )
        )
        assert "n.is_class = ?" in result.sql
        assert "n.is_embedded = ?" in result.sql
        assert "n.created_at >= ?" in result.sql
        assert "n.created_at <= ?" in result.sql
        assert list(result.params[1:]) == [0, 0, "2026-09-24T12:00:00.000Z", "2026-09-25T12:00:00.000Z"]

    def test_content_contains_likes_the_plaintext_fts_prefix_ands_it(self) -> None:
        contains = compiled(ast(ENTIRE, [{"type": "content", "op": "contains", "value": "citi"}]))
        assert "n.content_plain LIKE '%' || ? || '%'" in contains.sql
        assert contains.params == (WS, "citi")
        fts = compiled(ast(ENTIRE, [{"type": "content", "op": "fts", "value": "capital city"}]))
        # Token-prefix emulation: two GLOB arms per term (string-start +
        # boundary), ASCII case-folded (the FTS tokenizer convention).
        assert "upper(n.content_plain) GLOB ?" in fts.sql
        assert "OR upper(n.content_plain) GLOB ?" in fts.sql
        assert fts.params == (
            WS,
            "CAPITAL*",
            "*[^A-Z0-9]CAPITAL*",
            "CITY*",
            "*[^A-Z0-9]CITY*",
        )
        exists = compiled(ast(ENTIRE, [{"type": "content", "op": "exists"}]))
        assert "length(n.content_plain) > 0" in exists.sql

    def test_content_eq_neq_starts_ends_anchor_the_plaintext(self) -> None:
        eq = compiled(ast(ENTIRE, [{"type": "content", "op": "eq", "value": "Sync"}]))
        assert "n.content_plain = ?" in eq.sql
        starts = compiled(ast(ENTIRE, [{"type": "content", "op": "starts", "value": "The"}]))
        assert "n.content_plain LIKE ? || '%'" in starts.sql
        ends = compiled(ast(ENTIRE, [{"type": "content", "op": "ends", "value": "ing"}]))
        assert "n.content_plain LIKE '%' || ?" in ends.sql

    def test_name_conditions_probe_the_flattened_title(self) -> None:
        contains = compiled(ast(ENTIRE, [{"type": "name", "op": "contains", "value": "paris"}]))
        assert "group_concat(json_extract(j.value, '$.text'), ' ')" in contains.sql
        assert "json_each(n.content)" in contains.sql
        assert "LIKE '%' || ? || '%'" in contains.sql
        assert contains.params == (WS, "paris")
        eq = compiled(ast(ENTIRE, [{"type": "name", "op": "eq", "value": "Paris"}]))
        assert ") = ?" in eq.sql
        assert eq.params == (WS, "Paris")
        exists = compiled(ast(ENTIRE, [{"type": "name", "op": "exists"}]))
        assert "COALESCE(length(" in exists.sql
        assert "> 0" in exists.sql
        assert exists.params == (WS,)
        # The probed expression IS the sort-by-name column (parse the ORDER
        # BY of a name-sorted query and require the condition to carry it).
        sorted_sql = compiled(ast(ENTIRE, [], [{"field": "name", "dir": "asc"}]))
        title_expr = sorted_sql.sql.split("ORDER BY ")[1].split(" IS NULL")[0]
        assert title_expr in contains.sql

    def test_property_condition_effective_read_model_params(self) -> None:
        result = compiled(ast(ENTIRE, [{"type": "property", "schemaId": PRIORITY, "op": "eq", "value": "high"}]))
        # Three schema-id params: authored CTE, winning-binding CTE, derived
        # no-authored-at-idx-0 check; then the value param.
        assert list(result.params[1:]) == [PRIORITY, PRIORITY, PRIORITY, "high"]
        assert "WITH authored AS (" in result.sql
        assert "property_value_tombstone" in result.sql
        assert "ROW_NUMBER() OVER (" in result.sql
        assert "json_extract(value, '$') = ?" in result.sql
        assert "UNION ALL" in result.sql

    def test_property_condition_include_defaults_false_is_authored_only(self) -> None:
        result = compiled(
            ast(ENTIRE, [{"type": "property", "schemaId": PRIORITY, "op": "exists", "includeDefaults": False}])
        )
        assert list(result.params) == [WS, PRIORITY]
        assert "WITH authored AS (" in result.sql
        assert "derived AS (" not in result.sql

    def test_property_comparison_ops_compile_to_extracted_scalar_comparisons(self) -> None:
        result = compiled(ast(ENTIRE, [{"type": "property", "schemaId": YEAR, "op": "gt", "value": 1900}]))
        assert "json_extract(value, '$') > ?" in result.sql
        assert list(result.params[1:]) == [YEAR, YEAR, YEAR, 1900]

    def test_iso_date_bound_values_gain_the_date_ref_arm(self) -> None:
        text = compiled(ast(ENTIRE, [{"type": "property", "schemaId": OPENED, "op": "lte", "value": "sometime"}]))
        assert "json_extract(value, '$') <= ?" in text.sql
        assert list(text.params[1:]) == [OPENED, OPENED, OPENED, "sometime"]
        lte = compiled(ast(ENTIRE, [{"type": "property", "schemaId": OPENED, "op": "lte", "value": "1937-05-06"}]))
        assert "json_extract(value, '$') <= ?" in lte.sql
        assert "substr(json_extract(value, '$.nodeId'), 25) <= ?" in lte.sql
        assert "IN ('dd', 'aa', 'bb')" in lte.sql
        assert list(lte.params[1:]) == [OPENED, OPENED, OPENED, "1937-05-06", "193705060000", "193705060000"]
        eq = compiled(ast(ENTIRE, [{"type": "property", "schemaId": OPENED, "op": "eq", "value": "1937-05-06"}]))
        assert list(eq.params[1:]) == [
            OPENED,
            OPENED,
            OPENED,
            "1937-05-06",
            "19370506",
            "193705",
            "1937",
            "193705060000",
            "19370506",
            "193705",
            "1937",
        ]

    def test_property_comparison_ops_are_part_of_the_versioned_schema(self) -> None:
        for op in ("gt", "gte", "lt", "lte"):
            parsed = parse_query_ast(ast(PAGES, [{"type": "property", "schemaId": YEAR, "op": op, "value": 1900}]))
            assert parsed.root.children[0].op == op

    def test_sort_maps_to_a_deterministic_order_by_with_id_tiebreak(self) -> None:
        result = compiled(
            ast(PAGES, [], [{"field": "name", "dir": "asc"}, {"field": "createdAt", "dir": "desc"}])
        )
        assert "ASC, n.created_at IS NULL, n.created_at DESC, n.id ASC" in result.sql
        assert "COLLATE NOCASE ASC" in result.sql
        assert "json_each(n.content)" in result.sql
        by_class = compiled(ast(PAGES, [], [{"field": "isClass", "dir": "desc"}]))
        assert "ORDER BY n.is_class DESC, n.id ASC" in by_class.sql
        by_main = compiled(ast(PAGES, [], [{"field": "isEmbedded", "dir": "asc"}]))
        assert "ORDER BY n.is_embedded ASC, n.id ASC" in by_main.sql

    def test_user_values_never_reach_the_sql_string(self) -> None:
        injection = "x' OR '1'='1"
        result = compiled(ast(ENTIRE, [{"type": "content", "op": "contains", "value": injection}, m_in("class", [PLACE])]))
        assert injection not in result.sql
        assert list(result.params[1:]) == [injection, PLACE]
        assert len(re.findall(r"\?", result.sql)) >= len(result.params) - 1

    def test_fails_loud_fts_value_with_no_search_terms_property_eq_without_value(self) -> None:
        with pytest.raises(QueryCompileError, match="no search terms"):
            compiled(ast(ENTIRE, [{"type": "content", "op": "fts", "value": "!!!"}]))
        with pytest.raises(QueryCompileError, match="requires a non-null value"):
            compiled(ast(ENTIRE, [{"type": "property", "schemaId": PRIORITY, "op": "eq"}]))


class TestMemberGrammarPins:
    def test_member_in_query_nests_to_depth_two(self) -> None:
        result = compiled(
            ast(
                ENTIRE,
                [m_in_q("links", [m_in_q("class", [{"type": "content", "op": "contains", "value": "city"}])])],
            )
        )
        assert "anchors AS (" in result.sql
        # The nested class-in-query binds the fresh scope alias (D6).
        assert "json_each(m.class_ids)" in result.sql
        assert list(result.params) == [WS, WS, WS, "city", WS, WS, WS]

    def test_page_in_query_params_follow_text_order(self) -> None:
        """R3: the containing-page subquery's params precede the page row's
        workspace predicate and the fresh-scope arm (bind order is the
        contract — a misordered push silently filters on the class id)."""
        result = compiled(ast(ENTIRE, [m_in_q("page", [m_in("class", [CITY])])]))
        assert list(result.params) == [WS, WS, WS, WS, WS, WS, CITY]

    def test_root_in_query_params_follow_text_order(self) -> None:
        result = compiled(ast(ENTIRE, [m_in_q("root", [m_in("class", [PLACE])])]))
        assert list(result.params) == [WS, WS, WS, WS, WS, WS, PLACE]

    def test_not_in_wraps_the_in_predicate(self) -> None:
        result = compiled(
            ast(ENTIRE, [{"type": "member", "family": "class", "op": "not-in", "target": {"mode": "ids", "ids": [CITY]}}])
        )
        assert "NOT (" in result.sql
        assert "json_each(n.class_ids)" in result.sql
        assert result.params == (WS, CITY)

    def test_not_exists_wraps_the_set_predicate(self) -> None:
        result = compiled(ast(ENTIRE, [{"type": "member", "family": "links", "op": "not-exists"}]))
        assert "NOT (EXISTS (SELECT 1 FROM edge WHERE target_id = n.id" in result.sql

    def test_not_wraps_every_condition_type(self) -> None:
        nots = [
            {"type": "not", "child": {"type": "content", "op": "exists"}},
            {"type": "not", "child": {"type": "name", "op": "exists"}},
            {"type": "not", "child": {"type": "property", "schemaId": PRIORITY, "op": "exists"}},
            {"type": "not", "child": {"type": "date", "field": "created", "op": "within", "period": "today"}},
            {"type": "not", "child": nf("isClass", "eq", "true")},
            {"type": "not", "child": m_in("class", [PLACE])},
        ]
        for not_condition in nots:
            result = compiled(ast(ENTIRE, [not_condition]))
            assert "NOT (" in result.sql

    def test_a_self_target_nested_inside_a_query_target_fails_loud_without_self_node_id(self) -> None:
        with pytest.raises(QueryCompileError, match="selfNodeId"):
            compiled(ast(ENTIRE, [m_in_q("links", [m_self("class")])]))

    def test_member_exists_on_a_family_without_emptiness_semantics_fails_loud(self) -> None:
        with pytest.raises(QueryCompileError, match="emptiness"):
            compiled(ast(ENTIRE, [m_exists("referencePath")]))
        with pytest.raises(QueryCompileError, match="emptiness"):
            compiled(ast(ENTIRE, [m_exists("root")]))

    def test_member_exact_compiles_to_the_exact_class_set_equality_probe(self) -> None:
        result = compiled(
            ast(ENTIRE, [{"type": "member", "family": "class", "op": "exact", "target": {"mode": "ids", "ids": [PLACE]}}])
        )
        assert "(SELECT COUNT(*) FROM json_each(n.class_ids)) = 1" in result.sql
        assert result.params == (WS, PLACE)

    def test_fails_loud_missing_operands_and_bad_boolean_strings(self) -> None:
        with pytest.raises(QueryCompileError, match="requires a timestamp"):
            compiled(ast(ENTIRE, [{"type": "date", "field": "created", "op": "after"}]))
        with pytest.raises(QueryCompileError, match="both bounds"):
            compiled(ast(ENTIRE, [{"type": "date", "field": "created", "op": "between", "from": "a"}]))
        with pytest.raises(QueryCompileError, match='"true" or "false"'):
            compiled(ast(ENTIRE, [nf("isClass", "eq", "yes")]))
        with pytest.raises(QueryCompileError, match="requires a non-null value"):
            compiled(ast(ENTIRE, [nf("coverAsset", "eq")]))
        with pytest.raises(QueryCompileError, match="supports only op 'eq'"):
            compiled(ast(ENTIRE, [nf("isEmbedded", "exists")]))


class TestSelfArmsCompile:
    """Every family's self arm compiles (with the hosting page supplied) into
    its single-anchor form — the params pin the anchor generation order."""

    @pytest.mark.parametrize(
        "family",
        ["class", "tags", "links", "referencePath", "parent", "child", "childPath", "page", "parentPath", "root"],
    )
    def test_self_arm_compiles_with_the_host(self, family: str) -> None:
        options = CompileOptions(workspace_id=WS, self_node_id=FRANCE)
        result = compiled(ast(ENTIRE, [m_self(family)]), options)
        # The self id rides either as its own param or inside the page/root
        # single-element ids JSON — both shapes name the host.
        assert any(FRANCE in param for param in (str(item) for item in result.params))

    @pytest.mark.parametrize(
        "family",
        ["class", "tags", "links", "referencePath", "parent", "child", "childPath", "page", "parentPath", "root"],
    )
    def test_every_self_arm_fails_loud_without_the_hosting_page(self, family: str) -> None:
        with pytest.raises(QueryCompileError, match="selfNodeId"):
            compiled(ast(ENTIRE, [m_self(family)]))


COUNT_BY_CLASS_BIT = {"dimensions": [{"kind": "isClass"}], "measures": [{"function": "count"}]}


def with_aggregation(raw: dict[str, Any], aggregation: dict[str, Any]) -> dict[str, Any]:
    return {**raw, "aggregation": aggregation}


class TestAggregation:
    def test_filtered_cte_group_by_deterministic_aliases(self) -> None:
        result = compiled(with_aggregation(all_in(PAGES), COUNT_BY_CLASS_BIT))
        assert "WITH filtered AS (" in result.sql
        assert "SELECT n.id, n.is_class, n.is_embedded, n.class_ids" in result.sql
        assert 'GROUP BY "isClass"' in result.sql
        assert 'AS "isClass"' in result.sql
        assert 'AS "count"' in result.sql
        assert "ORDER BY n.id" not in result.sql  # sort is ignored under aggregation
        assert result.params == (WS, WS)  # base + the parentlessRole demote (pages scope)

    def test_class_dimension_is_the_hierarchy_aware_membership_probe(self) -> None:
        result = compiled(
            with_aggregation(
                all_in(ENTIRE), {"dimensions": [{"kind": "class", "id": PLACE}], "measures": [{"function": "count"}]}
            )
        )
        assert "json_each(f.class_ids)" in result.sql
        assert "class_hierarchy WHERE ancestor_id = ?" in result.sql
        assert f'AS "class:{PLACE}"' in result.sql
        assert result.params == (WS, PLACE)

    def test_property_dimensions_measures_reuse_the_effective_read_model(self) -> None:
        result = compiled(
            with_aggregation(
                all_in(ENTIRE),
                {
                    "dimensions": [{"kind": "property", "id": PRIORITY}],
                    "measures": [
                        {"function": "countDistinct", "kind": "node"},
                        {"function": "avg", "kind": "property", "id": RATING},
                    ],
                },
            )
        )
        # Three schema-id params per property dimension/measure (authored,
        # binding, derived-shadow) — property first, then the measure.
        assert list(result.params[1:]) == [PRIORITY, PRIORITY, PRIORITY, RATING, RATING, RATING]
        assert "json_extract(ev.value, '$')" in result.sql
        assert "property_value_tombstone" in result.sql
        assert "COUNT(DISTINCT f.id)" in result.sql
        assert f'AS "avg:{RATING}"' in result.sql

    def test_scope_condition_params_precede_dimension_measure_params(self) -> None:
        result = compiled(with_aggregation(ast(ENTIRE, [m_in("class", [PLACE])]), COUNT_BY_CLASS_BIT))
        assert result.params == (WS, PLACE)

    def test_compile_aggregate_exposes_columns_duplicate_names_get_positional_suffixes(self) -> None:
        result = compile_aggregate(
            parse_query_ast(
                with_aggregation(
                    all_in(PAGES),
                    {"dimensions": [{"kind": "isClass"}], "measures": [{"function": "count"}, {"function": "count"}]},
                )
            ),
            OPT,
        )
        assert result.columns == ("isClass", "count", "count#2")
        assert 'AS "count#2"' in result.sql

    def test_zero_dimensions_compiles_to_a_grand_total(self) -> None:
        result = compile_aggregate(
            parse_query_ast(with_aggregation(all_in(ENTIRE), {"dimensions": [], "measures": [{"function": "count"}]})),
            OPT,
        )
        assert "GROUP BY" not in result.sql
        assert result.columns == ("count",)

    def test_compile_aggregate_requires_an_aggregation(self) -> None:
        with pytest.raises(QueryCompileError, match="requires an AST with an aggregation"):
            compile_aggregate(parse_query_ast(all_in(PAGES)), OPT)


class TestBuildMatchExpression:
    def test_splits_terms_at_punctuation_boundaries(self) -> None:
        assert build_match_expression("ISO 11607-1") == "ISO* AND 11607* AND 1*"
        assert build_match_expression("state-of-the-art") == "state* AND of* AND the* AND art*"

    def test_keeps_plain_terms_as_bare_prefix_tokens(self) -> None:
        assert build_match_expression("Kuhn Scient") == "Kuhn* AND Scient*"

    def test_compiles_quoted_segments_to_exact_phrases(self) -> None:
        assert build_match_expression('"quick brown" fox') == '"quick brown" AND fox*'
        assert build_match_expression('ISO "11607-1" validation') == '"11607 1" AND ISO* AND validation*'
        assert build_match_expression('"a   b"') == '"a b"'

    def test_treats_an_unterminated_quote_as_plain_text(self) -> None:
        assert build_match_expression('"quick brown') == "quick* AND brown*"

    def test_returns_none_when_nothing_searchable_remains(self) -> None:
        assert build_match_expression("---") is None
        assert build_match_expression("   ") is None
        assert build_match_expression('""') is None
