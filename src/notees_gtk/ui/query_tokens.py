"""Query-token content helpers — the read/write surface the query section's
builder uses for working with ``query`` content tokens on a node's content.

Port of ``apps/web/src/ui/queryTokens.ts`` + the editor's ``object.update``
authoring shape (``notees_gtk.ui.editor`` / ``notees_gtk.ui.alias_ops``
precedents). All writes go through the normal content update path
(``object.update`` on the node's contentAst, token splice by index) — the
tokens ride the op log, so a query's AST syncs like any other content.

Headless-testable by design: no GTK imports here (the conventions keep pure
UI logic importable without the ``ui`` extra, exactly like
:mod:`notees_gtk.ui.alias_ops`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from notees_gtk.core.protocol.clock import Clock
from notees_gtk.core.protocol.content import parse_content_ast
from notees_gtk.core.protocol.models import RelayEnvelope, new_envelope

__all__ = [
    "append_query_token",
    "content_update_envelope",
    "query_token_indexes",
    "read_query_token",
    "update_query_token_ast",
]


def _tokens(content: str | Sequence[Any] | None) -> list[Any]:
    """Normalize stored content to a raw token list (the
    :func:`notees_gtk.core.protocol.content.parse_content_ast` passthrough —
    legacy plaintext stays a single text token, exactly the read path)."""
    return parse_content_ast(content)


def _query_token_at(tokens: Sequence[Any], index: int) -> dict[str, Any]:
    """The token at ``index``, fail loud when it is not a ``query`` token —
    the write path never guesses (the content-update op would otherwise
    silently no-op at the splice)."""
    if not 0 <= index < len(tokens):
        raise IndexError(f"query token index {index} out of range ({len(tokens)} tokens)")
    token = tokens[index]
    if not isinstance(token, dict) or token.get("type") != "query":
        raise ValueError(f"token at index {index} is not a query token")
    return token


def query_token_indexes(content: str | Sequence[Any] | None) -> list[int]:
    """The indexes of every ``query`` token in the content array (the
    section chrome's address space — the view records carry the index, the
    builder writes through it)."""
    return [index for index, token in enumerate(_tokens(content)) if isinstance(token, dict) and token.get("type") == "query"]


def read_query_token(content: str | Sequence[Any] | None, index: int) -> dict[str, Any]:
    """The raw ``query`` token at ``index`` (its ``queryAst`` rides at the
    ``queryAst`` key; absent = the unspecified query)."""
    return dict(_query_token_at(_tokens(content), index))


def update_query_token_ast(
    content: str | Sequence[Any] | None,
    index: int,
    query_ast: Any,
) -> list[Any]:
    """Rewrite the token's ``queryAst`` at ``index`` — the per-section
    builder's commit (``updateQueryTokenAst`` port: the token splices
    verbatim at its index, every other token untouched)."""
    tokens = _tokens(content)
    token = dict(_query_token_at(tokens, index))
    token["queryAst"] = query_ast
    return [token if i == index else tokens[i] for i in range(len(tokens))]


def append_query_token(content: str | Sequence[Any] | None, query_ast: Any) -> tuple[list[Any], int]:
    """Append a new ``query`` token (``appendQueryToken`` port) — returns the
    next content array and the new token's index. A classed block's first
    Apply creates the payload."""
    tokens = _tokens(content)
    index = len(tokens)
    return [*tokens, {"type": "query", "queryAst": query_ast}], index


def content_update_envelope(
    *,
    workspace_id: str,
    actor_id: str,
    device_id: str,
    node_id: str,
    content_ast: Sequence[Any],
    clock: Clock,
) -> RelayEnvelope:
    """Build the ``object.update`` envelope carrying the next content array —
    the same authoring shape the editor's save uses (the whole ``contentAst``
    rides the payload; the applier's key-presence rule re-derives the
    plaintext and the edge index in the same transaction)."""
    return new_envelope(
        workspace_id=workspace_id,
        actor_id=actor_id,
        device_id=device_id,
        op_type="object.update",
        payload={"objectId": node_id, "contentAst": list(content_ast)},
        clock=clock,
        affected_node_ids=(node_id,),
    )
