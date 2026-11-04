"""Execution helpers over the GTK mirror (``packages/query/src/execute.ts``
port). The TS takes a structural store interface; the structural surface
collapses to a stdlib ``sqlite3.Connection`` here — any connection whose
derived schema matches the mirror works.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from notees_gtk.core.protocol.query_ast import QueryAst
from notees_gtk.core.query.compiler import CompileOptions, compile, compile_aggregate

__all__ = ["AggregateResult", "QueryResult", "count_query", "matches", "run_aggregate", "run_query"]


@dataclass(frozen=True)
class QueryResult:
    """The plain-query result: node ids in the deterministic compiler order,
    plus the full ``n.*`` rows (plus ``distance`` for subtree/linkedTo scopes)."""

    ids: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class AggregateResult:
    """The grouped grid: the compiler's deterministic column order, one row
    per group aligned with ``columns``."""

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]


def _fetch_all(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, params)
    names = [column[0] for column in cursor.description or ()]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def run_query(conn: sqlite3.Connection, ast: QueryAst, options: CompileOptions) -> QueryResult:
    """Run a plain query: ids + full rows. Aggregation ASTs are rejected —
    execute them with :func:`run_aggregate`."""
    if ast.aggregation is not None:
        raise ValueError("query run: AST carries an aggregation — execute it with run_aggregate instead")
    compiled = compile(ast, options)
    rows = _fetch_all(conn, compiled.sql, compiled.params)
    return QueryResult(ids=tuple(str(row["id"]) for row in rows), rows=tuple(rows))


def run_aggregate(conn: sqlite3.Connection, ast: QueryAst, options: CompileOptions) -> AggregateResult:
    """Run an aggregation AST: the grouped grid."""
    compiled = compile_aggregate(ast, options)
    raw = _fetch_all(conn, compiled.sql, compiled.params)
    return AggregateResult(
        columns=compiled.columns,
        rows=tuple(tuple(row[column] for column in compiled.columns) for row in raw),
    )


def count_query(conn: sqlite3.Connection, ast: QueryAst, options: CompileOptions) -> int:
    """The number of nodes matching the query (COUNT over the compiled select)."""
    compiled = compile(ast, options)
    row = conn.execute(f"SELECT COUNT(*) AS count FROM ({compiled.sql})", compiled.params).fetchone()
    assert row is not None
    return int(row[0])


def matches(conn: sqlite3.Connection, node_id: str, ast: QueryAst, options: CompileOptions) -> bool:
    """Convenience: does this one node match the query?"""
    compiled = compile(ast, options)
    row = conn.execute(f"SELECT 1 FROM ({compiled.sql}) q WHERE q.id = ?", (*compiled.params, node_id)).fetchone()
    return row is not None
