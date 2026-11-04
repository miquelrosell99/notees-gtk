"""The global-search AST builder — the pure headless half of the slice-1
search surface.

``ast_for_search_text`` turns a search-box string into the canonical v2 AST
(``{version: 2, scope: entire_workspace, root: group-and[content contains
<text>]}``); the window's search entry compiles + runs it through the
``LocalStore.run_query_ast`` seam. Kept pure here so the UI stays thin and
the mapping is unit-tested without GTK (the core law).
"""

from __future__ import annotations

from typing import Any

from notees_gtk.core.protocol.query_ast import (
    ContentCondition,
    EntireWorkspaceScope,
    Group,
    QueryAst,
)

__all__ = ["ast_for_search_text", "search_wire"]


def ast_for_search_text(text: str) -> QueryAst:
    """Build the canonical search AST: ``content contains <text>`` over the
    entire workspace (substring semantics — the search-box expectation; the
    ``fts`` prefix-AND operator surprises in a plain search box).

    Args:
        text: The search string (a non-empty string; the caller's entry
            clears/ignores empty input).

    Raises:
        ValueError: Empty or whitespace-only text has no honest reading.
    """
    value = text.strip()
    if not value:
        raise ValueError("search text must be non-empty")
    return QueryAst(
        scope=EntireWorkspaceScope(),
        root=Group(logic="and", children=(ContentCondition(op="contains", value=value),)),
    )


def search_wire(text: str) -> dict[str, Any]:
    """The serialized search AST — the wire shape the ``LocalStore``
    ``run_query_ast`` seam strict-parses. Pure; the UI passes this through."""
    ast = ast_for_search_text(text)
    condition = ast.root.children[0]
    assert isinstance(condition, ContentCondition)
    return {
        "version": 2,
        "scope": {"type": "entire_workspace"},
        "root": {
            "type": "group",
            "logic": "and",
            "children": [{"type": "content", "op": "contains", "value": condition.value}],
        },
    }
