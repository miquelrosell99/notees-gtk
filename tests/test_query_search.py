"""The global-search AST builder tests — the pure headless half of the
slice-1 search surface (``ast_for_search_text``).

The builder maps a search-box string onto the canonical v2 AST
(``{version: 2, scope: entire_workspace, root: group-and[content contains
<text>]}``); the window's search entry compiles + runs it through the
``LocalStore.run_query_ast`` seam (seam tests live in test_query_execute).
"""

from __future__ import annotations

import pytest

from notees_gtk.core.protocol.query_ast import (
    ContentCondition,
    EntireWorkspaceScope,
    Group,
    parse_query_ast,
)
from notees_gtk.core.query.search import ast_for_search_text, search_wire


class TestAstForSearchText:
    def test_builds_the_canonical_content_contains_ast(self) -> None:
        ast = ast_for_search_text("capital city")
        assert ast.version == 2
        assert isinstance(ast.scope, EntireWorkspaceScope)
        assert ast.sort is None and ast.aggregation is None
        assert isinstance(ast.root, Group)
        assert ast.root.logic == "and"
        assert len(ast.root.children) == 1
        condition = ast.root.children[0]
        assert isinstance(condition, ContentCondition)
        assert condition.op == "contains"
        assert condition.value == "capital city"

    def test_the_built_ast_round_trips_through_the_strict_parser(self) -> None:
        raw = {
            "version": 2,
            "scope": {"type": "entire_workspace"},
            "root": {
                "type": "group",
                "logic": "and",
                "children": [{"type": "content", "op": "contains", "value": "vidéo"}],
            },
        }
        assert parse_query_ast(raw) == ast_for_search_text("vidéo")

    def test_surrounding_whitespace_is_trimmed(self) -> None:
        ast = ast_for_search_text("  meeting notes  ")
        assert ast.root.children[0].value == "meeting notes"

    def test_search_wire_serializes_the_shape_the_store_seam_parses(self) -> None:
        wire = search_wire("capital")
        assert wire == {
            "version": 2,
            "scope": {"type": "entire_workspace"},
            "root": {
                "type": "group",
                "logic": "and",
                "children": [{"type": "content", "op": "contains", "value": "capital"}],
            },
        }
        assert parse_query_ast(wire) == ast_for_search_text("capital")

    def test_empty_text_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            ast_for_search_text("")
        with pytest.raises(ValueError, match="non-empty"):
            ast_for_search_text("   ")
