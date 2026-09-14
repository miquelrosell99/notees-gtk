"""Pure AST → view-record renderer for the GTK UI.

Headless-testable by design: no GTK imports here (the UI smoke test in
``tests/test_ast_render.py`` guards the ``gi`` import separately). Mirrors the
frontend contract:

- always unwrap the CRDT storage wrapper before parsing
  (port of ``unwrapCrdtContentAst``, ``frontend/src/lib/astBuilder.ts:561``);
- a ``node_link`` resolves via store lookup, falls back to the link label,
  then to the target UUID from ``link_id`` — never an "…" placeholder
  (``skills/notees/rules/coding-standards.md``).

Block shapes follow ``frontend/src/types/ast.ts`` (paragraph, heading,
whiteboard, query) plus the block-level ``todo``/``code``/``math`` forms the
GTK client renders as first-class views.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "BlockView",
    "CodeView",
    "HeadingView",
    "MathView",
    "NodeLinkRef",
    "PageView",
    "ParagraphView",
    "PlaceholderView",
    "TextRun",
    "TodoView",
    "ast_to_plaintext",
    "ast_to_view",
    "paragraphs_from_plaintext",
    "unwrap",
]

#: Mark names understood by the renderer (``ast.ts`` mark nodes plus the
#: inline ``code`` leaf, which is rendered as a marked run).
_MARK_NAMES: frozenset[str] = frozenset({"strong", "em", "strikethrough", "highlight", "underline"})

#: Placeholder kinds emitted for blocks the plain-text MVP cannot render.
_PLACEHOLDER_WHITEBOARD = "whiteboard"
_PLACEHOLDER_QUERY = "query"
_PLACEHOLDER_UNSUPPORTED = "unsupported"

#: Node-name resolver supplied by the UI layer: maps a target node id to its
#: display name. A falsy return means "unresolvable" and triggers the
#: label → target-UUID fallback.
type NameResolver = Callable[[str], str | None]


@dataclass(frozen=True)
class NodeLinkRef:
    """Resolved display target of a ``node_link`` inline.

    Attributes:
        target_id: Target node UUID (first segment of the AST ``link_id``,
            which has the form ``targetUuid:linkUuid``).
        label: Custom label stored inline in the AST, if any.
    """

    target_id: str
    label: str | None = None


@dataclass(frozen=True)
class TextRun:
    """One inline run of a paragraph/heading/todo view.

    Attributes:
        text: Plain display text (already resolved for node links).
        marks: Active mark names (strong/em/strikethrough/highlight/code).
        node_link: Present when this run is a node-link pill.
    """

    text: str
    marks: frozenset[str] = frozenset()
    node_link: NodeLinkRef | None = None


@dataclass(frozen=True)
class HeadingView:
    """Heading block; ``level`` is clamped to 1–6."""

    level: int
    runs: tuple[TextRun, ...] = ()


@dataclass(frozen=True)
class ParagraphView:
    """Paragraph block as inline runs."""

    runs: tuple[TextRun, ...] = ()


@dataclass(frozen=True)
class TodoView:
    """Task block with a checked flag."""

    checked: bool
    runs: tuple[TextRun, ...] = ()


@dataclass(frozen=True)
class CodeView:
    """Literal code block."""

    text: str


@dataclass(frozen=True)
class MathView:
    """Block math; ``latex`` is the source without delimiters."""

    latex: str


@dataclass(frozen=True)
class PlaceholderView:
    """Non-text block the MVP cannot render (whiteboard/query/unknown)."""

    kind: str


@dataclass(frozen=True)
class PageView:
    """Immutable render-ready projection of one node's content AST."""

    blocks: tuple[BlockView, ...] = ()


type BlockView = HeadingView | ParagraphView | TodoView | CodeView | MathView | PlaceholderView


# --------------------------------------------------------------------- unwrap


def _is_text_node(node: Any) -> bool:
    """Port of ``isTextNode``: an object ``{type: 'text', text: str}``."""
    return (
        isinstance(node, Mapping)
        and node.get("type") == "text"
        and isinstance(node.get("text"), str)
    )


def _try_parse_document_json(text: str) -> list[Any] | None:
    """Parse a JSON document, requiring a non-empty array of typed objects."""
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if not isinstance(parsed, list):
        return None
    if any(not isinstance(item, Mapping) or "type" not in item for item in parsed):
        return None
    return parsed


def unwrap(ast: Sequence[Any]) -> list[Any]:
    """Undo the CRDT text-update storage wrapper (port of ``unwrapCrdtContentAst``).

    The inline editor serializes the real AST to JSON and stores that string
    inside the node's text CRDT, so the mirrored ``content`` column can be
    ``[{type: 'text', text: '[<real AST>]'}]`` (or the paragraph-wrapped
    equivalent). Returns the inner AST when the wrapper is detected;
    otherwise returns the input unchanged.
    """
    if len(ast) != 1:
        return list(ast)
    block = ast[0]
    wrapped_text: str | None = None
    if _is_text_node(block):
        wrapped_text = block["text"]
    elif (
        isinstance(block, Mapping)
        and block.get("type") == "paragraph"
        and isinstance(block.get("children"), list)
        and len(block["children"]) == 1
        and _is_text_node(block["children"][0])
    ):
        wrapped_text = block["children"][0]["text"]
    if wrapped_text is None:
        return list(ast)
    inner = _try_parse_document_json(wrapped_text)
    if inner:
        return inner
    return list(ast)


# --------------------------------------------------------------------- parsing


def _parse_document(ast_json: str | Sequence[Any] | None) -> list[Any]:
    """Normalize stored content (JSON string or parsed list) to a raw AST list.

    Legacy plaintext content that is not a JSON array of typed blocks is
    rendered as a single paragraph carrying the raw string (mirroring
    ``nodeNameToText``'s fallback, which avoids treating JSON objects/arrays
    as display text).
    """
    if ast_json is None:
        return []
    if isinstance(ast_json, str):
        text = ast_json.strip()
        if not text:
            return []
        try:
            parsed = json.loads(ast_json)
        except ValueError:
            return [_plaintext_block(ast_json)]
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, (int, float, bool)):
            return [_plaintext_block(ast_json)]
        return [_plaintext_block(ast_json)] if not text.startswith(("{", "[")) else []
    if isinstance(ast_json, Sequence):
        return list(ast_json)
    return []


def _plaintext_block(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "children": [{"type": "text", "text": text}]}


# --------------------------------------------------------------------- inlines


def _resolve_node_link_text(target_id: str, label: str | None, resolve_name: NameResolver | None) -> str:
    """Apply the display fallback chain: store lookup → label → target UUID."""
    if target_id and resolve_name is not None:
        try:
            resolved = resolve_name(target_id)
        except Exception:  # noqa: BLE001 — a failing store lookup must never break rendering
            resolved = None
        if resolved:
            return resolved
    if label:
        return label
    return target_id


def _node_link_run(node: Mapping[str, Any], marks: frozenset[str], resolve_name: NameResolver | None) -> TextRun:
    link_id = node.get("link_id")
    link_id = link_id if isinstance(link_id, str) else ""
    raw_label = node.get("label")
    label = raw_label if isinstance(raw_label, str) and raw_label else None
    # link_id has the form ``targetUuid:linkUuid``; the first segment is the
    # recovery target.
    target_id = link_id.split(":", 1)[0] if ":" in link_id else link_id
    text = _resolve_node_link_text(target_id, label, resolve_name)
    return TextRun(text=text, marks=marks, node_link=NodeLinkRef(target_id=target_id, label=label))


def _inline_runs(nodes: Any, marks: frozenset[str], resolve_name: NameResolver | None) -> list[TextRun]:
    """Map an inline-node list to runs, accumulating marks through nesting."""
    if not isinstance(nodes, list):
        return []
    runs: list[TextRun] = []
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        ntype = node.get("type")
        if ntype == "text":
            runs.append(TextRun(text=str(node.get("text", "")), marks=marks))
        elif ntype == "hard_break":
            runs.append(TextRun(text="\n", marks=marks))
        elif ntype == "code":
            runs.append(TextRun(text=str(node.get("text", "")), marks=marks | {"code"}))
        elif ntype == "math":
            runs.append(TextRun(text=str(node.get("expression", "")), marks=marks))
        elif ntype in ("node_link", "broken_link"):
            runs.append(_node_link_run(node, marks, resolve_name))
        elif ntype == "date_range":
            raw_label = node.get("label")
            label = raw_label if isinstance(raw_label, str) and raw_label else None
            text = label or f"{node.get('start', '')} – {node.get('end', '')}"
            runs.append(TextRun(text=text, marks=marks))
        elif ntype == "external_link":
            children = node.get("children")
            if isinstance(children, list):
                runs.extend(_inline_runs(children, marks, resolve_name))
            else:
                runs.append(TextRun(text=str(node.get("url", "")), marks=marks))
        elif ntype in _MARK_NAMES:
            runs.extend(_inline_runs(node.get("children"), marks | {str(ntype)}, resolve_name))
        else:
            # Unknown inline: keep any literal text so nothing is dropped.
            leftover = node.get("text")
            if leftover is not None:
                runs.append(TextRun(text=str(leftover), marks=marks))
    return runs


# ---------------------------------------------------------------------- blocks


def _children(block: Mapping[str, Any]) -> Any:
    return block.get("children")


def _heading_level(raw: Any) -> int:
    try:
        level = int(raw)
    except (TypeError, ValueError):
        return 1
    return min(max(level, 1), 6)


def _block_view(block: Any, resolve_name: NameResolver | None) -> BlockView:
    if not isinstance(block, Mapping):
        return PlaceholderView(kind=_PLACEHOLDER_UNSUPPORTED)
    btype = block.get("type")
    if btype == "paragraph":
        return ParagraphView(runs=tuple(_inline_runs(_children(block), frozenset(), resolve_name)))
    if btype == "heading":
        return HeadingView(
            level=_heading_level(block.get("level")),
            runs=tuple(_inline_runs(_children(block), frozenset(), resolve_name)),
        )
    if btype == "todo":
        return TodoView(
            checked=bool(block.get("checked")),
            runs=tuple(_inline_runs(_children(block), frozenset(), resolve_name)),
        )
    if btype == "code":
        return CodeView(text=str(block.get("text", "")))
    if btype == "math":
        return MathView(latex=str(block.get("expression", "")))
    if btype == "whiteboard":
        return PlaceholderView(kind=_PLACEHOLDER_WHITEBOARD)
    if btype == "query":
        return PlaceholderView(kind=_PLACEHOLDER_QUERY)
    return PlaceholderView(kind=_PLACEHOLDER_UNSUPPORTED)


# ------------------------------------------------------------------ public API


def ast_to_view(
    ast_json: str | list[Any] | None,
    resolve_name: NameResolver | None = None,
) -> PageView:
    """Render stored content into an immutable :class:`PageView`.

    Args:
        ast_json: Raw ``content`` mirror (JSON string or parsed list), ``None``
            for empty content.
        resolve_name: Optional store-backed lookup of a target node id to its
            display name, used for node-link pills.
    """
    doc = unwrap(_parse_document(ast_json))
    blocks = tuple(_block_view(block, resolve_name) for block in doc)
    return PageView(blocks=blocks)


def ast_to_plaintext(
    ast_json: str | list[Any] | None,
    resolve_name: NameResolver | None = None,
) -> str:
    """Flatten content to plain text (editor seed; sidebar names).

    Blocks are joined with newlines; todos become ``- [x] ``/``- [ ] `` and
    headings ``"#" * level + " "`` prefixes. Non-text blocks render as
    bracketed placeholders (``[whiteboard]``) so nothing silently disappears.
    """
    doc = unwrap(_parse_document(ast_json))
    lines: list[str] = []
    for block in doc:
        if not isinstance(block, Mapping):
            continue
        btype = block.get("type")
        if btype == "paragraph":
            lines.append("".join(run.text for run in _inline_runs(_children(block), frozenset(), resolve_name)))
        elif btype == "heading":
            prefix = "#" * _heading_level(block.get("level")) + " "
            text = "".join(run.text for run in _inline_runs(_children(block), frozenset(), resolve_name))
            lines.append(prefix + text)
        elif btype == "todo":
            prefix = "- [x] " if block.get("checked") else "- [ ] "
            text = "".join(run.text for run in _inline_runs(_children(block), frozenset(), resolve_name))
            lines.append(prefix + text)
        elif btype == "code":
            lines.append(str(block.get("text", "")))
        elif btype == "math":
            lines.append(str(block.get("expression", "")))
        elif btype == "whiteboard":
            lines.append("[whiteboard]")
        elif btype == "query":
            lines.append("[query]")
    return "\n".join(lines)


def paragraphs_from_plaintext(text: str) -> list[dict[str, Any]]:
    """Rebuild the paragraph-per-line AST stored on editor save.

    This is the honest MVP round-trip: the plain-text editor form cannot
    express marks or node links, so every line becomes one paragraph with a
    single text child (mirroring the Flutter client's non-CRDT form).
    """
    lines = text.split("\n")
    return [{"type": "paragraph", "children": [{"type": "text", "text": line}]} for line in lines]
