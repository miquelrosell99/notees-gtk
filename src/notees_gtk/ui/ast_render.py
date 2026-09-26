"""Pure token-stream → view-record renderer for the GTK UI (v2 content grammar).

Headless-testable by design: no GTK imports here (the UI smoke test in
``tests/test_ast_render.py`` guards the ``gi`` import separately).

SCHEMA.md's Content grammar is normative: a block node's content is ONE flat,
ordered token array — there are no block-level segments, and rendering defines
presentation. Resolution rules implemented here:

- ``mention``: ``displayText`` wins, then the resolver's current target name
  (auto-rename free), then the raw target id — never an "…" placeholder;
  the captured ``text`` is non-authoritative (Fork 4);
- ``class_chip``: render-only (Fork 3) — ``displayText`` ?? resolved class
  name ?? raw class id; inserting/deleting a chip never mutates ``class_ids``;
- ``typed_link``: a mark on the prose word (01-knowledge-model.md §9) — the
  marked word renders underlined, the verb is carried for tooltips/metadata;
- block-scale tokens (``asset_ref``/``embed_ref``/``query``/``whiteboard``)
  render as labeled placeholders until the GTK client grows real views.

Plaintext (editor seed, sidebar names) is the v2 excerpt derivation
(:func:`notees_gtk.core.protocol.content.plaintext_excerpt`) — derived, never
stored as truth.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from notees_gtk.core.protocol.content import parse_content_ast, plaintext_excerpt

__all__ = [
    "ClassChipRun",
    "ExternalLinkRun",
    "HardBreakRun",
    "InlineView",
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

#: v2 mark names (content-mark.ts MARKS): attributes on text runs, not nodes.
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
class ExternalLinkRun:
    href: str
    text: str


@dataclass(frozen=True)
class MathRun:
    expression: str


#: One inline-scale view record (everything that can sit in a rendered line).
type InlineView = TextRun | HardBreakRun | MentionRun | ClassChipRun | TypedLinkRun | ExternalLinkRun | MathRun


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


def _class_chip_text(token: Mapping[str, Any], resolver: NameResolver | None) -> tuple[str, str]:
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


def _inline_view(token: Any, resolver: NameResolver | None) -> InlineView | None:
    if not isinstance(token, Mapping):
        return None
    ttype = token.get("type")
    if ttype == "text":
        raw_marks = token.get("marks")
        marks = (
            frozenset(str(mark) for mark in raw_marks if str(mark) in _VALID_MARKS)
            if isinstance(raw_marks, list)
            else frozenset()
        )
        return TextRun(text=str(token.get("text", "")), marks=marks)
    if ttype == "hard_break":
        return HardBreakRun()
    if ttype == "mention":
        target_id, text = _mention_text(token, resolver)
        return MentionRun(target_id=target_id, text=text)
    if ttype == "class_chip":
        class_id, text = _class_chip_text(token, resolver)
        return ClassChipRun(class_id=class_id, text=text)
    if ttype == "typed_link":
        return TypedLinkRun(verb=_verb_string(token.get("verb")), text=str(token.get("text", "")))
    if ttype == "external_link":
        return ExternalLinkRun(href=str(token.get("href", "")), text=str(token.get("text", "")))
    if ttype == "math":
        return MathRun(expression=str(token.get("expression", "")))
    return None


def _page_item(token: Any, resolver: NameResolver | None) -> PageItem:
    if not isinstance(token, Mapping):
        return PlaceholderView(kind="unsupported")
    ttype = token.get("type")
    if ttype == "quote":
        children = token.get("children")
        inner: list[InlineView] = []
        if isinstance(children, list):
            for child in children:
                view = _inline_view(child, resolver)
                if view is not None:
                    inner.append(view)
        return QuoteView(children=tuple(inner))
    if ttype in _PLACEHOLDER_TOKENS:
        return PlaceholderView(kind=str(ttype))
    inline = _inline_view(token, resolver)
    if inline is not None:
        return inline
    return PlaceholderView(kind="unsupported")


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
    return PageView(items=tuple(_page_item(token, resolve_name) for token in tokens))


def ast_to_plaintext(
    ast_json: str | Sequence[Any] | None,
    resolve_name: NameResolver | None = None,
) -> str:
    """Flatten content to plain text (editor seed; sidebar names).

    This is the v2 excerpt derivation (``plainTextExcerpt``): text and
    typed-link runs contribute their text, mentions their ``displayText``
    (captured text when absent), math its expression, quotes recurse, and
    ``hard_break`` becomes a space; whitespace collapses to single spaces.
    ``resolve_name`` is accepted for call-site compatibility and unused —
    plaintext is a derivation of stored content, not of current names.
    """
    return plaintext_excerpt(parse_content_ast(ast_json))
