"""Tests for the pure AST renderer used by the GTK UI (``notees_gtk.ui.ast_render``).

Covers the CRDT wrapper unwrap port, block/inline mapping to view records,
``node_link`` resolution order (resolver → label → target UUID, never "…"),
plain-text seeding for the editor, and the paragraph rebuild used on save.
"""

from __future__ import annotations

import json

import pytest

from notees_gtk.ui import ast_render
from notees_gtk.ui.ast_render import (
    CodeView,
    HeadingView,
    MathView,
    PageView,
    ParagraphView,
    PlaceholderView,
    TextRun,
    TodoView,
)

INNER_DOC = [{"type": "paragraph", "children": [{"type": "text", "text": "Hello"}]}]
INNER_JSON = json.dumps(INNER_DOC)


def doc_json(doc: object) -> str:
    """Serialize a document the way the derived ``content`` column stores it."""
    return json.dumps(doc, ensure_ascii=False)


# --------------------------------------------------------------------- unwrap


def test_unwrap_bare_text_wrapper() -> None:
    wrapped = [{"type": "text", "text": INNER_JSON}]
    assert ast_render.unwrap(wrapped) == INNER_DOC


def test_unwrap_paragraph_wrapped() -> None:
    wrapped = [{"type": "paragraph", "children": [{"type": "text", "text": INNER_JSON}]}]
    assert ast_render.unwrap(wrapped) == INNER_DOC


def test_unwrap_already_unwrapped_multi_block() -> None:
    doc = [
        {"type": "paragraph", "children": [{"type": "text", "text": "A"}]},
        {"type": "heading", "level": 1, "children": [{"type": "text", "text": "B"}]},
    ]
    assert ast_render.unwrap(doc) == doc


def test_unwrap_single_block_with_two_children_is_not_a_wrapper() -> None:
    doc = [
        {
            "type": "paragraph",
            "children": [
                {"type": "text", "text": INNER_JSON},
                {"type": "text", "text": " more"},
            ],
        }
    ]
    assert ast_render.unwrap(doc) == doc


def test_unwrap_single_non_text_block_is_not_a_wrapper() -> None:
    doc = [{"type": "code", "text": INNER_JSON}]
    assert ast_render.unwrap(doc) == doc


def test_unwrap_garbage_inner_text_passthrough() -> None:
    doc = [{"type": "text", "text": "this is not json"}]
    assert ast_render.unwrap(doc) == doc


def test_unwrap_inner_json_not_a_document_passthrough() -> None:
    for inner in ("42", '{"type": "paragraph"}', "[1, 2, 3]", json.dumps([{"text": "no type"}])):
        doc = [{"type": "text", "text": inner}]
        assert ast_render.unwrap(doc) == doc, inner


def test_unwrap_inner_empty_document_passthrough() -> None:
    doc = [{"type": "text", "text": "[]"}]
    assert ast_render.unwrap(doc) == doc


# ---------------------------------------------------------------- ast_to_view


def test_view_none_is_empty() -> None:
    assert ast_render.ast_to_view(None) == PageView(blocks=())


def test_view_empty_string_is_empty() -> None:
    assert ast_render.ast_to_view("") == PageView(blocks=())


def test_view_plaintext_string_becomes_paragraph() -> None:
    """Non-JSON content (legacy plaintext) renders as a single paragraph."""
    view = ast_render.ast_to_view("just some words")
    assert view == PageView(blocks=(ParagraphView(runs=(TextRun(text="just some words"),)),))


def test_view_wrapped_string_content_is_unwrapped() -> None:
    wrapped = doc_json([{"type": "paragraph", "children": [{"type": "text", "text": INNER_JSON}]}])
    view = ast_render.ast_to_view(wrapped)
    assert view == PageView(blocks=(ParagraphView(runs=(TextRun(text="Hello"),)),))


def test_view_heading() -> None:
    view = ast_render.ast_to_view(doc_json([{"type": "heading", "level": 2, "children": [{"type": "text", "text": "Title"}]}]))
    assert view == PageView(
        blocks=(HeadingView(level=2, runs=(TextRun(text="Title"),)),)
    )


def test_view_todo_checked_and_unchecked() -> None:
    doc = [
        {"type": "todo", "checked": True, "children": [{"type": "text", "text": "done"}]},
        {"type": "todo", "checked": False, "children": [{"type": "text", "text": "todo"}]},
    ]
    view = ast_render.ast_to_view(doc)
    assert view.blocks == (
        TodoView(checked=True, runs=(TextRun(text="done"),)),
        TodoView(checked=False, runs=(TextRun(text="todo"),)),
    )


def test_view_code_block() -> None:
    view = ast_render.ast_to_view(doc_json([{"type": "code", "text": "print('hi')"}]))
    assert view == PageView(blocks=(CodeView(text="print('hi')"),))


def test_view_math_block() -> None:
    view = ast_render.ast_to_view(doc_json([{"type": "math", "expression": "e^{i\\pi}"}]))
    assert view == PageView(blocks=(MathView(latex="e^{i\\pi}"),))


def test_view_whiteboard_and_query_are_placeholders() -> None:
    doc = [{"type": "whiteboard", "data": {}}, {"type": "query", "data": {}}]
    view = ast_render.ast_to_view(doc)
    assert view.blocks == (PlaceholderView(kind="whiteboard"), PlaceholderView(kind="query"))


def test_view_unknown_block_is_unsupported_placeholder() -> None:
    view = ast_render.ast_to_view(doc_json([{"type": "spreadsheet", "data": {}}]))
    assert view.blocks == (PlaceholderView(kind="unsupported"),)


def test_view_marks_accumulate_through_nesting() -> None:
    doc = [
        {
            "type": "paragraph",
            "children": [
                {
                    "type": "strong",
                    "children": [
                        {"type": "text", "text": "bold"},
                        {"type": "em", "children": [{"type": "text", "text": " both"}]},
                        {"type": "code", "text": "mono"},
                        {"type": "strikethrough", "children": [{"type": "text", "text": "gone"}]},
                        {"type": "highlight", "children": [{"type": "text", "text": "mark"}]},
                    ],
                }
            ],
        }
    ]
    (block,) = ast_render.ast_to_view(doc).blocks
    assert isinstance(block, ParagraphView)
    runs = {run.text: run.marks for run in block.runs}
    assert runs["bold"] == frozenset({"strong"})
    assert runs[" both"] == frozenset({"strong", "em"})
    assert runs["mono"] == frozenset({"strong", "code"})
    assert runs["gone"] == frozenset({"strong", "strikethrough"})
    assert runs["mark"] == frozenset({"strong", "highlight"})


def test_view_external_link_flattens_children() -> None:
    doc = [
        {
            "type": "paragraph",
            "children": [
                {"type": "external_link", "url": "https://example.com", "children": [{"type": "text", "text": "site"}]},
            ],
        }
    ]
    (block,) = ast_render.ast_to_view(doc).blocks
    assert isinstance(block, ParagraphView)
    assert block.runs == (TextRun(text="site"),)


def test_view_hard_break_is_newline_run() -> None:
    doc = [
        {
            "type": "paragraph",
            "children": [
                {"type": "text", "text": "a"},
                {"type": "hard_break"},
                {"type": "text", "text": "b"},
            ],
        }
    ]
    (block,) = ast_render.ast_to_view(doc).blocks
    assert isinstance(block, ParagraphView)
    assert [run.text for run in block.runs] == ["a", "\n", "b"]


# ------------------------------------------------------------- node_link pills


def test_node_link_resolved_via_store_lookup() -> None:
    doc = [{"type": "paragraph", "children": [{"type": "node_link", "link_id": "target-uuid"}]}]
    (block,) = ast_render.ast_to_view(doc, resolve_name=lambda target: f"name({target})").blocks
    assert isinstance(block, ParagraphView)
    (run,) = block.runs
    assert run.text == "name(target-uuid)"
    assert run.node_link is not None
    assert run.node_link.target_id == "target-uuid"


def test_node_link_falls_back_to_label() -> None:
    doc = [{"type": "paragraph", "children": [{"type": "node_link", "link_id": "target-uuid", "label": "My Label"}]}]
    view = ast_render.ast_to_view(doc, resolve_name=lambda _target: "")
    (block,) = view.blocks
    assert isinstance(block, ParagraphView)
    assert block.runs[0].text == "My Label"


def test_node_link_falls_back_to_target_uuid() -> None:
    doc = [{"type": "paragraph", "children": [{"type": "node_link", "link_id": "target-uuid"}]}]
    view = ast_render.ast_to_view(doc)  # no resolver at all
    (block,) = view.blocks
    assert isinstance(block, ParagraphView)
    assert block.runs[0].text == "target-uuid"


def test_node_link_link_id_splits_recovery_target() -> None:
    """``link_id`` is ``targetUuid:linkUuid``; the first segment is recovery metadata."""
    doc = [{"type": "paragraph", "children": [{"type": "node_link", "link_id": "target-uuid:link-uuid", "label": "L"}]}]
    view = ast_render.ast_to_view(doc, resolve_name=lambda target: f"name({target})")
    (block,) = view.blocks
    assert isinstance(block, ParagraphView)
    (run,) = block.runs
    assert run.text == "name(target-uuid)"
    assert run.node_link is not None
    assert run.node_link.target_id == "target-uuid"
    assert run.node_link.label == "L"


def test_node_link_resolver_error_still_renders() -> None:
    def boom(_target: str) -> str:
        raise RuntimeError("store exploded")

    doc = [{"type": "paragraph", "children": [{"type": "node_link", "link_id": "target-uuid", "label": "L"}]}]
    (block,) = ast_render.ast_to_view(doc, resolve_name=boom).blocks
    assert isinstance(block, ParagraphView)
    assert block.runs[0].text == "L"


def test_node_link_broken_link_behaves_like_node_link() -> None:
    doc = [{"type": "paragraph", "children": [{"type": "broken_link", "link_id": "dead-uuid", "label": "Ghost"}]}]
    (block,) = ast_render.ast_to_view(doc).blocks
    assert isinstance(block, ParagraphView)
    assert block.runs[0].text == "Ghost"


# ------------------------------------------------------------ ast_to_plaintext


def test_plaintext_joins_blocks_with_newlines() -> None:
    doc = [
        {"type": "paragraph", "children": [{"type": "text", "text": "one"}]},
        {"type": "paragraph", "children": [{"type": "text", "text": "two"}]},
    ]
    assert ast_render.ast_to_plaintext(doc_json(doc)) == "one\ntwo"


def test_plaintext_todos() -> None:
    doc = [
        {"type": "todo", "checked": True, "children": [{"type": "text", "text": "done"}]},
        {"type": "todo", "checked": False, "children": [{"type": "text", "text": "later"}]},
    ]
    assert ast_render.ast_to_plaintext(doc) == "- [x] done\n- [ ] later"


def test_plaintext_headings() -> None:
    doc = [
        {"type": "heading", "level": 1, "children": [{"type": "text", "text": "H1"}]},
        {"type": "heading", "level": 3, "children": [{"type": "text", "text": "H3"}]},
    ]
    assert ast_render.ast_to_plaintext(doc) == "# H1\n### H3"


def test_plaintext_code_and_math_are_literal() -> None:
    doc = [{"type": "code", "text": "x = 1"}, {"type": "math", "expression": "a^2"}]
    assert ast_render.ast_to_plaintext(doc) == "x = 1\na^2"


def test_plaintext_unwraps_first() -> None:
    wrapped = doc_json([{"type": "text", "text": INNER_JSON}])
    assert ast_render.ast_to_plaintext(wrapped) == "Hello"


def test_plaintext_placeholders_are_bracketed() -> None:
    doc = [{"type": "whiteboard", "data": {}}, {"type": "query", "data": {}}]
    assert ast_render.ast_to_plaintext(doc) == "[whiteboard]\n[query]"


def test_plaintext_none_and_garbage() -> None:
    assert ast_render.ast_to_plaintext(None) == ""
    assert ast_render.ast_to_plaintext("not json") == "not json"


def test_plaintext_node_link_falls_back_without_resolver() -> None:
    doc = [{"type": "paragraph", "children": [{"type": "node_link", "link_id": "target-uuid", "label": "L"}]}]
    assert ast_render.ast_to_plaintext(doc) == "L"


# ------------------------------------------------- editor save (paragraph AST)


def test_paragraphs_from_plaintext() -> None:
    ast = ast_render.paragraphs_from_plaintext("one\ntwo\n\nthree")
    assert ast == [
        {"type": "paragraph", "children": [{"type": "text", "text": "one"}]},
        {"type": "paragraph", "children": [{"type": "text", "text": "two"}]},
        {"type": "paragraph", "children": [{"type": "text", "text": ""}]},
        {"type": "paragraph", "children": [{"type": "text", "text": "three"}]},
    ]


def test_paragraphs_from_plaintext_empty_is_single_empty_paragraph() -> None:
    assert ast_render.paragraphs_from_plaintext("") == [
        {"type": "paragraph", "children": [{"type": "text", "text": ""}]}
    ]


# ------------------------------------------------------------------ UI smoke


def test_ui_ast_render_imports_on_gtk_host() -> None:
    """Smoke test: the ``ui`` package must import on a machine with PyGObject.

    Skipped on headless boxes without GTK; kept here so ``uv run pytest`` on a
    GTK host (Arch + libadwaita) exercises the import path at least once.
    """
    pytest.importorskip("gi")
    from notees_gtk.ui import ast_render as _ui_ast_render  # noqa: F401
