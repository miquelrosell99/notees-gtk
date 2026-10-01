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
- :func:`stringify_content_ast` — port of ``stringifyContentAst``: flatten any
  token stream to text-only content (pages and classes carry text-only
  content — SCHEMA.md "title-is-content"; structural ``whiteboard``/``query``
  widgets survive);
- :func:`derive_display_name` — port of ``deriveDisplayName``: the display
  name IS the content excerpt (date labels in the YYYYMMDD shape format as
  YYYY/MM(/DD));
- :func:`tokens_from_plaintext` — the editor's honest MVP mapping: one source
  line becomes one ``text`` run, with ``hard_break`` between lines.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "DISPLAY_NAME_MAX",
    "derive_display_name",
    "format_date_node_name",
    "parse_content_ast",
    "plaintext_excerpt",
    "stringify_content_ast",
    "tokens_from_plaintext",
]

#: Display-name excerpt cap (``DISPLAY_NAME_MAX`` in domain/node.ts).
DISPLAY_NAME_MAX = 80

_WHITESPACE_RE = re.compile(r"\s+")
_DATE_LABEL_RE = re.compile(r"^\d{8}$")


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


def stringify_content_ast(tokens: Sequence[Any] | None) -> list[Any]:
    """Flatten any token stream to text-only content.

    Port of v2 ``stringifyContentAst`` (packages/domain/src/node.ts): pages
    and classes carry text-only content (SCHEMA.md "title-is-content").
    Inline rich tokens (mentions, chips, links, marks) fold into their plain
    text; block-scale structural widgets (``whiteboard``, ``query``) survive
    as tokens — they are displays, not prose. The flattened stream is one
    leading ``text`` token (when non-empty) followed by the surviving
    widgets, so replicas deriving from any rich source converge byte-equal.
    """
    if not tokens:
        return []
    out: list[Any] = []
    for token in tokens:
        if isinstance(token, Mapping) and token.get("type") in ("whiteboard", "query"):
            out.append(dict(token))
    text = plaintext_excerpt(tokens).strip()
    if text != "":
        out.insert(0, {"type": "text", "text": text})
    return out


def format_date_node_name(name: str, class_ids: Sequence[str] | None = None) -> str | None:
    """Format a raw date-node label; ``None`` when not the YYYYMMDD shape.

    Port of v2 ``formatDateNodeName``: 20290000 → 2029, 20290600 → 2029/06,
    20290627 → 2029/06/27 (zero-padded segments dropped). The class check is
    deliberately NOT required — migrated date pages may lack the
    day/month/year classes, and an 8-digit label is unambiguous.
    """
    del class_ids  # retained in the signature for callers that have it
    digits = re.sub(r"\D", "", name)
    if not _DATE_LABEL_RE.match(digits):
        return None
    year, month, day = digits[:4], digits[4:6], digits[6:8]
    if month == "00":
        return year
    if day == "00":
        return f"{year}/{month}"
    return f"{year}/{month}/{day}"


def derive_display_name(tokens: Sequence[Any] | None, class_ids: Sequence[str] | None = None) -> str:
    """Derive the display name from a node's content (title-is-content).

    Port of v2 ``deriveDisplayName`` (packages/domain/src/node.ts,
    2026-10-01): a node's title IS its own text content — there is no name
    field for any node (pages, blocks AND classes); renames never propagate.
    Callers fall back to a human "Untitled" label when this returns "".
    Date nodes (year/month/day system classes) carry the raw YYYYMMDD-style
    label as their content; display formats it YYYY/MM(/DD).
    """
    excerpt = plaintext_excerpt(tokens or []).strip()
    if not excerpt:
        return ""
    date_formatted = format_date_node_name(excerpt, class_ids)
    return (date_formatted or excerpt)[:DISPLAY_NAME_MAX]


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
