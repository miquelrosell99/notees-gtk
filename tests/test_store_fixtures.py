"""Acceptance suite: replay the vendored v2 protocol fixtures through the store.

This is the GTK client's half of the cross-implementation fixture gate: the
same envelopes the monorepo's store tests replay (``v2/packages/store/test/
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

from notees_gtk.core.protocol.models import RelayEnvelope
from notees_gtk.data.errors import CycleError
from notees_gtk.data.store import (
    EffectiveProperty,
    EffectivePropertySchema,
    LocalStore,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "v2"

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
