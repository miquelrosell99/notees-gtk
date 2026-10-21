"""Pure token-stream → view-record renderer for the GTK UI (content grammar).

Headless-testable by design: no GTK imports here (the UI smoke test in
``tests/test_ast_render.py`` guards the ``gi`` import separately).

SCHEMA.md's Content grammar is normative: a block node's content is ONE flat,
ordered token array — there are no block-level segments, and rendering defines
presentation. Resolution rules implemented here:

- ``mention``: ``displayText`` wins, then the resolver's current target name
  (auto-rename free), then the raw target id — never an "…" placeholder;
  the captured ``text`` is non-authoritative (Fork 4);
- ``class_link``: render-only (Fork 3) — ``displayText`` ?? resolved class
  name ?? raw class id; inserting/deleting a chip never mutates ``class_ids``;
- ``typed_link``: a mark on the prose word —
  the marked word renders underlined, the verb is carried for tooltips/metadata;
- ``text``: markdown ``[label](href)`` spans — the retired ``external_link``
  token's replacement (SCHEMA.md render grammar) — split into link runs at
  render time (``packages/domain/src/markdownLinks.ts`` port), http/https/
  mailto hrefs only; every other shape stays literal text;
- block-scale tokens (``asset_ref``/``embed_ref``/``query``/``whiteboard``)
  render as labeled placeholders until the GTK client grows real views.

Plaintext (editor seed, sidebar names) is the excerpt derivation
(:func:`notees_gtk.core.protocol.content.plaintext_excerpt`) — derived, never
stored as truth.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from notees_gtk.core.protocol.content import parse_content_ast, plaintext_excerpt

__all__ = [
    "ClassChipRun",
    "HardBreakRun",
    "InlineView",
    "MarkdownLinkRun",
    "MathRun",
    "MentionRun",
    "PageItem",
    "PageView",
    "PlaceholderView",
    "QuoteView",
    "TextRun",
    "TypedLinkRun",
    "ast_to_plaintext",
    "ast_to_view",
]

#: Node-name resolver supplied by the UI layer: maps a target node id (mention
#: target or class id) to its display name. A falsy return means
#: "unresolvable" and triggers the raw-id fallback.
type NameResolver = Callable[[str], str | None]

#: Mark names (content-mark.ts MARKS): attributes on text runs, not nodes.
_VALID_MARKS: frozenset[str] = frozenset({"bold", "italic", "strike", "highlight", "code"})

#: Block-scale tokens rendered as labeled placeholders (no GTK views yet).
_PLACEHOLDER_TOKENS: frozenset[str] = frozenset({"asset_ref", "embed_ref", "query", "whiteboard"})


@dataclass(frozen=True)
class TextRun:
    """One marked (or plain) text run."""

    text: str
    marks: frozenset[str] = frozenset()


@dataclass(frozen=True)
class HardBreakRun:
    """Shift+enter line jump inside a block — flushes the rendered line."""


@dataclass(frozen=True)
class MentionRun:
    """Mention pill: ``displayText`` ?? resolved name ?? raw target id."""

    target_id: str
    text: str


@dataclass(frozen=True)
class ClassChipRun:
    """Class chip (render-only reference to a class node)."""

    class_id: str
    text: str


@dataclass(frozen=True)
class TypedLinkRun:
    """The marked word of a typed link; ``verb`` may be a free string or a
    property-schema id (create-and-bind gesture)."""

    verb: str
    text: str


@dataclass(frozen=True)
class MarkdownLinkRun:
    """A markdown ``[label](href)`` span parsed out of a text token at render
    time — the retired ``external_link`` token's replacement (SCHEMA.md render
    grammar; the ``{ kind: "link" }`` segment of
    ``packages/domain/src/markdownLinks.ts``)."""

    href: str
    text: str


@dataclass(frozen=True)
class MathRun:
    expression: str


#: One inline-scale view record (everything that can sit in a rendered line).
type InlineView = TextRun | HardBreakRun | MentionRun | ClassChipRun | TypedLinkRun | MarkdownLinkRun | MathRun


@dataclass(frozen=True)
class QuoteView:
    """The only nested token: a quote contains inline tokens."""

    children: tuple[InlineView, ...] = ()


@dataclass(frozen=True)
class PlaceholderView:
    """Block-scale token the GTK client renders as a labeled placeholder."""

    kind: str


#: Anything :class:`PageView` can hold at top level.
type PageItem = InlineView | QuoteView | PlaceholderView


@dataclass(frozen=True)
class PageView:
    """Immutable render-ready projection of one node's content token array."""

    items: tuple[PageItem, ...] = ()


# --------------------------------------------------------------------- helpers


def _resolve(resolver: NameResolver | None, node_id: str) -> str | None:
    """Run the UI-supplied name lookup; a failing store must never break rendering."""
    if not node_id or resolver is None:
        return None
    try:
        return resolver(node_id)
    except Exception:  # noqa: BLE001 — a failing store lookup must never break rendering
        return None


def _mention_text(token: Mapping[str, Any], resolver: NameResolver | None) -> tuple[str, str]:
    target_id = str(token.get("targetNodeId") or "")
    display = token.get("displayText")
    if isinstance(display, str) and display:
        return target_id, display
    resolved = _resolve(resolver, target_id)
    # Fork 4: broken targets render the raw id; captured ``text`` is
    # non-authoritative and never surfaces when the target is unresolvable.
    return target_id, resolved or target_id


def _class_link_text(token: Mapping[str, Any], resolver: NameResolver | None) -> tuple[str, str]:
    class_id = str(token.get("classId") or "")
    display = token.get("displayText")
    if isinstance(display, str) and display:
        return class_id, display
    return class_id, _resolve(resolver, class_id) or class_id


def _verb_string(raw: Any) -> str:
    """Typed-link verb: free string or ``{propertySchemaId}`` binding."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, Mapping):
        return str(raw.get("propertySchemaId") or "")
    return ""


#: The markdown-link span grammar (SCHEMA.md "Content grammar", the normative
#: render rule shared by every renderer) — port of ``MARKDOWN_LINK_RE`` in
#: ``packages/domain/src/markdownLinks.ts``: the label holds no ``]`` or
#: newline, the href no whitespace or parentheses.
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^()\s]+)\)")

#: The scheme allow-list (markdownLinks.ts ``linkableHref`` — the
#: security-relevant part of the rule): a non-http(s)/mailto href must never
#: become a clickable anchor.
_LINKABLE_HREF_RE = re.compile(r"^(https?://|mailto:)", re.IGNORECASE)


def _split_markdown_links(text: str, marks: frozenset[str]) -> list[InlineView]:
    """Port of ``splitMarkdownLinks`` (packages/domain/src/markdownLinks.ts).

    ``[label](href)`` spans with an http/https/mailto href become
    :class:`MarkdownLinkRun`; anything else (unclosed brackets, other schemes,
    relative paths) stays a literal :class:`TextRun`. A text token carrying no
    linkable span renders as one run, exactly like before the split.
    """
    runs: list[InlineView] = []
    cursor = 0
    for match in _MARKDOWN_LINK_RE.finditer(text):
        if match.start() > cursor:
            runs.append(TextRun(text=text[cursor : match.start()], marks=marks))
        raw, label, href = match.group(0), match.group(1), match.group(2)
        if _LINKABLE_HREF_RE.match(href):
            runs.append(MarkdownLinkRun(href=href, text=label))
        else:
            runs.append(TextRun(text=raw, marks=marks))
        cursor = match.end()
    if not runs:
        return [TextRun(text=text, marks=marks)]
    if cursor < len(text):
        runs.append(TextRun(text=text[cursor:], marks=marks))
    return runs


def _inline_views(token: Any, resolver: NameResolver | None) -> list[InlineView]:
    """Render one inline token to zero or more view records.

    A ``text`` token splits into runs at markdown-link spans (a link run plus
    its literal-text neighbours, the token's marks on the text segments); the
    retired ``external_link`` type has no arm — it rejects as unknown at the
    strict validator and renders as an unsupported placeholder here. Every
    other token maps to exactly one record.
    """
    if not isinstance(token, Mapping):
        return []
    ttype = token.get("type")
    if ttype == "text":
        raw_marks = token.get("marks")
        marks = (
            frozenset(str(mark) for mark in raw_marks if str(mark) in _VALID_MARKS)
            if isinstance(raw_marks, list)
            else frozenset()
        )
        return _split_markdown_links(str(token.get("text", "")), marks)
    if ttype == "hard_break":
        return [HardBreakRun()]
    if ttype == "mention":
        target_id, text = _mention_text(token, resolver)
        return [MentionRun(target_id=target_id, text=text)]
    if ttype == "class_link":
        class_id, text = _class_link_text(token, resolver)
        return [ClassChipRun(class_id=class_id, text=text)]
    if ttype == "typed_link":
        return [TypedLinkRun(verb=_verb_string(token.get("verb")), text=str(token.get("text", "")))]
    if ttype == "math":
        return [MathRun(expression=str(token.get("expression", "")))]
    return []


def _page_items(token: Any, resolver: NameResolver | None) -> tuple[PageItem, ...]:
    """Render one top-level token to one or more page items.

    Only ``text`` tokens can fan out (one run per markdown-link segment);
    quotes recurse their children through the same splitter, block-scale
    tokens stay labeled placeholders, and unknown types render unsupported.
    """
    if not isinstance(token, Mapping):
        return (PlaceholderView(kind="unsupported"),)
    ttype = token.get("type")
    if ttype == "quote":
        children = token.get("children")
        inner: list[InlineView] = []
        if isinstance(children, list):
            for child in children:
                inner.extend(_inline_views(child, resolver))
        return (QuoteView(children=tuple(inner)),)
    if ttype in _PLACEHOLDER_TOKENS:
        return (PlaceholderView(kind=str(ttype)),)
    inline = _inline_views(token, resolver)
    if not inline:
        return (PlaceholderView(kind="unsupported"),)
    return tuple(inline)


# ------------------------------------------------------------------ public API


def ast_to_view(
    ast_json: str | Sequence[Any] | None,
    resolve_name: NameResolver | None = None,
) -> PageView:
    """Render stored content into an immutable :class:`PageView`.

    Args:
        ast_json: Raw ``content`` mirror (serialized token JSON or parsed
            list), ``None`` for empty content; legacy plaintext renders as a
            single text run.
        resolve_name: Optional store-backed lookup of a node id to its display
            name, used for mention pills and class chips.
    """
    tokens = parse_content_ast(ast_json)
    items = tuple(item for token in tokens for item in _page_items(token, resolve_name))
    return PageView(items=items)


def ast_to_plaintext(
    ast_json: str | Sequence[Any] | None,
    resolve_name: NameResolver | None = None,
) -> str:
    """Flatten content to plain text (editor seed; sidebar names).

    This is the excerpt derivation (``plainTextExcerpt``): text and
    typed-link runs contribute their text, mentions their ``displayText``
    (captured text when absent), math its expression, quotes recurse, and
    ``hard_break`` becomes a space; whitespace collapses to single spaces.
    ``resolve_name`` is accepted for call-site compatibility and unused —
    plaintext is a derivation of stored content, not of current names.
    """
    return plaintext_excerpt(parse_content_ast(ast_json))
