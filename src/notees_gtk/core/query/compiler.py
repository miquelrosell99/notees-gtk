"""QueryAST v2 → SQLite compiler over the GTK derived mirror.

Method-for-method port of ``packages/query/src/compiler.ts``. The reference
compiles over the TS store schema (table ``node`` singular, single-workspace,
FTS5 ``search_index``); the GTK mirror differs, so the schema deltas are
ISOLATED in this constants/helpers block (the porting contract):

- **D1 — table name + workspace threading (every fragment).** The GTK table
  is ``nodes`` (plural, composite PK ``(workspace_id, id)``); the TS compiler
  deliberately omits workspace filtering (single-workspace store). Every
  ``node n`` occurrence (including the fresh scopes inside dynamic ``query``
  targets and every recursive CTE arm) becomes ``nodes n`` AND the scan
  carries an ``<alias>.workspace_id = ?`` predicate; ``edge`` carries
  ``workspace_id`` too and is filtered. The workspace-free tables
  (``class_hierarchy``, ``class_member_set``, ``class_property``,
  ``property_value``, both tombstone tables, ``tag_member_set``) carry no
  workspace column and need none — node/class ids are global UUIDs. One
  helper generates every node-table reference so the delta lives in exactly
  one place: :func:`_nodes_ref`.
- **D2 — no FTS (content conditions).** The TS probes ``search_index``
  (FTS5) + ``search_index_docid``; the GTK mirror has no FTS tables — it
  derives ``nodes.search_plain``, the TS ``search_index`` row's counterpart
  (the store's ``extractSearchPlaintext`` port: mention-resolved content
  plaintext + asset original names + text-ish property values, diacritic-
  folded at derive time). Content conditions read ``search_plain`` directly
  (``contains`` → LIKE substring, ``eq``/``neq`` → equality,
  ``starts``/``ends`` → anchored LIKE, ``exists`` → the non-empty probe,
  ``fts`` → the prefix-AND terms emulated as GLOB token-prefix arms — see
  :meth:`_Compiler._content_sql`). The needle stays VERBATIM at compare time
  (the fold is derive-time only, the one mirror pair — ``peru`` matches
  "Perú" while "perú" matches nothing folded, exactly the TS SQL
  semantics); :func:`build_match_expression` ports verbatim anyway — it is
  the FTS backend's term compiler and part of the package's public surface.
- **D3 — identical (verified, ported verbatim):** ``is_class``,
  ``is_embedded``, ``parent_id``, ``class_ids``, ``tag_ids``, ``content``,
  ``created_at``, ``updated_at``, the wire node fields,
  ``parentless_role``, ``class_hierarchy``, ``class_member_set``,
  ``class_property``, ``property_schema``, ``property_value`` and both
  tombstone tables (the compiler never reads ``class_property.pinned``; the
  ``name`` sort/conditions ride the TS ``titleSql`` over ``content``, never
  the retained-but-unused ``name`` column).
- **D6 — fresh-scope alias.** The TS fresh ``node n`` scopes (dynamic query
  targets) reuse the outer alias ``n``; on the GTK mirror's system SQLite the
  shadowing breaks the correlated CTE anchors (the containing-page/root
  walks correlate the OUTER row, and a same-named nearer scope hijacks the
  name resolution). The fresh scopes therefore alias ``m`` and every
  condition fragment takes the row alias as a parameter — identical
  semantics, no shadowing.

Everything is parameterized with positional ``?`` placeholders; user values
never reach the SQL string. Params are pushed in SQL TEXT ORDER (the TS
build-before-compose discipline): a fragment's placeholders are always
generated before the string composing them is assembled, and every
workspace predicate is generated at its own textual position.

Fail-loud boundaries (compiler-side, matching the TS):
- ``self`` member targets without ``CompileOptions.self_node_id`` raise;
- ``referencePath``/``root`` ``exists`` raise (no emptiness semantics);
- missing operands (date/property/nodeField values) raise;
- unknown condition types raise (unreachable through the strict parse — the
  guard stays for defense in depth).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from notees_gtk.core.protocol.query_ast import (
    Aggregation,
    AggregationDimension,
    AggregationMeasure,
    Condition,
    ContentCondition,
    DateCondition,
    Group,
    IdsTarget,
    LinkedToScope,
    MemberCondition,
    NameCondition,
    NodeFieldCondition,
    Not,
    PagesScope,
    PropertyCondition,
    QueryAst,
    QueryTarget,
    SelfTarget,
    SortSpec,
    SubtreeScope,
    TargetRef,
)
from notees_gtk.core.query.placeholders import (
    placeholder_end_date,
    placeholder_start_date,
    resolve_day_bounds,
    resolve_period_bounds,
    resolve_timestamp_placeholder,
    resolve_value_placeholder,
)

__all__ = [
    "CompileOptions",
    "CompiledAggregate",
    "CompiledQuery",
    "QueryCompileError",
    "build_match_expression",
    "compile",
    "compile_aggregate",
]


class QueryCompileError(ValueError):
    """An AST shape the parser admits but the compiler cannot compile."""


#: The derived search-text backend this compile targets (D2): the mirror's
#: folded search-text column (the TS ``search_index`` row's counterpart —
#: mention-resolved content plaintext + asset original names + text-ish
#: property values, diacritic-folded at derive time). The appliers maintain
#: the column at the TS ``reindexNode`` trigger sites.
TEXT_BACKEND = "search_plain"

#: The fresh node-scope alias (D6): distinct from the outer ``n`` so the
#: correlated CTE anchors (containing page / root walks) keep resolving the
#: OUTER row on system SQLite.
FRESH_ALIAS = "m"


def _nodes_ref(alias: str) -> str:
    """D1: the mirror's node-table reference (the TS ``node`` is singular)."""
    return f"nodes {alias}" if alias else "nodes"


def _split_terms(text: str) -> list[str]:
    """The unicode61 token split (``splitTerms`` port): maximal runs of
    letters/digits (``isalnum`` is the ``\\p{L}\\p{N}`` class), every other
    boundary a split."""
    terms: list[str] = []
    current: list[str] = []
    for char in text.strip():
        if char.isalnum():
            current.append(char)
        elif current:
            terms.append("".join(current))
            current = []
    if current:
        terms.append("".join(current))
    return terms


def build_match_expression(query: str) -> str | None:
    """Prefix-AND FTS match expression (``buildMatchExpression`` port, kept
    verbatim — the FTS backend's term compiler). Each maximal run of letters/
    digits OUTSIDE double quotes becomes a bare prefix token (``term*``);
    each double-quoted segment becomes an exact FTS phrase. Null when nothing
    searchable remains. (The content ``fts`` op under the ``search_plain``
    backend instead ANDs GLOB token-prefix arms — same filter semantics, see
    :meth:`_Compiler._content_sql`.)"""
    clauses: list[str] = []
    unquoted: list[str] = []
    last = 0
    for match in re.finditer(r'"([^"]*)"', query):
        unquoted.append(query[last : match.start()])
        last = match.end()
        terms = _split_terms(match.group(1))
        if terms:
            clauses.append(f'"{" ".join(terms)}"')
    unquoted.append(query[last:])
    for term in _split_terms(" ".join(unquoted)):
        clauses.append(f"{term}*")
    if not clauses:
        return None
    return " AND ".join(clauses)


def _title_sql(alias: str) -> str:
    """The title (the flattened content text) for the node row aliased
    ``alias`` — title-is-content: the exact expression sort-by-name orders
    by (``titleSql`` verbatim; D3: the mirror stores the same content JSON)."""
    return (
        f"(SELECT group_concat(json_extract(j.value, '$.text'), ' ') FROM json_each({alias}.content) j "
        "WHERE json_extract(j.value, '$.type') = 'text')"
    )


@dataclass(frozen=True)
class CompiledQuery:
    """The compiled SQL + positional params (user values never reach the SQL)."""

    sql: str
    params: tuple[Any, ...]


@dataclass(frozen=True)
class CompiledAggregate(CompiledQuery):
    """An aggregate compilation additionally carries the deterministic column
    order (dimensions in declared order, then measures)."""

    columns: tuple[str, ...]


@dataclass(frozen=True)
class CompileOptions:
    """The compile-context resolutions the AST does not carry.

    Attributes:
        workspace_id: MANDATORY in GTK (D1) — every ``nodes``/``edge`` scan is
            filtered to it. This is the one genuine addition over the TS
            options (the TS store is single-workspace).
        now: The placeholder clock: ``{today}``-style tokens in timestamps
            and comparison-bound property values resolve against this
            instant. Tests pin it; runtime defaults to the current time.
        self_node_id: The hosting page for the ``self`` member target mode —
            supplied by the run site (never baked into the AST). A self
            target compiling WITHOUT it fails loud (there is no honest
            reading of "self" without a host).
    """

    workspace_id: str
    now: datetime | None = None
    self_node_id: str | None = None


def compile(ast: QueryAst, options: CompileOptions) -> CompiledQuery:
    """Compile a plain query (an AST carrying an aggregation compiles to the
    aggregate shape — execute it with ``run_aggregate``)."""
    return _Compiler(options).compile(ast)


def compile_aggregate(ast: QueryAst, options: CompileOptions) -> CompiledAggregate:
    """Compile an aggregation AST (requires ``ast.aggregation``)."""
    return _Compiler(options).compile_aggregate(ast)


class _Compiler:
    """The method-for-method port of the TS ``Compiler`` class."""

    def __init__(self, options: CompileOptions) -> None:
        self._options = options
        self._params: list[Any] = []

    # -- params discipline: placeholders are generated in SQL text order --

    def _push(self, value: Any) -> str:
        self._params.append(value)
        return "?"

    def _ws_value(self) -> str:
        """One bare ``workspace_id = ?`` predicate (alias-less scans)."""
        return f"workspace_id = {self._push(self._options.workspace_id)}"

    def _ws(self, alias: str) -> str:
        """One ``<alias>.workspace_id = ?`` predicate (param pushed at the
        call site — always in text order)."""
        return f"{alias}.workspace_id = {self._push(self._options.workspace_id)}"

    def _group_produces_clauses(self, group: Group) -> bool:
        """The structural half of ``groupSql``'s empty check (a group yields
        SQL iff any child yields: conditions/nots always do, groups recurse)
        — lets the dynamic arms decide the degradation branch BEFORE pushing
        the group's params, keeping pushes in text order."""
        return any(
            not isinstance(child, Group) or self._group_produces_clauses(child) for child in group.children
        )

    # -- entry points ------------------------------------------------------------

    def compile(self, ast: QueryAst) -> CompiledQuery:
        if ast.aggregation is not None:
            sql, _ = self._aggregation_sql(ast, ast.aggregation)
            return CompiledQuery(sql=sql, params=tuple(self._params))
        from_clause, where, has_distance = self._filtered_from_where(ast)
        columns = "n.*, sc.distance AS distance" if has_distance else "n.*"
        sql = (
            f"SELECT {columns}\nFROM {from_clause}\nWHERE {' AND '.join(where)}\n"
            f"ORDER BY {self._order_sql(ast.sort)}"
        )
        return CompiledQuery(sql=sql, params=tuple(self._params))

    def compile_aggregate(self, ast: QueryAst) -> CompiledAggregate:
        if ast.aggregation is None:
            raise QueryCompileError("query compile: compileAggregate requires an AST with an aggregation")
        sql, columns = self._aggregation_sql(ast, ast.aggregation)
        return CompiledAggregate(sql=sql, params=tuple(self._params), columns=columns)

    def _aggregation_sql(self, ast: QueryAst, aggregation: Aggregation) -> tuple[str, tuple[str, ...]]:
        from_clause, where, _ = self._filtered_from_where(ast)
        dim_exprs = [self._dimension_sql(dimension) for dimension in aggregation.dimensions]
        measure_exprs = [self._measure_sql(measure) for measure in aggregation.measures]
        columns = _aggregation_columns(aggregation)
        # GROUP BY/ORDER BY reference the output aliases: repeating the
        # expressions would repeat their `?` placeholders (one param push per
        # generation), and SQLite resolves result-column names in both clauses.
        dim_aliases = [f'"{column}"' for column in columns[: len(dim_exprs)]]
        select_list = [f"{expr} AS \"{columns[index]}\"" for index, expr in enumerate([*dim_exprs, *measure_exprs])]
        sql = (
            "WITH filtered AS (\n"
            "SELECT n.id, n.is_class, n.is_embedded, n.class_ids\n"
            f"FROM {from_clause}\n"
            f"WHERE {' AND '.join(where)}\n"
            ")\n"
            f"SELECT {', '.join(select_list)}\nFROM filtered f\n"
        )
        if dim_exprs:
            sql += f"GROUP BY {', '.join(dim_aliases)}\n"
            sql += f"ORDER BY {', '.join(dim_aliases)} ASC"
        return sql, columns

    # -- aggregation ----------------------------------------------------------------

    def _dimension_sql(self, dimension: AggregationDimension) -> str:
        if dimension.kind == "isClass":
            return "f.is_class"
        if dimension.kind == "isEmbedded":
            return "f.is_embedded"
        if dimension.kind == "class":
            assert dimension.id is not None
            # Same membership probe as the class condition: class_hierarchy
            # carries the extends closure (self-row included). The key is the
            # boolean membership (1/0) — a node is or is not in the class.
            class_id = self._push(dimension.id)
            return (
                "CASE WHEN EXISTS (\n  SELECT 1 FROM json_each(f.class_ids)\n"
                f"  WHERE value IN (SELECT class_id FROM class_hierarchy WHERE ancestor_id = {class_id})\n"
                ") THEN 1 ELSE 0 END"
            )
        assert dimension.id is not None
        return self._effective_value_sql(dimension.id)

    def _measure_sql(self, measure: AggregationMeasure) -> str:
        if measure.function == "count":
            return "COUNT(*)"
        if measure.function == "countDistinct":
            return "COUNT(DISTINCT f.id)"
        assert measure.id is not None
        # json_extract yields the JSON number for numeric values; text values
        # aggregate as SQLite's numeric coercion dictates (non-numeric text
        # sums error out — bind numeric schemas to sum/avg, as the builder does).
        return f"{measure.function.upper()}({self._effective_value_sql(measure.id)})"

    @staticmethod
    def _visible_value_predicate(alias: str) -> str:
        """The property_value suppression predicate (``visibleValuePredicate``
        verbatim): no winning SLOT tombstone (the full (hlc, actor) tuple) and
        no strictly-newer ELEMENT tombstone (add-wins ties)."""
        return (
            "NOT EXISTS (\n"
            "  SELECT 1 FROM property_value_tombstone t\n"
            f"  WHERE t.node_id = {alias}.node_id AND t.property_schema_id = {alias}.property_schema_id\n"
            f"    AND t.idx = {alias}.idx\n"
            f"    AND (t.hlc_physical, t.hlc_logical, COALESCE(t.actor_id, '')) >=\n"
            f"        ({alias}.hlc_physical, {alias}.hlc_logical, COALESCE({alias}.actor_id, ''))\n"
            ")\n"
            "AND NOT EXISTS (\n"
            "  SELECT 1 FROM property_value_element_tombstone et\n"
            f"  WHERE et.element_id = {alias}.id\n"
            "    AND (et.hlc_physical, et.hlc_logical) >\n"
            f"        ({alias}.hlc_physical, {alias}.hlc_logical)\n"
            ")"
        )

    def _effective_value_sql(self, schema_id: str) -> str:
        """The effective/authored value at idx 0 of a property for the filtered
        node (alias ``f``) — the same read model as the property conditions.
        Params per call: three schema ids (authored filter, binding filter,
        derived-shadow check)."""
        authored = self._push(schema_id)
        binding = self._push(schema_id)
        shadow = self._push(schema_id)
        tombstone = _Compiler._visible_value_predicate("pv")
        return (
            "(SELECT json_extract(ev.value, '$') FROM (\n"
            "  SELECT pv.node_id, pv.value FROM property_value pv\n"
            f"  WHERE pv.property_schema_id = {authored} AND pv.node_id = f.id AND pv.idx = 0 AND {tombstone}\n"
            "  UNION ALL\n"
            "  SELECT wb.node_id, wb.default_value FROM (\n"
            "    SELECT cms.node_id, ps.default_value,\n"
            "           ROW_NUMBER() OVER (\n"
            "             PARTITION BY cms.node_id\n"
            "             ORDER BY cms.hlc_physical, cms.hlc_logical, cms.class_id\n"
            "           ) AS rn\n"
            "    FROM class_member_set cms\n"
            "    JOIN class_property cp ON cp.class_id = cms.class_id\n"
            "    JOIN property_schema ps ON ps.id = cp.property_schema_id\n"
            f"    WHERE cms.present = 1 AND cp.property_schema_id = {binding} AND cp.active = 1\n"
            "      AND ps.default_value IS NOT NULL\n"
            "  ) wb\n"
            "  WHERE wb.rn = 1 AND wb.node_id = f.id\n"
            "    AND NOT EXISTS (\n"
            "      SELECT 1 FROM property_value pv\n"
            f"      WHERE pv.node_id = wb.node_id AND pv.property_schema_id = {shadow}\n"
            f"        AND pv.idx = 0 AND {tombstone}\n"
            "    )\n"
            ") ev LIMIT 1)"
        )

    # -- filtered set (shared by the plain query and the aggregation) ---------------

    def _filtered_from_where(self, ast: QueryAst) -> tuple[str, list[str], bool]:
        from_clause = _nodes_ref("n")
        has_distance = False
        if isinstance(ast.scope, SubtreeScope):
            from_clause += f"\nJOIN ({self._subtree_sql(ast.scope.page_id)}) sc ON sc.id = n.id"
            has_distance = True
        elif isinstance(ast.scope, LinkedToScope):
            from_clause += f"\nJOIN ({self._linked_to_sql(ast.scope.node_id)}) sc ON sc.id = n.id"
            has_distance = True
        base_ws = self._push(self._options.workspace_id)
        where = [f"n.workspace_id = {base_ws}", "n.is_active = 1"]
        if isinstance(ast.scope, PagesScope):
            # Document-chrome predicate (Revision 12) + the parentlessRole
            # demote conjunct (pushes its own workspace param inside the text).
            where.append(
                "n.is_class = 0 AND (n.parent_id IS NULL OR n.is_embedded = 0)"
                f" AND (n.parent_id IS NOT NULL OR NOT {self._has_parentless_block_class_sql('n')})"
            )
        group = self._group_sql(ast.root, "n")
        if group is not None:
            where.append(f"({group})")
        return from_clause, where, has_distance

    # -- scope -----------------------------------------------------------------

    def _anchor_node_sql(self, node_id: str) -> str:
        """Anchor-set seed for a single node (distance 0)."""
        ws = self._ws_value()
        return f"SELECT id, 0 AS distance FROM {_nodes_ref('')} WHERE {ws} AND id = {self._push(node_id)}"

    def _self_id_value(self) -> str:
        """The SELF id (``selfIdValue`` port): fails loud without a host."""
        if not self._options.self_node_id:
            raise QueryCompileError(
                "query compile: a '*Self' condition requires CompileOptions.selfNodeId (the query's hosting page)"
            )
        return self._options.self_node_id

    def _self_id(self) -> str:
        """The SELF id as one ``?`` param."""
        return self._push(self._self_id_value())

    def _self_anchor_sql(self) -> str:
        """Anchor-set seed for the SELF family: the hosting page (distance 0)."""
        ws = self._ws_value()
        return f"SELECT id, 0 AS distance FROM {_nodes_ref('')} WHERE {ws} AND id = {self._self_id()}"

    def _group_anchors_sql(self, root: Group) -> str:
        """Anchor-set seed for a nested group: the matching ACTIVE nodes
        (distance 0). The fresh scope rides the D6 alias so a page/root
        condition inside the group cannot hijack the outer correlation."""
        m = FRESH_ALIAS
        ws = self._push(self._options.workspace_id)
        group = self._group_sql(root, m)
        text = f"SELECT {m}.id, 0 AS distance FROM {_nodes_ref(m)} WHERE {m}.is_active = 1 AND {m}.workspace_id = {ws}"
        if group is not None:
            text += f" AND ({group})"
        return text

    def _subtree_set_sql(self, anchors_sql: str) -> str:
        """Anchor SET + descendants, one row per node, distance = levels below."""
        ws = self._push(self._options.workspace_id)
        return (
            "WITH RECURSIVE sub(id, distance) AS (\n"
            f"{anchors_sql}\n"
            "  UNION ALL\n"
            f"  SELECT n.id, sub.distance + 1 FROM sub JOIN {_nodes_ref('n')} ON n.parent_id = sub.id"
            f" AND n.workspace_id = {ws}\n"
            ") SELECT id, distance FROM sub"
        )

    def _subtree_sql(self, anchor_id: str) -> str:
        """Anchor + descendants — the single-anchor convenience."""
        return self._subtree_set_sql(self._anchor_node_sql(anchor_id))

    def _linked_to_set_sql(self, targets_sql: str) -> str:
        """backlinksWithRollup membership over a TARGET SET (``linkedToSetSql``
        port): targets = the anchor rows themselves; sub = targets ∪ their
        subtrees; hits = direct edges to ANY target, UNION sources inside any
        subtree linking OUTSIDE the whole sub union. D1: every nodes/edge scan
        in the CTE arms carries the workspace predicate (params pushed in arm
        order: sub recursion, then the two edge arms)."""
        ws_sub = self._push(self._options.workspace_id)
        ws_direct = self._push(self._options.workspace_id)
        ws_containment = self._push(self._options.workspace_id)
        return (
            "WITH RECURSIVE anchors AS (" + targets_sql + "),\n"
            "sub(id, distance) AS (\n"
            "  SELECT id, 0 AS distance FROM anchors\n"
            "  UNION ALL\n"
            f"  SELECT n.id, sub.distance + 1 FROM sub JOIN {_nodes_ref('n')} ON n.parent_id = sub.id"
            f" AND n.workspace_id = {ws_sub}\n"
            "), hits AS (\n"
            "  SELECT e.source_id AS id, 0 AS distance FROM edge e\n"
            f"  WHERE e.target_id IN (SELECT id FROM anchors) AND e.workspace_id = {ws_direct}\n"
            "  UNION ALL\n"
            "  SELECT e.source_id, sub.distance FROM edge e\n"
            "  JOIN sub ON sub.id = e.source_id\n"
            f"  WHERE sub.distance > 0 AND e.target_id NOT IN (SELECT id FROM sub) AND e.workspace_id = {ws_containment}\n"
            ") SELECT id, MIN(distance) AS distance FROM hits GROUP BY id"
        )

    def _linked_to_sql(self, node_id: str) -> str:
        """Single-node backlinksWithRollup membership (the scope + static form)."""
        return self._linked_to_set_sql(self._anchor_node_sql(node_id))

    # -- group / not --------------------------------------------------------------

    def _group_sql(self, group: Group, alias: str) -> str | None:
        clauses: list[str] = []
        for child in group.children:
            if isinstance(child, Group):
                nested = self._group_sql(child, alias)
                if nested is not None:
                    clauses.append(f"({nested})")
            elif isinstance(child, Not):
                clauses.append(self._not_sql(child, alias))
            else:
                clauses.append(self._condition_sql(child, alias))
        if not clauses:
            return None  # empty group = no constraint
        return f" {group.logic.upper()} ".join(clauses)

    def _not_sql(self, node: Not, alias: str) -> str:
        inner = (
            self._group_sql(node.child, alias)
            if isinstance(node.child, Group)
            else self._condition_sql(node.child, alias)
        )
        return f"NOT ({inner or '1=1'})"

    # -- conditions -----------------------------------------------------------------

    def _condition_sql(self, condition: Condition, alias: str) -> str:
        if isinstance(condition, MemberCondition):
            return self._member_sql(condition, alias)
        if isinstance(condition, ContentCondition):
            if condition.op == "exists":
                # D2: the derived search text is non-empty (the content "is
                # set" — property values alone carry the row, the TS
                # length(s.content) > 0 arm) — the mirror's search_plain
                # column replaces the FTS length probe.
                return f"length({alias}.search_plain) > 0"
            return self._content_sql(condition, alias)
        if isinstance(condition, NameCondition):
            return self._name_sql(condition, alias)
        if isinstance(condition, PropertyCondition):
            if condition.op == "between":
                return self._property_range_sql(condition.schema_id, condition.from_, condition.to, alias)
            if condition.op == "within":
                return self._property_within_sql(condition.schema_id, condition.period, alias)
            return self._property_sql(condition, alias)
        if isinstance(condition, DateCondition):
            return self._date_sql(condition, alias)
        if isinstance(condition, NodeFieldCondition):
            if condition.field in ("isClass", "isEmbedded"):
                # The Revision-12 booleans ride op "eq" with value "true"/"false".
                if condition.op != "eq":
                    raise QueryCompileError(
                        f"query compile: nodeField '{condition.field}' supports only op 'eq'"
                    )
                if condition.value not in ("true", "false"):
                    raise QueryCompileError(
                        f"query compile: nodeField '{condition.field}' requires value \"true\" or \"false\""
                    )
                column = "is_class" if condition.field == "isClass" else "is_embedded"
                return f"{alias}.{column} = {self._push(1 if condition.value == 'true' else 0)}"
            columns = {
                "coverAsset": "cover_asset_id",
                "bannerAsset": "banner_asset_id",
                "aliasedNode": "aliased_node_id",
            }
            return self._node_field_sql(condition.field, columns[condition.field], condition, alias)
        raise QueryCompileError(f"query compile: unknown condition type {type(condition).__name__}")

    def _member_sql(self, condition: MemberCondition, alias: str) -> str:
        if condition.op in ("in", "not-in"):
            assert condition.target is not None
            predicate = self._member_in_sql(condition.family, condition.target, alias)
            return f"NOT ({predicate})" if condition.op == "not-in" else predicate
        if condition.op in ("exists", "not-exists"):
            predicate = self._member_exists_sql(condition.family, alias)
            return f"NOT ({predicate})" if condition.op == "not-exists" else predicate
        # exact: EXACT class-set equality (the closure deliberately does NOT
        # apply — "exactly Day, nothing else").
        assert isinstance(condition.target, IdsTarget)
        class_id = self._push(condition.target.ids[0])
        return (
            f"(SELECT COUNT(*) FROM json_each({alias}.class_ids)) = 1\n"
            f"AND EXISTS (\n  SELECT 1 FROM json_each({alias}.class_ids) WHERE value = {class_id}\n)"
        )

    def _dynamic_targets(self, group: Group) -> str:
        """The fresh node-scope subquery selecting a dynamic target set (the
        class/tags/referencePath query modes). An empty nested group degrades
        to the never-matching subquery (D1: workspace-filtered; D6: the fresh
        scope rides its own alias)."""
        m = FRESH_ALIAS
        if not self._group_produces_clauses(group):
            return f"SELECT {m}.id FROM {_nodes_ref(m)} WHERE {m}.is_active = 0 AND {self._ws(m)}"
        ws = self._push(self._options.workspace_id)
        inner = self._group_sql(group, m)
        return f"SELECT {m}.id FROM {_nodes_ref(m)} WHERE {m}.is_active = 1 AND {m}.workspace_id = {ws} AND ({inner})"

    def _fresh_matches(self, group: Group) -> str:
        """The fresh node-scope subquery for the EXISTS-wrapped dynamic modes
        (parent/child/page/root/parentPath): the group's ACTIVE matches (D6
        alias). Callers only invoke this when the group produces clauses."""
        m = FRESH_ALIAS
        ws = self._push(self._options.workspace_id)
        inner = self._group_sql(group, m)
        return (
            f"SELECT {m}.id FROM {_nodes_ref(m)} WHERE {m}.is_active = 1 AND {m}.workspace_id = {ws} AND ({inner})"
        )

    def _member_in_sql(self, family: str, target: TargetRef, alias: str) -> str:
        if family == "class":
            if isinstance(target, IdsTarget):
                # ANY-of membership — closure-aware per picked id
                # (class_hierarchy includes the self-row).
                ids = ", ".join(self._push(node_id) for node_id in target.ids)
                return (
                    f"EXISTS (\n  SELECT 1 FROM json_each({alias}.class_ids)\n"
                    f"  WHERE value IN (SELECT class_id FROM class_hierarchy WHERE ancestor_id IN ({ids}))\n)"
                )
            if isinstance(target, SelfTarget):
                # The hosting page used as a class (closure-aware).
                class_id = self._self_id()
                return (
                    f"EXISTS (\n  SELECT 1 FROM json_each({alias}.class_ids)\n"
                    f"  WHERE value IN (SELECT class_id FROM class_hierarchy WHERE ancestor_id = {class_id})\n)"
                )
            assert isinstance(target, QueryTarget)
            targets = self._dynamic_targets(target.root)
            return (
                f"EXISTS (\n  SELECT 1 FROM json_each({alias}.class_ids)\n"
                f"  WHERE value IN (SELECT class_id FROM class_hierarchy WHERE ancestor_id IN ({targets}))\n)"
            )
        if family == "tags":
            if isinstance(target, IdsTarget):
                targets = ", ".join(self._push(node_id) for node_id in target.ids)
            elif isinstance(target, SelfTarget):
                targets = self._self_id()
            else:
                assert isinstance(target, QueryTarget)
                targets = self._dynamic_targets(target.root)
            # Tag membership over the OR-set tag_ids (a tag is any page — no
            # hierarchy, so the probe is the literal id).
            return f"EXISTS (\n  SELECT 1 FROM json_each({alias}.tag_ids)\n  WHERE value IN ({targets})\n)"
        if family == "links":
            # backlinksWithRollup membership over the target set: ids an
            # anchor list, query the group's matches, self the one
            # hosting-page anchor.
            if isinstance(target, IdsTarget):
                ws = self._ws_value()
                anchor_ids = ", ".join(self._push(node_id) for node_id in target.ids)
                anchor_set = f"SELECT id, 0 AS distance FROM {_nodes_ref('')} WHERE {ws} AND id IN ({anchor_ids})"
            elif isinstance(target, SelfTarget):
                anchor_set = self._self_anchor_sql()
            else:
                assert isinstance(target, QueryTarget)
                anchor_set = self._group_anchors_sql(target.root)
            return f"{alias}.id IN (SELECT id FROM ({self._linked_to_set_sql(anchor_set)}))"
        if family == "childPath":
            if isinstance(target, IdsTarget):
                # Subtree membership of ANY picked node, anchors excluded — the
                # anchor list reaches the NOT-IN through ONE json_each param.
                ws = self._ws_value()
                anchor_ids = ", ".join(self._push(node_id) for node_id in target.ids)
                anchor_set = f"SELECT id, 0 AS distance FROM {_nodes_ref('')} WHERE {ws} AND id IN ({anchor_ids})"
                subtree = self._subtree_set_sql(anchor_set)
                exclude = self._push(json.dumps(list(target.ids)))
                return (
                    f"{alias}.id IN (SELECT id FROM ({subtree})) "
                    f"AND {alias}.id NOT IN (SELECT value FROM json_each({exclude}))"
                )
            if isinstance(target, SelfTarget):
                # The row sits strictly inside the hosting page.
                subtree = self._subtree_set_sql(self._self_anchor_sql())
                anchor = self._self_id()
                return f"{alias}.id IN (SELECT id FROM ({subtree})) AND {alias}.id != {anchor}"
            assert isinstance(target, QueryTarget)
            subtree = self._subtree_set_sql(self._group_anchors_sql(target.root))
            anchors = self._group_anchors_sql(target.root)
            return f"{alias}.id IN (SELECT id FROM ({subtree})) AND {alias}.id NOT IN (SELECT id FROM ({anchors}))"
        if family == "parent":
            if isinstance(target, IdsTarget):
                # The DIRECT parent (one hop) is one of the picked nodes (one
                # json_each param; the COALESCE maps the parentless NULL to ''
                # so the empty parent reads two-valued).
                ids = self._push(json.dumps(list(target.ids)))
                return f"COALESCE({alias}.parent_id, '') IN (SELECT value FROM json_each({ids}))"
            if isinstance(target, SelfTarget):
                return f"COALESCE({alias}.parent_id, '') = {self._self_id()}"
            assert isinstance(target, QueryTarget)
            ws = self._push(self._options.workspace_id)
            inner = f" AND p.id IN ({self._fresh_matches(target.root)})" if self._group_produces_clauses(target.root) else ""
            return (
                "EXISTS (\n  SELECT 1 FROM nodes p\n"
                f"  WHERE p.id = {alias}.parent_id AND p.is_active = 1 AND p.workspace_id = {ws}{inner}\n)"
            )
        if family == "child":
            ws_c = self._ws("c")
            if isinstance(target, IdsTarget):
                ids = self._push(json.dumps(list(target.ids)))
                return (
                    "EXISTS (\n  SELECT 1 FROM nodes c\n"
                    f"  WHERE c.parent_id = {alias}.id AND c.is_active = 1 AND {ws_c}"
                    f" AND c.id IN (SELECT value FROM json_each({ids}))\n)"
                )
            if isinstance(target, SelfTarget):
                return (
                    "EXISTS (\n  SELECT 1 FROM nodes c\n"
                    f"  WHERE c.parent_id = {alias}.id AND c.is_active = 1 AND {ws_c} AND c.id = {self._self_id()}\n)"
                )
            assert isinstance(target, QueryTarget)
            inner = f" AND c.id IN ({self._fresh_matches(target.root)})" if self._group_produces_clauses(target.root) else ""
            return (
                "EXISTS (\n  SELECT 1 FROM nodes c\n"
                f"  WHERE c.parent_id = {alias}.id AND c.is_active = 1 AND {ws_c}{inner}\n)"
            )
        if family == "referencePath":
            if isinstance(target, IdsTarget):
                targets = f"SELECT value AS id FROM json_each({self._push(json.dumps(list(target.ids)))})"
            elif isinstance(target, SelfTarget):
                targets = self._self_anchor_sql()
            else:
                assert isinstance(target, QueryTarget)
                targets = self._dynamic_targets(target.root)
            return self._reference_path_sql(targets, alias)
        if family == "page":
            if isinstance(target, IdsTarget):
                ids = self._push(json.dumps(list(target.ids)))
                return (
                    f"EXISTS (\n  SELECT 1 FROM json_each({ids})\n"
                    f"  WHERE value = {self._containing_page_id_sql(alias)}\n)"
                )
            if isinstance(target, SelfTarget):
                ids = self._push(json.dumps([self._self_id_value()]))
                return (
                    f"EXISTS (\n  SELECT 1 FROM json_each({ids})\n"
                    f"  WHERE value = {self._containing_page_id_sql(alias)}\n)"
                )
            assert isinstance(target, QueryTarget)
            # Text order: the containing-page subquery's params, then the page
            # row's workspace predicate, then the fresh-scope arm.
            containing = self._containing_page_id_sql(alias)
            ws = self._push(self._options.workspace_id)
            inner = (
                f" AND page.id IN ({self._fresh_matches(target.root)})"
                if self._group_produces_clauses(target.root)
                else ""
            )
            return (
                "EXISTS (\n  SELECT 1 FROM nodes page\n"
                f"  WHERE page.id = {containing} AND page.is_active = 1"
                f" AND page.workspace_id = {ws}{inner}\n)"
            )
        if family == "parentPath":
            anc_ws_seed = self._push(self._options.workspace_id)
            anc_ws_rec = self._push(self._options.workspace_id)

            def anc(where_sql: str) -> str:
                return (
                    "EXISTS (\n  WITH RECURSIVE anc(id, depth) AS (\n"
                    f"    SELECT nd.parent_id, 0 FROM nodes nd WHERE nd.id = {alias}.id AND nd.workspace_id = {anc_ws_seed}\n"
                    "    UNION ALL\n"
                    "    SELECT nd.parent_id, anc.depth + 1 FROM nodes nd JOIN anc ON nd.id = anc.id\n"
                    f"    WHERE anc.id IS NOT NULL AND anc.depth < 64 AND nd.workspace_id = {anc_ws_rec}\n"
                    "  )\n"
                    f"  {where_sql}\n)"
                )

            if isinstance(target, IdsTarget):
                # Some STRICT ancestor (self excluded) is one of the picked
                # nodes (the ids ride one json_each param).
                ids = self._push(json.dumps(list(target.ids)))
                return anc(f"SELECT 1 FROM anc JOIN json_each({ids}) ON value = anc.id")
            if isinstance(target, SelfTarget):
                return anc(f"SELECT 1 FROM anc WHERE anc.id = {self._self_id()}")
            assert isinstance(target, QueryTarget)
            ws = self._push(self._options.workspace_id)
            inner = (
                f" AND a.id IN ({self._fresh_matches(target.root)})"
                if self._group_produces_clauses(target.root)
                else ""
            )
            return anc(
                "SELECT 1 FROM anc JOIN nodes a ON a.id = anc.id\n"
                f"  WHERE a.is_active = 1 AND a.workspace_id = {ws}{inner}"
            )
        if family == "root":
            if isinstance(target, IdsTarget):
                ids = self._push(json.dumps(list(target.ids)))
                return f"EXISTS (\n  SELECT 1 FROM json_each({ids})\n  WHERE value = {self._root_id_sql(alias)}\n)"
            if isinstance(target, SelfTarget):
                ids = self._push(json.dumps([self._self_id_value()]))
                return f"EXISTS (\n  SELECT 1 FROM json_each({ids})\n  WHERE value = {self._root_id_sql(alias)}\n)"
            assert isinstance(target, QueryTarget)
            # Text order: the root-id subquery's params, then the root row's
            # workspace predicate, then the fresh-scope arm.
            root_id = self._root_id_sql(alias)
            ws = self._push(self._options.workspace_id)
            inner = (
                f" AND root.id IN ({self._fresh_matches(target.root)})"
                if self._group_produces_clauses(target.root)
                else ""
            )
            return (
                "EXISTS (\n  SELECT 1 FROM nodes root\n"
                f"  WHERE root.id = {root_id} AND root.is_active = 1 AND root.workspace_id = {ws}{inner}\n)"
            )
        raise QueryCompileError(f"query compile: unknown member family {family!r}")

    def _member_exists_sql(self, family: str, alias: str) -> str:
        if family == "class":
            # The class set is non-empty (the "is set" bit).
            return f"(SELECT COUNT(*) FROM json_each({alias}.class_ids)) > 0"
        if family == "tags":
            return f"(SELECT COUNT(*) FROM json_each({alias}.tag_ids)) > 0"
        if family == "links":
            # At least one inbound link — a direct edge targets the node.
            return f"EXISTS (SELECT 1 FROM edge WHERE target_id = {alias}.id AND {self._ws_value()})"
        if family in ("parent", "parentPath"):
            # parentPath's emptiness reads "has a parent" (the parentSet bit).
            return f"{alias}.parent_id IS NOT NULL"
        if family == "child":
            # At least one direct child.
            return (
                "EXISTS (\n  SELECT 1 FROM nodes c\n"
                f"  WHERE c.parent_id = {alias}.id AND c.is_active = 1 AND {self._ws('c')}\n)"
            )
        if family == "childPath":
            # At least one descendant (any depth below). The subtree CTE
            # anchors at the outer row (correlated anchor); any member other
            # than the row itself is a descendant.
            subtree = self._subtree_set_sql(f"SELECT {alias}.id, 0 AS distance")
            return f"EXISTS (SELECT 1 FROM ({subtree}) WHERE id != {alias}.id LIMIT 1)"
        if family == "page":
            # The row lives inside a page — the containing page differs from
            # the row itself (a page's own page is itself).
            return f"{self._containing_page_id_sql(alias)} != {alias}.id"
        raise QueryCompileError(f"query compile: the '{family}' family has no emptiness (exists) semantics")

    # -- date ---------------------------------------------------------------------

    def _date_sql(self, condition: DateCondition, alias: str) -> str:
        column = "created_at" if condition.field == "created" else "updated_at"
        if condition.op in ("eq", "neq", "gt", "gte", "lt", "lte"):
            if condition.timestamp is None:
                raise QueryCompileError(f"query compile: date op '{condition.op}' requires a timestamp")
            return self._date_cmp_sql(column, condition.op, condition.timestamp, alias)
        if condition.op == "after":
            if condition.timestamp is None:
                raise QueryCompileError("query compile: date op 'after' requires a timestamp")
            bound = self._push(resolve_timestamp_placeholder(condition.timestamp, "after", self._options.now))
            return f"{alias}.{column} >= {bound}"
        if condition.op == "before":
            if condition.timestamp is None:
                raise QueryCompileError("query compile: date op 'before' requires a timestamp")
            bound = self._push(resolve_timestamp_placeholder(condition.timestamp, "before", self._options.now))
            return f"{alias}.{column} <= {bound}"
        if condition.op == "between":
            if condition.from_ is None or condition.to is None:
                raise QueryCompileError("query compile: date op 'between' requires both bounds")
            return self._date_between_sql(column, condition.from_, condition.to, alias)
        assert condition.op == "within"
        if condition.period is None:
            raise QueryCompileError("query compile: date op 'within' requires a period")
        return self._date_within_sql(column, condition.period, alias)

    def _node_field_sql(self, field: str, column: str, condition: NodeFieldCondition, alias: str) -> str:
        if condition.op == "exists":
            return f"{alias}.{column} IS NOT NULL"
        if condition.value is None:
            raise QueryCompileError(f"query compile: {field} op '{condition.op}' requires a non-null value")
        return f"{alias}.{column} {'=' if condition.op == 'eq' else '!='} {self._push(condition.value)}"

    def _name_sql(self, condition: NameCondition, alias: str) -> str:
        title = _title_sql(alias)
        if condition.op == "exists":
            # COALESCE'd two-valued so the not-wrap reads "is untitled".
            return f"COALESCE(length({title}), 0) > 0"
        if condition.value is None:
            raise QueryCompileError(f"query compile: name op '{condition.op}' requires a non-null value")
        param = self._push(condition.value)
        if condition.op in ("eq", "neq"):
            return f"{title} {'=' if condition.op == 'eq' else '!='} {param}"
        return f"{title} LIKE '%' || {param} || '%'"

    def _content_sql(self, condition: ContentCondition, alias: str) -> str:
        """The plaintext condition over the mirror's derived search text (D2:
        the folded ``search_plain`` column — mention-resolved content +
        asset names + text-ish property values — replaces the TS
        search_index probes; the needle stays verbatim, LIKE is ASCII
        case-insensitive, matching the TS convention)."""
        if condition.value is None:
            raise QueryCompileError(f"query compile: content op '{condition.op}' requires a non-null value")
        if condition.op == "fts":
            # Prefix-AND emulation: the TS fts is a prefix-AND MATCH over the
            # search text — token-prefix semantics. GLOB arms emulate exactly
            # that over the search-text column: a term matches at the string
            # start or right after a non-alphanumeric boundary (the
            # unicode61 token boundaries), ASCII case-folded like the FTS
            # tokenizer (terms are alnum-only, so the patterns are GLOB-safe).
            terms = [term for segment in re.findall(r'"([^"]*)"', condition.value) for term in _split_terms(segment)]
            remainder = re.sub(r'"[^"]*"', " ", condition.value)
            terms.extend(_split_terms(remainder))
            if not terms:
                raise QueryCompileError(
                    f"query compile: content fts value produced no search terms {json.dumps(condition.value)}"
                )
            arms = []
            for term in terms:
                upper = term.upper()
                start_pattern = self._push(f"{upper}*")
                boundary_pattern = self._push(f"*[^A-Z0-9]{upper}*")
                arms.append(
                    f"(upper({alias}.search_plain) GLOB {start_pattern}"
                    f" OR upper({alias}.search_plain) GLOB {boundary_pattern})"
                )
            return "(" + " AND ".join(arms) + ")"
        param = self._push(condition.value)
        if condition.op == "eq":
            return f"{alias}.search_plain = {param}"
        if condition.op == "neq":
            return f"{alias}.search_plain != {param}"
        anchor = {
            "starts": f"{param} || '%'",
            "ends": f"'%' || {param}",
            "contains": f"'%' || {param} || '%'",
        }[condition.op]
        return f"{alias}.search_plain LIKE {anchor}"

    def _property_sql(self, condition: PropertyCondition, alias: str) -> str:
        value_ops = ("eq", "neq", "contains", "gt", "gte", "lt", "lte")
        # The comparison bound rides the placeholder clock: a token resolves
        # to the period start as a plain YYYY-MM-DD date. contains is a
        # text-substring op and exists carries no value: neither resolves.
        bound: Any = condition.value
        if isinstance(condition.value, str) and condition.op not in ("contains", "exists"):
            bound = resolve_value_placeholder(condition.value, self._options.now)
        if condition.op in value_ops and bound is None:
            raise QueryCompileError(f"query compile: property op '{condition.op}' requires a non-null value")
        include_defaults = True if condition.include_defaults is None else condition.include_defaults
        ctes, unions = self._effective_value_ctes(condition.schema_id, include_defaults)

        # exists carries no predicate: every effective row matches (the
        # template interpolates the empty string, exactly the TS `let
        # predicate = ""` start).
        predicate = ""
        bound_iso = bound if isinstance(bound, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", bound) else None
        if condition.op == "eq":
            scalar = f"json_extract(value, '$') = {self._push(bound)}"
            predicate = f"WHERE {scalar} OR {self._date_ref_sql('=', bound_iso)}" if bound_iso else f"WHERE {scalar}"
        elif condition.op == "neq":
            if bound_iso is not None:
                scalar = f"json_extract(value, '$') = {self._push(bound)}"
                predicate = f"WHERE NOT ({scalar} OR {self._date_ref_sql('=', bound_iso)})"
            else:
                predicate = f"WHERE json_extract(value, '$') != {self._push(bound)}"
        elif condition.op == "contains":
            scalar = f"CAST(json_extract(value, '$') AS TEXT) LIKE '%' || {self._push(condition.value)} || '%'"
            predicate = f"WHERE {scalar} OR {self._date_ref_sql('=', bound_iso)}" if bound_iso else f"WHERE {scalar}"
        elif condition.op != "exists":
            # gt/gte/lt/lte — plain comparison over the extracted scalar
            # (numeric for JSON numbers, lexicographic for text).
            sql_op = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[condition.op]
            if bound_iso is not None:
                # The scalar arm is gated to non-object values: a
                # { "nodeId": … } ref would otherwise compare as raw JSON text.
                scalar = (
                    f"json_type(value, '$') != 'object' AND json_extract(value, '$') {sql_op} {self._push(bound)}"
                )
                predicate = f"WHERE ({scalar}) OR {self._date_ref_sql(sql_op, bound_iso)}"
            else:
                predicate = f"WHERE json_extract(value, '$') {sql_op} {self._push(bound)}"
        membership = (
            f"{alias}.id IN (\n  {ctes},\n  effective AS (\n  {unions}\n  )\n"
            f"  SELECT node_id FROM effective\n  {predicate}\n)"
        )
        return membership

    def _effective_value_ctes(self, schema_id: str, include_defaults: bool) -> tuple[str, str]:
        """The effective-values CTE pair (``effectiveValueCtes`` verbatim —
        no nodes/edge scans, D1 does not apply). Params push in CTE text
        order (schema, then the two derived arms)."""
        schema_param = self._push(schema_id)
        tombstone = _Compiler._visible_value_predicate("pv")
        ctes = (
            "WITH authored AS (\n"
            "  SELECT pv.node_id AS node_id, pv.value AS value\n"
            "  FROM property_value pv\n"
            f"  WHERE pv.property_schema_id = {schema_param}\n"
            f"    AND {tombstone}\n"
            ")"
        )
        unions = "SELECT node_id, value FROM authored"
        if include_defaults:
            binding_schema_id = self._push(schema_id)
            authored_schema_id = self._push(schema_id)
            ctes += (
                ",\nderived AS (\n"
                "  SELECT wb.node_id AS node_id, wb.default_value AS value\n"
                "  FROM (\n"
                "    SELECT cms.node_id, ps.default_value,\n"
                "           ROW_NUMBER() OVER (\n"
                "             PARTITION BY cms.node_id\n"
                "             ORDER BY cms.hlc_physical, cms.hlc_logical, cms.class_id\n"
                "           ) AS rn\n"
                "    FROM class_member_set cms\n"
                "    JOIN class_property cp ON cp.class_id = cms.class_id\n"
                "    JOIN property_schema ps ON ps.id = cp.property_schema_id\n"
                f"    WHERE cms.present = 1 AND cp.property_schema_id = {binding_schema_id} AND cp.active = 1\n"
                "      AND ps.default_value IS NOT NULL\n"
                # Read-side option membership (the effective.ts rule): a
                # select-typed default whose value is no longer among the
                # schema's options derives nothing.
                "      AND (ps.type NOT IN ('select', 'multi_select')\n"
                "        OR EXISTS (\n"
                "          SELECT 1 FROM json_each(ps.options) je\n"
                "          WHERE json_extract(je.value, '$.id') = json_extract(ps.default_value, '$')\n"
                "        ))\n"
                "  ) wb\n"
                "  WHERE wb.rn = 1\n"
                "    AND NOT EXISTS (\n"
                "      SELECT 1 FROM property_value pv\n"
                f"      WHERE pv.node_id = wb.node_id AND pv.property_schema_id = {authored_schema_id}\n"
                "        AND pv.idx = 0\n"
                f"        AND {tombstone}\n"
                "    )\n"
                ")"
            )
            unions += "\nUNION ALL\nSELECT node_id, value FROM derived"
        return ctes, unions

    def _has_parentless_block_class_sql(self, alias: str) -> str:
        """The class-level parentlessRole demote (``hasParentlessBlockClassSql``
        port; D1: the class-node scan is workspace-filtered)."""
        ws = self._push(self._options.workspace_id)
        return (
            "EXISTS (SELECT 1 FROM class_hierarchy ch\n"
            f"  JOIN nodes c ON c.id = ch.ancestor_id AND c.workspace_id = {ws}\n"
            f"  WHERE ch.class_id IN (SELECT value FROM json_each({alias}.class_ids))\n"
            "    AND c.parentless_role = 'block')"
        )

    def _containing_page_id_sql(self, alias: str) -> str:
        """The CONTAINING PAGE id as a correlated scalar subquery (D1: every
        nodes scan is workspace-filtered). Params push in text order: the
        up-CTE arm, the demote probe, then the outer pg row."""
        ws_up = self._push(self._options.workspace_id)
        demote = self._has_parentless_block_class_sql("pg")
        ws_pg = self._push(self._options.workspace_id)
        return (
            "(SELECT pg.id FROM nodes pg JOIN (\n"
            "  WITH RECURSIVE up(id, depth) AS (\n"
            f"    SELECT {alias}.id, 0\n"
            "    UNION ALL\n"
            f"    SELECT nd.parent_id, up.depth + 1 FROM nodes nd JOIN up ON nd.id = up.id\n    WHERE nd.parent_id IS NOT NULL AND up.depth < 64 AND nd.workspace_id = {ws_up}\n"
            "  ) SELECT id, depth FROM up\n"
            ") ch ON ch.id = pg.id\n"
            "WHERE (pg.is_class = 1 OR pg.parent_id IS NULL OR pg.is_embedded = 0)\n"
            f"  AND (pg.parent_id IS NOT NULL OR NOT {demote})\n"
            f"  AND pg.workspace_id = {ws_pg}\n"
            "ORDER BY ch.depth LIMIT 1)"
        )

    def _root_id_sql(self, alias: str) -> str:
        """The ROOT ancestor id (the workspace-root child) as a correlated
        scalar subquery (D1-rewritten; params in text order)."""
        ws_pg = self._push(self._options.workspace_id)
        ws_rec = self._push(self._options.workspace_id)
        ws_final = self._push(self._options.workspace_id)
        return (
            f"(SELECT pg.id FROM nodes pg WHERE pg.workspace_id = {ws_pg} AND pg.id = (\n"
            "  WITH RECURSIVE up(id) AS (\n"
            f"    SELECT {alias}.id\n"
            "    UNION ALL\n"
            f"    SELECT nd.parent_id FROM nodes nd JOIN up ON nd.id = up.id\n    WHERE nd.parent_id IS NOT NULL AND nd.workspace_id = {ws_rec}\n"
            "  )\n"
            f"  SELECT up.id FROM up JOIN nodes nd ON nd.id = up.id\n  WHERE nd.parent_id IS NULL AND nd.workspace_id = {ws_final} LIMIT 1\n))"
        )

    def _reference_path_sql(self, targets_sql: str, alias: str) -> str:
        """The referencePath family rollup over a target set (D1 rewrite: the
        anc CTE + the edge arm are workspace-filtered). Params push in text
        order — the targets subquery textually PRECEDES the CTE predicates,
        so the tv CTE is declared first (neither CTE references the other;
        ``WITH RECURSIVE`` admits plain CTEs)."""
        ws_anc = self._push(self._options.workspace_id)
        ws_edge = self._push(self._options.workspace_id)
        return (
            "EXISTS (\n  WITH RECURSIVE tv AS (\n  " + targets_sql + "\n  ),\n"
            "  anc(id, depth) AS (\n"
            f"    SELECT {alias}.id, 0\n"
            "    UNION ALL\n"
            f"    SELECT nd.parent_id, anc.depth + 1 FROM nodes nd JOIN anc ON nd.id = anc.id\n    WHERE anc.id IS NOT NULL AND anc.depth < 64 AND nd.workspace_id = {ws_anc}\n"
            "  )\n"
            "  SELECT 1 FROM tv\n"
            f"  WHERE tv.id IN (SELECT id FROM anc WHERE id != {alias}.id)\n"
            "     OR EXISTS (\n       SELECT 1 FROM edge e\n"
            f"       WHERE e.target_id = tv.id AND e.source_id IN (SELECT id FROM anc) AND e.workspace_id = {ws_edge}\n     ))"
        )

    def _date_cmp_sql(self, column: str, op: str, timestamp: str, alias: str) -> str:
        if op in ("eq", "neq"):
            day = resolve_day_bounds(timestamp, self._options.now)
            start = self._push(day[0])
            end = self._push(day[1])
            match = f"{alias}.{column} >= {start} AND {alias}.{column} <= {end}"
            return match if op == "eq" else f"NOT ({match})"
        direction = "after" if op in ("gt", "gte") else "before"
        sql_op = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
        bound = self._push(resolve_timestamp_placeholder(timestamp, direction, self._options.now))
        return f"{alias}.{column} {sql_op} {bound}"

    def _date_between_sql(self, column: str, from_value: str, to_value: str, alias: str) -> str:
        from_bound = self._push(resolve_timestamp_placeholder(from_value, "after", self._options.now))
        to_bound = self._push(resolve_timestamp_placeholder(to_value, "before", self._options.now))
        return f"{alias}.{column} >= {from_bound} AND {alias}.{column} <= {to_bound}"

    def _date_within_sql(self, column: str, period: str, alias: str) -> str:
        bounds = resolve_period_bounds(period, self._options.now)
        from_param = self._push(bounds[0])
        to_param = self._push(bounds[1])
        return f"{alias}.{column} >= {from_param} AND {alias}.{column} <= {to_param}"

    def _property_range_sql(self, schema_id: str, from_value: Any, to_value: Any, alias: str) -> str:
        if from_value is None or to_value is None:
            raise QueryCompileError("query compile: property op 'between' requires both bounds")
        ctes, unions = self._effective_value_ctes(schema_id, True)
        from_iso = from_value if isinstance(from_value, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", from_value) else None
        to_iso = to_value if isinstance(to_value, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", to_value) else None
        from_param = self._push(from_value)
        to_param = self._push(to_value)
        predicate = f"WHERE json_extract(value, '$') >= {from_param} AND json_extract(value, '$') <= {to_param}"
        if from_iso is not None and to_iso is not None:
            # ISO bounds also speak to date-node refs; the scalar arm gates
            # non-object values (raw JSON text would sort wrong).
            scalar = f"json_type(value, '$') != 'object' AND {predicate[len('WHERE '):]}"
            predicate = (
                f"WHERE ({scalar}) OR ({self._date_ref_sql('>=', from_iso)}"
                f" AND {self._date_ref_sql('<=', to_iso)})"
            )
        return (
            f"{alias}.id IN (\n  {ctes},\n  effective AS (\n  {unions}\n  )\n"
            f"  SELECT node_id FROM effective\n  {predicate}\n)"
        )

    def _property_within_sql(self, schema_id: str, period: str | None, alias: str) -> str:
        if period is None:
            raise QueryCompileError("query compile: property op 'within' requires a period")
        ctes, unions = self._effective_value_ctes(schema_id, True)
        token = f"{{{period}}}"
        from_iso = placeholder_start_date(token, self._options.now)
        to_iso = placeholder_end_date(token, self._options.now)
        if from_iso is None or to_iso is None:
            raise QueryCompileError(f"query compile: unknown property period {json.dumps(period)}")
        from_param = self._push(from_iso)
        to_param = self._push(to_iso)
        scalar = (
            f"json_type(value, '$') != 'object' AND json_extract(value, '$') >= {from_param}"
            f" AND json_extract(value, '$') <= {to_param}"
        )
        predicate = f"WHERE ({scalar}) OR ({self._date_ref_sql('>=', from_iso)} AND {self._date_ref_sql('<=', to_iso)})"
        return (
            f"{alias}.id IN (\n  {ctes},\n  effective AS (\n  {unions}\n  )\n"
            f"  SELECT node_id FROM effective\n  {predicate}\n)"
        )

    def _date_ref_sql(self, op: str, iso: str | None) -> str:
        """Date-ref match arms over the deterministic date-node ids carried in
        the datetime value union (``dateRefSql`` verbatim — pure JSON/SQL
        functions over the property value text, no node scans)."""
        assert iso is not None
        compact = iso.replace("-", "")
        node_id = "json_extract(value, '$.nodeId')"
        marker = f"substr({node_id}, 22, 2)"
        start_id = "json_extract(value, '$.start.nodeId')"
        start_marker = f"substr({start_id}, 22, 2)"
        end_id = "json_extract(value, '$.end.nodeId')"
        end_marker = f"substr({end_id}, 22, 2)"
        range_gate = "(json_type(value, '$.start') IS NOT NULL OR json_type(value, '$.end') IS NOT NULL)"
        if op == "=":
            point_arm = (
                f"({marker} = 'dd' AND substr({node_id}, 25, 8) = {self._push(compact)} "
                f"OR {marker} = 'aa' AND {self._push(compact[:6])} = substr({node_id}, 25, 6) "
                f"OR {marker} = 'bb' AND {self._push(compact[:4])} = substr({node_id}, 25, 4))"
            )
            start_payload = self._push(f"{compact}0000")
            end_day = self._push(compact)
            end_month = self._push(compact[:6])
            end_year = self._push(compact[:4])
            range_arm = (
                f"{range_gate} "
                f"AND ({start_id} IS NULL OR {start_marker} IN ('dd', 'aa', 'bb') AND substr({start_id}, 25) <= {start_payload}) "
                f"AND ({end_id} IS NULL OR ({end_marker} = 'dd' AND substr({end_id}, 25, 8) >= {end_day} "
                f"OR {end_marker} = 'aa' AND substr({end_id}, 25, 6) >= {end_month} "
                f"OR {end_marker} = 'bb' AND substr({end_id}, 25, 4) >= {end_year}))"
            )
            return f"({point_arm} OR {range_arm})"
        point_arm = f"({marker} IN ('dd', 'aa', 'bb') AND substr({node_id}, 25) {op} {self._push(f'{compact}0000')})"
        range_arm = (
            f"{range_gate} AND {start_marker} IN ('dd', 'aa', 'bb') "
            f"AND substr({start_id}, 25) {op} {self._push(f'{compact}0000')}"
        )
        return f"({point_arm} OR ({range_arm}))"

    # -- sort -----------------------------------------------------------------------

    def _order_sql(self, sort: tuple[SortSpec, ...] | None) -> str:
        terms: list[str] = []
        for spec in sort or ():
            if spec.field == "name":
                column: str = _title_sql("n")
                nullable = True
                nocase = True
            elif spec.field == "createdAt":
                column = "n.created_at"
                nullable = True
                nocase = False
            elif spec.field == "isClass":
                column = "n.is_class"
                nullable = False
                nocase = False
            else:
                column = "n.is_embedded"
                nullable = False
                nocase = False
            direction = "DESC" if spec.dir == "desc" else "ASC"
            ordered = f"{column} COLLATE NOCASE {direction}" if nocase else f"{column} {direction}"
            # NULLs last on nullable columns (IS NULL is portable across the
            # sql.js-era builds the TS comment cites).
            if nullable:
                terms.append(f"{column} IS NULL")
            terms.append(ordered)
        terms.append("n.id ASC")
        return ", ".join(terms)


def _aggregation_columns(aggregation: Aggregation) -> tuple[str, ...]:
    """Deterministic aggregate column labels (``aggregationColumns`` port):
    dimensions in declared order, then measures; collisions get a positional
    ``#n`` suffix."""
    names = [
        f"{dimension.kind}:{dimension.id}"
        if dimension.kind in ("class", "property") and dimension.id
        else dimension.kind
        for dimension in aggregation.dimensions
    ]
    for measure in aggregation.measures:
        if measure.function in ("count", "countDistinct"):
            names.append(measure.function)
        else:
            names.append(f"{measure.function}:{measure.id}")
    used: set[str] = set()
    columns: list[str] = []
    for name in names:
        if name not in used:
            used.add(name)
            columns.append(name)
            continue
        index = 2
        while f"{name}#{index}" in used:
            index += 1
        suffixed = f"{name}#{index}"
        used.add(suffixed)
        columns.append(suffixed)
    return tuple(columns)
