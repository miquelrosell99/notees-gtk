"""The query surface: compiler + executor over the local derived mirror.

Ports of ``packages/query/src/{compiler,execute,placeholders}.ts`` (the
``@notees/query`` package): the versioned QueryAST v2 model is parsed in
:mod:`notees_gtk.core.protocol.query_ast`; this package compiles it to
parameterized SQLite and executes against the GTK mirror. Everything here is
headless — no GTK imports (the core/data law).
"""

from notees_gtk.core.query.compiler import (
    CompiledAggregate,
    CompiledQuery,
    CompileOptions,
    QueryCompileError,
    build_match_expression,
    compile,
    compile_aggregate,
)
from notees_gtk.core.query.execute import AggregateResult, QueryResult, count_query, matches, run_aggregate, run_query
from notees_gtk.core.query.search import ast_for_search_text, search_wire

__all__ = [
    "AggregateResult",
    "CompileOptions",
    "CompiledAggregate",
    "CompiledQuery",
    "QueryCompileError",
    "QueryResult",
    "ast_for_search_text",
    "search_wire",
    "build_match_expression",
    "compile",
    "compile_aggregate",
    "count_query",
    "matches",
    "run_aggregate",
    "run_query",
]
