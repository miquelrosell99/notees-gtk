"""Execution parity tests — the real convergence pin.

The France/Paris topology + classes + property bindings of
``packages/query/test/query.test.ts`` is built from envelopes through the
GTK appliers, then the TS suite's result expectations are pinned here
(hand-ported, not re-derived): scope isolation, the class family closure,
title-is-content name conditions, the property effective read model
(defaults surfacing, tombstone suppression, the option-membership rule),
comparison operators, backlinksWithRollup, the hierarchy families, dynamic
target sets, the self target arms, tags-as-links, composition, content
contains vs the fts prefix-AND emulation, created/updated windows, soft
deletes, sort orderings, aggregation grids, the execute helpers, date-node
refs (point/range arms), nodeField wire predicates, archived-class
resolution, and the parentlessRole demote.

Store-shape divergences from the TS suite (documented, deliberate):

- The TS ``class.update`` title workaround envelopes are NOT needed: the GTK
  ``class.create`` lands the title on the INSERT directly (the LWW-gate bug
  the workaround papers over is a TS-store quirk the GTK applier avoids by
  construction — see ``_upsert_class_node``).
- ``content``/``fts`` conditions read ``nodes.search_plain`` (D2): the
  derived search text (mention-resolved content + asset names + text-ish
  property values, diacritic-folded at derive time) — the pins below use
  ASCII titles/bodies with no assets/properties so the result sets agree
  with the excerpt-only shape; the full source matrix is pinned in
  ``test_search_text_parity.py``.
- The GTK mirror stores timestamps as Python ``isoformat()`` strings; the
  query-bound placeholders use the TS ``toISOString()`` shape. The two
  compare identically at second resolution, which every pin below respects.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from notees_gtk.core.protocol.clock import Hlc
from notees_gtk.core.protocol.dates import chain_node_ids
from notees_gtk.core.protocol.ids import new_uuid7
from notees_gtk.core.protocol.models import PROTOCOL_VERSION, RelayEnvelope
from notees_gtk.core.protocol.query_ast import parse_query_ast
from notees_gtk.core.query.compiler import CompileOptions
from notees_gtk.core.query.execute import count_query, matches, run_aggregate, run_query
from notees_gtk.data.store import LocalStore

WS = "0192a000-0000-7000-8000-000000000001"
ACTOR = "0192a000-0000-7000-8000-000000000002"

FRANCE = "0192a000-0000-7000-8000-000000000101"
PARIS = "0192a000-0000-7000-8000-000000000102"
LONE = "0192a000-0000-7000-8000-000000000103"
FR_BLOCK = "0192a000-0000-7000-8000-000000000111"
PARIS_BLOCK = "0192a000-0000-7000-8000-000000000112"
LONE_BLOCK = "0192a000-0000-7000-8000-000000000113"
OUT_BLOCK = "0192a000-0000-7000-8000-000000000114"

PLACE = "0192a000-0000-7000-8000-000000000201"
CITY = "0192a000-0000-7000-8000-000000000202"

PRIORITY = "0192a000-0000-7000-8000-000000000301"
RATING = "0192a000-0000-7000-8000-000000000302"
YEAR = "0192a000-0000-7000-8000-000000000303"
OPENED = "0192a000-0000-7000-8000-000000000304"
PUBLISHED = "0192a000-0000-7000-8000-000000000305"
FOUNDED = "0192a000-0000-7000-8000-000000000306"
SPAN = "0192a000-0000-7000-8000-000000000307"

T0 = 1727200000000  # France
STEP = 1000

OPT = CompileOptions(workspace_id=WS)

ENTIRE = {"type": "entire_workspace"}
PAGES = {"type": "pages"}


def env(op_type: str, payload: dict[str, Any], physical: int) -> RelayEnvelope:
    return RelayEnvelope(
        id=new_uuid7(),
        protocolVersion=PROTOCOL_VERSION,
        workspaceId=WS,
        actorId=ACTOR,
        deviceId="test-device-query",
        hlc=Hlc(physical=physical, logical=0),
        affectedNodeIds=[],
        opType=op_type,
        payload=payload,
        timestamp=datetime.fromtimestamp(physical / 1000, UTC),
    )


def text(value: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": value}]


def mention(target_node_id: str, label: str) -> dict[str, Any]:
    return {"type": "mention", "targetNodeId": target_node_id, "customLabel": label}


def world_envelopes() -> list[RelayEnvelope]:
    """The France/Paris topology + classes + property bindings, in apply order."""
    t = lambda i: T0 + i * STEP  # noqa: E731
    return [
        env("class.create", {"classId": PLACE, "contentAst": text("Place")}, t(0)),
        env("class.create", {"classId": CITY, "contentAst": text("City")}, t(1)),
        env("class.setExtends", {"classId": CITY, "parentClassIds": [PLACE]}, t(2)),
        env(
            "propertySchema.create",
            {
                "propertySchemaId": PRIORITY,
                "name": "priority",
                "type": "select",
                "options": [
                    {"id": "low", "label": "low"},
                    {"id": "medium", "label": "medium"},
                    {"id": "high", "label": "high"},
                ],
            },
            t(3),
        ),
        env("class.property.set", {"classId": PLACE, "propertySchemaId": PRIORITY}, t(4)),
        env("class.property.set", {"classId": CITY, "propertySchemaId": PRIORITY}, t(5)),
        # The default is SCHEMA-level.
        env("propertySchema.update", {"propertySchemaId": PRIORITY, "defaultValue": "medium"}, t(5) + 1),
        env("object.create", {"objectId": FRANCE, "contentAst": text("France"), "classIds": [PLACE]}, t(6)),
        # Paris rides in France's main-children zone (the create carries the render bit).
        env(
            "object.create",
            {
                "objectId": PARIS,
                "parentId": FRANCE,
                "isEmbedded": False,
                "contentAst": text("Paris"),
                "classIds": [CITY],
            },
            t(7),
        ),
        env("object.create", {"objectId": LONE, "contentAst": text("Lone Page")}, t(8)),
        env(
            "object.create",
            {"objectId": FR_BLOCK, "parentId": FRANCE, "contentAst": [*text("The capital city"), mention(PARIS, "Paris")]},
            t(9),
        ),
        env("object.create", {"objectId": PARIS_BLOCK, "parentId": PARIS, "contentAst": text("Capital of France")}, t(10)),
        env("object.create", {"objectId": LONE_BLOCK, "parentId": LONE, "contentAst": text("An orphan note about cooking")}, t(11)),
        env(
            "object.create",
            {"objectId": OUT_BLOCK, "parentId": FRANCE, "contentAst": [*text("Traveller notes"), mention(LONE, "Lone Page")]},
            t(12),
        ),
        env("property.set", {"objectId": LONE, "propertySchemaId": PRIORITY, "value": "low"}, t(13)),
        env("propertySchema.create", {"propertySchemaId": RATING, "name": "rating", "type": "number"}, t(14)),
        env("class.property.set", {"classId": PLACE, "propertySchemaId": RATING}, t(15)),
        env("propertySchema.update", {"propertySchemaId": RATING, "defaultValue": 1}, t(15) + 1),
        env("property.set", {"objectId": PARIS, "propertySchemaId": RATING, "value": 3}, t(16)),
        # Comparison-operator fixture: "year" a JSON number, "opened" an ISO date string.
        env("propertySchema.create", {"propertySchemaId": YEAR, "name": "year", "type": "number"}, T0 + 40 * STEP),
        env("propertySchema.create", {"propertySchemaId": OPENED, "name": "opened", "type": "text"}, T0 + 41 * STEP),
        env("property.set", {"objectId": FRANCE, "propertySchemaId": YEAR, "value": 1900}, T0 + 42 * STEP),
        env("property.set", {"objectId": PARIS, "propertySchemaId": YEAR, "value": 1950}, T0 + 43 * STEP),
        env("property.set", {"objectId": LONE, "propertySchemaId": YEAR, "value": 1920}, T0 + 44 * STEP),
        env("property.set", {"objectId": FRANCE, "propertySchemaId": OPENED, "value": "1900-01-15"}, T0 + 45 * STEP),
        env("property.set", {"objectId": PARIS, "propertySchemaId": OPENED, "value": "1937-05-06"}, T0 + 46 * STEP),
        env("property.set", {"objectId": LONE, "propertySchemaId": OPENED, "value": "1920-06-20"}, T0 + 47 * STEP),
    ]


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    instance = LocalStore(tmp_path / "store.db")
    yield instance
    instance.close()


@pytest.fixture
def world(store: LocalStore) -> LocalStore:
    for envelope in world_envelopes():
        assert store.apply_remote(envelope)
    return store


def ast(scope: dict[str, Any], children: list[Any], sort: list[Any] | None = None) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "version": 2,
        "scope": scope,
        "root": {"type": "group", "logic": "and", "children": children},
    }
    if sort is not None:
        raw["sort"] = sort
    return raw


def all_in(scope: dict[str, Any]) -> dict[str, Any]:
    return ast(scope, [])


def run(conn: Any, raw: dict[str, Any], options: CompileOptions = OPT) -> Any:
    return run_query(conn, parse_query_ast(raw), options)


def m_in(family: str, ids: list[str]) -> dict[str, Any]:
    return {"type": "member", "family": family, "op": "in", "target": {"mode": "ids", "ids": ids}}


def m_in_q(family: str, children: list[Any]) -> dict[str, Any]:
    return {
        "type": "member",
        "family": family,
        "op": "in",
        "target": {"mode": "query", "root": {"type": "group", "logic": "and", "children": children}},
    }


def m_self(family: str) -> dict[str, Any]:
    return {"type": "member", "family": family, "op": "in", "target": {"mode": "self"}}


def m_exists(family: str) -> dict[str, Any]:
    return {"type": "member", "family": family, "op": "exists"}


def nf(field: str, op: str, value: str | None = None) -> dict[str, Any]:
    condition: dict[str, Any] = {"type": "nodeField", "field": field, "op": op}
    if value is not None:
        condition["value"] = value
    return condition


def ids_of(result: Any) -> list[str]:
    return sorted(result.ids)


class TestScopeIsolation:
    def test_entire_workspace_returns_every_active_node(self, world: LocalStore) -> None:
        result = run(world._conn, all_in(ENTIRE))
        # 7 world nodes + the Place/City class nodes (class.create is a node row).
        assert len(result.ids) == 9
        assert {FRANCE, LONE_BLOCK, PLACE} <= set(result.ids)

    def test_pages_returns_only_pages(self, world: LocalStore) -> None:
        assert ids_of(run(world._conn, all_in(PAGES))) == sorted([FRANCE, LONE, PARIS])

    def test_subtree_returns_the_page_plus_descendants_with_distances(self, world: LocalStore) -> None:
        result = run(world._conn, all_in({"type": "subtree", "pageId": FRANCE}))
        assert ids_of(result) == sorted([FRANCE, PARIS, FR_BLOCK, PARIS_BLOCK, OUT_BLOCK])
        assert LONE not in result.ids
        assert LONE_BLOCK not in result.ids
        distance = {str(row["id"]): row["distance"] for row in result.rows}
        assert distance[FRANCE] == 0
        assert distance[PARIS] == 1
        assert distance[FR_BLOCK] == 1
        assert distance[PARIS_BLOCK] == 2
        assert distance[OUT_BLOCK] == 1

    def test_subtree_of_a_nonexistent_page_matches_nothing(self, world: LocalStore) -> None:
        result = run(world._conn, all_in({"type": "subtree", "pageId": "0192a000-0000-7000-8000-000000000999"}))
        assert result.ids == ()

    def test_multi_workspace_stores_do_not_leak_rows(self, store: LocalStore) -> None:
        """R2 of the plan: the workspace predicate is injected inside every
        recursive/subquery arm, so a same-shaped row in another workspace is
        invisible to every family — not just the base scan."""
        other = "0192a000-0000-7000-8000-0000000000f2"
        for envelope in world_envelopes():
            twin = envelope.model_copy(update={"workspaceId": other})
            assert store.apply_remote(twin)
        options = CompileOptions(workspace_id=WS)
        # entire_workspace sees exactly its own 9 rows, not 18.
        assert len(run(store._conn, all_in(ENTIRE), options).ids) == 9
        # subtree recursion stops at the workspace boundary.
        assert len(run(store._conn, all_in({"type": "subtree", "pageId": FRANCE}), options).ids) == 5
        # backlinksWithRollup only counts same-workspace edges.
        assert ids_of(run(store._conn, ast(ENTIRE, [m_in("links", [PARIS])]), options)) == [FR_BLOCK]
        # The containing-page walk too.
        assert ids_of(run(store._conn, ast(ENTIRE, [m_in("page", [PARIS])]), options)) == sorted([PARIS, PARIS_BLOCK])


class TestClassFamily:
    def test_direct_membership_matches(self, world: LocalStore) -> None:
        assert list(run(world._conn, ast(ENTIRE, [m_in("class", [CITY])])).ids) == [PARIS]

    def test_child_classes_match_the_ancestor_class_condition(self, world: LocalStore) -> None:
        # City extends Place, so a Place query must find City members too.
        assert ids_of(run(world._conn, ast(ENTIRE, [m_in("class", [PLACE])]))) == sorted([FRANCE, PARIS])


class TestNameConditions:
    UNTITLED = "0192a000-0000-7000-8000-0000000005a1"

    @pytest.fixture
    def untitled_world(self, world: LocalStore) -> LocalStore:
        world.apply_remote(env("object.create", {"objectId": self.UNTITLED, "contentAst": []}, T0 + 50 * STEP))
        return world

    def test_eq_is_exact_and_case_sensitive_over_the_title(self, untitled_world: LocalStore) -> None:
        conn = untitled_world._conn
        assert list(run(conn, ast(ENTIRE, [{"type": "name", "op": "eq", "value": "Paris"}])).ids) == [PARIS]
        assert list(run(conn, ast(ENTIRE, [{"type": "name", "op": "eq", "value": "paris"}])).ids) == []
        assert list(run(conn, ast(ENTIRE, [{"type": "name", "op": "eq", "value": "Lone Page"}])).ids) == [LONE]
        assert list(run(conn, ast(ENTIRE, [{"type": "name", "op": "eq", "value": "Lone"}])).ids) == []

    def test_contains_is_a_case_insensitive_title_substring(self, untitled_world: LocalStore) -> None:
        conn = untitled_world._conn
        assert list(run(conn, ast(ENTIRE, [{"type": "name", "op": "contains", "value": "paris"}])).ids) == [PARIS]
        assert ids_of(run(conn, ast(ENTIRE, [{"type": "name", "op": "contains", "value": "capital"}]))) == sorted(
            [FR_BLOCK, PARIS_BLOCK]
        )
        # frBlock's CONTENT mentions Paris, but its title is "The capital city".
        assert ids_of(run(conn, ast(ENTIRE, [{"type": "content", "op": "contains", "value": "paris"}]))) == sorted(
            [FR_BLOCK, PARIS]
        )

    def test_neq_matches_every_titled_node_except_the_equal_one(self, untitled_world: LocalStore) -> None:
        result = run(untitled_world._conn, ast(ENTIRE, [{"type": "name", "op": "neq", "value": "Paris"}]))
        assert len(result.ids) == 8
        assert PARIS not in result.ids
        assert self.UNTITLED not in result.ids

    def test_exists_matches_titled_nodes_only_the_not_wrap_reads_untitled(self, untitled_world: LocalStore) -> None:
        conn = untitled_world._conn
        result = run(conn, ast(ENTIRE, [{"type": "name", "op": "exists"}]))
        assert len(result.ids) == 9
        assert self.UNTITLED not in result.ids
        untitled = run(conn, ast(ENTIRE, [{"type": "not", "child": {"type": "name", "op": "exists"}}]))
        assert list(untitled.ids) == [self.UNTITLED]


class TestPropertyConditions:
    def test_eq_matches_binding_derived_defaults(self, world: LocalStore) -> None:
        conn = world._conn
        # France (Place) and Paris (City) BOTH derive "medium"; Lone's authored
        # "low" is the only other effective value.
        assert ids_of(run(conn, ast(ENTIRE, [{"type": "property", "schemaId": PRIORITY, "op": "eq", "value": "medium"}]))) == sorted(
            [FRANCE, PARIS]
        )
        assert list(run(conn, ast(ENTIRE, [{"type": "property", "schemaId": PRIORITY, "op": "eq", "value": "low"}])).ids) == [LONE]

    def test_exists_matches_default_derived_and_authored_values(self, world: LocalStore) -> None:
        result = run(world._conn, ast(ENTIRE, [{"type": "property", "schemaId": PRIORITY, "op": "exists"}]))
        assert ids_of(result) == sorted([FRANCE, PARIS, LONE])

    def test_include_defaults_false_tests_authored_rows_only(self, world: LocalStore) -> None:
        conn = world._conn
        assert list(
            run(
                conn,
                ast(
                    ENTIRE,
                    [{"type": "property", "schemaId": PRIORITY, "op": "exists", "includeDefaults": False}],
                ),
            ).ids
        ) == [LONE]
        assert (
            list(
                run(
                    conn,
                    ast(
                        ENTIRE,
                        [
                            {
                                "type": "property",
                                "schemaId": PRIORITY,
                                "op": "eq",
                                "value": "medium",
                                "includeDefaults": False,
                            }
                        ],
                    ),
                ).ids
            )
            == []
        )

    def test_authored_value_shadows_the_class_binding_default(self, world: LocalStore) -> None:
        world.apply_remote(env("property.set", {"objectId": FRANCE, "propertySchemaId": PRIORITY, "value": "urgent"}, T0 + 20 * STEP))
        conn = world._conn
        assert list(run(conn, ast(ENTIRE, [{"type": "property", "schemaId": PRIORITY, "op": "eq", "value": "medium"}])).ids) == [PARIS]
        assert list(run(conn, ast(ENTIRE, [{"type": "property", "schemaId": PRIORITY, "op": "eq", "value": "urgent"}])).ids) == [FRANCE]

    def test_tombstoned_authored_values_drop_out_and_the_default_resurfaces(self, world: LocalStore) -> None:
        world.apply_remote(
            env("property.unset", {"objectId": LONE, "propertySchemaId": PRIORITY, "idx": 0}, T0 + 21 * STEP)
        )
        result = run(
            world._conn,
            ast(ENTIRE, [{"type": "property", "schemaId": PRIORITY, "op": "exists", "includeDefaults": False}]),
        )
        assert result.ids == ()

    def test_a_select_default_whose_option_was_deleted_derives_nothing(self, world: LocalStore) -> None:
        world.apply_remote(
            env(
                "propertySchema.update",
                {
                    "propertySchemaId": PRIORITY,
                    "options": [
                        {"id": "low", "label": "low"},
                        {"id": "high", "label": "high"},
                    ],
                },
                T0 + 22 * STEP,
            )
        )
        conn = world._conn
        assert list(run(conn, ast(ENTIRE, [{"type": "property", "schemaId": PRIORITY, "op": "eq", "value": "medium"}])).ids) == []
        assert ids_of(run(conn, ast(ENTIRE, [{"type": "property", "schemaId": PRIORITY, "op": "exists"}]))) == [LONE]
        assert list(run(conn, ast(ENTIRE, [{"type": "property", "schemaId": PRIORITY, "op": "eq", "value": "low"}])).ids) == [LONE]

    def test_contains_matches_substrings_of_effective_values_neq_negates_membership(self, world: LocalStore) -> None:
        conn = world._conn
        assert ids_of(run(conn, ast(ENTIRE, [{"type": "property", "schemaId": PRIORITY, "op": "contains", "value": "med"}]))) == sorted(
            [FRANCE, PARIS]
        )
        assert ids_of(run(conn, ast(ENTIRE, [{"type": "property", "schemaId": PRIORITY, "op": "neq", "value": "medium"}]))) == [LONE]

    def test_inactive_binding_stops_deriving_defaults(self, world: LocalStore) -> None:
        world.apply_remote(
            env("class.property.set", {"classId": PLACE, "propertySchemaId": PRIORITY, "active": False}, T0 + 22 * STEP)
        )
        conn = world._conn
        # France's binding is inactive; Paris's City binding stays active.
        assert list(run(conn, ast(ENTIRE, [{"type": "property", "schemaId": PRIORITY, "op": "eq", "value": "medium"}])).ids) == [PARIS]
        # Lone's AUTHORED "low" still matches.
        assert list(run(conn, ast(ENTIRE, [{"type": "property", "schemaId": PRIORITY, "op": "eq", "value": "low"}])).ids) == [LONE]

    def test_an_element_tombstoned_authored_value_drops_out(self, world: LocalStore) -> None:
        elem = "0192a000-0000-7000-8000-0000000007e1"
        world.apply_remote(
            env(
                "property.set",
                {"objectId": LONE, "propertySchemaId": RATING, "value": 5, "elementId": elem, "idx": 0},
                T0 + 23 * STEP,
            )
        )
        conn = world._conn
        query = ast(
            ENTIRE,
            [{"type": "property", "schemaId": RATING, "op": "eq", "value": 5, "includeDefaults": False}],
        )
        assert list(run(conn, query).ids) == [LONE]
        world.apply_remote(
            env("property.unset", {"objectId": LONE, "propertySchemaId": RATING, "elementId": elem}, T0 + 24 * STEP)
        )
        assert list(run(conn, query).ids) == []


class TestPropertyComparisons:
    def test_numeric_json_values_compare_numerically(self, world: LocalStore) -> None:
        conn = world._conn
        assert ids_of(run(conn, ast(ENTIRE, [{"type": "property", "schemaId": YEAR, "op": "lt", "value": 1950}]))) == sorted(
            [FRANCE, LONE]
        )
        assert ids_of(run(conn, ast(ENTIRE, [{"type": "property", "schemaId": YEAR, "op": "gt", "value": 1900}]))) == sorted(
            [LONE, PARIS]
        )
        assert list(run(conn, ast(ENTIRE, [{"type": "property", "schemaId": YEAR, "op": "gte", "value": 1950}])).ids) == [PARIS]
        assert list(run(conn, ast(ENTIRE, [{"type": "property", "schemaId": YEAR, "op": "lte", "value": 1900}])).ids) == [FRANCE]

    def test_iso_date_strings_compare_lexicographically(self, world: LocalStore) -> None:
        conn = world._conn
        assert ids_of(
            run(conn, ast(ENTIRE, [{"type": "property", "schemaId": OPENED, "op": "gte", "value": "1920-01-01"}]))
        ) == sorted([LONE, PARIS])
        assert list(run(conn, ast(ENTIRE, [{"type": "property", "schemaId": OPENED, "op": "lt", "value": "1900-06-01"}])).ids) == [
            FRANCE
        ]
        assert list(run(conn, ast(ENTIRE, [{"type": "property", "schemaId": OPENED, "op": "gt", "value": "1937-05-06"}])).ids) == []

    def test_composes_with_and_and_class_conditions(self, world: LocalStore) -> None:
        conn = world._conn
        assert list(
            run(
                conn,
                ast(
                    ENTIRE,
                    [
                        {"type": "property", "schemaId": YEAR, "op": "gt", "value": 1900},
                        {"type": "property", "schemaId": YEAR, "op": "lt", "value": 1950},
                    ],
                ),
            ).ids
        ) == [LONE]
        assert ids_of(
            run(
                conn,
                ast(
                    ENTIRE,
                    [m_in("class", [PLACE]), {"type": "property", "schemaId": YEAR, "op": "gte", "value": 1900}],
                ),
            )
        ) == sorted([FRANCE, PARIS])
        assert list(
            run(
                conn,
                ast(
                    ENTIRE,
                    [m_in("class", [CITY]), {"type": "property", "schemaId": YEAR, "op": "gte", "value": 1900}],
                ),
            ).ids
        ) == [PARIS]

    def test_nodes_without_an_effective_value_never_match_a_comparison(self, world: LocalStore) -> None:
        result = run(world._conn, ast(ENTIRE, [{"type": "property", "schemaId": YEAR, "op": "gt", "value": 0}]))
        assert ids_of(result) == sorted([FRANCE, LONE, PARIS])


class TestLinksFamily:
    def test_direct_edges_match(self, world: LocalStore) -> None:
        assert list(run(world._conn, ast(ENTIRE, [m_in("links", [PARIS])])).ids) == [FR_BLOCK]

    def test_containment_roll_up_a_block_inside_france_linking_outward_links_france(self, world: LocalStore) -> None:
        result = run(world._conn, ast(ENTIRE, [m_in("links", [FRANCE])]))
        # frBlock -> Paris is intra-subtree (excluded); outBlock -> Lone Page is outward.
        assert list(result.ids) == [OUT_BLOCK]

    def test_scope_form_restricts_the_universe_and_carries_distance(self, world: LocalStore) -> None:
        result = run(world._conn, ast({"type": "linkedTo", "nodeId": FRANCE}, [nf("isEmbedded", "eq", "true")]))
        assert list(result.ids) == [OUT_BLOCK]
        assert result.rows[0]["distance"] == 1


class TestChildPathFamily:
    def test_every_row_inside_the_anchors_tree_matches_the_anchor_itself_does_not(self, world: LocalStore) -> None:
        result = run(world._conn, ast(ENTIRE, [m_in("childPath", [FRANCE])]))
        assert ids_of(result) == sorted([PARIS, FR_BLOCK, PARIS_BLOCK, OUT_BLOCK])

    def test_a_single_level_chain_matches_exactly_the_children(self, world: LocalStore) -> None:
        assert list(run(world._conn, ast(ENTIRE, [m_in("childPath", [PARIS])])).ids) == [PARIS_BLOCK]
        assert list(run(world._conn, ast(ENTIRE, [m_in("childPath", [LONE])])).ids) == [LONE_BLOCK]

    def test_composes_with_the_other_conditions(self, world: LocalStore) -> None:
        result = run(world._conn, ast(ENTIRE, [m_in("childPath", [FRANCE]), nf("isEmbedded", "eq", "true")]))
        assert ids_of(result) == sorted([FR_BLOCK, PARIS_BLOCK, OUT_BLOCK])


class TestDateFamily:
    def test_the_edit_window_reads_updated_at(self, world: LocalStore) -> None:
        result = run(world._conn, ast(ENTIRE, [{"type": "date", "field": "updated", "op": "after", "timestamp": "2026-01-01"}]))
        # Every world row's updated_at is its create time (2024) — nothing matches a 2026 window.
        assert result.ids == ()
        before = run(world._conn, ast(ENTIRE, [{"type": "date", "field": "updated", "op": "before", "timestamp": "1999-01-01"}]))
        assert before.ids == ()

    def test_date_after_before_are_inclusive_on_created_at(self, world: LocalStore) -> None:
        conn = world._conn
        t = lambda i: datetime.fromtimestamp((T0 + i * STEP) / 1000, UTC).isoformat()  # noqa: E731
        after = run(conn, ast(ENTIRE, [{"type": "date", "field": "created", "op": "after", "timestamp": t(7)}]))
        assert PARIS in after.ids and FRANCE not in after.ids
        assert ids_of(after) == sorted([PARIS, LONE, FR_BLOCK, PARIS_BLOCK, LONE_BLOCK, OUT_BLOCK])
        before = run(conn, ast(ENTIRE, [{"type": "date", "field": "created", "op": "before", "timestamp": t(7)}]))
        # Class nodes are node rows too (created at t0/t1).
        assert ids_of(before) == sorted([PLACE, CITY, FRANCE, PARIS])

    def test_soft_deleted_nodes_are_excluded(self, world: LocalStore) -> None:
        world.apply_remote(env("object.delete", {"objectId": LONE_BLOCK, "permanent": False}, T0 + 22 * STEP))
        conn = world._conn
        assert list(run(conn, ast(ENTIRE, [{"type": "content", "op": "contains", "value": "cook"}])).ids) == []
        assert count_query(conn, parse_query_ast(all_in(ENTIRE)), OPT) == 8

    def test_day_granular_equality_and_the_instant_comparisons(self, world: LocalStore) -> None:
        conn = world._conn
        now = datetime.fromtimestamp(T0 / 1000, UTC)
        options = CompileOptions(workspace_id=WS, now=now)
        date = lambda op, extra: {"type": "date", "field": "created", "op": op, **extra}  # noqa: E731
        # Day equality: every world node was created on the pinned day.
        assert len(run(conn, ast(ENTIRE, [date("eq", {"timestamp": "{today}"})]), options).ids) == 9
        assert len(run(conn, ast(ENTIRE, [date("neq", {"timestamp": "{today}"})]), options).ids) == 0
        # Instant comparisons against a plain date: lexicographic over ISO.
        assert len(run(conn, ast(ENTIRE, [date("gt", {"timestamp": "2024-09-24"})])).ids) == 9
        assert len(run(conn, ast(ENTIRE, [date("lte", {"timestamp": "2024-09-24"})])).ids) == 0
        # The inclusive range covers the creation day.
        assert len(run(conn, ast(ENTIRE, [date("between", {"from": "2024-09-24", "to": "2024-09-25"})])).ids) == 9
        assert len(run(conn, ast(ENTIRE, [date("between", {"from": "2024-09-25", "to": "2024-09-26"})])).ids) == 0
        # The relative period: pinned to the creation year everything matches.
        assert len(run(conn, ast(ENTIRE, [date("within", {"period": "this_year"})]), options).ids) == 9
        a_year_later = CompileOptions(workspace_id=WS, now=datetime(2025, 6, 15, 12, 0, 0))
        assert len(run(conn, ast(ENTIRE, [date("within", {"period": "this_year"})]), a_year_later).ids) == 0

    def test_property_between_the_effective_value_inside_the_inclusive_range(self, world: LocalStore) -> None:
        conn = world._conn
        assert ids_of(
            run(conn, ast(ENTIRE, [{"type": "property", "schemaId": RATING, "op": "between", "from": 1, "to": 5}]))
        ) == sorted([FRANCE, PARIS])
        assert list(run(conn, ast(ENTIRE, [{"type": "property", "schemaId": RATING, "op": "between", "from": 4, "to": 10}])).ids) == []

    def test_property_within_the_effective_date_valued_property_inside_the_period(self, world: LocalStore) -> None:
        now = datetime.fromtimestamp(T0 / 1000, UTC)
        result = run(
            world._conn,
            ast(ENTIRE, [{"type": "property", "schemaId": OPENED, "op": "within", "period": "this_year"}]),
            CompileOptions(workspace_id=WS, now=now),
        )
        assert result.ids == ()  # Everything is far in the past.


class TestDynamicTargets:
    def test_links_to_any_node_matching_the_nested_group(self, world: LocalStore) -> None:
        result = run(
            world._conn,
            ast(
                ENTIRE,
                [
                    m_in_q(
                        "links",
                        [m_in("class", [CITY]), {"type": "property", "schemaId": YEAR, "op": "gt", "value": 0}],
                    )
                ],
            ),
        )
        assert list(result.ids) == [FR_BLOCK]

    def test_the_single_anchor_static_form_and_the_one_member_dynamic_form_agree(self, world: LocalStore) -> None:
        result = run(
            world._conn,
            ast(
                ENTIRE,
                [m_in_q("links", [nf("isClass", "eq", "false"), {"type": "content", "op": "contains", "value": "Paris"}])],
            ),
        )
        assert list(result.ids) == [FR_BLOCK]

    def test_dynamic_child_path_the_ancestor_chain_contains_any_node_matching_the_group(self, world: LocalStore) -> None:
        result = run(world._conn, ast(ENTIRE, [m_in_q("childPath", [m_in("class", [CITY])])]))
        assert list(result.ids) == [PARIS_BLOCK]

    def test_member_in_query_nests_to_depth_two_and_executes(self, world: LocalStore) -> None:
        result = run(
            world._conn,
            ast(
                ENTIRE,
                [m_in_q("links", [m_in_q("class", [{"type": "content", "op": "contains", "value": "city"}])])],
            ),
        )
        assert list(result.ids) == [FR_BLOCK]


class TestSelfTarget:
    def test_page_self_the_containing_page_is_the_hosting_page(self, world: LocalStore) -> None:
        options = CompileOptions(workspace_id=WS, self_node_id=FRANCE)
        assert ids_of(run(world._conn, ast(ENTIRE, [m_self("page")]), options)) == sorted([FRANCE, FR_BLOCK, OUT_BLOCK])

    def test_parent_path_self(self, world: LocalStore) -> None:
        options = CompileOptions(workspace_id=WS, self_node_id=FRANCE)
        assert ids_of(run(world._conn, ast(ENTIRE, [m_self("parentPath")]), options)) == sorted(
            [PARIS, PARIS_BLOCK, FR_BLOCK, OUT_BLOCK]
        )

    def test_root_self(self, world: LocalStore) -> None:
        options = CompileOptions(workspace_id=WS, self_node_id=FRANCE)
        assert ids_of(run(world._conn, ast(ENTIRE, [m_self("root")]), options)) == sorted(
            [FRANCE, PARIS, FR_BLOCK, PARIS_BLOCK, OUT_BLOCK]
        )

    def test_parent_self(self, world: LocalStore) -> None:
        options = CompileOptions(workspace_id=WS, self_node_id=FRANCE)
        assert ids_of(run(world._conn, ast(ENTIRE, [m_self("parent")]), options)) == sorted([PARIS, FR_BLOCK, OUT_BLOCK])

    def test_child_self(self, world: LocalStore) -> None:
        options = CompileOptions(workspace_id=WS, self_node_id=PARIS)
        assert list(run(world._conn, ast(ENTIRE, [m_self("child")]), options).ids) == [FRANCE]

    def test_child_path_self(self, world: LocalStore) -> None:
        options = CompileOptions(workspace_id=WS, self_node_id=FRANCE)
        assert ids_of(run(world._conn, ast(ENTIRE, [m_self("childPath")]), options)) == sorted(
            [PARIS, FR_BLOCK, PARIS_BLOCK, OUT_BLOCK]
        )

    def test_links_self(self, world: LocalStore) -> None:
        options = CompileOptions(workspace_id=WS, self_node_id=FRANCE)
        assert list(run(world._conn, ast(ENTIRE, [m_self("links")]), options).ids) == [OUT_BLOCK]

    def test_reference_path_self(self, world: LocalStore) -> None:
        options = CompileOptions(workspace_id=WS, self_node_id=FRANCE)
        assert ids_of(run(world._conn, ast(ENTIRE, [m_self("referencePath")]), options)) == sorted(
            [PARIS, FR_BLOCK, PARIS_BLOCK, OUT_BLOCK]
        )

    def test_class_self(self, world: LocalStore) -> None:
        options = CompileOptions(workspace_id=WS, self_node_id=PLACE)
        assert ids_of(run(world._conn, ast(ENTIRE, [m_self("class")]), options)) == sorted([FRANCE, PARIS])

    def test_tags_self(self, world: LocalStore) -> None:
        options = CompileOptions(workspace_id=WS, self_node_id=FRANCE)
        assert list(run(world._conn, ast(ENTIRE, [m_self("tags")]), options).ids) == []


class TestTagsFamily:
    TAG = "0192a000-0000-7000-8000-000000000401"

    @pytest.fixture
    def tagged_world(self, world: LocalStore) -> LocalStore:
        world.apply_remote(env("object.create", {"objectId": self.TAG, "contentAst": text("Peri")}, T0 + 90 * STEP))
        world.apply_remote(env("object.create", {"objectId": FRANCE, "tagIds": [self.TAG]}, T0 + 91 * STEP))
        return world

    def test_links_matches_a_tagged_node_exactly_like_a_mention_link(self, tagged_world: LocalStore) -> None:
        assert list(run(tagged_world._conn, ast(ENTIRE, [m_in("links", [self.TAG])])).ids) == [FRANCE]

    def test_reference_path_the_tag_edge_rides_the_row_or_ancestor_arm(self, tagged_world: LocalStore) -> None:
        result = run(tagged_world._conn, ast(ENTIRE, [m_in("referencePath", [self.TAG])]))
        assert ids_of(result) == sorted([FRANCE, PARIS, FR_BLOCK, PARIS_BLOCK, OUT_BLOCK])

    def test_class_assignment_and_class_chips_never_count_as_links(self, tagged_world: LocalStore) -> None:
        result = run(tagged_world._conn, ast(ENTIRE, [m_in("links", [CITY, PLACE])]))
        assert result.ids == ()


class TestComposition:
    def test_and_intersects(self, world: LocalStore) -> None:
        result = run(
            world._conn,
            ast(ENTIRE, [{"type": "content", "op": "contains", "value": "capital"}, nf("isEmbedded", "eq", "true")]),
        )
        assert ids_of(result) == sorted([FR_BLOCK, PARIS_BLOCK])

    def test_or_unions(self, world: LocalStore) -> None:
        or_ast = {
            "version": 2,
            "scope": ENTIRE,
            "root": {
                "type": "group",
                "logic": "or",
                "children": [m_in("class", [PLACE]), nf("isEmbedded", "eq", "true")],
            },
        }
        result = run(world._conn, or_ast)
        assert ids_of(result) == sorted([FRANCE, PARIS, FR_BLOCK, PARIS_BLOCK, LONE_BLOCK, OUT_BLOCK])

    def test_not_negates_a_condition_and_a_nested_group(self, world: LocalStore) -> None:
        conn = world._conn
        assert list(
            run(conn, ast(PAGES, [{"type": "not", "child": {"type": "content", "op": "contains", "value": "france"}}])).ids
        ) == [PARIS, LONE]
        result = run(
            conn,
            ast(
                ENTIRE,
                [
                    nf("isEmbedded", "eq", "true"),
                    nf("isClass", "eq", "false"),
                    {
                        "type": "not",
                        "child": {
                            "type": "group",
                            "logic": "or",
                            "children": [m_in("links", [PARIS]), m_in("links", [LONE])],
                        },
                    },
                ],
            ),
        )
        assert ids_of(result) == sorted([PARIS_BLOCK, LONE_BLOCK])

    def test_not_in_and_not_exists_execute_as_the_negated_reads(self, world: LocalStore) -> None:
        conn = world._conn
        not_in = run(
            conn,
            ast(ENTIRE, [{"type": "member", "family": "class", "op": "not-in", "target": {"mode": "ids", "ids": [CITY]}}]),
        )
        assert PARIS not in not_in.ids
        assert FRANCE in not_in.ids
        not_exists = run(conn, ast(ENTIRE, [{"type": "member", "family": "links", "op": "not-exists"}]))
        assert PARIS not in not_exists.ids
        assert FRANCE in not_exists.ids

    def test_not_wraps_every_condition_type_execution(self, world: LocalStore) -> None:
        conn = world._conn
        now = datetime.fromtimestamp(T0 / 1000, UTC)
        options = CompileOptions(workspace_id=WS, now=now)
        cases = [
            ({"type": "not", "child": m_in("class", [CITY])}, lambda ids: PARIS not in ids and len(ids) == 8),
            ({"type": "not", "child": {"type": "content", "op": "exists"}}, lambda ids: ids == []),
            ({"type": "not", "child": {"type": "name", "op": "exists"}}, lambda ids: ids == []),
            (
                {"type": "not", "child": {"type": "property", "schemaId": PRIORITY, "op": "exists"}},
                lambda ids: len(ids) == 6,
            ),
            (
                {"type": "not", "child": {"type": "date", "field": "created", "op": "within", "period": "this_year"}},
                lambda ids: ids == [],
            ),
            ({"type": "not", "child": nf("isClass", "eq", "true")}, lambda ids: len(ids) == 7),
        ]
        for not_condition, check in cases:
            check(list(run(conn, ast(ENTIRE, [not_condition]), options).ids))


class TestContent:
    def test_contains_is_a_substring_match_over_title_plus_content_plaintext(self, world: LocalStore) -> None:
        conn = world._conn
        assert list(run(conn, ast(ENTIRE, [{"type": "content", "op": "contains", "value": "cook"}])).ids) == [LONE_BLOCK]
        # Pages are findable by title (the title IS the content plaintext).
        assert list(run(conn, ast(PAGES, [{"type": "content", "op": "contains", "value": "lone p"}])).ids) == [LONE]

    def test_fts_matches_whole_token_prefixes_contains_matches_any_substring(self, world: LocalStore) -> None:
        conn = world._conn
        # "ity" is a substring of "city" but no token STARTS with it.
        assert ids_of(run(conn, ast(ENTIRE, [{"type": "content", "op": "contains", "value": "ity"}]))) == sorted(
            [CITY, FR_BLOCK]
        )
        assert list(run(conn, ast(ENTIRE, [{"type": "content", "op": "fts", "value": "ity"}])).ids) == []
        # Prefix-AND across terms: both tokens must be present.
        assert list(run(conn, ast(ENTIRE, [{"type": "content", "op": "fts", "value": "capital france"}])).ids) == [
            PARIS_BLOCK
        ]

    def test_content_text_ops_eq_neq_starts_ends_and_the_exists_bit(self, world: LocalStore) -> None:
        conn = world._conn
        assert list(run(conn, ast(ENTIRE, [{"type": "content", "op": "starts", "value": "The capital"}])).ids) == [FR_BLOCK]
        assert list(run(conn, ast(ENTIRE, [{"type": "content", "op": "ends", "value": "cooking"}])).ids) == [LONE_BLOCK]
        assert list(run(conn, ast(ENTIRE, [{"type": "content", "op": "eq", "value": "Capital of France"}])).ids) == [
            PARIS_BLOCK
        ]
        neq = run(conn, ast(ENTIRE, [{"type": "not", "child": {"type": "content", "op": "eq", "value": "Capital of France"}}]))
        assert PARIS_BLOCK not in neq.ids
        assert len(run(conn, ast(ENTIRE, [{"type": "content", "op": "exists"}])).ids) == 9
        assert len(run(conn, ast(ENTIRE, [{"type": "not", "child": {"type": "content", "op": "exists"}}])).ids) == 0


class TestSort:
    def test_orders_by_name_with_id_tiebreak_desc_reverses(self, world: LocalStore) -> None:
        conn = world._conn
        asc = run(conn, ast(PAGES, [], [{"field": "name", "dir": "asc"}]))
        assert list(asc.ids) == [FRANCE, LONE, PARIS]
        desc = run(conn, ast(PAGES, [], [{"field": "name", "dir": "desc"}]))
        assert list(desc.ids) == [PARIS, LONE, FRANCE]

    def test_folds_case_lowercase_sorts_among_uppercase(self, world: LocalStore) -> None:
        conn = world._conn
        t = lambda i: T0 + 60 * STEP + i * STEP  # noqa: E731
        lower_a = "0192a000-0000-7000-8000-000000000501"
        upper_b = "0192a000-0000-7000-8000-000000000502"
        lower_c = "0192a000-0000-7000-8000-000000000503"
        for node_id, title, at in [(lower_a, "antioxidante", t(0)), (upper_b, "Beta", t(1)), (lower_c, "charlie", t(2))]:
            world.apply_remote(env("object.create", {"objectId": node_id, "contentAst": text(title)}, at))
        asc = run(conn, ast(PAGES, [], [{"field": "name", "dir": "asc"}]))
        assert list(asc.ids) == [lower_a, upper_b, lower_c, FRANCE, LONE, PARIS]


class TestAggregationExecution:
    COUNT_BY_CLASS_BIT = {"dimensions": [{"kind": "isClass"}], "measures": [{"function": "count"}]}

    def aggregate(self, conn: Any, raw: dict[str, Any], options: CompileOptions = OPT) -> Any:
        result = run_aggregate(conn, parse_query_ast({**raw, "sort": None} if "sort" in raw else raw), options)
        return AggregateGrid(result.columns, [list(row) for row in result.rows])


class AggregateGrid:
    """The rows materialized as lists (the TS grid shape)."""

    def __init__(self, columns: tuple[str, ...], rows: list[list[Any]]) -> None:
        self.columns = columns
        self.rows = rows

    def test_groups_by_the_class_identity_bit_with_count(self, world: LocalStore) -> None:
        result = self.aggregate(world._conn, {**all_in(ENTIRE), "aggregation": self.COUNT_BY_CLASS_BIT})
        assert result.columns == ("isClass", "count")
        # 7 world nodes + 2 class nodes: 9 non-class, 2 classes.
        assert list(result.rows) == [[0, 7], [1, 2]]

    def test_groups_by_the_embedding_flag(self, world: LocalStore) -> None:
        result = self.aggregate(
            world._conn,
            {**all_in(ENTIRE), "aggregation": {"dimensions": [{"kind": "isEmbedded"}], "measures": [{"function": "count"}]}},
        )
        assert result.columns == ("isEmbedded", "count")
        # Standalone: France, Paris, Lone + the two class rows (flag 0);
        # embedded: the four blocks.
        assert list(result.rows) == [[0, 5], [1, 4]]

    def test_count_and_count_distinct_agree(self, world: LocalStore) -> None:
        result = self.aggregate(
            world._conn,
            {
                **all_in(ENTIRE),
                "aggregation": {
                    "dimensions": [{"kind": "isClass"}],
                    "measures": [{"function": "count"}, {"function": "countDistinct", "kind": "node"}],
                },
            },
        )
        assert result.columns == ("isClass", "count", "countDistinct")
        assert list(result.rows) == [[0, 7, 7], [1, 2, 2]]

    def test_groups_classed_nodes_by_a_property_value_summing_a_numeric_property(self, world: LocalStore) -> None:
        result = self.aggregate(
            world._conn,
            {
                **ast(ENTIRE, [m_in("class", [PLACE])]),
                "aggregation": {
                    "dimensions": [{"kind": "property", "id": PRIORITY}],
                    "measures": [
                        {"function": "sum", "kind": "property", "id": RATING},
                        {"function": "avg", "kind": "property", "id": RATING},
                    ],
                },
            },
        )
        assert result.columns == (f"property:{PRIORITY}", f"sum:{RATING}", f"avg:{RATING}")
        # Both land in ONE "medium" group: Paris's rating is authored 3,
        # France's is the derived default 1.
        assert list(result.rows) == [["medium", 4, 2.0]]

    def test_class_dimension_groups_by_hierarchy_aware_membership(self, world: LocalStore) -> None:
        result = self.aggregate(
            world._conn,
            {
                **all_in(ENTIRE),
                "aggregation": {"dimensions": [{"kind": "class", "id": CITY}], "measures": [{"function": "count"}]},
            },
        )
        assert list(result.rows) == [[0, 8], [1, 1]]  # Paris — class nodes carry no memberships

    def test_empty_dimensions_a_single_grand_total_row(self, world: LocalStore) -> None:
        result = self.aggregate(
            world._conn,
            {**all_in(ENTIRE), "aggregation": {"dimensions": [], "measures": [{"function": "count"}]}},
        )
        assert result.columns == ("count",)
        assert list(result.rows) == [[9]]

    def test_composes_with_scopes_and_conditions(self, world: LocalStore) -> None:
        conn = world._conn
        by_bit_in_france = self.aggregate(
            conn,
            {
                **all_in({"type": "subtree", "pageId": FRANCE}),
                "aggregation": {"dimensions": [{"kind": "isEmbedded"}], "measures": [{"function": "count"}]},
            },
        )
        assert list(by_bit_in_france.rows) == [[0, 2], [1, 3]]
        capital_blocks = self.aggregate(
            conn,
            {
                **ast(ENTIRE, [{"type": "content", "op": "contains", "value": "capital"}]),
                "aggregation": self.COUNT_BY_CLASS_BIT,
            },
        )
        assert list(capital_blocks.rows) == [[0, 2]]
        pages_only = self.aggregate(conn, {**all_in(PAGES), "aggregation": self.COUNT_BY_CLASS_BIT})
        assert list(pages_only.rows) == [[0, 3]]

    def test_live_updates_the_aggregate_reflects_later_applies(self, world: LocalStore) -> None:
        world.apply_remote(
            env(
                "object.create",
                {"objectId": "0192a000-0000-7000-8000-000000000105", "contentAst": text("Rome"), "classIds": [CITY]},
                T0 + 30 * STEP,
            )
        )
        result = self.aggregate(world._conn, {**all_in(PAGES), "aggregation": self.COUNT_BY_CLASS_BIT})
        assert list(result.rows) == [[0, 4]]


class TestExecuteHelpers:
    def test_count_query_matches_run_query_length(self, world: LocalStore) -> None:
        conn = world._conn
        blocks = ast(ENTIRE, [nf("isEmbedded", "eq", "true"), nf("isClass", "eq", "false")])
        assert count_query(conn, parse_query_ast(blocks), OPT) == 4
        assert len(run(conn, blocks).ids) == 4

    def test_matches_tests_a_single_node(self, world: LocalStore) -> None:
        conn = world._conn
        city_query = ast(ENTIRE, [m_in("class", [CITY])])
        assert matches(conn, PARIS, parse_query_ast(city_query), OPT)
        assert not matches(conn, FRANCE, parse_query_ast(city_query), OPT)
        assert matches(conn, PARIS, parse_query_ast(all_in({"type": "subtree", "pageId": FRANCE})), OPT)
        assert not matches(conn, LONE, parse_query_ast(all_in({"type": "subtree", "pageId": FRANCE})), OPT)

    def test_run_query_rejects_aggregation_asts(self, world: LocalStore) -> None:
        with pytest.raises(ValueError, match="run_aggregate"):
            run(world._conn, {**all_in(PAGES), "aggregation": TestAggregationExecution.COUNT_BY_CLASS_BIT})


class TestDateNodeRefs:
    """The SCHEMA.md datetime value shapes: { nodeId } points and
    { start, end } ranges over the deterministic date-chain ids."""

    DAY_A = chain_node_ids("1937-05-06")  # Paris published
    DAY_B = chain_node_ids("1900-01-15")  # France published
    YEAR_X = chain_node_ids("1889-03-31")  # Lone founded (year precision)

    @pytest.fixture
    def date_world(self, world: LocalStore) -> LocalStore:
        chain_text = text
        for chain in (self.DAY_A, self.DAY_B, self.YEAR_X):
            world.apply_remote(env("object.create", {"objectId": chain["year"], "contentAst": chain_text("1937")}, T0 + 48 * STEP))
            world.apply_remote(
                env(
                    "object.create",
                    {"objectId": chain["month"], "parentId": chain["year"], "contentAst": chain_text("1937-05")},
                    T0 + 48 * STEP,
                )
            )
            world.apply_remote(
                env(
                    "object.create",
                    {"objectId": chain["day"], "parentId": chain["month"], "contentAst": chain_text("1937-05-06")},
                    T0 + 48 * STEP,
                )
            )
        world.apply_remote(
            env("propertySchema.create", {"propertySchemaId": PUBLISHED, "name": "published", "type": "datetime"}, T0 + 50 * STEP)
        )
        world.apply_remote(
            env(
                "property.set",
                {"objectId": PARIS, "propertySchemaId": PUBLISHED, "value": {"nodeId": self.DAY_A["day"]}},
                T0 + 51 * STEP,
            )
        )
        world.apply_remote(
            env(
                "property.set",
                {"objectId": FRANCE, "propertySchemaId": PUBLISHED, "value": {"nodeId": self.DAY_B["day"]}},
                T0 + 52 * STEP,
            )
        )
        world.apply_remote(
            env(
                "propertySchema.create",
                {"propertySchemaId": FOUNDED, "name": "founded", "type": "datetime", "datePrecision": "year"},
                T0 + 53 * STEP,
            )
        )
        world.apply_remote(
            env(
                "property.set",
                {"objectId": LONE, "propertySchemaId": FOUNDED, "value": {"nodeId": self.YEAR_X["year"]}},
                T0 + 54 * STEP,
            )
        )
        return world

    def test_eq_contains_match_the_node_dated_that_day(self, date_world: LocalStore) -> None:
        conn = date_world._conn
        eq = lambda value: ast(ENTIRE, [{"type": "property", "schemaId": PUBLISHED, "op": "eq", "value": value}])  # noqa: E731
        assert list(run(conn, eq("1937-05-06")).ids) == [PARIS]
        assert list(
            run(conn, ast(ENTIRE, [{"type": "property", "schemaId": PUBLISHED, "op": "contains", "value": "1900-01-15"}])).ids
        ) == [FRANCE]
        # neq: nodes with a different value match; the equal node does not.
        assert list(run(conn, ast(ENTIRE, [{"type": "property", "schemaId": PUBLISHED, "op": "neq", "value": "1937-05-06"}])).ids) == [
            FRANCE
        ]
        assert list(run(conn, eq("2000-01-01")).ids) == []

    def test_a_year_precision_value_answers_a_query_for_any_date_of_that_year(self, date_world: LocalStore) -> None:
        conn = date_world._conn
        query = ast(ENTIRE, [{"type": "property", "schemaId": FOUNDED, "op": "eq", "value": "1889-06-01"}])
        assert list(run(conn, query).ids) == [LONE]
        assert list(
            run(conn, ast(ENTIRE, [{"type": "property", "schemaId": FOUNDED, "op": "eq", "value": "1890-01-01"}])).ids
        ) == []

    def test_relational_ops_order_by_the_embedded_date_payload(self, date_world: LocalStore) -> None:
        conn = date_world._conn
        published = lambda op, value: ast(ENTIRE, [{"type": "property", "schemaId": PUBLISHED, "op": op, "value": value}])  # noqa: E731
        assert list(run(conn, published("gt", "1900-01-15")).ids) == [PARIS]
        assert list(run(conn, published("gte", "1937-05-06")).ids) == [PARIS]
        # A year period compares at its start: 1889-01-01 < 1889-06-01 bound.
        assert list(
            run(conn, ast(ENTIRE, [{"type": "property", "schemaId": FOUNDED, "op": "lt", "value": "1889-06-01"}])).ids
        ) == [LONE]
        # Scalar (legacy/default) values still compare on the plain arm.
        assert ids_of(run(conn, ast(ENTIRE, [{"type": "property", "schemaId": OPENED, "op": "lte", "value": "1937-05-06"}]))) == sorted(
            [FRANCE, PARIS, LONE]
        )


class TestDatetimeRanges:
    SPAN_S = chain_node_ids("1937-05-01")
    SPAN_E = chain_node_ids("1937-05-10")
    OPEN_END = chain_node_ids("1900-01-20")
    TIMED_END = chain_node_ids("1920-06-15")

    @pytest.fixture
    def span_world(self, world: LocalStore) -> LocalStore:
        for chain in (self.SPAN_S, self.SPAN_E, self.OPEN_END, self.TIMED_END):
            year_label = chain["year"][24:28]
            month_label = chain["month"][24:30]
            day_label = chain["day"][24:32]
            world.apply_remote(
                env("object.create", {"objectId": chain["year"], "contentAst": text(year_label)}, T0 + 46 * STEP)
            )
            world.apply_remote(
                env(
                    "object.create",
                    {"objectId": chain["month"], "parentId": chain["year"], "contentAst": text(month_label)},
                    T0 + 46 * STEP,
                )
            )
            world.apply_remote(
                env(
                    "object.create",
                    {"objectId": chain["day"], "parentId": chain["month"], "contentAst": text(day_label)},
                    T0 + 46 * STEP,
                )
            )
        world.apply_remote(
            env("propertySchema.create", {"propertySchemaId": SPAN, "name": "span", "type": "datetime"}, T0 + 50 * STEP)
        )
        world.apply_remote(
            env(
                "property.set",
                {
                    "objectId": PARIS,
                    "propertySchemaId": SPAN,
                    "value": {"start": {"nodeId": self.SPAN_S["day"]}, "end": {"nodeId": self.SPAN_E["day"]}},
                },
                T0 + 51 * STEP,
            )
        )
        world.apply_remote(
            env(
                "property.set",
                {"objectId": FRANCE, "propertySchemaId": SPAN, "value": {"start": None, "end": {"nodeId": self.OPEN_END["day"]}}},
                T0 + 52 * STEP,
            )
        )
        world.apply_remote(
            env(
                "property.set",
                {"objectId": LONE, "propertySchemaId": SPAN, "value": {"start": None, "end": None}},
                T0 + 53 * STEP,
            )
        )
        return world

    def eq(self, value: str) -> dict[str, Any]:
        return ast(ENTIRE, [{"type": "property", "schemaId": SPAN, "op": "eq", "value": value}])

    def test_eq_matches_a_bound_within_start_end_outside_does_not(self, span_world: LocalStore) -> None:
        conn = span_world._conn
        # A bound inside the closed span (Paris) and on its edges; Lone's
        # both-open range is unbounded, so it answers every bound.
        assert ids_of(run(conn, self.eq("1937-05-05"))) == sorted([LONE, PARIS])
        assert ids_of(run(conn, self.eq("1937-05-01"))) == sorted([LONE, PARIS])
        assert ids_of(run(conn, self.eq("1937-05-10"))) == sorted([LONE, PARIS])
        assert ids_of(
            run(conn, ast(ENTIRE, [{"type": "property", "schemaId": SPAN, "op": "contains", "value": "1937-05-06"}]))
        ) == sorted([LONE, PARIS])
        # Just outside the closed span (Lone's open range still answers).
        assert list(run(conn, self.eq("1937-04-30")).ids) == [LONE]
        assert list(run(conn, self.eq("1937-05-11")).ids) == [LONE]
        assert ids_of(run(conn, self.eq("1800-01-01"))) == sorted([FRANCE, LONE])

    def test_an_open_start_range_matches_early_bounds(self, span_world: LocalStore) -> None:
        conn = span_world._conn
        assert ids_of(run(conn, self.eq("1800-06-01"))) == sorted([FRANCE, LONE])
        assert ids_of(run(conn, self.eq("1900-01-19"))) == sorted([FRANCE, LONE])
        assert list(run(conn, self.eq("1900-01-21")).ids) == [LONE]

    def test_relational_ops_compare_the_range_start_a_null_start_never_matches(self, span_world: LocalStore) -> None:
        conn = span_world._conn
        gte = lambda value: ast(ENTIRE, [{"type": "property", "schemaId": SPAN, "op": "gte", "value": value}])  # noqa: E731
        assert list(run(conn, gte("1937-05-01")).ids) == [PARIS]
        assert list(
            run(conn, ast(ENTIRE, [{"type": "property", "schemaId": SPAN, "op": "lt", "value": "1937-05-01"}])).ids
        ) == []
        assert list(
            run(conn, ast(ENTIRE, [{"type": "property", "schemaId": SPAN, "op": "gt", "value": "1937-05-01"}])).ids
        ) == []
        # Paris's start orders after France's END — the start payload is the comparator.
        assert list(run(conn, gte("1900-01-19")).ids) == [PARIS]

    def test_point_values_keep_matching_alongside_ranges(self, span_world: LocalStore) -> None:
        span_world.apply_remote(
            env(
                "property.set",
                {
                    "objectId": LONE,
                    "propertySchemaId": SPAN,
                    "value": {"nodeId": self.TIMED_END["day"], "time": "18:45"},
                },
                T0 + 54 * STEP,
            )
        )
        conn = span_world._conn
        # eq: the point's day contains the bound (time ignored).
        assert list(run(conn, self.eq("1920-06-15")).ids) == [LONE]
        assert list(run(conn, self.eq("1920-06-16")).ids) == []
        # relational: the point sorts with its day.
        assert ids_of(
            run(conn, ast(ENTIRE, [{"type": "property", "schemaId": SPAN, "op": "gt", "value": "1900-01-19"}]))
        ) == sorted([LONE, PARIS])


class TestNodeFieldPredicates:
    ASSET = "0192a000-0000-7000-8000-000000000901"
    MAIN = "0192a000-0000-7000-8000-000000000902"

    @pytest.fixture
    def fields_world(self, world: LocalStore) -> LocalStore:
        world.apply_remote(env("object.create", {"objectId": self.ASSET, "contentAst": text("Asset")}, T0 + 60 * STEP))
        world.apply_remote(env("object.create", {"objectId": self.MAIN, "contentAst": text("Main")}, T0 + 61 * STEP))
        # France: cover -> ASSET. Paris: banner -> ASSET + alias -> MAIN.
        world.apply_remote(env("object.update", {"objectId": FRANCE, "coverAssetId": self.ASSET}, T0 + 62 * STEP))
        world.apply_remote(
            env("object.update", {"objectId": PARIS, "bannerAssetId": self.ASSET, "aliasedNodeId": self.MAIN}, T0 + 63 * STEP)
        )
        return world

    def test_eq_exists_compile_into_the_node_columns_unset_matches_neither(self, fields_world: LocalStore) -> None:
        conn = fields_world._conn
        assert list(run(conn, ast(ENTIRE, [nf("coverAsset", "eq", self.ASSET)])).ids) == [FRANCE]
        assert list(run(conn, ast(ENTIRE, [nf("bannerAsset", "eq", self.ASSET)])).ids) == [PARIS]
        assert list(run(conn, ast(ENTIRE, [nf("aliasedNode", "eq", self.MAIN)])).ids) == [PARIS]
        assert list(run(conn, ast(ENTIRE, [nf("coverAsset", "exists")])).ids) == [FRANCE]
        assert list(run(conn, ast(ENTIRE, [nf("aliasedNode", "exists")])).ids) == [PARIS]
        # neq matches rows set to a DIFFERENT id; unset rows stay out (SQL NULL).
        assert list(run(conn, ast(ENTIRE, [nf("coverAsset", "neq", self.ASSET)])).ids) == []
        assert list(run(conn, ast(ENTIRE, [nf("coverAsset", "neq", self.MAIN)])).ids) == [FRANCE]
        # "is unset" reads not-exists: every active node except France.
        unset = run(conn, ast(ENTIRE, [{"type": "not", "child": nf("coverAsset", "exists")}]))
        assert FRANCE not in unset.ids
        assert PARIS in unset.ids
        assert LONE in unset.ids

    def test_eq_neq_require_a_value(self, fields_world: LocalStore) -> None:
        with pytest.raises(ValueError, match="requires a non-null value"):
            run(fields_world._conn, ast(ENTIRE, [nf("coverAsset", "eq")]))


class TestMemberClassOperatorSet:
    def exact(self, class_id: str) -> dict[str, Any]:
        return {"type": "member", "family": "class", "op": "exact", "target": {"mode": "ids", "ids": [class_id]}}

    def test_exact_the_class_set_is_exactly_the_picked_class(self, world: LocalStore) -> None:
        conn = world._conn
        assert list(run(conn, ast(ENTIRE, [self.exact(PLACE)])).ids) == [FRANCE]
        # Paris rides City (extends Place): exact {Place} does NOT match it.
        assert list(run(conn, ast(ENTIRE, [self.exact(CITY)])).ids) == [PARIS]

    def test_in_ids_any_of_membership_closure_aware(self, world: LocalStore) -> None:
        conn = world._conn
        assert ids_of(run(conn, ast(ENTIRE, [m_in("class", [PLACE])]))) == sorted([FRANCE, PARIS])
        assert ids_of(run(conn, ast(ENTIRE, [m_in("class", [PLACE, CITY])]))) == sorted([FRANCE, PARIS])

    def test_in_query_closure_aware_membership_of_any_matching_class(self, world: LocalStore) -> None:
        conn = world._conn
        result = run(conn, ast(ENTIRE, [m_in_q("class", [nf("isClass", "eq", "true")])]))
        assert ids_of(result) == sorted([FRANCE, PARIS])
        result = run(conn, ast(ENTIRE, [m_in_q("class", [{"type": "content", "op": "eq", "value": "City"}])]))
        assert list(result.ids) == [PARIS]
        negated = run(conn, ast(ENTIRE, [{"type": "not", "child": m_in_q("class", [nf("isClass", "eq", "true")])}]))
        assert FRANCE not in negated.ids
        assert PARIS not in negated.ids
        assert LONE in negated.ids

    def test_exists_the_class_set_is_non_empty(self, world: LocalStore) -> None:
        conn = world._conn
        result = run(conn, ast(ENTIRE, [m_exists("class")]))
        assert FRANCE in result.ids and PARIS in result.ids
        assert LONE not in result.ids and FR_BLOCK not in result.ids
        negated = run(conn, ast(ENTIRE, [{"type": "not", "child": m_exists("class")}]))
        assert FRANCE not in negated.ids and PARIS not in negated.ids
        assert LONE in negated.ids and FR_BLOCK in negated.ids


class TestHierarchyReferenceFamilies:
    def test_page_family_in_ids_a_page_row_matches_only_when_picked_itself(self, world: LocalStore) -> None:
        conn = world._conn
        assert ids_of(run(conn, ast(ENTIRE, [m_in("page", [FRANCE])]))) == sorted([FRANCE, FR_BLOCK, OUT_BLOCK])
        assert ids_of(run(conn, ast(ENTIRE, [m_in("page", [PARIS])]))) == sorted([PARIS, PARIS_BLOCK])
        assert ids_of(run(conn, ast(ENTIRE, [m_in("page", [LONE, PARIS])]))) == sorted(
            [LONE, LONE_BLOCK, PARIS, PARIS_BLOCK]
        )
        negated = run(conn, ast(ENTIRE, [{"type": "not", "child": m_in("page", [FRANCE])}]))
        assert FRANCE not in negated.ids
        assert PARIS_BLOCK in negated.ids
        assert LONE in negated.ids

    def test_page_family_in_query_the_subquery_informs_the_page_set(self, world: LocalStore) -> None:
        conn = world._conn
        assert ids_of(run(conn, ast(ENTIRE, [m_in_q("page", [m_in("class", [CITY])])]))) == sorted(
            [PARIS, PARIS_BLOCK]
        )
        assert ids_of(run(conn, ast(ENTIRE, [m_in_q("page", [m_in("class", [PLACE])])]))) == sorted(
            [FRANCE, FR_BLOCK, OUT_BLOCK, PARIS, PARIS_BLOCK]
        )

    def test_page_family_exists_the_row_lives_inside_a_page(self, world: LocalStore) -> None:
        result = run(world._conn, ast(ENTIRE, [m_exists("page")]))
        assert ids_of(result) == sorted([FR_BLOCK, LONE_BLOCK, OUT_BLOCK, PARIS_BLOCK])

    def test_parent_path_family_in_ids_self_excluded(self, world: LocalStore) -> None:
        conn = world._conn
        assert list(run(conn, ast(ENTIRE, [m_in("parentPath", [PARIS])])).ids) == [PARIS_BLOCK]
        assert list(run(conn, ast(ENTIRE, [m_in("parentPath", [CITY])])).ids) == []
        assert ids_of(run(conn, ast(ENTIRE, [m_in("parentPath", [FRANCE, PARIS])]))) == sorted(
            [FR_BLOCK, OUT_BLOCK, PARIS, PARIS_BLOCK]
        )
        negated = run(conn, ast(ENTIRE, [{"type": "not", "child": m_in("parentPath", [FRANCE])}]))
        assert FRANCE in negated.ids
        assert LONE in negated.ids
        assert PARIS not in negated.ids

    def test_parent_path_family_in_query_a_strict_ancestor_matches_the_group(self, world: LocalStore) -> None:
        result = run(world._conn, ast(ENTIRE, [m_in_q("parentPath", [m_in("class", [PLACE])])]))
        assert ids_of(result) == sorted([FR_BLOCK, OUT_BLOCK, PARIS, PARIS_BLOCK])

    def test_root_family_in_ids_the_workspace_root_ancestor(self, world: LocalStore) -> None:
        conn = world._conn
        assert ids_of(run(conn, ast(ENTIRE, [m_in("root", [FRANCE])]))) == sorted(
            [FRANCE, FR_BLOCK, OUT_BLOCK, PARIS, PARIS_BLOCK]
        )
        # The class nodes are workspace roots — picked, each matches itself.
        assert list(run(conn, ast(ENTIRE, [m_in("root", [CITY])])).ids) == [CITY]
        assert ids_of(run(conn, ast(ENTIRE, [m_in("root", [LONE])]))) == sorted([LONE, LONE_BLOCK])
        negated = run(conn, ast(ENTIRE, [{"type": "not", "child": m_in("root", [FRANCE])}]))
        assert LONE in negated.ids
        assert PARIS_BLOCK not in negated.ids

    def test_root_family_in_query_the_root_ancestor_matches_the_group(self, world: LocalStore) -> None:
        conn = world._conn
        assert ids_of(run(conn, ast(ENTIRE, [m_in_q("root", [m_in("class", [PLACE])])]))) == sorted(
            [FRANCE, FR_BLOCK, OUT_BLOCK, PARIS, PARIS_BLOCK]
        )
        assert list(run(conn, ast(ENTIRE, [m_in_q("root", [m_in("class", [CITY])])])).ids) == []

    def test_parent_family_in_ids_the_direct_parent(self, world: LocalStore) -> None:
        conn = world._conn
        assert ids_of(run(conn, ast(ENTIRE, [m_in("parent", [FRANCE])]))) == sorted([FR_BLOCK, OUT_BLOCK, PARIS])
        assert list(run(conn, ast(ENTIRE, [m_in("parent", [PARIS])])).ids) == [PARIS_BLOCK]
        assert list(run(conn, ast(ENTIRE, [m_in("parent", [LONE])])).ids) == [LONE_BLOCK]
        negated = run(conn, ast(ENTIRE, [{"type": "not", "child": m_in("parent", [FRANCE])}]))
        assert FRANCE in negated.ids
        assert PARIS not in negated.ids

    def test_parent_family_in_query_the_direct_parent_matches_the_group(self, world: LocalStore) -> None:
        result = run(world._conn, ast(ENTIRE, [m_in_q("parent", [m_in("class", [PLACE])])]))
        assert ids_of(result) == sorted([FR_BLOCK, OUT_BLOCK, PARIS, PARIS_BLOCK])

    def test_child_family_in_ids_a_direct_child_is_one_of_the_picked(self, world: LocalStore) -> None:
        conn = world._conn
        assert ids_of(run(conn, ast(ENTIRE, [m_in("child", [FR_BLOCK, PARIS_BLOCK])]))) == sorted([FRANCE, PARIS])
        assert list(run(conn, ast(ENTIRE, [m_in("child", [PARIS])])).ids) == [FRANCE]

    def test_child_family_in_query_a_direct_child_matches_the_group(self, world: LocalStore) -> None:
        result = run(world._conn, ast(ENTIRE, [m_in_q("child", [{"type": "content", "op": "contains", "value": "Capital"}])]))
        assert ids_of(result) == sorted([FRANCE, PARIS])

    def test_child_and_child_path_exists_the_set_bits(self, world: LocalStore) -> None:
        conn = world._conn
        assert ids_of(run(conn, ast(ENTIRE, [m_exists("child")]))) == sorted([FRANCE, LONE, PARIS])
        assert ids_of(run(conn, ast(ENTIRE, [m_exists("childPath")]))) == sorted([FRANCE, LONE, PARIS])

    def test_child_path_any_of_plus_the_parent_exists_bit(self, world: LocalStore) -> None:
        conn = world._conn
        assert ids_of(run(conn, ast(ENTIRE, [m_in("childPath", [FRANCE, LONE])]))) == sorted(
            [FR_BLOCK, LONE_BLOCK, OUT_BLOCK, PARIS, PARIS_BLOCK]
        )
        parent_exists = run(conn, ast(ENTIRE, [m_exists("parent")]))
        assert FRANCE not in parent_exists.ids
        assert LONE not in parent_exists.ids
        assert PARIS_BLOCK in parent_exists.ids

    def test_reference_path_family_in_ids_the_self_or_ancestor_rollup(self, world: LocalStore) -> None:
        conn = world._conn
        assert ids_of(run(conn, ast(ENTIRE, [m_in("referencePath", [PARIS])]))) == sorted([FR_BLOCK, PARIS_BLOCK])
        assert ids_of(run(conn, ast(ENTIRE, [m_in("referencePath", [LONE])]))) == sorted([LONE_BLOCK, OUT_BLOCK])
        negated = run(conn, ast(ENTIRE, [{"type": "not", "child": m_in("referencePath", [PARIS])}]))
        assert PARIS in negated.ids
        assert FR_BLOCK not in negated.ids

    def test_reference_path_family_in_query_the_targets_are_the_matches(self, world: LocalStore) -> None:
        result = run(world._conn, ast(ENTIRE, [m_in_q("referencePath", [m_in("class", [CITY])])]))
        assert ids_of(result) == sorted([FR_BLOCK, PARIS_BLOCK])


class TestTagsExecution:
    def test_tags_in_ids_and_the_exists_bit_over_the_or_set(self, store: LocalStore) -> None:
        tag_a = "0192a000-0000-7000-8000-000000000501"
        tag_b = "0192a000-0000-7000-8000-000000000502"
        t = lambda i: T0 + 100 * STEP + i * STEP  # noqa: E731
        for envelope in [
            env("object.create", {"objectId": tag_a, "contentAst": text("Urgent")}, t(0)),
            env("object.create", {"objectId": tag_b, "contentAst": text("Later")}, t(1)),
            env("object.create", {"objectId": FRANCE, "contentAst": text("France"), "tagIds": [tag_a]}, t(2)),
            env("object.create", {"objectId": PARIS, "contentAst": text("Paris"), "tagIds": [tag_a, tag_b]}, t(3)),
            env("object.create", {"objectId": LONE, "contentAst": text("Lone")}, t(4)),
        ]:
            store.apply_remote(envelope)
        conn = store._conn
        assert ids_of(run(conn, ast(ENTIRE, [m_in("tags", [tag_a])]))) == sorted([FRANCE, PARIS])
        assert list(run(conn, ast(ENTIRE, [m_in("tags", [tag_b])])).ids) == [PARIS]
        assert ids_of(run(conn, ast(ENTIRE, [m_in("tags", [tag_a, tag_b])]))) == sorted([FRANCE, PARIS])
        assert ids_of(run(conn, ast(ENTIRE, [m_exists("tags")]))) == sorted([FRANCE, PARIS])
        # The untagged: Lone plus the tag nodes themselves.
        assert ids_of(run(conn, ast(ENTIRE, [{"type": "not", "child": m_exists("tags")}]))) == sorted(
            [LONE, tag_a, tag_b]
        )
        # Dynamic tags: the nested group's matches ARE the tag nodes.
        assert ids_of(
            run(conn, ast(ENTIRE, [m_in_q("tags", [{"type": "content", "op": "eq", "value": "Urgent"}])]))
        ) == sorted([FRANCE, PARIS])
        assert list(run(conn, ast(ENTIRE, [m_in_q("tags", [{"type": "content", "op": "eq", "value": "Later"}])])).ids) == [
            PARIS
        ]
        assert list(run(conn, ast(ENTIRE, [m_in_q("tags", [{"type": "content", "op": "eq", "value": "Zebras"}])])).ids) == []

    def test_links_any_of_and_the_inbound_link_exists_bit(self, world: LocalStore) -> None:
        conn = world._conn
        assert ids_of(run(conn, ast(ENTIRE, [m_in("links", [PARIS, LONE])]))) == sorted([FR_BLOCK, OUT_BLOCK])
        assert ids_of(run(conn, ast(ENTIRE, [m_exists("links")]))) == sorted([LONE, PARIS])
        assert PARIS not in run(conn, ast(ENTIRE, [{"type": "not", "child": m_exists("links")}])).ids


class TestArchivedClassResolution:
    SOURCE_CLASS = "0192a000-0000-7000-8000-0000000006a1"
    BOOK_SECTION = "0192a000-0000-7000-8000-0000000006a2"
    ARTWORK = "0192a000-0000-7000-8000-0000000006a3"
    LATER_CLASS = "0192a000-0000-7000-8000-0000000006a4"
    CHAPTER = "0192a000-0000-7000-8000-0000000006b1"
    PAINTING = "0192a000-0000-7000-8000-0000000006b2"
    T0A = 1727300000000

    @pytest.fixture
    def archived_world(self, store: LocalStore) -> LocalStore:
        base = self.T0A
        for envelope in [
            env("class.create", {"classId": self.SOURCE_CLASS, "contentAst": text("Source")}, base),
            env("class.create", {"classId": self.BOOK_SECTION, "contentAst": text("Book section")}, base + 1000),
            env("class.create", {"classId": self.ARTWORK, "contentAst": text("Artwork")}, base + 2000),
            env("class.setExtends", {"classId": self.BOOK_SECTION, "parentClassIds": [self.SOURCE_CLASS]}, base + 3000),
            env("class.setExtends", {"classId": self.ARTWORK, "parentClassIds": [self.SOURCE_CLASS]}, base + 4000),
            # Archival: the visibility bit must not change what a class IS.
            env("object.update", {"objectId": self.BOOK_SECTION, "active": False}, base + 5000),
            # A LATER unrelated extends write rebuilds the whole closure.
            env("class.create", {"classId": self.LATER_CLASS, "contentAst": text("Tag")}, base + 6000),
            env("class.setExtends", {"classId": self.LATER_CLASS, "parentClassIds": [self.SOURCE_CLASS]}, base + 7000),
            env(
                "object.create",
                {"objectId": self.CHAPTER, "contentAst": text("A chapter"), "classIds": [self.BOOK_SECTION]},
                base + 8000,
            ),
            env(
                "object.create",
                {"objectId": self.PAINTING, "contentAst": text("A painting"), "classIds": [self.ARTWORK]},
                base + 9000,
            ),
        ]:
            store.apply_remote(envelope)
        return store

    def test_member_class_in_ids_matches_a_node_classed_by_an_archived_type(self, archived_world: LocalStore) -> None:
        result = run(archived_world._conn, ast(ENTIRE, [m_in("class", [self.BOOK_SECTION])]))
        assert list(result.ids) == [self.CHAPTER]

    def test_the_family_base_matches_archived_type_members_through_extends(self, archived_world: LocalStore) -> None:
        result = run(archived_world._conn, ast(ENTIRE, [m_in("class", [self.SOURCE_CLASS])]))
        assert ids_of(result) == sorted([self.CHAPTER, self.PAINTING])

    def test_any_of_matches_archived_type_members_closure_aware(self, archived_world: LocalStore) -> None:
        result = run(
            archived_world._conn,
            ast(ENTIRE, [m_in("class", [self.BOOK_SECTION, self.ARTWORK])]),
        )
        assert ids_of(result) == sorted([self.CHAPTER, self.PAINTING])

    def test_the_aggregate_class_dimension_counts_archived_type_members(self, archived_world: LocalStore) -> None:
        result = run_aggregate(
            archived_world._conn,
            parse_query_ast(
                {
                    **ast(ENTIRE, [m_in("class", [self.SOURCE_CLASS])]),
                    "aggregation": {
                        "dimensions": [{"kind": "class", "id": self.SOURCE_CLASS}],
                        "measures": [{"function": "count"}],
                    },
                }
            ),
            OPT,
        )
        assert result.columns == (f"class:{self.SOURCE_CLASS}", "count")
        assert [tuple(row) for row in result.rows] == [(1, 2)]


class TestParentlessRoleDemote:
    TASK = "0192a000-0000-7000-8000-000000000701"
    ERRAND = "0192a000-0000-7000-8000-000000000702"
    PROJECT = "0192a000-0000-7000-8000-000000000703"
    STANDALONE_TASK = "0192a000-0000-7000-8000-000000000711"
    STANDALONE_ERRAND = "0192a000-0000-7000-8000-000000000712"
    ROOT_PAGE = "0192a000-0000-7000-8000-000000000713"
    MAIN_CHILD = "0192a000-0000-7000-8000-000000000714"

    @pytest.fixture
    def role_world(self, store: LocalStore) -> LocalStore:
        t = lambda i: T0 + i * STEP  # noqa: E731
        for envelope in [
            env("class.create", {"classId": self.TASK, "contentAst": text("Task"), "parentlessRole": "block"}, t(0)),
            env("class.create", {"classId": self.ERRAND, "contentAst": text("Errand")}, t(1)),
            env("class.setExtends", {"classId": self.ERRAND, "parentClassIds": [self.TASK]}, t(2)),
            env("class.create", {"classId": self.PROJECT, "contentAst": text("Project")}, t(3)),
            env(
                "object.create",
                {"objectId": self.STANDALONE_TASK, "classIds": [self.TASK], "contentAst": text("Fix the sink")},
                t(4),
            ),
            # Errand carries NO own flag — the demote arrives through extends.
            env(
                "object.create",
                {"objectId": self.STANDALONE_ERRAND, "classIds": [self.ERRAND], "contentAst": text("Buy screws")},
                t(5),
            ),
            env("object.create", {"objectId": self.ROOT_PAGE, "contentAst": text("Root page")}, t(6)),
            env(
                "object.create",
                {
                    "objectId": self.MAIN_CHILD,
                    "parentId": self.ROOT_PAGE,
                    "isEmbedded": False,
                    "contentAst": text("Main child"),
                },
                t(7),
            ),
        ]:
            store.apply_remote(envelope)
        return store

    def test_pages_scope_excludes_flagged_parentless_nodes_keeps_real_pages(self, role_world: LocalStore) -> None:
        result = run(role_world._conn, all_in(PAGES))
        assert ids_of(result) == sorted([self.MAIN_CHILD, self.ROOT_PAGE])

    def test_page_family_a_block_role_standalone_node_has_no_containing_page(self, role_world: LocalStore) -> None:
        conn = role_world._conn
        # A standalone-claim row IS a page — it reads its own page.
        assert list(run(conn, ast(ENTIRE, [m_in("page", [self.MAIN_CHILD])])).ids) == [self.MAIN_CHILD]
        assert list(run(conn, ast(ENTIRE, [m_in("page", [self.ROOT_PAGE])])).ids) == [self.ROOT_PAGE]
        # The block-role standalone tasks' containing page is NULL.
        assert list(run(conn, ast(ENTIRE, [m_in("page", [self.STANDALONE_TASK])])).ids) == []
        assert list(run(conn, ast(ENTIRE, [m_in("page", [self.STANDALONE_ERRAND])])).ids) == []


class TestStoreSeam:
    """The LocalStore.run_query_ast / run_aggregate_ast surface: parse +
    compile + execute under the store lock, over the private connection."""

    def test_run_query_ast_parses_compiles_and_executes(self, world: LocalStore) -> None:
        result = world.run_query_ast(WS, ast(ENTIRE, [{"type": "content", "op": "contains", "value": "capital"}]))
        assert result.ids == tuple(sorted([FR_BLOCK, PARIS_BLOCK]))

    def test_run_query_ast_rejects_v1_and_malformed_shapes(self, world: LocalStore) -> None:
        from notees_gtk.core.protocol.query_ast import QueryAstError

        with pytest.raises(QueryAstError):
            world.run_query_ast(WS, {"version": 1, "scope": PAGES, "root": {"type": "group", "logic": "and", "children": []}})

    def test_self_targets_fail_loud_without_the_hosting_page(self, world: LocalStore) -> None:
        with pytest.raises(ValueError, match="selfNodeId"):
            world.run_query_ast(WS, ast(ENTIRE, [m_self("links")]))

    def test_run_aggregate_ast_returns_the_grouped_grid(self, world: LocalStore) -> None:
        result = world.run_aggregate_ast(
            WS,
            {**all_in(PAGES), "aggregation": {"dimensions": [{"kind": "isClass"}], "measures": [{"function": "count"}]}},
        )
        assert result.columns == ("isClass", "count")
        assert [tuple(row) for row in result.rows] == [(0, 3)]
