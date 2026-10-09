"""Acceptance suite: replay the vendored protocol fixtures through the store.

This is the GTK client's half of the cross-implementation fixture gate: the
same envelopes the monorepo's store tests replay (``packages/store/test/
store.test.ts``) must converge to the same derived state here. The cycle
fixture is deliberately excluded from the all-fixtures replay — its final two
envelopes close cycles and MUST throw CycleError on apply (see
``TestCycleFixture``).

Expected outcomes mirror the monorepo assertions: fixture LWW winner, move
positions, closure rows, idempotent replay.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from notees_gtk.core.protocol.clock import Hlc
from notees_gtk.core.protocol.models import RelayEnvelope
from notees_gtk.data.errors import CycleError
from notees_gtk.data.store import (
    EffectiveProperty,
    EffectivePropertySchema,
    LocalStore,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "wire"

#: The cycle fixture must throw — never part of the replay/determinism sets.
REPLAY_EXCLUDED = {"class-extends-cycle.json"}

WS = "0192a000-0000-7000-8000-000000000001"
NODE_PAGE = "0192a000-0000-7000-8000-000000000010"
NODE_BOOK = "0192a000-0000-7000-8000-000000000011"
NODE_BLOCK = "0192a000-0000-7000-8000-000000000020"  # block under NODE_PAGE (typed-link fixtures)
PROP_SCHEMA = "0192a000-0000-7000-8000-0000000000a1"
BOOK_CLASS = "00000000-0000-0000-0001-000000000025"

#: object-move.json ids.
MOVE_P = "0192a000-0000-7000-8000-000000000020"
MOVE_A = "0192a000-0000-7000-8000-000000000021"
MOVE_B = "0192a000-0000-7000-8000-000000000022"
MOVE_C = "0192a000-0000-7000-8000-000000000023"

#: object-move-before.json ids (mirrors the monorepo store test).
MOVE_BEFORE_P = "0192a000-0000-7000-8000-000000000140"
MOVE_BEFORE_A = "0192a000-0000-7000-8000-000000000141"
MOVE_BEFORE_B = "0192a000-0000-7000-8000-000000000142"
MOVE_BEFORE_C = "0192a000-0000-7000-8000-000000000143"
MOVE_BEFORE_D = "0192a000-0000-7000-8000-000000000144"

#: class-extends-cycle.json ids.
CYCLE_ROOT = "0192a000-0000-7000-8000-0000000000d1"
CYCLE_LEAF = "0192a000-0000-7000-8000-0000000000d2"


def load_fixture(name: str) -> list[dict[str, Any]]:
    raw = json.loads((FIXTURES_DIR / name).read_text())
    envelopes = raw.get("envelopes") if isinstance(raw, dict) else None
    return envelopes if isinstance(envelopes, list) else [raw]


def all_fixture_envelopes() -> list[dict[str, Any]]:
    names = sorted(path.name for path in FIXTURES_DIR.glob("*.json") if path.name not in REPLAY_EXCLUDED)
    return [envelope for name in names for envelope in load_fixture(name)]


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    instance = LocalStore(tmp_path / "store.db")
    yield instance
    instance.close()


def raw(store: LocalStore, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(store._conn.execute("PRAGMA database_list").fetchone()[2]) as conn:  # noqa: SLF001
        return conn.execute(sql, params).fetchall()


#: Derived tables racing-toggle convergence compares (the GTK dumpDb
#: analogue): everything the feature/property appliers write.
_CONVERGENCE_TABLES = (
    "nodes",
    "class",
    "class_member_set",
    "class_property",
    "class_hierarchy",
    "property_schema",
    "property_value",
    "property_value_tombstone",
    "property_value_element_tombstone",
    "workspace_feature",
)


def _dump_convergence_tables(store: LocalStore) -> list[tuple[str, list[tuple[Any, ...]]]]:
    """The derived-state dump the both-orders convergence tests compare —
    every table the feature/property appliers write, each SELECT ordered by
    all its columns so the comparison is byte-deterministic."""
    dumps: list[tuple[str, list[tuple[Any, ...]]]] = []
    for table in _CONVERGENCE_TABLES:
        columns = [row[1] for row in raw(store, f"PRAGMA table_info({table})")]
        order = ", ".join(str(index + 1) for index in range(len(columns)))
        dumps.append((table, raw(store, f"SELECT * FROM {table} ORDER BY {order}")))
    return dumps


def base_store(tmp_path: Path) -> LocalStore:
    """The monorepo tests seed two pages (plus the typed-link block) before
    replaying most fixtures."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    instance = LocalStore(tmp_path / "base.db")
    seeds = [
        ({"objectId": NODE_PAGE, "classIds": []}, 1727200000000),
        ({"objectId": NODE_BOOK, "classIds": []}, 1727200001000),
        ({"objectId": NODE_BLOCK, "parentId": NODE_PAGE}, 1727200000500),
    ]
    for payload, physical in seeds:
        envelope = RelayEnvelope.model_validate(
            {
                "protocolVersion": 3,
                "workspaceId": WS,
                "actorId": "0192a000-0000-7000-8000-000000000002",
                "deviceId": "fixture-test-device",
                "hlc": {"physical": physical, "logical": 0},
                "opType": "object.create",
                "timestamp": "2026-09-24T12:00:00.000Z",
                "payload": payload,
            }
        )
        assert instance.apply_remote(envelope) is True
    return instance


class TestObjectCreateFixtures:
    def test_pages_land_with_content_and_or_set_class_ids(self, store: LocalStore) -> None:
        for envelope in load_fixture("envelope-minimal.json") + load_fixture("object-create.json"):
            assert store.apply_remote(RelayEnvelope.model_validate(envelope)) is True
        page = store.node(WS, NODE_PAGE)
        book = store.node(WS, NODE_BOOK)
        assert page is not None and (page.is_class, page.present_as_main) == (False, True)
        assert book is not None and (book.is_class, book.present_as_main) == (False, True)
        # Title-is-content: the fixture's title rides as a text token of the
        # node's content (no name field on the wire).
        assert book.content == json.dumps([{"type": "text", "text": "The Structure of Scientific Revolutions"}])
        assert book.content_plain == "The Structure of Scientific Revolutions"
        # classIds seed the OR-Set membership, projected into node.class_ids.
        assert book.class_ids == (BOOK_CLASS,)


class TestPropertyLwwFixture:
    def test_converges_to_the_higher_hlc_phone_value(self, tmp_path: Path) -> None:
        laptop, phone = (RelayEnvelope.model_validate(item) for item in load_fixture("property-set-lww.json"))
        expected = (
            json.dumps({"nodeId": "0192a000-0000-7000-8000-000000000031"}),
            json.dumps({"since": "1963"}),
            "0192a000-0000-7000-8000-000000000003",
        )

        forward = base_store(tmp_path)
        forward.apply_remote(laptop)
        forward.apply_remote(phone)
        backward = base_store(tmp_path / "backward")
        backward.apply_remote(phone)
        backward.apply_remote(laptop)

        for instance in (forward, backward):
            assert raw(
                instance,
                "SELECT value, metadata, actor_id FROM property_value"
                " WHERE node_id = ? AND property_schema_id = ? AND idx = 0",
                (NODE_BOOK, PROP_SCHEMA),
            ) == [expected]
            instance.close()


class TestTypedLinkFixtures:
    def test_mark_then_deleted_lands_the_final_content(self, tmp_path: Path) -> None:
        instance = base_store(tmp_path)
        for name in ("typed-link-mark.json", "typed-link-mark-deleted.json"):
            for envelope in load_fixture(name):
                assert instance.apply_remote(RelayEnvelope.model_validate(envelope)) is True
        content = json.loads(instance.node(WS, NODE_BLOCK).content or "[]")
        assert content == [{"type": "text", "text": "Kuhn cites earlier work."}]
        assert instance.node(WS, NODE_BLOCK).content_plain == "Kuhn cites earlier work."
        instance.close()

    def test_content_updates_converge_regardless_of_order(self, tmp_path: Path) -> None:
        mark = RelayEnvelope.model_validate(load_fixture("typed-link-mark.json")[0])
        deleted = RelayEnvelope.model_validate(load_fixture("typed-link-mark-deleted.json")[0])
        first = base_store(tmp_path)
        first.apply_remote(mark)
        first.apply_remote(deleted)
        second = base_store(tmp_path / "second")
        second.apply_remote(deleted)
        second.apply_remote(mark)
        assert first.node(WS, NODE_BLOCK).content == second.node(WS, NODE_BLOCK).content
        assert json.loads(first.node(WS, NODE_BLOCK).content or "[]") == [
            {"type": "text", "text": "Kuhn cites earlier work."}
        ]
        first.close()
        second.close()


class TestObjectMoveFixture:
    def test_replay_lands_c_under_a_with_expected_positions(self, store: LocalStore) -> None:
        for envelope in load_fixture("object-move.json"):
            assert store.apply_remote(RelayEnvelope.model_validate(envelope)) is True
        assert store.node(WS, MOVE_C).parent_id == MOVE_A
        assert [row.id for row in store.children(WS, MOVE_P)] == [MOVE_A, MOVE_B]
        assert [row.id for row in store.children(WS, MOVE_A)] == [MOVE_C]
        # Exactly one child_order row per node — no dual-parent residue.
        assert raw(
            store,
            "SELECT child_id, position FROM node_child_order WHERE parent_id = ? ORDER BY position",
            (MOVE_P,),
        ) == [(MOVE_A, "a"), (MOVE_B, "aa")]
        assert raw(store, "SELECT COUNT(*) FROM node_child_order WHERE child_id = ?", (MOVE_C,)) == [(1,)]


class TestObjectMoveBeforeFixture:
    def test_replay_lands_b_c_d_a_with_one_child_order_row_each(self, store: LocalStore) -> None:
        for envelope in load_fixture("object-move-before.json"):
            assert store.apply_remote(RelayEnvelope.model_validate(envelope)) is True
        assert [row.id for row in store.children(WS, MOVE_BEFORE_P)] == [
            MOVE_BEFORE_B,
            MOVE_BEFORE_C,
            MOVE_BEFORE_D,
            MOVE_BEFORE_A,
        ]
        # Exactly one child_order row per node — no dual-parent residue.
        for child in (MOVE_BEFORE_A, MOVE_BEFORE_B, MOVE_BEFORE_C, MOVE_BEFORE_D):
            assert raw(store, "SELECT COUNT(*) FROM node_child_order WHERE child_id = ?", (child,)) == [(1,)]


class TestClassExtendsFixture:
    def test_m2m_lands_the_child_under_both_ancestors(self, store: LocalStore) -> None:
        for envelope in load_fixture("class-extends-m2m.json"):
            assert store.apply_remote(RelayEnvelope.model_validate(envelope)) is True
        a = "0192a000-0000-7000-8000-0000000000c5"
        b = "0192a000-0000-7000-8000-0000000000c6"
        c = "0192a000-0000-7000-8000-0000000000c7"
        edges = raw(
            store, "SELECT parent_class_id FROM class_extends WHERE class_id = ? ORDER BY parent_class_id", (c,)
        )
        assert edges == [(a,), (b,)]
        ancestors = raw(store, "SELECT ancestor_id FROM class_hierarchy WHERE class_id = ? ORDER BY ancestor_id", (c,))
        assert ancestors == [(a,), (b,), (c,)]
        for parent in (a, b):
            descendants = raw(
                store, "SELECT class_id FROM class_hierarchy WHERE ancestor_id = ? ORDER BY class_id", (parent,)
            )
            assert (c,) in descendants


class TestClassPropertyDefaultsFixture:
    """Replay of class-property-defaults.json with intermediate states
    (mirrors the monorepo store test): default applies → multi-class conflict
    resolves first-applied-wins → unset flips the winner."""

    PRIORITY = "0192a000-0000-7000-8000-000000000301"
    TASK = "0192a000-0000-7000-8000-000000000302"
    PROJECT = "0192a000-0000-7000-8000-000000000303"
    ITEM = "0192a000-0000-7000-8000-000000000304"

    def _load(self) -> list[RelayEnvelope]:
        return [RelayEnvelope.model_validate(item) for item in load_fixture("class-property-defaults.json")]

    def test_prefix_5_node_reads_task_default(self, store: LocalStore) -> None:
        envelopes = self._load()
        for env in envelopes[:5]:
            assert store.apply_remote(env) is True
        effective = store.get_effective_properties(self.ITEM)
        assert effective == [
            EffectiveProperty(
                property_schema_id=self.PRIORITY,
                idx=0,
                element_id=f"default:{self.PRIORITY}:0",
                schema=EffectivePropertySchema(id=self.PRIORITY, name="priority", type="select", multi=False),
                value="medium",
                metadata=None,
                source="default",
                bound_by=self.TASK,
                required=None,
                readonly=None,
                hide_when_empty=None,
                sequence=0,
            )
        ]
        # The default is DERIVED: no property_value row was ever written.
        assert raw(store, "SELECT COUNT(*) FROM property_value WHERE node_id = ?", (self.ITEM,)) == [(0,)]

    def test_prefix_7_first_applied_class_wins_the_conflict(self, store: LocalStore) -> None:
        envelopes = self._load()
        for env in envelopes[:7]:
            store.apply_remote(env)  # envelope 7 is a re-create: tree no-op by design
        # Task's membership add HLC (envelope 4) precedes Project's
        # (envelope 7): Task's 'medium' beats Project's 'high'.
        effective = store.get_effective_properties(self.ITEM)
        assert [(row.value, row.source, row.bound_by) for row in effective] == [("medium", "default", self.TASK)]

    def test_full_8_unset_flips_the_winner(self, store: LocalStore) -> None:
        envelopes = self._load()
        for env in envelopes:
            store.apply_remote(env)
        effective = store.get_effective_properties(self.ITEM)
        assert [(row.value, row.source, row.bound_by) for row in effective] == [("high", "default", self.PROJECT)]


class TestClassUnassignFixture:
    """Replay of class-unassign.json with intermediate states (mirrors the
    monorepo store test): unassign drops the derived default and empties
    class_ids, the authored value survives unbound, and the re-issued
    object.create re-assigns (add-wins, newer HLC) restoring both."""

    EFFORT = "0192a000-0000-7000-8000-000000000410"
    IMPACT = "0192a000-0000-7000-8000-000000000411"
    TASK = "0192a000-0000-7000-8000-000000000412"
    ITEM = "0192a000-0000-7000-8000-000000000413"

    def _load(self) -> list[RelayEnvelope]:
        return [RelayEnvelope.model_validate(item) for item in load_fixture("class-unassign.json")]

    def test_prefix_7_derived_default_plus_authored_shadow(self, store: LocalStore) -> None:
        envelopes = self._load()
        for env in envelopes[:7]:
            store.apply_remote(env)
        effective = store.get_effective_properties(self.ITEM)
        assert [(row.property_schema_id, row.value, row.source, row.bound_by) for row in effective] == [
            (self.EFFORT, "xs", "default", self.TASK),
            (self.IMPACT, "authored", "authored", self.TASK),
        ]

    def test_unassign_drops_default_and_empties_class_ids_authored_survives_unbound(self, store: LocalStore) -> None:
        envelopes = self._load()
        for env in envelopes[:8]:
            store.apply_remote(env)
        row = store.node(WS, self.ITEM)
        assert row is not None and row.class_ids == ()
        effective = store.get_effective_properties(self.ITEM)
        assert [(row.property_schema_id, row.value, row.source, row.bound_by) for row in effective] == [
            (self.IMPACT, "authored", "authored", None),
        ]

    def test_reassign_restores_default_and_binding(self, store: LocalStore) -> None:
        envelopes = self._load()
        for env in envelopes:
            store.apply_remote(env)
        row = store.node(WS, self.ITEM)
        assert row is not None and row.class_ids == (self.TASK,)
        effective = store.get_effective_properties(self.ITEM)
        assert [(row.property_schema_id, row.value, row.source, row.bound_by) for row in effective] == [
            (self.EFFORT, "xs", "default", self.TASK),
            (self.IMPACT, "authored", "authored", self.TASK),
        ]


class TestObjectWireFieldsFixture:
    """Replay of object-wire-fields.json (M27 + the 2026-10-09 page-subtitle
    batch): the wire node fields land set-then-clear through object.update —
    presence writes, present-null clears — and an absent field is never a
    write (mirrors the monorepo store test's fixture replay)."""

    PAGE = "0192a000-0000-7000-8000-00000000052a"
    ASSET = "0192a000-0000-7000-8000-00000000052b"
    MAIN = "0192a000-0000-7000-8000-00000000052c"

    def _load(self) -> list[RelayEnvelope]:
        return [RelayEnvelope.model_validate(item) for item in load_fixture("object-wire-fields.json")]

    def test_the_fixture_lands_set_then_clear_through_object_update(self, store: LocalStore) -> None:
        create_page, create_asset, create_main, *updates = self._load()
        assert store.apply_remote(create_page) is True
        assert store.apply_remote(create_asset) is True
        assert store.apply_remote(create_main) is True
        row = store.node(WS, self.PAGE)
        assert row is not None
        assert (row.cover_asset_id, row.banner_asset_id, row.aliased_node_id) == (None, None, None)
        assert row.description is None
        assert store.apply_remote(updates[0]) is True  # coverAssetId = ASSET
        assert store.node(WS, self.PAGE).cover_asset_id == self.ASSET
        assert store.apply_remote(updates[1]) is True  # bannerAssetId = ASSET, aliasedNodeId = MAIN
        row = store.node(WS, self.PAGE)
        assert (row.banner_asset_id, row.aliased_node_id) == (self.ASSET, self.MAIN)
        assert store.apply_remote(updates[2]) is True  # aliasedNodeId = null (clear)
        row = store.node(WS, self.PAGE)
        assert row.aliased_node_id is None and row.cover_asset_id == self.ASSET
        assert store.apply_remote(updates[3]) is True  # coverAssetId = null
        row = store.node(WS, self.PAGE)
        assert (row.cover_asset_id, row.banner_asset_id) == (None, self.ASSET)
        assert store.apply_remote(updates[4]) is True  # bannerAssetId = null
        row = store.node(WS, self.PAGE)
        assert (row.cover_asset_id, row.banner_asset_id, row.aliased_node_id) == (None, None, None)
        assert store.apply_remote(updates[5]) is True  # description = "Subtitle text"
        assert store.node(WS, self.PAGE).description == "Subtitle text"
        assert store.apply_remote(updates[6]) is True  # description = null (clear)
        assert store.node(WS, self.PAGE).description is None

    def test_the_fields_are_columns_on_the_derived_node_table(self, store: LocalStore) -> None:
        """The wire fields project as derived node columns (store schema
        v14, web v18 parity) — the NodeRow read surfaces them."""
        for envelope in self._load():
            store.apply_remote(envelope)
        columns = {row[1] for row in raw(store, "PRAGMA table_info(nodes)")}
        assert {"cover_asset_id", "banner_asset_id", "aliased_node_id", "description"} <= columns


class TestClassConvertFixture:
    """Replay of class-convert.json (M47): class.create on an EXISTING node
    DECLARES it a class — the identity bit flips, a parented node is cut to
    a root (parent edge + child-order row drop), the render bit clears, the
    registry adopts the existing title, re-declaration is a replace no-op,
    and a fresh id still declares a brand-new class (mirrors the monorepo
    store test)."""

    PAGE = "0192a000-0000-7000-8000-00000000053a"
    RACK = "0192a000-0000-7000-8000-00000000053b"
    SHELF = "0192a000-0000-7000-8000-00000000053c"
    FRESH = "0192a000-0000-7000-8000-00000000053d"

    def _load(self) -> list[RelayEnvelope]:
        return [RelayEnvelope.model_validate(item) for item in load_fixture("class-convert.json")]

    def test_declares_an_existing_parentless_page_a_class_the_title_rides(self, store: LocalStore) -> None:
        page_create, _rack, _shelf, convert_page = self._load()[:4]
        assert store.apply_remote(page_create) is True
        assert store.apply_remote(convert_page) is True
        row = store.node(WS, self.PAGE)
        assert row is not None
        assert (row.is_class, row.present_as_main, row.parent_id) == (True, False, None)
        # Title-is-content: the node's existing text content is the class
        # title (the conversion payload carried no contentAst).
        assert json.loads(row.content or "[]") == [{"type": "text", "text": "Genre collection"}]
        # The registry row adopted the title + the hierarchy self-row landed.
        assert raw(store, "SELECT name, active FROM class WHERE id = ?", (self.PAGE,)) == [
            ("Genre collection", 1)
        ]
        assert raw(
            store, "SELECT 1 FROM class_hierarchy WHERE class_id = ? AND ancestor_id = ?", (self.PAGE, self.PAGE)
        ) == [(1,)]

    def test_converting_a_parented_node_cuts_it_to_a_root(self, store: LocalStore) -> None:
        _page, rack_create, shelf_create, _convert_page, convert_shelf = self._load()[:5]
        assert store.apply_remote(rack_create) is True
        assert store.apply_remote(shelf_create) is True
        assert [row.id for row in store.children(WS, self.RACK)] == [self.SHELF]
        assert store.apply_remote(convert_shelf) is True
        row = store.node(WS, self.SHELF)
        assert row is not None and (row.is_class, row.parent_id) == (True, None)
        assert store.children(WS, self.RACK) == []
        assert raw(store, "SELECT COUNT(*) FROM node_child_order WHERE child_id = ?", (self.SHELF,)) == [(0,)]

    def test_redeclaration_is_a_replace_noop_and_fresh_declaration_keeps_working(self, store: LocalStore) -> None:
        page_create, _rack, _shelf, convert_page, _convert_shelf, redeclare, fresh_create = self._load()
        assert store.apply_remote(page_create) is True
        assert store.apply_remote(convert_page) is True
        content_before = store.node(WS, self.PAGE).content
        assert store.apply_remote(redeclare) is True
        # Absent fields preserve on re-declaration: the content rides untouched.
        assert store.node(WS, self.PAGE).content == content_before
        assert store.apply_remote(fresh_create) is True
        fresh = store.node(WS, self.FRESH)
        assert fresh is not None and fresh.is_class is True
        assert json.loads(fresh.content or "[]") == [{"type": "text", "text": "Fresh genre"}]


class TestPropertyAssetTypeFixture:
    """Replay of property-asset-type.json (M38): asset-typed schemas land
    (multi class-scoped + single object-scoped) and a name-only update
    coexists on the single-value schema (mirrors the monorepo store test)."""

    def test_the_fixture_lands_asset_typed_schemas_and_the_update_coexists(self, store: LocalStore) -> None:
        for envelope in load_fixture("property-asset-type.json"):
            assert store.apply_remote(RelayEnvelope.model_validate(envelope)) is True
        assert raw(
            store,
            "SELECT id, type, multi, scope, name FROM property_schema ORDER BY id",
        ) == [
            ("0192a000-0000-7000-8000-000000000541", "asset", 1, "class", "Attachment"),
            ("0192a000-0000-7000-8000-000000000542", "asset", 0, "object", "Cover file (renamed)"),
        ]


class TestObjectColorFixture:
    """Replay of object-color.json (token | #hex | null-clear):
    object.update applies the preset token, then the custom hex, then null
    as a clear; class.create carries a token color onto BOTH the node row
    and the registry row, and class.update null clears both."""

    PAGE = "0192a000-0000-7000-8000-000000000510"
    CLASS = "0192a000-0000-7000-8000-000000000511"

    def _load(self) -> list[RelayEnvelope]:
        return [RelayEnvelope.model_validate(item) for item in load_fixture("object-color.json")]

    def test_object_update_applies_token_hex_then_null_clear(self, store: LocalStore) -> None:
        create, token_update, hex_update, clear_update, *_ = self._load()
        assert store.apply_remote(create) is True
        assert store.apply_remote(token_update) is True
        assert store.node(WS, self.PAGE).color == "sky"
        assert store.apply_remote(hex_update) is True
        assert store.node(WS, self.PAGE).color == "#123abc"
        assert store.apply_remote(clear_update) is True
        assert store.node(WS, self.PAGE).color is None

    def test_class_create_token_lands_on_both_rows_update_null_clears_both(self, store: LocalStore) -> None:
        envelopes = self._load()
        create, class_create, class_update = envelopes[0], envelopes[4], envelopes[5]
        assert store.apply_remote(create) is True
        assert store.apply_remote(class_create) is True
        assert store.node(WS, self.CLASS).color == "pink"
        assert raw(store, "SELECT color FROM class WHERE id = ?", (self.CLASS,)) == [("pink",)]
        assert store.apply_remote(class_update) is True
        assert store.node(WS, self.CLASS).color is None
        assert raw(store, "SELECT color FROM class WHERE id = ?", (self.CLASS,)) == [(None,)]


class TestWorkspaceFeatureSetFixture:
    """Replay of workspace-feature-set.json: the racing
    tasks toggles resolve LWW to the higher-HLC phone disable, events
    disables, sources re-enables — and the LOSING laptop enable still ran the
    task-family ensure on every enable payload, so both delivery orders
    converge byte-identical (the archival bits normalize to the current row
    state on every path)."""

    TASKS_DISABLE = "0192a000-0000-7000-8000-000000000602"
    TASK_CLASS = "00000000-0000-0000-0001-000000000012"

    def _load(self) -> list[RelayEnvelope]:
        return [RelayEnvelope.model_validate(item) for item in load_fixture("workspace-feature-set.json")]

    def test_racing_toggles_resolve_lww_and_the_family_ensure_rides_every_enable(self, tmp_path: Path) -> None:
        forward = LocalStore(tmp_path / "forward.db")
        backward = LocalStore(tmp_path / "backward.db")
        envelopes = self._load()
        for env in envelopes:
            assert forward.apply_remote(env) is True
        for env in reversed(envelopes):
            # Reversed delivery drops the lower-HLC racing toggles by LWW —
            # convergence, not a bug.
            backward.apply_remote(env)

        for instance in (forward, backward):
            assert instance.is_feature_enabled(WS, "tasks") is False
            assert instance.is_feature_enabled(WS, "events") is False
            assert instance.is_feature_enabled(WS, "sources") is True
            # Absent rows read enabled (F2).
            assert instance.is_feature_enabled(WS, "meetings") is True
            assert instance.is_feature_enabled(WS, "persons") is True
            # The winning rows carry the higher-HLC causality.
            assert raw(
                instance,
                "SELECT feature, enabled FROM workspace_feature ORDER BY feature",
            ) == [("events", 0), ("sources", 1), ("tasks", 0)]
            # The losing enable still authored the family: the task class and
            # its six schemas exist, archived per the winning disable.
            assert raw(instance, "SELECT active FROM class WHERE id = ?", (self.TASK_CLASS,)) == [(0,)]
            assert raw(
                instance,
                "SELECT is_active FROM nodes WHERE id = ? AND is_class = 1",
                (self.TASK_CLASS,),
            ) == [(0,)]
            assert raw(instance, "SELECT COUNT(*) FROM property_schema WHERE type IN ('select', 'date')") == [(6,)]
            assert raw(instance, "SELECT COUNT(*) FROM class_property WHERE class_id = ?", (self.TASK_CLASS,)) == [(6,)]

        # The flips are pure active-bit projections: both orders converge
        # byte-identical on every derived table the toggles touch.
        assert _dump_convergence_tables(forward) == _dump_convergence_tables(backward)
        forward.close()
        backward.close()


class TestClassDeleteManagedFixture:
    """Replay of class-delete-managed.json (F4): the
    class.delete on the managed task BASE class routes to the toggle as a
    feature-disable — membership pairs survive untouched — and the racing
    re-enable wins the (workspace, tasks) slot under either delivery
    order."""

    TASK_CLASS = "00000000-0000-0000-0001-000000000012"
    MEMBER = "0192a000-0000-7000-8000-000000000615"

    def _load(self) -> list[RelayEnvelope]:
        return [RelayEnvelope.model_validate(item) for item in load_fixture("class-delete-managed.json")]

    def test_routed_delete_keeps_memberships_and_the_reenable_wins(self, store: LocalStore) -> None:
        for env in self._load():
            assert store.apply_remote(env) is True
        assert store.is_feature_enabled(WS, "tasks") is True
        assert store.node(WS, self.TASK_CLASS).is_active is True
        assert raw(store, "SELECT active FROM class WHERE id = ?", (self.TASK_CLASS,)) == [(1,)]
        # The membership pair survived the routed delete untouched (F3).
        assert store.node(WS, self.MEMBER).class_ids == (self.TASK_CLASS,)
        assert raw(
            store,
            "SELECT present FROM class_member_set WHERE node_id = ? AND class_id = ?",
            (self.MEMBER, self.TASK_CLASS),
        ) == [(1,)]

    def test_routed_delete_and_racing_reenable_converge_under_either_order(self, tmp_path: Path) -> None:
        envelopes = self._load()
        base, routed_delete, reenable = envelopes[:2], envelopes[2], envelopes[3]
        forward = LocalStore(tmp_path / "f.db")
        backward = LocalStore(tmp_path / "b.db")
        for env in [*base, routed_delete, reenable]:
            assert forward.apply_remote(env) is True
        for env in [*base, reenable, routed_delete]:
            # Reversed: the delete (@11200) loses the (ws, tasks) slot to the
            # already-standing re-enable (@11300) and drops by LWW.
            backward.apply_remote(env)
        # The enable (@11300) wins the (ws, tasks) slot over the delete (@11200).
        for instance in (forward, backward):
            assert instance.is_feature_enabled(WS, "tasks") is True
            assert instance.node(WS, self.TASK_CLASS).is_active is True
        assert _dump_convergence_tables(forward) == _dump_convergence_tables(backward)
        forward.close()
        backward.close()


class TestCodeBlockFixture:
    """Replay of code-block.json: the code_block token is a
    PROMOTION SURVIVOR — the promote op flattens the surrounding rich tokens
    to one text run but keeps the block (a code page is a real surface); the
    language-less inline block keeps its token verbatim."""

    BLOCK = "0192a000-0000-7000-8000-000000000621"
    PLAIN = "0192a000-0000-7000-8000-000000000623"

    def test_code_block_survives_promotion(self, store: LocalStore) -> None:
        for envelope in load_fixture("code-block.json"):
            assert store.apply_remote(RelayEnvelope.model_validate(envelope)) is True
        promoted = json.loads(store.node(WS, self.BLOCK).content or "[]")
        assert promoted == [
            {"type": "text", "text": "before after"},
            {"type": "code_block", "language": "python", "text": "print('hi')\nprint('bye')"},
        ]
        assert json.loads(store.node(WS, self.PLAIN).content or "[]") == [
            {"type": "code_block", "text": "plain snippet"},
        ]


class TestHrFixture:
    """Replay of hr.json: hr is deliberately NOT a promotion
    survivor — the promote op stringifies it away; an inline block keeps the
    token verbatim."""

    BLOCK = "0192a000-0000-7000-8000-000000000631"
    RULE_ONLY = "0192a000-0000-7000-8000-000000000633"

    def test_hr_flattens_on_promotion_and_survives_inline(self, store: LocalStore) -> None:
        for envelope in load_fixture("hr.json"):
            assert store.apply_remote(RelayEnvelope.model_validate(envelope)) is True
        assert json.loads(store.node(WS, self.BLOCK).content or "[]") == [{"type": "text", "text": "above below"}]
        assert json.loads(store.node(WS, self.RULE_ONLY).content or "[]") == [{"type": "hr"}]


class TestEmbedRefViewFixture:
    """Replay of embed-ref-view.json: the view field applies
    verbatim — absent = the full transclusion default, wide_card rides the
    token; the small_card authored at create time was overwritten by the
    later content update (row LWW)."""

    BLOCK = "0192a000-0000-7000-8000-000000000642"
    TARGET = "0192a000-0000-7000-8000-000000000640"

    def test_view_field_applies_verbatim_default_absent(self, store: LocalStore) -> None:
        for envelope in load_fixture("embed-ref-view.json"):
            assert store.apply_remote(RelayEnvelope.model_validate(envelope)) is True
        assert json.loads(store.node(WS, self.BLOCK).content or "[]") == [
            {"type": "embed_ref", "nodeId": self.TARGET},
            {"type": "embed_ref", "nodeId": self.TARGET, "view": "wide_card"},
        ]


class TestPropertyValueElementsFixture:
    """Replay of property-value-elements.json (PG5): same-idx
    concurrent element adds coexist (ordered by element id), the element
    remove tombstones phone's element, the newer re-add revives it
    (add-wins), and the legacy positional add lands at its deterministic
    positional element."""

    NODE = "0192a000-0000-7000-8000-000000000712"
    SCHEMA = "0192a000-0000-7000-8000-000000000711"
    ALPHA = "0192a000-0000-7000-8000-000000000721"
    BETA = "0192a000-0000-7000-8000-000000000722"
    GAMMA = "0192a000-0000-7000-8000-000000000723"
    POSITIONAL_ROW = f"{NODE}:{SCHEMA}:4"

    def _load(self) -> list[RelayEnvelope]:
        return [RelayEnvelope.model_validate(item) for item in load_fixture("property-value-elements.json")]

    def test_or_set_semantics_end_to_end(self, store: LocalStore) -> None:
        for env in self._load():
            assert store.apply_remote(env) is True
        # Same-idx concurrent adds coexist; the remove + newer re-add revived
        # beta; the legacy positional add landed at its deterministic id.
        assert raw(
            store,
            "SELECT id, value, idx, metadata FROM property_value WHERE node_id = ? ORDER BY idx, id",
            (self.NODE,),
        ) == [
            (self.ALPHA, '"alpha"', 0, '{"since": "2020"}'),
            (self.BETA, '"beta"', 1, None),
            (self.GAMMA, '"gamma"', 1, None),
            (self.POSITIONAL_ROW, '"positional"', 4, None),
        ]
        # The remove's causality is recorded on the element tombstone.
        assert raw(
            store,
            "SELECT hlc_physical, hlc_logical FROM property_value_element_tombstone WHERE element_id = ?",
            (self.BETA,),
        ) == [(1727200020500, 0)]

    def test_effective_read_surfaces_every_element_ordered_by_idx_then_id(self, store: LocalStore) -> None:
        for env in self._load():
            store.apply_remote(env)
        effective = store.get_effective_properties(self.NODE)
        assert [(row.idx, row.element_id, row.value, row.source, row.bound_by) for row in effective] == [
            (0, self.ALPHA, "alpha", "authored", None),
            (1, self.BETA, "beta", "authored", None),
            (1, self.GAMMA, "gamma", "authored", None),
            (4, self.POSITIONAL_ROW, "positional", "authored", None),
        ]

    def test_same_hlc_remove_and_add_resolve_add_wins_under_both_orders(self, tmp_path: Path) -> None:
        """The membership comparator is HLC-only: an element remove at the
        SAME HLC as the add loses (add-wins) regardless of actor — under
        either delivery order."""
        base = [RelayEnvelope.model_validate(item) for item in load_fixture("property-value-elements.json")[:4]]
        remove = self._load()[5].model_copy(update={"hlc": Hlc(physical=1727200020300, logical=0)})
        re_add = self._load()[6]
        forward = LocalStore(tmp_path / "f.db")
        backward = LocalStore(tmp_path / "b.db")
        for instance, tail in ((forward, (remove, re_add)), (backward, (re_add, remove))):
            for env in base:
                assert instance.apply_remote(env) is True
            for env in tail:
                instance.apply_remote(env)
            # The equal-HLC remove lost to the add; the newer re-add keeps beta.
            assert raw(instance, "SELECT id FROM property_value WHERE node_id = ? ORDER BY id", (self.NODE,)) == [
                (self.ALPHA,),
                (self.BETA,),
            ]
            instance.close()


class TestClassPropertyActiveFixture:
    """Replay of class-property-active.json (PC4 + the option
    icon, display position corrected to PROPERTY-level): the disable
    wins the row LWW race over the interleaved lower-HLC enable, so the
    derived default vanishes while the ROW survives; an authored value
    written while inactive reads unbound; the re-enable restores the flag
    (the authored value at idx 0 keeps shadowing the default). The schema's
    options carry the icon (plus the color) verbatim into the
    stored options JSON, and a trailing propertySchema.update positions the
    value display at the bullet — a SCHEMA-side render contract since the
    owner review, read off the schema, never the binding."""

    SCHEMA = "0192a000-0000-7000-8000-000000000741"
    CLASS = "0192a000-0000-7000-8000-000000000742"
    ITEM = "0192a000-0000-7000-8000-000000000743"

    def _load(self) -> list[RelayEnvelope]:
        return [RelayEnvelope.model_validate(item) for item in load_fixture("class-property-active.json")]

    def test_prefix_6_disable_wins_the_row_race_default_vanishes_row_survives(self, store: LocalStore) -> None:
        for env in self._load()[:6]:
            assert store.apply_remote(env) is True
        # The ROW survives with its default intact, flagged inactive…
        assert raw(
            store,
            "SELECT active, default_value FROM class_property WHERE class_id = ? AND property_schema_id = ?",
            (self.CLASS, self.SCHEMA),
        ) == [(0, '"opt-a"')]
        # …so the effective read shows neither the default nor metadata.
        assert store.get_effective_properties(self.ITEM) == []

    def test_authored_value_while_inactive_reads_unbound(self, store: LocalStore) -> None:
        for env in self._load()[:7]:
            assert store.apply_remote(env) is True
        effective = store.get_effective_properties(self.ITEM)
        assert [(row.value, row.source, row.bound_by, row.sequence) for row in effective] == [
            ("opt-b", "authored", None, None)
        ]

    def test_full_reenable_restores_flag_authored_shadows_default(self, store: LocalStore) -> None:
        for env in self._load():
            assert store.apply_remote(env) is True
        assert raw(
            store,
            "SELECT active FROM class_property WHERE class_id = ? AND property_schema_id = ?",
            (self.CLASS, self.SCHEMA),
        ) == [(1,)]
        effective = store.get_effective_properties(self.ITEM)
        assert [(row.value, row.source, row.bound_by, row.sequence, row.element_id) for row in effective] == [
            ("opt-b", "authored", self.CLASS, 1, f"{self.ITEM}:{self.SCHEMA}:0")
        ]

    def test_full_replay_carries_the_option_icon_color_and_bullet_display(self, store: LocalStore) -> None:
        """The option decoration lands in the stored options
        JSON verbatim (the applier serializes the raw payload), and the
        trailing propertySchema.update{display:"bullet"} rides onto the
        SCHEMA row — the effective read sources the position from the schema,
        never the binding."""
        for env in self._load():
            assert store.apply_remote(env) is True
        options = raw(store, "SELECT options FROM property_schema WHERE id = ?", (self.SCHEMA,))
        assert json.loads(options[0][0]) == [
            {"id": "opt-a", "label": "A", "icon": "mdiCircle", "color": "yellow"},
            {"id": "opt-b", "label": "B"},
        ]
        assert raw(store, "SELECT display FROM property_schema WHERE id = ?", (self.SCHEMA,)) == [("bullet",)]
        effective = store.get_effective_properties(self.ITEM)
        assert [row.display for row in effective] == ["bullet"]


class TestPropertyDateQualifierFixture:
    """Replay of property-date-qualifier.json (PC6): chain-created
    refs apply verbatim at idx 0; the legacy ISO-string startDate at idx 1
    normalizes ON WRITE to the deterministic day-node ref."""

    CLUB = "0192a000-0000-7000-8000-000000000763"
    SCHEMA = "0192a000-0000-7000-8000-000000000761"
    ADA = "0192a000-0000-7000-8000-000000000764"
    DAY_2020 = "00000000-0000-0000-00dd-202003040000"
    DAY_2022 = "00000000-0000-0000-00dd-202205060000"
    DAY_2019 = "00000000-0000-0000-00dd-201901150000"

    def _load(self) -> list[RelayEnvelope]:
        return [RelayEnvelope.model_validate(item) for item in load_fixture("property-date-qualifier.json")]

    def test_refs_apply_verbatim_and_the_iso_string_normalizes_on_write(self, store: LocalStore) -> None:
        for env in self._load():
            assert store.apply_remote(env) is True
        assert raw(
            store,
            "SELECT idx, value, metadata FROM property_value WHERE node_id = ? ORDER BY idx",
            (self.CLUB,),
        ) == [
            (
                0,
                json.dumps({"nodeId": self.ADA}),
                json.dumps({"startDate": {"nodeId": self.DAY_2020}, "endDate": {"nodeId": self.DAY_2022}}),
            ),
            (1, json.dumps({"nodeId": self.ADA}), json.dumps({"startDate": {"nodeId": self.DAY_2019}})),
        ]
        # Every reader stays lenient: the effective read surfaces both
        # shapes as authored.
        effective = store.get_effective_properties(self.CLUB)
        assert [(row.idx, row.value, row.metadata, row.element_id) for row in effective] == [
            (
                0,
                {"nodeId": self.ADA},
                {"startDate": {"nodeId": self.DAY_2020}, "endDate": {"nodeId": self.DAY_2022}},
                f"{self.CLUB}:{self.SCHEMA}:0",
            ),
            (1, {"nodeId": self.ADA}, {"startDate": {"nodeId": self.DAY_2019}}, f"{self.CLUB}:{self.SCHEMA}:1"),
        ]

    def test_the_date_chain_landed(self, store: LocalStore) -> None:
        for env in self._load():
            store.apply_remote(env)
        assert store.node(WS, self.DAY_2019) is not None
        assert store.node(WS, self.DAY_2019).parent_id == "00000000-0000-0000-00aa-201901000000"
        assert store.node(WS, self.DAY_2020) is not None
        assert store.node(WS, self.DAY_2022) is not None


class TestCycleFixture:
    def test_cycle_closing_envelopes_throw_and_roll_back(self, store: LocalStore) -> None:
        root, leaf, leaf_extends_root, root_extends_leaf, root_extends_root_self = (
            RelayEnvelope.model_validate(item) for item in load_fixture("class-extends-cycle.json")
        )
        # The PREFIX applies cleanly: Root and Leaf created, Leaf extends [Root].
        assert store.apply_remote(root) is True
        assert store.apply_remote(leaf) is True
        assert store.apply_remote(leaf_extends_root) is True
        assert raw(
            store,
            "SELECT ancestor_id FROM class_hierarchy WHERE class_id = ? ORDER BY ancestor_id",
            (CYCLE_LEAF,),
        ) == [(CYCLE_ROOT,), (CYCLE_LEAF,)]

        # Multi-hop cycle (Root extends [Leaf] with Leaf already under Root)...
        with pytest.raises(CycleError):
            store.apply_remote(root_extends_leaf)
        # ...and the self-parent case (Root extends [Root]).
        with pytest.raises(CycleError):
            store.apply_remote(root_extends_root_self)

        # A thrown apply rolls back: closure and edges are exactly the prefix state.
        assert raw(
            store,
            "SELECT ancestor_id FROM class_hierarchy WHERE class_id = ? ORDER BY ancestor_id",
            (CYCLE_ROOT,),
        ) == [(CYCLE_ROOT,)]
        assert raw(store, "SELECT COUNT(*) FROM class_extends") == [(1,)]
        # And the thrown envelope ids were not consumed: a retry after a wipe
        # re-applies the prefix (the throw rolled the dedupe record back too).
        assert raw(store, "SELECT COUNT(*) FROM relay_operations") == [(3,)]


class TestReplayIdempotence:
    def test_reapplying_the_whole_fixture_set_is_idempotent(self, store: LocalStore) -> None:
        envelopes = [RelayEnvelope.model_validate(item) for item in all_fixture_envelopes()]
        # Note: alphabetical fixture order lands typed-link-mark-deleted
        # (higher HLC) before typed-link-mark, so the mark is legitimately
        # dropped by row LWW on first replay — convergence, not a bug.
        for envelope in envelopes:
            store.apply_remote(envelope)
        # Sanity: the move fixture's subtree landed (the typed-link fixture
        # updates lose the row LWW to object-move's newer HLCs here — the
        # per-fixture tests cover their own outcomes).
        assert store.node(WS, MOVE_C).parent_id == MOVE_A
        before = raw(store, "SELECT id, content, class_ids, name FROM nodes ORDER BY id")
        assert [store.apply_remote(envelope) for envelope in envelopes] == [False] * len(envelopes)
        assert raw(store, "SELECT id, content, class_ids, name FROM nodes ORDER BY id") == before
        assert raw(store, "SELECT COUNT(*) FROM relay_operations") == [(len(envelopes),)]
