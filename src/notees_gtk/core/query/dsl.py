"""The Notees text query DSL — the ``packages/query/src/dsl.ts`` port.

A compact, safe, injection-free language that compiles to the versioned
QueryAST (port of ``app/domain/services/query_language.py`` concepts,
extended where the original fell short: ISO-8601 dates, DOIs/URLs and other
values containing ``:`` or ``/`` read cleanly, and property comparison
operators).

Grammar (fail loud — every syntax/resolution error is a
:class:`QueryLanguageError` with a useful message)::

    query        := or
    or           := and (OR and)*
    and          := not ((AND)? not)*        -- juxtaposition = implicit AND
    not          := NOT primary | primary
    primary      := "(" or ")" | STRING | term
    term         := bareWord                 -- content contains (substring)
                  | field (op value)?        -- see fields below

Fields (word followed by an operator):

- ``class:Name`` — class membership (resolved by NAME, hierarchy-aware at
  compile time); ``!=`` negates
- ``isClass:true|false`` / ``isEmbedded:true|false`` — the Revision 12 bits;
  ``!=`` negates
- ``text:term`` — content contains (value required)
- ``name:term`` — title contains; bare ``:`` = title non-empty (exists);
  ``:=``/``=`` eq, ``!=`` neq — exact, case-sensitive
- ``linked:Name`` — backlinksWithRollup to the named node; ``!=`` negates
- ``coverAsset:`` / ``bannerAsset:`` / ``aliasedNode:<ref>`` — the wire node
  fields (uuid or node NAME via the alias twins): bare ``:`` = is set,
  ``=`` eq, ``!=`` neq — range/contains don't fit uuid references
- ``prop:name<op>val`` — property condition on the schema called ``name``;
  no value → exists
- ``<schema>:<op>val`` — shorthand: a bare field that resolves to a property
  schema name is a property condition (``year:>2010``)

Operators: ``:`` (contains; bare ``:`` with no value → exists), ``:=`` /
``=`` (eq), ``!=`` (neq), ``:>`` / ``>`` (gt), ``:>=`` / ``>=`` (gte),
``:`` / ``<`` (lt), ``:<=`` / ``<=`` (lte).

Values: numbers coerce to JSON numbers (numeric comparison), ``"quoted
strings"`` (phrase contains), or a bare run of non-space/non-paren text
(ISO dates, DOIs, URLs, citekeys — lexicographic comparison).

Name resolution is injected by the caller (``resolvers``) because ids are
workspace state the parser must not fetch: classes and property schemas
resolve by name, nodes by display name (for ``linked:``). Unknown fields and
unresolvable names are hard errors, never silent text search.

The parser emits only the supported AST — v2 conditions: property (all eight
ops), member (class/links families, in over an ids target), nodeField
(isClass/isEmbedded as eq "true"/"false", plus the coverAsset/bannerAsset/
aliasedNode wire fields eq/neq/exists), content contains, and title (name)
eq/neq/contains/exists — no new condition kinds.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from notees_gtk.core.protocol.query_ast import (
    Child,
    Condition,
    ContentCondition,
    EntireWorkspaceScope,
    Group,
    IdsTarget,
    MemberCondition,
    NameCondition,
    NodeFieldCondition,
    Not,
    PropertyCondition,
    QueryAst,
    Scope,
)

__all__ = [
    "QueryLanguageError",
    "QueryLanguageResolvers",
    "looks_like_query_language",
    "parse_query_language",
]

#: Reserved field names; a bare word matching none of these must resolve as a
#: property schema (the TS ``RESERVED_FIELDS``, verbatim — ``presentasmain``
#: stays listed even though the field retired from the grammar).
RESERVED_FIELDS = (
    "class",
    "isclass",
    "presentasmain",
    "prop",
    "text",
    "name",
    "linked",
    "coverasset",
    "bannerasset",
    "aliasednode",
)

#: Canonical uuid shape — a node-field value that IS a uuid skips name
#: resolution (the TS ``UUID_RE``).
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

Op = Literal[":", "=", "!=", ">=", "<=", ">", "<", ":=", ":>", ":>=", ":<", ":<="]

#: Longest-first operator table (the scanner matches a prefix against it).
OPERATORS: tuple[Op, ...] = (":>=", ":<=", ":=", ":>", ":<", "!=", ">=", "<=", ":", "=", ">", "<")

_WHITESPACE = " \t\n\r\f\v"
_OP_CHARS = ":=!<>"

_NUMBER_PATTERN = re.compile(r"^[+-]?\d+(\.\d+)?$")


class QueryLanguageError(ValueError):
    """A query string cannot be tokenized, parsed or resolved."""

    def __init__(self, message: str, position: int | None = None) -> None:
        super().__init__(message)
        #: Source position of the offending token, when known.
        self.position = position


@dataclass(frozen=True)
class QueryLanguageResolvers:
    """Name → id lookups the caller injects (any resolver may be absent;
    a missing one fails every name of that kind loud).

    Matching policy — e.g. case-insensitivity — is the resolver's job, as in
    the TS reference (``resolversFor`` in the web color-groups surface).
    """

    resolve_class: Callable[[str], str | None] | None = None
    resolve_property_schema: Callable[[str], str | None] | None = None
    resolve_node: Callable[[str], str | None] | None = None


# --- tokenizer ---------------------------------------------------------------


@dataclass(frozen=True)
class _Token:
    kind: str  # "raw" | "string" | "op" | "lparen" | "rparen" | "and" | "or" | "not" | "eof"
    start: int
    text: str | None = None
    op: Op | None = None


class _Scanner:
    """The TS ``Scanner`` port: raw tokens stop before whitespace, parens and
    operator chars; quoted strings honor ``\\`` escapes."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def _is_boundary(self, char: str | None) -> bool:
        return char is None or char in _WHITESPACE or char in "()"

    def next(self) -> _Token:
        while self.pos < len(self.text) and self.text[self.pos] in _WHITESPACE:
            self.pos += 1
        start = self.pos
        if self.pos >= len(self.text):
            return _Token(kind="eof", start=start)
        char = self.text[self.pos]
        if char == "(":
            self.pos += 1
            return _Token(kind="lparen", start=start)
        if char == ")":
            self.pos += 1
            return _Token(kind="rparen", start=start)
        if char in "\"'":
            return self._read_string(char, start)
        if char in _OP_CHARS:
            for op in OPERATORS:
                if self.text.startswith(op, self.pos):
                    self.pos += len(op)
                    return _Token(kind="op", start=start, op=op)
            raise QueryLanguageError(f"unexpected character '{char}' at position {start}", start)
        end = self.pos
        while end < len(self.text) and not self._is_boundary(self.text[end]) and self.text[end] not in _OP_CHARS:
            end += 1
        if end == self.pos:
            raise QueryLanguageError(f"unexpected character '{char}' at position {start}", start)
        self.pos = end
        text = self.text[start:end]
        upper = text.upper()
        if upper == "AND":
            return _Token(kind="and", start=start)
        if upper == "OR":
            return _Token(kind="or", start=start)
        if upper == "NOT":
            return _Token(kind="not", start=start)
        return _Token(kind="raw", start=start, text=text)

    def _read_string(self, quote: str, start: int) -> _Token:
        self.pos += 1  # opening quote
        value = ""
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == quote:
                self.pos += 1
                return _Token(kind="string", start=start, text=value)
            if char == "\\" and self.pos + 1 < len(self.text):
                value += self.text[self.pos + 1]
                self.pos += 2
                continue
            value += char
            self.pos += 1
        raise QueryLanguageError("unterminated string literal", start)


# --- value coercion ------------------------------------------------------------


def _coerce_value(raw: str) -> str | int | float:
    """Numbers coerce to JSON numbers (``1950`` → int, ``3.14`` → float —
    the TS ``Number()`` parity); everything else stays a string."""
    if _NUMBER_PATTERN.match(raw):
        return float(raw) if "." in raw else int(raw)
    return raw


# --- parser --------------------------------------------------------------------

#: The property ops the operator family maps onto.
_PropertyOp = Literal["eq", "neq", "contains", "exists", "gt", "gte", "lt", "lte"]


class _Parser:
    """The TS ``Parser`` port — recursive descent over the scanner tokens,
    emitting the frozen v2 dataclasses the strict parse layer accepts."""

    def __init__(self, text: str, scanner: _Scanner, resolvers: QueryLanguageResolvers | None, known_fields: list[str] | None, scope: Scope | None) -> None:
        self.text = text
        self.scanner = scanner
        self.resolvers = resolvers
        self.known_fields = known_fields
        self._scope = scope
        self.lookahead = self.scanner.next()

    # --- token plumbing ------------------------------------------------------

    def _advance(self) -> _Token:
        token = self.lookahead
        self.lookahead = self.scanner.next()
        return token

    def _advance_op(self) -> Op:
        token = self._advance()
        if token.kind != "op" or token.op is None:
            raise QueryLanguageError("parser internal error: expected an operator token", token.start)
        return token.op

    def _error(self, message: str, position: int | None = None) -> QueryLanguageError:
        return QueryLanguageError(message, self.lookahead.start if position is None else position)

    def _describe(self, token: _Token) -> str:
        if token.kind == "raw":
            return token.text or ""
        if token.kind == "string":
            return f'"{token.text}"'
        if token.kind == "op":
            return token.op or ""
        if token.kind in ("lparen", "rparen"):
            return token.kind
        if token.kind in ("and", "or", "not"):
            return token.kind.upper()
        return "end of query"

    # --- entry -----------------------------------------------------------------

    def parse(self) -> QueryAst:
        scope = self._scope if self._scope is not None else EntireWorkspaceScope()
        if self.lookahead.kind == "eof":
            return QueryAst(scope=scope, root=Group(logic="and", children=()))
        node = self._parse_or()
        rest = self.lookahead
        if rest.kind != "eof":
            raise self._error(f"unexpected '{self._describe(rest)}'", rest.start)
        root = node if isinstance(node, Group) else Group(logic="and", children=(node,))
        return QueryAst(scope=scope, root=root)

    # --- boolean layers (implicit-AND, OR-explicit, NOT prefix) ------------------

    def _parse_or(self) -> Child:
        left = self._parse_and()
        while self.lookahead.kind == "or":
            self._advance()
            right = self._parse_and()
            if isinstance(left, Group) and left.logic == "or":
                left = Group(logic="or", children=(*left.children, right))
            else:
                left = Group(logic="or", children=(left, right))
        return left

    def _parse_and(self) -> Child:
        left = self._parse_not()
        while self._is_operand_start(self.lookahead):
            if self.lookahead.kind == "and":
                self._advance()
            right = self._parse_not()
            if isinstance(left, Group) and left.logic == "and":
                left = Group(logic="and", children=(*left.children, right))
            else:
                left = Group(logic="and", children=(left, right))
        return left

    @staticmethod
    def _is_operand_start(token: _Token) -> bool:
        return token.kind in ("and", "not", "raw", "string", "lparen")

    def _parse_not(self) -> Child:
        if self.lookahead.kind == "not":
            self._advance()
            # NOT binds to one primary (the AST's Not wraps a Condition | Group).
            child = self._parse_primary()
            if isinstance(child, Not):
                raise self._error("NOT cannot negate another NOT; write NOT (…) once")
            return Not(child=child)
        return self._parse_primary()

    def _parse_primary(self) -> Child:
        token = self.lookahead
        if token.kind == "lparen":
            self._advance()
            inner = self._parse_or()
            if self.lookahead.kind != "rparen":
                raise self._error("expected ')'", self.lookahead.start)
            self._advance()
            return inner
        if token.kind == "string":
            self._advance()
            return _content_contains(token.text or "")
        if token.kind == "raw":
            if self._is_reserved_keyword(token.text or ""):
                raise self._error(
                    f"'{token.text}' is a reserved word; use it between operands or quote it", token.start
                )
            self._advance()
            if self.lookahead.kind == "op":
                op = self._advance_op()
                return self._build_field_condition(token.text or "", op)
            return _content_contains(token.text or "")
        if token.kind == "eof":
            raise self._error("expected a term", token.start)
        raise self._error(f"unexpected '{self._describe(token)}'", token.start)

    @staticmethod
    def _is_reserved_keyword(text: str) -> bool:
        upper = text.upper()
        return upper in ("AND", "OR", "NOT")

    # --- fields --------------------------------------------------------------------

    def _build_field_condition(self, field: str, op: Op) -> Child:
        lower = field.lower()
        if lower == "class":
            return self._negatable(op, lambda: MemberCondition(
                family="class",
                op="in",
                target=IdsTarget(ids=(self._resolve_class(str(self._read_value("a class name", "token"))),)),
            ))
        if lower == "isclass":
            return self._negatable(op, lambda: NodeFieldCondition(
                field="isClass",
                op="eq",
                value="true" if self._read_boolean("isClass") else "false",
            ))
        if lower == "isembedded":
            return self._negatable(op, lambda: NodeFieldCondition(
                field="isEmbedded",
                op="eq",
                value="true" if self._read_boolean("isEmbedded") else "false",
            ))
        if lower == "text":
            self._require_contains_op(op, "text")
            return _content_contains(str(self._read_value("a text term")))
        if lower == "name":
            return self._name_condition(op)
        if lower == "linked":
            return self._negatable(op, lambda: MemberCondition(
                family="links",
                op="in",
                target=IdsTarget(ids=(self._resolve_node(str(self._read_value("a node name", "token"))),)),
            ))
        if lower == "coverasset":
            return self._node_field_condition("coverAsset", op)
        if lower == "bannerasset":
            return self._node_field_condition("bannerAsset", op)
        if lower == "aliasednode":
            return self._node_field_condition("aliasedNode", op)
        if lower == "prop":
            # `prop` is a prefix, not a condition: the schema name follows the
            # first colon, and a second operator (`prop:year:>2010`) is the real
            # property operator. With no second operator the first one applies
            # (`prop:year:1950` = contains; `prop:year` = exists).
            schema_name = self._read_schema_name()
            effective_op = self._advance_op() if self.lookahead.kind == "op" else op
            return self._build_property_condition(schema_name, effective_op)
        return self._build_property_condition(field, op)

    def _negatable(self, op: Op, build: Callable[[], Condition]) -> Child:
        """class/isClass/isEmbedded/linked support ":" and "=" (positive) and
        "!=" (wrapped in NOT)."""
        if op in (":", "="):
            return build()
        if op == "!=":
            return Not(child=build())
        raise self._error(f"operator '{op}' is not supported here (use ':', '=' or '!=')")

    def _node_field_condition(self, field: Literal["coverAsset", "bannerAsset", "aliasedNode"], op: Op) -> Child:
        """A wire node-field predicate: bare ":" is the set/unset probe — no
        value (also before AND/OR/NOT) reads "is set", a value reads eq ("is
        unset" is `NOT coverAsset:`); "=" / ":=" eq and "!=" neq take a node
        reference — a uuid passes through, anything else resolves through the
        node-name resolver (the linked: precedent). Range/contains ops don't
        fit uuid references (the AST admits only eq/neq/exists)."""
        if op == ":":
            if self.lookahead.kind in ("eof", "rparen", "and", "or", "not"):
                return NodeFieldCondition(field=field, op="exists")
            return NodeFieldCondition(field=field, op="eq", value=self._read_node_ref())
        if op in ("=", ":="):
            return NodeFieldCondition(field=field, op="eq", value=self._read_node_ref())
        if op == "!=":
            return NodeFieldCondition(field=field, op="neq", value=self._read_node_ref())
        raise self._error(f"operator '{op}' is not supported here (use ':', '=', '!=')")

    def _read_node_ref(self) -> str:
        """A node-field reference: uuid as-is, otherwise the display-name resolver."""
        raw = str(self._read_value("a node name or uuid"))
        return raw if _UUID_RE.match(raw) else self._resolve_node(raw)

    def _require_contains_op(self, op: Op, field: str) -> None:
        if op not in (":", "="):
            raise self._error(f"operator '{op}' is not supported for {field} (use ':')")

    def _name_condition(self, op: Op) -> Child:
        """The title field (title-is-content: the flattened content text, the
        sort-by-name expression): bare ":" with no value (also before AND/OR/
        NOT) reads "title is non-empty" (the prop: exists precedent); a value
        reads contains (LIKE, ASCII case-insensitive — the text: convention);
        ":="/"=" eq and "!=" neq are exact, case-sensitive (the prop: :=
        convention). Range ops don't fit titles."""
        if op == ":":
            if self.lookahead.kind in ("eof", "rparen", "and", "or", "not"):
                return NameCondition(op="exists")
            return NameCondition(op="contains", value=str(self._read_value("a title")))
        if op in ("=", ":=", "!="):
            if self.lookahead.kind in ("eof", "rparen"):
                raise self._error(f"operator '{op}' requires a value")
            return NameCondition(op="neq" if op == "!=" else "eq", value=str(self._read_value("a title")))
        raise self._error(f"operator '{op}' is not supported for name (use ':', ':=' or '!=')")

    def _read_boolean(self, field: str) -> bool:
        raw = str(self._read_value("true or false", "token")).lower()
        if raw == "true":
            return True
        if raw == "false":
            return False
        raise self._error(f"unknown {field} value '{raw}' (expected true or false)")

    def _build_property_condition(self, schema_name: str, op: Op) -> Child:
        resolver = self.resolvers.resolve_property_schema if self.resolvers else None
        schema_id = resolver(schema_name) if resolver else None
        if schema_id is None:
            raise self._unknown_field(schema_name)
        property_op = self._map_property_op(op)
        if property_op == "exists":
            return PropertyCondition(schema_id=schema_id, op="exists")
        if self.lookahead.kind in ("eof", "rparen"):
            raise self._error(f"operator '{op}' requires a value")
        return PropertyCondition(schema_id=schema_id, op=property_op, value=self._read_value("a property value"))

    def _unknown_field(self, field: str) -> QueryLanguageError:
        known = [*RESERVED_FIELDS, *(self.known_fields or [])]
        return self._error(f"unknown field '{field}' (known fields: {', '.join(known)})")

    def _map_property_op(self, op: Op) -> _PropertyOp:
        """":" with no following value means exists; the rest map onto the
        operator family."""
        if op == ":":
            # A following boolean keyword ends the condition (the value slot is
            # empty): `prop:year AND …` / `prop:year NOT …` = year exists.
            return "exists" if self.lookahead.kind in ("eof", "rparen", "and", "or", "not") else "contains"
        if op in ("=", ":="):
            return "eq"
        if op == "!=":
            return "neq"
        if op in (">", ":>"):
            return "gt"
        if op in (">=", ":>="):
            return "gte"
        if op in ("<", ":<"):
            return "lt"
        return "lte"

    # --- resolution + values ----------------------------------------------------------

    def _resolve_class(self, name: str) -> str:
        resolver = self.resolvers.resolve_class if self.resolvers else None
        resolved = resolver(name) if resolver else None
        if resolved is None:
            raise self._error(f"unknown class '{name}'")
        return resolved

    def _resolve_node(self, name: str) -> str:
        resolver = self.resolvers.resolve_node if self.resolvers else None
        resolved = resolver(name) if resolver else None
        if resolved is None:
            raise self._error(f"unknown node '{name}'")
        return resolved

    def _read_schema_name(self) -> str:
        """Schema names are single tokens: `prop:year:>2010` must stop the name
        before the second operator."""
        token = self.lookahead
        if token.kind in ("eof", "op", "lparen", "rparen"):
            raise self._error("expected a property schema name", token.start)
        return str(self._read_value("a property schema name", "token"))

    def _read_value(self, what: str, mode: Literal["token", "slice"] = "slice") -> str | int | float:
        """Read a field value.

        "token" mode (names: class/type/linked/schema) stops the value at
        operator characters too, so a trailing operator surfaces as its own
        syntax error (`class:paper>3` complains about `>`, not about a class
        named "paper>3").

        "slice" mode (prop values, text terms) reads the raw source up to the
        next whitespace/paren, so URLs (`https://…`), DOIs (`10.2307/…`) and
        ISO dates survive intact (the original word/number lexer could not
        carry them). Numbers coerce to JSON numbers; everything else stays a
        string.
        """
        token = self.lookahead
        if token.kind in ("eof", "rparen", "op", "lparen"):
            raise self._error(f"expected {what}", token.start)
        if token.kind == "string":
            self._advance()
            return token.text or ""
        stop_at_op = mode == "token"
        end = token.start
        while end < len(self.text):
            char = self.text[end]
            if char in _WHITESPACE or char in "()" or (stop_at_op and char in _OP_CHARS):
                break
            end += 1
        self.scanner.pos = end
        self._advance()
        return _coerce_value(self.text[token.start:end])


def _content_contains(value: str) -> Condition:
    return ContentCondition(op="contains", value=value)


def parse_query_language(
    text: str,
    *,
    scope: Scope | None = None,
    resolvers: QueryLanguageResolvers | None = None,
    known_fields: list[str] | None = None,
) -> QueryAst:
    """Parse a query-language string into a versioned QueryAST. Syntax errors,
    unknown fields and unresolvable class/schema/node names all throw
    :class:`QueryLanguageError` with a position and a useful message.

    Args:
        text: The query-language string.
        scope: The scope of the emitted AST (defaults to the entire workspace).
        resolvers: The caller-injected name → id lookups (see
            :class:`QueryLanguageResolvers`).
        known_fields: Extra known field names for the unknown-field error
            (e.g. property schema names).

    Returns:
        The typed :class:`QueryAst` (the frozen v2 dataclasses).
    """
    return _Parser(text, _Scanner(text), resolvers, known_fields, scope).parse()


#: The field-prefix / keyword / quote heuristic shapes (the TS regexes, with
#: ASCII-explicit character classes — JS ``\\w`` is ASCII).
_FIELD_PREFIX_RE = re.compile(r"(?:^|\s)[A-Za-z_][A-Za-z0-9_.-]*:(?!//)")
_BOOLEAN_KEYWORD_RE = re.compile(r"(?:^|\s)(?:AND|OR|NOT)(?=\s|$)")


def looks_like_query_language(text: str) -> bool:
    """Heuristic gate for search boxes deciding whether an input should go
    through the DSL or plain text search: a ``word:`` field prefix (excluding
    URL schemes like ``http://``), standalone AND/OR/NOT keywords, or a quoted
    phrase."""
    trimmed = text.strip()
    if not trimmed:
        return False
    if _FIELD_PREFIX_RE.search(trimmed):
        return True
    if _BOOLEAN_KEYWORD_RE.search(trimmed):
        return True
    return '"' in trimmed
