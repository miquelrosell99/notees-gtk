"""The text query DSL tests — the parity pin against
``packages/query/test/dsl.test.ts``.

Every field kind, every operator, boolean composition, quoting, value
coercion, name resolution (injected resolvers) and the fail-loud error
surface (unknown fields list the known ones, unresolvable names, bad
operators, unterminated strings, missing values) — plus the store-seam
wiring pins: ``LocalStore.run_query_language`` resolves names against the
mirror and runs the emitted AST through the slice-1 compiler.

The emitted AST is the v2 grammar: ``class:``/``linked:`` emit ``member``
conditions (family class/links, op in, ids target), ``isClass:``/
``isEmbedded:`` emit ``nodeField`` eq "true"/"false", and the wire node
fields emit ``nodeField`` conditions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from notees_gtk.core.protocol.clock import Hlc
from notees_gtk.core.protocol.ids import new_uuid7
from notees_gtk.core.protocol.models import PROTOCOL_VERSION, RelayEnvelope
from notees_gtk.core.protocol.query_ast import (
    ContentCondition,
    EntireWorkspaceScope,
    Group,
    IdsTarget,
    MemberCondition,
    NameCondition,
    NodeFieldCondition,
    Not,
    PagesScope,
    PropertyCondition,
    QueryAst,
    parse_query_ast,
)
from notees_gtk.core.query.dsl import (
    QueryLanguageError,
    QueryLanguageResolvers,
    looks_like_query_language,
    parse_query_language,
)
from notees_gtk.data.store import LocalStore

# A small fixed world for resolution (the TS suite's ids).
CLASS_PAPER = "0192a000-0000-7000-8000-000000000201"
CLASS_BOOK = "0192a000-0000-7000-8000-000000000202"
SCHEMA_YEAR = "0192a000-0000-7000-8000-000000000301"
SCHEMA_TITLE = "0192a000-0000-7000-8000-000000000302"
SCHEMA_CITEKEY = "0192a000-0000-7000-8000-000000000303"
SCHEMA_AUTHOR = "0192a000-0000-7000-8000-000000000304"
NODE_PARIS = "0192a000-0000-7000-8000-000000000401"

RESOLVERS = QueryLanguageResolvers(
    resolve_class=lambda name: {"paper": CLASS_PAPER, "book": CLASS_BOOK}.get(name.lower()),
    resolve_property_schema=lambda name: {
        "year": SCHEMA_YEAR,
        "title": SCHEMA_TITLE,
        "citekey": SCHEMA_CITEKEY,
        "author": SCHEMA_AUTHOR,
    }.get(name.lower()),
    resolve_node=lambda name: NODE_PARIS if name.lower() == "paris" else None,
)
KNOWN_FIELDS = ["year", "title"]


def parse(text: str, *, scope: Any = None, resolvers: QueryLanguageResolvers = RESOLVERS) -> QueryAst:
    return parse_query_language(text, scope=scope, resolvers=resolvers, known_fields=KNOWN_FIELDS)


def children(ast: QueryAst) -> tuple[Any, ...]:
    return ast.root.children


def only(ast: QueryAst) -> Any:
    kids = children(ast)
    assert len(kids) == 1
    return kids[0]


def member_in(family: str, ids: list[str]) -> MemberCondition:
    return MemberCondition(family=family, op="in", target=IdsTarget(ids=tuple(ids)))  # type: ignore[arg-type]


def to_wire(child: Any) -> Any:
    """Serialize the emitted AST back to the wire shape (the TS pin feeds the
    emitted object straight into ``parseQueryAst``; the Python model is
    dataclass-typed, so the test carries the wire mapping)."""
    if isinstance(child, Group):
        return {"type": "group", "logic": child.logic, "children": [to_wire(c) for c in child.children]}
    if isinstance(child, Not):
        return {"type": "not", "child": to_wire(child.child)}
    if isinstance(child, MemberCondition):
        assert isinstance(child.target, IdsTarget)
        return {
            "type": "member",
            "family": child.family,
            "op": child.op,
            "target": {"mode": "ids", "ids": list(child.target.ids)},
        }
    if isinstance(child, ContentCondition):
        return {"type": "content", "op": child.op, **({"value": child.value} if child.value is not None else {})}
    if isinstance(child, NameCondition):
        return {"type": "name", "op": child.op, **({"value": child.value} if child.value is not None else {})}
    if isinstance(child, PropertyCondition):
        return {"type": "property", "schemaId": child.schema_id, "op": child.op, "value": child.value}
    if isinstance(child, NodeFieldCondition):
        return {
            "type": "nodeField",
            "field": child.field,
            "op": child.op,
            **({"value": child.value} if child.value is not None else {}),
        }
    raise AssertionError(f"unexpected child {child!r}")


def ast_wire(ast: QueryAst) -> dict[str, Any]:
    scope = ast.scope
    if isinstance(scope, EntireWorkspaceScope):
        scope_wire: dict[str, Any] = {"type": "entire_workspace"}
    elif isinstance(scope, PagesScope):
        scope_wire = {"type": "pages"}
    else:
        raise AssertionError(f"unexpected scope {scope!r}")
    return {"version": 2, "scope": scope_wire, "root": to_wire(ast.root)}


class TestFields:
    def test_empty_query_parses_to_an_everything_matching_ast(self) -> None:
        ast = parse("   ")
        assert ast.version == 2
        assert isinstance(ast.scope, EntireWorkspaceScope)
        assert ast.root == Group(logic="and", children=())

    def test_honours_a_caller_supplied_scope(self) -> None:
        scope = PagesScope()
        assert parse("class:paper", scope=scope).scope is scope

    def test_class_name_resolves_the_class_by_name(self) -> None:
        assert only(parse("class:paper")) == member_in("class", [CLASS_PAPER])
        assert only(parse("class:book")) == member_in("class", [CLASS_BOOK])

    def test_isclass_isembedded_emit_nodefield_eq_conditions_case_insensitive_field(self) -> None:
        assert only(parse("isClass:true")) == NodeFieldCondition(field="isClass", op="eq", value="true")
        assert only(parse("isclass:false")) == NodeFieldCondition(field="isClass", op="eq", value="false")
        assert only(parse("isEmbedded:true")) == NodeFieldCondition(field="isEmbedded", op="eq", value="true")
        assert only(parse("isembedded:FALSE")) == NodeFieldCondition(field="isEmbedded", op="eq", value="false")

    def test_text_and_bare_words_emit_content_contains_conditions(self) -> None:
        assert only(parse("text:relativity")) == ContentCondition(op="contains", value="relativity")
        assert only(parse("relativity")) == ContentCondition(op="contains", value="relativity")

    def test_name_variants_emit_title_conditions(self) -> None:
        # Bare ":" is a title contains (LIKE — ASCII case-insensitive at compile).
        assert only(parse("name:paris")) == NameCondition(op="contains", value="paris")
        # ":=" / "=" are exact title equality (case-sensitive at compile).
        assert only(parse("name:=Paris")) == NameCondition(op="eq", value="Paris")
        assert only(parse("name=Paris")) == NameCondition(op="eq", value="Paris")
        assert only(parse("name!=Paris")) == NameCondition(op="neq", value="Paris")
        # Bare ":" with no value is the exists arm (the prop: precedent)…
        assert only(parse("name:")) == NameCondition(op="exists")
        # …also before a boolean keyword.
        assert parse("name: NOT class:paper").root == Group(
            logic="and",
            children=(
                NameCondition(op="exists"),
                Not(child=member_in("class", [CLASS_PAPER])),
            ),
        )
        # Field names are case-insensitive like the rest of the grammar.
        assert only(parse("NAME:paris")) == NameCondition(op="contains", value="paris")
        # Quoted multi-word titles survive whole.
        assert only(parse('name:"Lone Page"')) == NameCondition(op="contains", value="Lone Page")
        assert only(parse('name:="Lone Page"')) == NameCondition(op="eq", value="Lone Page")

    def test_linked_name_resolves_the_node_by_name(self) -> None:
        assert only(parse("linked:Paris")) == member_in("links", [NODE_PARIS])

    def test_prop_bare_colon_is_contains(self) -> None:
        assert only(parse("prop:title:revolution")) == PropertyCondition(
            schema_id=SCHEMA_TITLE, op="contains", value="revolution"
        )

    def test_prop_with_no_value_is_exists_also_before_a_boolean_keyword(self) -> None:
        assert only(parse("prop:year")) == PropertyCondition(schema_id=SCHEMA_YEAR, op="exists")
        assert only(parse("prop:year:")) == PropertyCondition(schema_id=SCHEMA_YEAR, op="exists")
        assert parse("prop:year NOT class:paper").root == Group(
            logic="and",
            children=(
                PropertyCondition(schema_id=SCHEMA_YEAR, op="exists"),
                Not(child=member_in("class", [CLASS_PAPER])),
            ),
        )

    def test_bare_property_schema_field_is_shorthand_for_prop(self) -> None:
        assert only(parse("year:>2010")) == PropertyCondition(schema_id=SCHEMA_YEAR, op="gt", value=2010)
        assert only(parse("title:revolution")) == PropertyCondition(
            schema_id=SCHEMA_TITLE, op="contains", value="revolution"
        )

    def test_citekey_author_pin_the_bare_schema_shorthand(self) -> None:
        assert only(parse("citekey:doe2024")) == PropertyCondition(
            schema_id=SCHEMA_CITEKEY, op="contains", value="doe2024"
        )
        assert only(parse("author:doe")) == PropertyCondition(schema_id=SCHEMA_AUTHOR, op="contains", value="doe")
        assert only(parse('citekey:="doe2024"')) == PropertyCondition(
            schema_id=SCHEMA_CITEKEY, op="eq", value="doe2024"
        )

    def test_node_field_predicates_bare_is_set_uuid_passthrough_name_resolution(self) -> None:
        asset_uuid = "0192a000-0000-7000-8000-000000000501"
        other_uuid = "0192a000-0000-7000-8000-000000000502"
        assert only(parse("coverAsset:")) == NodeFieldCondition(field="coverAsset", op="exists")
        # Bare colon before a boolean keyword still reads exists (the prop: precedent).
        assert parse("coverAsset: NOT bannerAsset:").root == Group(
            logic="and",
            children=(
                NodeFieldCondition(field="coverAsset", op="exists"),
                Not(child=NodeFieldCondition(field="bannerAsset", op="exists")),
            ),
        )
        # A uuid value skips name resolution.
        assert only(parse(f"coverAsset:{asset_uuid}")) == NodeFieldCondition(
            field="coverAsset", op="eq", value=asset_uuid
        )
        assert only(parse(f"bannerAsset:={asset_uuid}")) == NodeFieldCondition(
            field="bannerAsset", op="eq", value=asset_uuid
        )
        # A non-uuid value resolves through the node-name resolver (linked: precedent).
        assert only(parse("aliasedNode:Paris")) == NodeFieldCondition(
            field="aliasedNode", op="eq", value=NODE_PARIS
        )
        # "!=" is neq (SQL NULL semantics: unset matches neither — "is unset" is NOT coverAsset:).
        assert only(parse(f"coverAsset!={other_uuid}")) == NodeFieldCondition(
            field="coverAsset", op="neq", value=other_uuid
        )
        # Field names are case-insensitive like the rest of the grammar.
        assert only(parse("CoverAsset:")) == NodeFieldCondition(field="coverAsset", op="exists")


class TestOperators:
    @pytest.mark.parametrize(
        ("syntax", "op"),
        [
            (":=", "eq"),
            ("=", "eq"),
            ("!=", "neq"),
            (":>", "gt"),
            (">", "gt"),
            (":>=", "gte"),
            (">=", "gte"),
            (":<", "lt"),
            ("<", "lt"),
            (":<=", "lte"),
            ("<=", "lte"),
        ],
    )
    def test_prop_operator_matrix(self, syntax: str, op: str) -> None:
        assert only(parse(f"prop:year{syntax}1950")) == PropertyCondition(schema_id=SCHEMA_YEAR, op=op, value=1950)  # type: ignore[arg-type]

    def test_numeric_values_coerce_to_numbers_iso_dates_stay_strings(self) -> None:
        assert only(parse("year:>2010")) == PropertyCondition(schema_id=SCHEMA_YEAR, op="gt", value=2010)
        exact = QueryLanguageResolvers(
            resolve_property_schema=lambda name: SCHEMA_TITLE if name == "published" else None,
        )
        assert only(parse("prop:published:>=2024-01-01", resolvers=exact)) == PropertyCondition(
            schema_id=SCHEMA_TITLE, op="gte", value="2024-01-01"
        )

    def test_values_with_colons_and_slashes_survive_whole(self) -> None:
        assert only(parse("text:https://example.com/x")) == ContentCondition(
            op="contains", value="https://example.com/x"
        )
        assert only(parse("prop:title:10.2307/2333763")) == PropertyCondition(
            schema_id=SCHEMA_TITLE, op="contains", value="10.2307/2333763"
        )


class TestComposition:
    def test_multiple_bare_words_and_implicitly(self) -> None:
        assert parse("capital france").root == Group(
            logic="and",
            children=(
                ContentCondition(op="contains", value="capital"),
                ContentCondition(op="contains", value="france"),
            ),
        )

    def test_and_or_not_parens_compose_exactly(self) -> None:
        assert parse("class:paper AND year:>2010 OR NOT class:book").root == Group(
            logic="or",
            children=(
                Group(
                    logic="and",
                    children=(
                        member_in("class", [CLASS_PAPER]),
                        PropertyCondition(schema_id=SCHEMA_YEAR, op="gt", value=2010),
                    ),
                ),
                Not(child=member_in("class", [CLASS_BOOK])),
            ),
        )

    def test_parenthesized_groups_nest(self) -> None:
        assert parse("(class:paper OR class:book) AND text:revolution").root == Group(
            logic="and",
            children=(
                Group(
                    logic="or",
                    children=(member_in("class", [CLASS_PAPER]), member_in("class", [CLASS_BOOK])),
                ),
                ContentCondition(op="contains", value="revolution"),
            ),
        )

    def test_not_negates_a_parenthesized_group(self) -> None:
        assert parse("NOT (class:paper OR class:book)").root == Group(
            logic="and",
            children=(
                Not(
                    child=Group(
                        logic="or",
                        children=(member_in("class", [CLASS_PAPER]), member_in("class", [CLASS_BOOK])),
                    )
                ),
            ),
        )

    def test_bang_eq_negates_class_boolean_and_linked_conditions(self) -> None:
        assert only(parse("class!=paper")) == Not(child=member_in("class", [CLASS_PAPER]))
        assert only(parse("isClass!=true")) == Not(
            child=NodeFieldCondition(field="isClass", op="eq", value="true")
        )
        assert only(parse("isEmbedded!=false")) == Not(
            child=NodeFieldCondition(field="isEmbedded", op="eq", value="false")
        )
        assert only(parse("linked!=Paris")) == Not(child=member_in("links", [NODE_PARIS]))

    def test_quoted_phrases_are_content_contains_terms_usable_as_field_values(self) -> None:
        assert only(parse('"scientific revolutions"')) == ContentCondition(
            op="contains", value="scientific revolutions"
        )
        assert only(parse('prop:title:"The Structure of Scientific Revolutions"')) == PropertyCondition(
            schema_id=SCHEMA_TITLE, op="contains", value="The Structure of Scientific Revolutions"
        )

    def test_the_emitted_ast_validates_against_the_strict_parser(self) -> None:
        assert parse_query_ast(ast_wire(parse("class:paper AND year:>2010"))) == parse("class:paper AND year:>2010")
        assert parse_query_ast(ast_wire(parse('name:="Lone Page" AND class:paper'))) == parse(
            'name:="Lone Page" AND class:paper'
        )


class TestErrors:
    def test_unknown_field_lists_the_known_fields(self) -> None:
        with pytest.raises(QueryLanguageError) as excinfo:
            parse("wobble:42")
        assert str(excinfo.value) == (
            "unknown field 'wobble' (known fields: class, isclass, presentasmain, prop, "
            "text, name, linked, coverasset, bannerasset, aliasednode, year, title)"
        )

    def test_unknown_class_schema_node_names_fail_at_resolution(self) -> None:
        with pytest.raises(QueryLanguageError, match="unknown class 'galaxy'"):
            parse("class:galaxy")
        with pytest.raises(QueryLanguageError, match="unknown field 'mass'"):
            parse("prop:mass:>1")
        with pytest.raises(QueryLanguageError, match="unknown node 'Berlin'"):
            parse("linked:Berlin")
        with pytest.raises(QueryLanguageError, match="unknown node 'Berlin'"):
            parse("coverAsset:Berlin")

    def test_wire_node_field_predicates_reject_unfitting_operators(self) -> None:
        with pytest.raises(QueryLanguageError, match="not supported here"):
            parse("coverAsset:>x")
        with pytest.raises(QueryLanguageError, match="unknown node 'x'"):
            parse("bannerAsset:x contains y")

    def test_unresolvable_names_surface_the_resolver_miss_case_insensitively(self) -> None:
        exact = QueryLanguageResolvers(
            resolve_class=lambda name: CLASS_PAPER if name == "paper" else None,
        )
        with pytest.raises(QueryLanguageError, match="unknown class 'PAPER'"):
            parse_query_language("class:PAPER", resolvers=exact)

    def test_rejects_unsupported_operators_per_field(self) -> None:
        with pytest.raises(QueryLanguageError, match=r"unexpected '>'"):
            parse("class:paper>3")
        with pytest.raises(QueryLanguageError, match="not supported for text"):
            parse("text!=foo")
        with pytest.raises(QueryLanguageError, match="not supported for name"):
            parse("name:>paris")
        with pytest.raises(QueryLanguageError, match="not supported for name"):
            parse("name:>=paris")
        with pytest.raises(QueryLanguageError, match=r"unknown isClass value 'galaxy'.*expected true or false"):
            parse("isClass:galaxy")
        with pytest.raises(QueryLanguageError, match=r"unknown isEmbedded value '1'"):
            parse("isEmbedded:1")

    def test_rejects_a_comparison_operator_without_a_value(self) -> None:
        with pytest.raises(QueryLanguageError, match="requires a value"):
            parse("year:>")
        with pytest.raises(QueryLanguageError, match="requires a value"):
            parse("name:=")
        with pytest.raises(QueryLanguageError, match="requires a value"):
            parse("name!=")

    def test_rejects_unterminated_strings_stray_parens_trailing_tokens(self) -> None:
        with pytest.raises(QueryLanguageError, match="unterminated string"):
            parse('"unterminated')
        with pytest.raises(QueryLanguageError, match=r"expected '\)'"):
            parse("(class:paper")
        with pytest.raises(QueryLanguageError, match="unexpected"):
            parse("class:paper)")
        with pytest.raises(QueryLanguageError, match=r"unexpected character '!' at position 4"):
            parse("year!x")

    def test_errors_carry_the_source_position(self) -> None:
        with pytest.raises(QueryLanguageError) as excinfo:
            parse("class:paper)")
        assert excinfo.value.position == 11


class TestLooksLikeQueryLanguage:
    def test_detects_field_prefixes_boolean_keywords_and_quoted_phrases(self) -> None:
        assert looks_like_query_language("class:paper AND year:>2010") is True
        assert looks_like_query_language("year:1950") is True
        assert looks_like_query_language("notes AND reminders") is True
        assert looks_like_query_language('"exact phrase"') is True

    def test_leaves_plain_text_alone(self) -> None:
        assert looks_like_query_language("meeting notes") is False
        assert looks_like_query_language("https://example.com/docs") is False
        assert looks_like_query_language("12:30 lunch") is False
        assert looks_like_query_language("") is False


# --------------------------------------------------------------- store wiring
#
# The search-entry seam: ``LocalStore.run_query_language`` resolves class /
# property-schema / node names against the mirror and runs the emitted AST
# through the slice-1 compiler (the window detects DSL input with
# ``looks_like_query_language`` and calls this seam instead of the plain
# content-contains path).

WS = "0192a000-0000-7000-8000-000000000001"
ACTOR = "0192a000-0000-7000-8000-000000000002"

PAPER = "0192a000-0000-7000-8000-000000000201"
BOOK = "0192a000-0000-7000-8000-000000000202"
YEAR = "0192a000-0000-7000-8000-000000000301"
ESSAY_A = "0192a000-0000-7000-8000-000000000101"
ESSAY_B = "0192a000-0000-7000-8000-000000000102"
NOVEL_C = "0192a000-0000-7000-8000-000000000103"
CITY_PARIS = "0192a000-0000-7000-8000-000000000401"
MENTION_BLOCK = "0192a000-0000-7000-8000-000000000501"


def _env(op_type: str, payload: dict[str, Any], physical: int) -> RelayEnvelope:
    return RelayEnvelope(
        id=new_uuid7(),
        protocolVersion=PROTOCOL_VERSION,
        workspaceId=WS,
        actorId=ACTOR,
        deviceId="test-device-query-dsl",
        hlc=Hlc(physical=physical, logical=0),
        affectedNodeIds=[],
        opType=op_type,
        payload=payload,
        timestamp=datetime.fromtimestamp(physical / 1000, UTC),
    )


def _text(value: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": value}]


def _world(store: LocalStore) -> None:
    t0 = 1727200000000
    envelopes = [
        _env("class.create", {"classId": PAPER, "contentAst": _text("Paper")}, t0),
        _env("class.create", {"classId": BOOK, "contentAst": _text("Book")}, t0 + 1000),
        _env("propertySchema.create", {"propertySchemaId": YEAR, "name": "year", "type": "number"}, t0 + 2000),
        _env("object.create", {"objectId": ESSAY_A, "contentAst": _text("Essay A"), "classIds": [PAPER]}, t0 + 3000),
        _env(
            "object.create",
            {"objectId": ESSAY_B, "contentAst": _text("Essay B"), "classIds": [PAPER]},
            t0 + 4000,
        ),
        _env("object.create", {"objectId": NOVEL_C, "contentAst": _text("Novel C"), "classIds": [BOOK]}, t0 + 5000),
        _env("object.create", {"objectId": CITY_PARIS, "contentAst": _text("Paris")}, t0 + 6000),
        _env("property.set", {"objectId": ESSAY_A, "propertySchemaId": YEAR, "value": 1900}, t0 + 7000),
        _env("property.set", {"objectId": ESSAY_B, "propertySchemaId": YEAR, "value": 1950}, t0 + 8000),
        _env(
            "object.create",
            {
                "objectId": "0192a000-0000-7000-8000-000000000501",
                "parentId": ESSAY_A,
                "contentAst": [*_text("About "), {"type": "mention", "targetNodeId": CITY_PARIS, "customLabel": "Paris"}],
            },
            t0 + 9000,
        ),
    ]
    for envelope in envelopes:
        assert store.apply_remote(envelope)


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    instance = LocalStore(tmp_path / "store.db")
    yield instance
    instance.close()


@pytest.fixture
def world(store: LocalStore) -> LocalStore:
    _world(store)
    return store


class TestRunQueryLanguage:
    def test_resolves_class_and_schema_names_and_runs(self, world: LocalStore) -> None:
        result = world.run_query_language(WS, "class:paper year:>1920")
        assert [row["id"] for row in result.rows] == [ESSAY_B]

    def test_name_resolution_is_case_insensitive_against_the_mirror(self, world: LocalStore) -> None:
        result = world.run_query_language(WS, "CLASS:PAPER")
        assert [row["id"] for row in result.rows] == [ESSAY_A, ESSAY_B]

    def test_linked_resolves_the_node_display_name(self, world: LocalStore) -> None:
        # backlinksWithRollup: the mentioning block matches (the roll-up arm
        # covers containment of the TARGET's subtree, not the source's page).
        result = world.run_query_language(WS, "linked:Paris")
        assert [row["id"] for row in result.rows] == [MENTION_BLOCK]

    def test_boolean_composition_rides_the_compiler(self, world: LocalStore) -> None:
        result = world.run_query_language(WS, "class:paper OR name:Novel")
        assert [row["id"] for row in result.rows] == [ESSAY_A, ESSAY_B, NOVEL_C]

    def test_unresolvable_names_fail_loud_from_the_seam(self, world: LocalStore) -> None:
        with pytest.raises(QueryLanguageError, match="unknown class 'magazine'"):
            world.run_query_language(WS, "class:magazine")

    def test_unknown_field_fails_loud_from_the_seam(self, world: LocalStore) -> None:
        with pytest.raises(QueryLanguageError, match=r"unknown field 'wobble' \(known fields:"):
            world.run_query_language(WS, "wobble:42")
