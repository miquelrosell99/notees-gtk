"""Tests for the content-grammar helpers (title-is-content lockstep).

Ports of the ``packages/domain/src/node.ts`` helpers the store and the
sidebar now share: ``stringifyContentAst`` (pages/classes carry text-only
content), ``deriveDisplayName`` (the display name IS the content excerpt,
capped at 80 chars) and ``formatDateNodeName`` (YYYYMMDD shapes render as
YYYY/MM(/DD)).
"""

from __future__ import annotations

import pytest

from notees_gtk.core.protocol.content import (
    DISPLAY_NAME_MAX,
    derive_display_name,
    format_date_node_name,
    parse_content_ast,
    plaintext_excerpt,
    stringify_content_ast,
    validate_content_ast,
    validate_content_token,
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


class TestCodeBlockAndHrGrammar:
    """``code_block`` is a promotion survivor (with whiteboard/
    query); ``hr`` is deliberately NOT — promotion stringifies it away."""

    def test_code_block_survives_stringification_with_surrounding_text_first(self) -> None:
        rich = [
            {"type": "text", "text": "before"},
            {"type": "code_block", "language": "python", "text": "print('hi')"},
            {"type": "text", "text": "after"},
        ]
        assert stringify_content_ast(rich) == [
            {"type": "text", "text": "before after"},
            {"type": "code_block", "language": "python", "text": "print('hi')"},
        ]

    def test_code_block_alone_survives_without_text(self) -> None:
        assert stringify_content_ast([{"type": "code_block", "text": "plain"}]) == [
            {"type": "code_block", "text": "plain"}
        ]

    def test_hr_is_not_a_promotion_survivor(self) -> None:
        rich = [{"type": "text", "text": "above"}, {"type": "hr"}, {"type": "text", "text": "below"}]
        assert stringify_content_ast(rich) == [{"type": "text", "text": "above below"}]
        assert stringify_content_ast([{"type": "hr"}]) == []

    def test_code_block_and_hr_are_silent_in_the_excerpt(self) -> None:
        tokens = [
            {"type": "text", "text": "notes"},
            {"type": "code_block", "text": "print('hi')"},
            {"type": "hr"},
        ]
        assert plaintext_excerpt(tokens) == "notes"


class TestStrictTokenValidation:
    """The strict grammar entries (contentTokenSchema parity): the
    code_block language tag, the hr shape, and the embed_ref.view enum."""

    @pytest.mark.parametrize(
        "token",
        [
            {"type": "code_block", "text": "x = 1"},
            {"type": "code_block", "language": "python", "text": "x = 1"},
            {"type": "code_block", "language": "c++", "text": "int main() {}"},
            {"type": "hr"},
            {"type": "embed_ref", "nodeId": "11111111-1111-7111-8111-111111111111"},
            {"type": "embed_ref", "nodeId": "11111111-1111-7111-8111-111111111111", "view": "embed"},
            {"type": "embed_ref", "nodeId": "11111111-1111-7111-8111-111111111111", "view": "small_card"},
            {"type": "embed_ref", "nodeId": "11111111-1111-7111-8111-111111111111", "view": "wide_card"},
        ],
    )
    def test_new_tokens_validate(self, token: dict) -> None:
        validate_content_token(token)

    @pytest.mark.parametrize(
        "token",
        [
            {"type": "code_block", "language": "Python", "text": "x"},  # uppercase hint
            {"type": "code_block", "language": "py thon", "text": "x"},
            {"type": "code_block", "text": "x", "extra": 1},  # strict keys
            {"type": "code_block"},  # text required
            {"type": "hr", "extra": True},
            {"type": "embed_ref", "nodeId": "11111111-1111-7111-8111-111111111111", "view": "banner"},
            {"type": "embed_ref", "nodeId": "not-a-uuid"},
            {"type": "unknown_kind"},
        ],
    )
    def test_bad_shapes_fail_loud(self, token: dict) -> None:
        with pytest.raises(ValueError):
            validate_content_token(token)

    def test_quote_children_stay_inline_only(self) -> None:
        with pytest.raises(ValueError):
            validate_content_token({"type": "quote", "children": [{"type": "hr"}]})
        with pytest.raises(ValueError):
            validate_content_token({"type": "quote", "children": [{"type": "code_block", "text": "x"}]})
        validate_content_token({"type": "quote", "children": [{"type": "text", "text": "quoted"}]})

    def test_validate_content_ast_walks_the_stream(self) -> None:
        validate_content_ast(
            [
                {"type": "text", "text": "See "},
                {"type": "embed_ref", "nodeId": "11111111-1111-7111-8111-111111111111", "view": "small_card"},
                {"type": "code_block", "language": "mermaid", "text": "graph TD; A-->B;"},
                {"type": "hr"},
            ]
        )
        with pytest.raises(ValueError):
            validate_content_ast([{"type": "code_block", "language": "MERMAID", "text": "x"}])
