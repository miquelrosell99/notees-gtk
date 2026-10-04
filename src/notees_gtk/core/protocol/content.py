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
  widgets survive, and ``code_block`` (§34.54) is a promotion survivor too —
  a code page is a real surface);
- :func:`validate_content_ast` / :func:`validate_content_token` — the strict
  v3 content-grammar validators (``contentTokenSchema`` parity, §34.54):
  every token variant incl. ``code_block {language?, text}`` (the language
  hint is a strict lowercase tag) and ``hr {}``, plus the ``embed_ref.view``
  enum ("embed" | "small_card" | "wide_card", absent = full);
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
    "EMBED_VIEW_MODES",
    "derive_display_name",
    "format_date_node_name",
    "parse_content_ast",
    "plaintext_excerpt",
    "stringify_content_ast",
    "tokens_from_plaintext",
    "validate_content_ast",
    "validate_content_token",
]

#: Display-name excerpt cap (``DISPLAY_NAME_MAX`` in domain/node.ts).
DISPLAY_NAME_MAX = 80

#: v2 mark names (content-mark.ts MARKS): attributes on text runs, not nodes.
MARKS: tuple[str, ...] = ("bold", "italic", "strike", "highlight", "code")

#: The embed_ref view enum (§34.54 B8): absent (or "embed") = the full live
#: transclusion; the card views are bounded identity cards that never
#: transclude.
EMBED_VIEW_MODES: tuple[str, ...] = ("embed", "small_card", "wide_card")

_WHITESPACE_RE = re.compile(r"\s+")
_DATE_LABEL_RE = re.compile(r"^\d{8}$")
#: The code_block language hint: a strict lowercase tag (content-mark.ts
#: ``/^[a-z0-9+#-]+$/``).
_LANGUAGE_RE = re.compile(r"^[a-z0-9+#-]+$")
_UUID_LIKE_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


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

    Port of v2 ``stringifyContentAst`` (packages/domain/src/node.ts,
    §34.54): pages and classes carry text-only content (SCHEMA.md
    "title-is-content"). Inline rich tokens (mentions, chips, links, marks)
    fold into their plain text; block-scale structural widgets
    (``whiteboard``, ``query``) survive as tokens — they are displays, not
    prose — and ``code_block`` survives with them (a code page is a real
    surface; flattening would destroy the source). ``hr`` is deliberately
    NOT a survivor: it carries no prose (a rule in a page title is
    meaningless). The flattened stream is one leading ``text`` token (when
    non-empty) followed by the surviving widgets, so replicas deriving from
    any rich source converge byte-equal.
    """
    if not tokens:
        return []
    out: list[Any] = []
    for token in tokens:
        if isinstance(token, Mapping) and token.get("type") in ("whiteboard", "query", "code_block"):
            out.append(dict(token))
    text = plaintext_excerpt(tokens).strip()
    if text != "":
        out.insert(0, {"type": "text", "text": text})
    return out


# ------------------------------------------------------- strict token grammar
#
# ``contentTokenSchema`` parity (content-mark.ts, §34.54): the strict v3
# validators every client shares. The op payload schemas carry contentAst as
# ``z.array(z.unknown())`` (the query token's loose-optional forward-compat
# union forbids rejecting unknown payloads at the wire gate), so these
# validators are the authoring/build surface — producers validate what they
# write; the store's applier grammar (stringify/plaintext above) stays
# shape-tolerant like the TS reference.


def _fail(message: str) -> ValueError:
    return ValueError(f"invalid content token: {message}")


def _require_str(token: Mapping[str, Any], key: str, *, min_length: int = 0, max_length: int | None = 4096) -> None:
    value = token.get(key)
    if not isinstance(value, str) or len(value) < min_length or (max_length is not None and len(value) > max_length):
        raise _fail(f"{key!r} must be a string of length {min_length}..{max_length}")


def _check_uuid(value: Any, what: str) -> None:
    if not isinstance(value, str) or not _UUID_LIKE_RE.match(value):
        raise _fail(f"{what} must be a uuid")


def _check_no_extra(token: Mapping[str, Any], allowed: set[str]) -> None:
    extra = set(token) - {"type"} - allowed
    if extra:
        raise _fail(f"unknown keys {sorted(extra)} (strict token schema)")


def validate_content_token(token: Any) -> None:
    """Validate ONE content token against the strict v3 grammar.

    Raises:
        ValueError: The token deviates from ``contentTokenSchema`` — an
            unknown type, an extra key (``.strict()`` parity), a bad uuid, a
            code_block language outside the lowercase tag grammar, or an
            ``embed_ref.view`` outside :data:`EMBED_VIEW_MODES`.
    """
    if not isinstance(token, Mapping):
        raise _fail("token must be an object")
    ttype = token.get("type")
    if not isinstance(ttype, str):
        raise _fail("'type' must be a string")
    if ttype == "text":
        _require_str(token, "text", max_length=None)  # z.string() — no cap
        _check_no_extra(token, {"text", "marks"})
        marks = token.get("marks")
        if marks is not None and (
            not isinstance(marks, list) or not all(isinstance(mark, str) and mark in MARKS for mark in marks)
        ):
            raise _fail(f"'marks' must be a list of mark names {MARKS}")
    elif ttype == "typed_link":
        verb = token.get("verb")
        if isinstance(verb, str):
            if not 1 <= len(verb) <= 128:
                raise _fail("typed_link verb string out of range")
        elif isinstance(verb, Mapping):
            _check_uuid(verb.get("propertySchemaId"), "typed_link verb propertySchemaId")
        else:
            raise _fail("typed_link 'verb' must be a string or {propertySchemaId}")
        _require_str(token, "text", min_length=1)
        _check_no_extra(token, {"verb", "text", "metadata"})
        metadata = token.get("metadata")
        if metadata is not None:
            if not isinstance(metadata, Mapping):
                raise _fail("typed_link 'metadata' must be an object")
            _check_no_extra(metadata, {"locator", "candidateSpans"})
            if metadata.get("locator") is not None:
                locator = metadata["locator"]
                if not isinstance(locator, str) or len(locator) > 1024:
                    raise _fail("typed_link metadata 'locator' must be a ≤1024 char string")
            spans = metadata.get("candidateSpans")
            if spans is not None and (
                not isinstance(spans, list)
                or len(spans) > 64
                or not all(isinstance(span, str) and span for span in spans)
            ):
                raise _fail("typed_link metadata 'candidateSpans' must be a list of ≤64 non-empty strings")
    elif ttype == "mention":
        _check_uuid(token.get("targetNodeId"), "mention targetNodeId")
        _require_str(token, "text", min_length=1)
        _check_no_extra(token, {"targetNodeId", "text", "displayText", "linkId"})
        if token.get("displayText") is not None:
            _require_str(token, "displayText", min_length=1, max_length=512)
        if token.get("linkId") is not None:
            _check_uuid(token.get("linkId"), "mention linkId")
    elif ttype == "class_chip":
        _check_uuid(token.get("classId"), "class_chip classId")
        _check_no_extra(token, {"classId", "displayText"})
        if token.get("displayText") is not None:
            _require_str(token, "displayText", min_length=1, max_length=512)
    elif ttype == "external_link":
        _require_str(token, "href", min_length=1, max_length=4096)
        _require_str(token, "text", min_length=1)
        _check_no_extra(token, {"href", "text"})
    elif ttype == "math":
        _require_str(token, "expression", min_length=1, max_length=4096)
        _check_no_extra(token, {"expression"})
    elif ttype == "hard_break":
        _check_no_extra(token, set())
    elif ttype == "asset_ref":
        _check_uuid(token.get("assetId"), "asset_ref assetId")
        _check_no_extra(token, {"assetId"})
    elif ttype == "embed_ref":
        _check_uuid(token.get("nodeId"), "embed_ref nodeId")
        _check_no_extra(token, {"nodeId", "view"})
        view = token.get("view")
        if view is not None and view not in EMBED_VIEW_MODES:
            raise _fail(f"embed_ref 'view' must be one of {EMBED_VIEW_MODES}")
    elif ttype == "query":
        # Loose-optional forward compat (content-mark.ts): ASTs this build
        # does not know parse as plain records — never rejected.
        if not isinstance(token.get("queryAst"), (Mapping, list)):
            raise _fail("query token needs a queryAst object")
        _check_no_extra(token, {"queryAst", "view"})
    elif ttype == "whiteboard":
        if not isinstance(token.get("layout"), Mapping):
            raise _fail("whiteboard token needs a layout object")
        _check_no_extra(token, {"layout"})
    elif ttype == "code_block":
        _require_str(token, "text", max_length=65536)
        _check_no_extra(token, {"language", "text"})
        language = token.get("language")
        if language is not None and (not isinstance(language, str) or not 1 <= len(language) <= 64):
            raise _fail("code_block 'language' must be a 1..64 char string")
        if isinstance(language, str) and not _LANGUAGE_RE.match(language):
            raise _fail("code_block language hint: lowercase letters, digits, +, #, -")
    elif ttype == "hr":
        _check_no_extra(token, set())
    elif ttype == "quote":
        children = token.get("children")
        if not isinstance(children, list):
            raise _fail("quote token needs a children list")
        _check_no_extra(token, {"children"})
        for child in children:
            # The quote union is the INLINE set only (content-mark.ts).
            if not isinstance(child, Mapping) or child.get("type") in (
                "asset_ref",
                "embed_ref",
                "query",
                "whiteboard",
                "code_block",
                "hr",
            ):
                raise _fail("quote children must be inline tokens")
            validate_content_token(child)
    else:
        raise _fail(f"unknown token type {ttype!r}")


def validate_content_ast(tokens: Any) -> None:
    """Validate a whole content array against the strict v3 grammar.

    Raises:
        ValueError: The first deviating token, with the reason (see
            :func:`validate_content_token`).
    """
    if not isinstance(tokens, list):
        raise ValueError("contentAst must be an array of tokens")
    for token in tokens:
        validate_content_token(token)


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
