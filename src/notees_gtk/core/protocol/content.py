"""Content grammar helpers for the v2 flat token stream (SCHEMA.md normative).

A block node's content is ONE flat, ordered token array. This module holds the
shared, UI-free operations both the store (plaintext derivation on apply) and
the renderer (display) consume:

- :func:`parse_content_ast` — normalize the stored ``content`` column
  (serialized JSON array, or legacy plaintext) to a token list;
- :func:`plaintext_excerpt` — port of ``plainTextExcerpt`` (v2
  ``packages/domain/src/node.ts``): the FTS/sidebar text derived from tokens
  (text + typed_link text, mention ``displayText ?? text``, math expressions,
  recursive quote children, ``hard_break`` as a space);
- :func:`tokens_from_plaintext` — the editor's honest MVP mapping: one source
  line becomes one ``text`` run, with ``hard_break`` between lines.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["parse_content_ast", "plaintext_excerpt", "tokens_from_plaintext"]

_WHITESPACE_RE = re.compile(r"\s+")


def parse_content_ast(raw: str | Sequence[Any] | None) -> list[Any]:
    """Normalize stored content to a raw token list.

    The v2 ``content`` column holds a serialized JSON array of typed tokens;
    legacy plaintext (anything that is not a JSON array) is returned as a
    single text token so old mirrors still render instead of vanishing.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(raw)
        except ValueError:
            return [{"type": "text", "text": raw}]
        if isinstance(parsed, list):
            return parsed
        return [{"type": "text", "text": raw}]
    return list(raw)


def plaintext_excerpt(tokens: Sequence[Any]) -> str:
    """Derive the search/sidebar plaintext of a token array.

    Port of v2 ``plainTextExcerpt``: text and typed-link runs contribute their
    text, mentions their ``displayText`` (captured ``text`` when absent), math
    its expression, quotes recurse into their children, ``hard_break`` becomes
    a space; every other token is silent. Whitespace collapses to single
    spaces. Plaintext is derived, never stored as truth.
    """
    parts: list[str] = []

    def walk(items: Sequence[Any]) -> None:
        for token in items:
            if not isinstance(token, Mapping):
                continue
            ttype = token.get("type")
            if ttype in ("text", "typed_link"):
                parts.append(str(token.get("text", "")))
            elif ttype == "mention":
                parts.append(str(token.get("displayText") or token.get("text") or ""))
            elif ttype == "math":
                parts.append(str(token.get("expression", "")))
            elif ttype == "quote":
                children = token.get("children")
                if isinstance(children, list):
                    walk(children)
            elif ttype == "hard_break":
                parts.append(" ")

    walk(tokens)
    return _WHITESPACE_RE.sub(" ", " ".join(parts)).strip()


def tokens_from_plaintext(text: str) -> list[dict[str, Any]]:
    """Map editor plaintext to the flat v2 token array (the honest MVP form).

    Each source line becomes one ``text`` run; ``hard_break`` separates lines
    (shift+enter is the only break in the v2 grammar — Enter creates a new
    node, which the plain-text editor cannot express). Empty input is an
    empty stream.
    """
    if text == "":
        return []
    tokens: list[dict[str, Any]] = []
    for index, line in enumerate(text.split("\n")):
        if index > 0:
            tokens.append({"type": "hard_break"})
        tokens.append({"type": "text", "text": line})
    return tokens
