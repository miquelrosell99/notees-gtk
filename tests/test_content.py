"""Tests for the content-grammar helpers (title-is-content lockstep).

Ports of the v2 ``packages/domain/src/node.ts`` helpers the store and the
sidebar now share: ``stringifyContentAst`` (pages/classes carry text-only
content), ``deriveDisplayName`` (the display name IS the content excerpt,
capped at 80 chars) and ``formatDateNodeName`` (YYYYMMDD shapes render as
YYYY/MM(/DD)).
"""

from __future__ import annotations

from notees_gtk.core.protocol.content import (
    DISPLAY_NAME_MAX,
    derive_display_name,
    format_date_node_name,
    parse_content_ast,
    plaintext_excerpt,
    stringify_content_ast,
)


class TestStringifyContentAst:
    def test_rich_tokens_fold_into_one_text_token(self) -> None:
        rich = [
            {"type": "text", "text": "Kuhn "},
            {"type": "typed_link", "verb": "cites", "text": "cites"},
            {"type": "mention", "text": "[[bob]]", "displayText": "Bob"},
        ]
        assert stringify_content_ast(rich) == [{"type": "text", "text": "Kuhn cites Bob"}]

    def test_structural_widgets_survive(self) -> None:
        rich = [{"type": "whiteboard", "id": "wb-1"}, {"type": "text", "text": "notes"}]
        assert stringify_content_ast(rich) == [
            {"type": "text", "text": "notes"},
            {"type": "whiteboard", "id": "wb-1"},
        ]

    def test_empty_and_none(self) -> None:
        assert stringify_content_ast(None) == []
        assert stringify_content_ast([]) == []


class TestDeriveDisplayName:
    def test_excerpt_for_every_node_type(self) -> None:
        assert derive_display_name([{"type": "text", "text": "My Page"}]) == "My Page"
        assert (
            derive_display_name([{"type": "text", "text": "A"}, {"type": "hard_break"}, {"type": "text", "text": "B"}])
            == "A B"
        )

    def test_empty_content_is_untitled_by_the_caller(self) -> None:
        assert derive_display_name([]) == ""
        assert derive_display_name(None) == ""

    def test_capped_at_display_name_max(self) -> None:
        long_text = "x" * (DISPLAY_NAME_MAX + 20)
        assert len(derive_display_name([{"type": "text", "text": long_text}])) == DISPLAY_NAME_MAX

    def test_date_labels_format_per_shape(self) -> None:
        assert derive_display_name([{"type": "text", "text": "20290000"}]) == "2029"
        assert derive_display_name([{"type": "text", "text": "20290600"}]) == "2029/06"
        assert derive_display_name([{"type": "text", "text": "20290627"}]) == "2029/06/27"

    def test_class_ids_argument_is_accepted(self) -> None:
        # The class check is deliberately NOT required (migrated date pages may
        # lack the day/month/year classes).
        assert derive_display_name([{"type": "text", "text": "20290627"}], ["some-class"]) == "2029/06/27"


class TestFormatDateNodeName:
    def test_non_date_shapes_return_none(self) -> None:
        assert format_date_node_name("hello") is None
        assert format_date_node_name("2029062") is None
        assert format_date_node_name("202906271") is None

    def test_digits_are_extracted(self) -> None:
        # The web port strips non-digits before matching the 8-digit shape.
        assert format_date_node_name("2029/06/27") == "2029/06/27"


class TestPlaintextExcerptParity:
    def test_quote_recursion_and_breaks(self) -> None:
        tokens = parse_content_ast(
            '[{"type":"text","text":"a"},{"type":"quote","children":[{"type":"text","text":"b"}]},'
            '{"type":"hard_break"},{"type":"math","expression":"x^2"}]'
        )
        assert plaintext_excerpt(tokens) == "a b x^2"
