"""Tests for the token-stream renderer (``notees_gtk.ui.ast_render``).

Covers the SCHEMA.md Content grammar mapping to view records — text runs
with marks, hard_break, mention (displayText → resolved name → raw id),
class_chip, typed_link (underlined verb mark), external_link, math, quote
(the only nested token), block tokens as labeled placeholders — plus the
plaintext excerpt derivation and the editor's plaintext→token mapping.
"""

from __future__ import annotations

import json

import pytest

from notees_gtk.core.protocol.content import tokens_from_plaintext
from notees_gtk.ui import ast_render
from notees_gtk.ui.ast_render import (
    ClassChipRun,
    ExternalLinkRun,
    HardBreakRun,
    MathRun,
    MentionRun,
    PageView,
    PlaceholderView,
    QuoteView,
    TextRun,
    TypedLinkRun,
)


def doc_json(doc: object) -> str:
    """Serialize a token array the way the derived ``content`` column stores it."""
    return json.dumps(doc, ensure_ascii=False)


# ---------------------------------------------------------------- ast_to_view


def test_view_none_and_empty_are_empty() -> None:
    assert ast_render.ast_to_view(None) == PageView(items=())
    assert ast_render.ast_to_view("") == PageView(items=())


def test_view_legacy_plaintext_becomes_text_run() -> None:
    """Non-JSON content (legacy plaintext) renders as a single text run."""
    view = ast_render.ast_to_view("just some words")
    assert view == PageView(items=(TextRun(text="just some words"),))


def test_view_text_run_with_v2_marks() -> None:
    doc = [{"type": "text", "text": "bold and code", "marks": ["bold", "code"]}]
    view = ast_render.ast_to_view(doc_json(doc))
    assert view == PageView(items=(TextRun(text="bold and code", marks=frozenset({"bold", "code"})),))


def test_view_ignores_unknown_marks() -> None:
    doc = [{"type": "text", "text": "x", "marks": ["blink", "bold"]}]
    view = ast_render.ast_to_view(doc_json(doc))
    assert view == PageView(items=(TextRun(text="x", marks=frozenset({"bold"})),))


def test_view_hard_break_is_a_line_marker() -> None:
    doc = [{"type": "text", "text": "a"}, {"type": "hard_break"}, {"type": "text", "text": "b"}]
    view = ast_render.ast_to_view(doc_json(doc))
    assert view == PageView(items=(TextRun(text="a"), HardBreakRun(), TextRun(text="b")))


def test_view_typed_link_is_underlined_mark() -> None:
    doc = [{"type": "text", "text": "Kuhn "}, {"type": "typed_link", "verb": "cites", "text": "cites"}]
    view = ast_render.ast_to_view(doc_json(doc))
    assert view == PageView(items=(TextRun(text="Kuhn "), TypedLinkRun(verb="cites", text="cites")))


def test_view_typed_link_verb_may_bind_a_property_schema() -> None:
    doc = [{"type": "typed_link", "verb": {"propertySchemaId": "sch-1"}, "text": "authored"}]
    view = ast_render.ast_to_view(doc_json(doc))
    assert view == PageView(items=(TypedLinkRun(verb="sch-1", text="authored"),))


def test_view_mention_display_text_wins() -> None:
    doc = [{"type": "mention", "targetNodeId": "target-1", "text": "captured", "displayText": "the Republic"}]
    view = ast_render.ast_to_view(doc_json(doc))
    assert view == PageView(items=(MentionRun(target_id="target-1", text="the Republic"),))


def test_view_mention_resolves_current_name() -> None:
    doc = [{"type": "mention", "targetNodeId": "target-1", "text": "captured"}]
    view = ast_render.ast_to_view(doc_json(doc), resolve_name=lambda target: f"name({target})")
    assert view == PageView(items=(MentionRun(target_id="target-1", text="name(target-1)"),))


def test_view_mention_falls_back_to_raw_id_not_captured_text() -> None:
    """Fork 4: broken targets render the raw id; captured text is non-authoritative."""
    doc = [{"type": "mention", "targetNodeId": "dead-1", "text": "captured"}]
    view = ast_render.ast_to_view(doc_json(doc))  # no resolver at all
    assert view == PageView(items=(MentionRun(target_id="dead-1", text="dead-1"),))


def test_view_mention_resolver_error_still_renders() -> None:
    def boom(_target: str) -> str:
        raise RuntimeError("store exploded")

    doc = [{"type": "mention", "targetNodeId": "t-1", "text": "captured"}]
    view = ast_render.ast_to_view(doc_json(doc), resolve_name=boom)
    assert view == PageView(items=(MentionRun(target_id="t-1", text="t-1"),))


def test_view_class_chip_resolves_class_name() -> None:
    doc = [{"type": "class_chip", "classId": "class-1"}]
    view = ast_render.ast_to_view(doc_json(doc), resolve_name=lambda target: f"class({target})")
    assert view == PageView(items=(ClassChipRun(class_id="class-1", text="class(class-1)"),))


def test_view_class_chip_display_text_and_raw_id_fallback() -> None:
    doc = [{"type": "class_chip", "classId": "class-1", "displayText": "One-off"}]
    assert ast_render.ast_to_view(doc_json(doc)) == PageView(items=(ClassChipRun(class_id="class-1", text="One-off"),))
    plain = [{"type": "class_chip", "classId": "class-9"}]
    assert ast_render.ast_to_view(doc_json(plain)) == PageView(
        items=(ClassChipRun(class_id="class-9", text="class-9"),)
    )


def test_view_external_link() -> None:
    doc = [{"type": "external_link", "href": "https://example.com", "text": "site"}]
    view = ast_render.ast_to_view(doc_json(doc))
    assert view == PageView(items=(ExternalLinkRun(href="https://example.com", text="site"),))


def test_view_math() -> None:
    doc = [{"type": "math", "expression": "e^{i\\pi}"}]
    assert ast_render.ast_to_view(doc_json(doc)) == PageView(items=(MathRun(expression="e^{i\\pi}"),))


def test_view_quote_is_the_only_nested_token() -> None:
    doc = [
        {
            "type": "quote",
            "children": [
                {"type": "text", "text": "quoted "},
                {"type": "mention", "targetNodeId": "t-1", "text": "captured"},
                {"type": "typed_link", "verb": "cites", "text": "cites"},
            ],
        }
    ]
    view = ast_render.ast_to_view(doc_json(doc), resolve_name=lambda _t: "Resolved")
    assert view == PageView(
        items=(
            QuoteView(
                children=(
                    TextRun(text="quoted "),
                    MentionRun(target_id="t-1", text="Resolved"),
                    TypedLinkRun(verb="cites", text="cites"),
                )
            ),
        )
    )


@pytest.mark.parametrize(
    "token",
    [
        {"type": "asset_ref", "assetId": "a-1"},
        {"type": "embed_ref", "nodeId": "n-1"},
        {"type": "query", "queryAst": {}},
        {"type": "whiteboard", "layout": {}},
    ],
)
def test_view_block_tokens_are_labeled_placeholders(token: object) -> None:
    view = ast_render.ast_to_view(doc_json([token]))
    assert view == PageView(items=(PlaceholderView(kind=str(token["type"])),))


def test_view_unknown_token_is_unsupported_placeholder() -> None:
    view = ast_render.ast_to_view(doc_json([{"type": "spreadsheet", "data": {}}]))
    assert view == PageView(items=(PlaceholderView(kind="unsupported"),))


# ------------------------------------------------------------ ast_to_plaintext


def test_plaintext_is_the_v2_excerpt() -> None:
    doc = [
        {"type": "text", "text": "Kuhn "},
        {"type": "typed_link", "verb": "cites", "text": "cites"},
        {"type": "text", "text": " earlier work on "},
        {"type": "mention", "targetNodeId": "t-1", "text": "The Structure"},
        {"type": "hard_break"},
        {"type": "math", "expression": "a^2"},
        {"type": "quote", "children": [{"type": "text", "text": "nested quote"}]},
        {"type": "asset_ref", "assetId": "a-1"},  # block tokens are silent
    ]
    assert ast_render.ast_to_plaintext(doc_json(doc)) == "Kuhn cites earlier work on The Structure a^2 nested quote"


def test_plaintext_mention_prefers_display_text() -> None:
    doc = [{"type": "mention", "targetNodeId": "t-1", "text": "captured", "displayText": "the Republic"}]
    assert ast_render.ast_to_plaintext(doc_json(doc)) == "the Republic"


def test_plaintext_collapses_whitespace() -> None:
    doc = [{"type": "text", "text": "  a \n b  "}]
    assert ast_render.ast_to_plaintext(doc_json(doc)) == "a b"


def test_plaintext_none_and_legacy() -> None:
    assert ast_render.ast_to_plaintext(None) == ""
    assert ast_render.ast_to_plaintext("not json") == "not json"


# ------------------------------------------------- tokens_from_plaintext (editor)


def test_tokens_from_plaintext_maps_lines_with_hard_breaks() -> None:
    tokens = tokens_from_plaintext("one\ntwo\n\nthree")
    assert tokens == [
        {"type": "text", "text": "one"},
        {"type": "hard_break"},
        {"type": "text", "text": "two"},
        {"type": "hard_break"},
        {"type": "text", "text": ""},
        {"type": "hard_break"},
        {"type": "text", "text": "three"},
    ]


def test_tokens_from_plaintext_single_line() -> None:
    assert tokens_from_plaintext("solo") == [{"type": "text", "text": "solo"}]


def test_tokens_from_plaintext_empty_is_empty_stream() -> None:
    assert tokens_from_plaintext("") == []


def test_tokens_from_plaintext_round_trips_through_plaintext() -> None:
    text = "first line\nsecond line"
    tokens = tokens_from_plaintext(text)
    # The excerpt collapses the hard_break to a single space (the excerpt rules).
    assert ast_render.ast_to_plaintext(json.dumps(tokens)) == "first line second line"


# ------------------------------------------------------------------ UI smoke


def test_ui_ast_render_imports_on_gtk_host() -> None:
    """Smoke test: the ``ui`` package must import on a machine with PyGObject.

    Skipped on headless boxes without GTK; kept here so ``uv run pytest`` on a
    GTK host (Arch + libadwaita) exercises the import path at least once.
    """
    pytest.importorskip("gi")
    from notees_gtk.ui import ast_render as _ui_ast_render  # noqa: F401
