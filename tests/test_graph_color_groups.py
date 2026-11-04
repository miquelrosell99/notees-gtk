"""QueryAST color groups — the ``colorGroups.ts`` port's pins over a live
mirror: the at-load migrations (legacy text-DSL rows through the slice-2
parser with mirror-backed name resolution; v1 ASTs through the pure
mapping; failures keep the group, marked erroneous), first-match-wins
evaluation with the list order as priority, empty/erroneous groups painting
nothing, runner failures skipping one group never the rest, live recolor on
store change, and the device-cache persistence round trip.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from notees_gtk.core.graph.color_groups import (
    GraphColorGroup,
    evaluate_color_groups,
    group_to_raw,
    is_legacy_color_group,
    load_color_groups,
    migrate_color_group,
)
from notees_gtk.core.protocol.clock import Hlc
from notees_gtk.core.protocol.ids import new_uuid7
from notees_gtk.core.protocol.models import PROTOCOL_VERSION, RelayEnvelope
from notees_gtk.data.store import LocalStore

WS_A = "ws-a"


def uid(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"notees-gtk/test/{label}"))


def make_env(op_type: str, payload: dict[str, Any], *, hlc: tuple[int, int] = (1, 0)) -> RelayEnvelope:
    return RelayEnvelope(
        id=new_uuid7(),
        protocolVersion=PROTOCOL_VERSION,
        workspaceId=WS_A,
        actorId="color-group-test",
        deviceId="color-group-test-device",
        hlc=Hlc(physical=hlc[0], logical=hlc[1]),
        affectedNodeIds=[str(payload.get("objectId") or payload.get("classId") or "")],
        opType=op_type,
        payload=payload,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )


def text(value: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": value}]


@pytest.fixture
def store(tmp_path: Path) -> Iterator[LocalStore]:
    instance = LocalStore(tmp_path / "store.db")
    try:
        yield instance
    finally:
        instance.close()


def build_class_world(store: LocalStore) -> tuple[str, str, str, str]:
    """A ``Person`` class with two members and one unclassified page."""
    person = uid("person")
    member_a = uid("member-a")
    member_b = uid("member-b")
    other = uid("other")
    store.apply_remote(make_env("class.create", {"classId": person, "contentAst": text("Person")}))
    store.apply_remote(
        make_env("object.create", {"objectId": member_a, "parentId": None, "classIds": [person], "contentAst": text("Ada")})
    )
    store.apply_remote(
        make_env("object.create", {"objectId": member_b, "parentId": None, "classIds": [person], "contentAst": text("Grace")})
    )
    store.apply_remote(make_env("object.create", {"objectId": other, "parentId": None, "contentAst": text("Other")}))
    return person, member_a, member_b, other


def dsl_group(group_id: str, label: str, color: str, query: str) -> dict[str, Any]:
    return {"id": group_id, "label": label, "color": color, "query": query}


class TestLegacyMigration:
    def test_legacy_dsl_row_parses_through_the_mirror(self, store: LocalStore) -> None:
        _, member_a, member_b, other = build_class_world(store)
        raw = dsl_group("g1", "People", "#ff0000", "class: Person")
        assert is_legacy_color_group(raw) is True
        group = migrate_color_group(store, WS_A, raw)
        assert group.error is None
        assert group.ast is not None
        assignment = evaluate_color_groups(store, WS_A, [group])
        # The two members match; the class itself and the unclassified page
        # do not.
        assert set(assignment) == {member_a, member_b}
        assert other not in assignment

    def test_legacy_dsl_failure_keeps_the_group_erroneous(self, store: LocalStore) -> None:
        build_class_world(store)
        raw = dsl_group("g1", "Broken", "#00ff00", "class: Nobody")
        group = migrate_color_group(store, WS_A, raw)
        assert group.ast is None
        assert group.error is not None
        assert evaluate_color_groups(store, WS_A, [group]) == {}

    def test_non_object_row_fails_loud(self, store: LocalStore) -> None:
        with pytest.raises(ValueError, match="color group row"):
            migrate_color_group(store, WS_A, ["not", "a", "row"])


class TestV1Migration:
    def test_v1_ast_row_maps_to_v2(self, store: LocalStore) -> None:
        person, member_a, member_b, _ = build_class_world(store)
        raw = {
            "id": "g1",
            "label": "People",
            "color": "#123456",
            "ast": {
                "type": "group",
                "logic": "and",
                "children": [{"type": "classAny", "classIds": [person]}],
            },
        }
        group = migrate_color_group(store, WS_A, raw)
        assert group.error is None
        assert group.ast is not None
        assert set(evaluate_color_groups(store, WS_A, [group])) == {member_a, member_b}

    def test_v1_tree_with_date_and_boolean_conditions(self, store: LocalStore) -> None:
        person, member_a, member_b, _ = build_class_world(store)
        raw = {
            "id": "g1",
            "label": "People",
            "color": "#123456",
            "ast": {
                "type": "group",
                "logic": "and",
                "children": [
                    {"type": "classAny", "classIds": [person]},
                    {"type": "isEmbedded", "isEmbedded": False},
                ],
            },
        }
        group = migrate_color_group(store, WS_A, raw)
        assert group.error is None
        # Both members are parentless pages (isEmbedded = False), so the
        # folded boolean narrows nothing here — the pin is that the v1
        # `isEmbedded` hat maps at all and the tree still runs.
        assert set(evaluate_color_groups(store, WS_A, [group])) == {member_a, member_b}

    def test_unmappable_v1_tree_keeps_the_group_erroneous(self, store: LocalStore) -> None:
        raw = {
            "id": "g1",
            "label": "Old",
            "color": "#123456",
            "ast": {"type": "group", "logic": "and", "children": [{"type": "warp"}]},
        }
        group = migrate_color_group(store, WS_A, raw)
        assert group.ast is None
        assert "unknown v1 condition type" in (group.error or "")

    def test_v2_row_passes_through_the_strict_parse(self, store: LocalStore) -> None:
        person, member_a, _, _ = build_class_world(store)
        migrated = migrate_color_group(
            store,
            WS_A,
            {
                "id": "g1",
                "label": "People",
                "color": "#123456",
                "ast": {"type": "group", "logic": "and", "children": [{"type": "classAny", "classIds": [person]}]},
            },
        )
        assert migrated.ast is not None
        raw = group_to_raw(migrated)
        assert "error" not in raw
        reloaded = migrate_color_group(store, WS_A, raw)
        assert reloaded == migrated


class TestEvaluation:
    def test_first_match_wins_in_list_order(self, store: LocalStore) -> None:
        _, member_a, _, _ = build_class_world(store)
        red = migrate_color_group(store, WS_A, dsl_group("g1", "People", "#ff0000", "class: Person"))
        # An everyone-matching group — but the first match already won.
        everyone = migrate_color_group(
            store, WS_A, dsl_group("g2", "All", "#0000ff", "isClass: false OR isClass: true")
        )
        assert everyone.error is None
        assignment = evaluate_color_groups(store, WS_A, [red, everyone])
        assert assignment[member_a] == "#ff0000"
        assert len(assignment) > 2  # the everyone group still paints the rest

    def test_empty_group_matches_nothing(self, store: LocalStore) -> None:
        build_class_world(store)
        empty = GraphColorGroup(id="g1", label="Empty", color="#ff0000", ast=None)
        assert evaluate_color_groups(store, WS_A, [empty]) == {}

    def test_runner_failure_skips_only_that_group(self, store: LocalStore) -> None:
        _, member_a, _, _ = build_class_world(store)
        working = migrate_color_group(store, WS_A, dsl_group("g1", "People", "#00ff00", "class: Person"))
        assert working.ast is not None
        # A self target without a hosting page fails LOUD in the runner — the
        # web's `catch { continue }`: this group skips, the next still paints.
        broken = migrate_color_group(
            store,
            WS_A,
            {
                "id": "self",
                "label": "Self",
                "color": "#ff00ff",
                "ast": {
                    "type": "group",
                    "logic": "and",
                    "children": [
                        {"type": "member", "family": "child", "op": "in", "target": {"mode": "self"}}
                    ],
                },
            },
        )
        assert broken.ast is not None
        raw = group_to_raw(broken)
        with pytest.raises(Exception):  # noqa: B017,BLE001 — the runner is fail-loud without a host
            store.run_query_ast(
                WS_A,
                {"version": 2, "scope": {"type": "entire_workspace"}, "root": raw["ast"]},
            )
        assignment = evaluate_color_groups(store, WS_A, [broken, working])
        assert assignment == {member_a: "#00ff00", uid("member-b"): "#00ff00"}

    def test_recolor_on_store_change(self, store: LocalStore) -> None:
        person, member_a, member_b, _ = build_class_world(store)
        group = migrate_color_group(store, WS_A, dsl_group("g1", "People", "#ff0000", "class: Person"))
        before = evaluate_color_groups(store, WS_A, [group])
        assert set(before) == {member_a, member_b}
        # Unassign one member — the OR-Set tombstone re-derives, the next
        # evaluation paints the smaller set (live recolor rides the same
        # store-change notify the chrome subscribes to).
        store.apply_remote(
            make_env("class.unassign", {"objectId": member_a, "classId": person}, hlc=(9, 0))
        )
        after = evaluate_color_groups(store, WS_A, [group])
        assert set(after) == {member_b}


class TestPersistence:
    def test_device_cache_round_trip(self, store: LocalStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from notees_gtk.ui import config_store

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        build_class_world(store)
        group = migrate_color_group(store, WS_A, dsl_group("g1", "People", "#ff0000", "class: Person"))
        config_store.save_graph_color_groups(WS_A, [group_to_raw(group)])
        raws = config_store.load_graph_color_groups(WS_A)
        reloaded = load_color_groups(store, WS_A, raws)
        assert reloaded == [group]

    def test_workspace_rows_are_isolated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from notees_gtk.ui import config_store

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        row = {"id": "g1", "label": "L", "color": "#fff", "ast": None}
        config_store.save_graph_color_groups("ws-a", [row])
        config_store.save_graph_color_groups("ws-b", [])
        assert config_store.load_graph_color_groups("ws-a") == [row]
        assert config_store.load_graph_color_groups("ws-b") == []
        assert config_store.load_graph_color_groups("ws-c") == []

    def test_garbled_cache_reads_as_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from notees_gtk.ui import config_store

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        config_store.save_graph_color_groups("ws-a", [{"id": "g1", "label": "L", "color": "#fff", "ast": None}])
        path = config_store.config_dir() / "config.json"
        path.write_text('{"graph_color_groups": "garbled"}', encoding="utf-8")
        assert config_store.load_graph_color_groups("ws-a") == []
        path.write_text('{"graph_color_groups": {"ws-a": "garbled"}}', encoding="utf-8")
        assert config_store.load_graph_color_groups("ws-a") == []

    def test_non_list_cache_fails_loud_at_load(self, store: LocalStore) -> None:
        with pytest.raises(ValueError, match="must be a list"):
            load_color_groups(store, WS_A, {"not": "a list"})

    def test_save_rejects_non_object_rows(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from notees_gtk.ui import config_store

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        with pytest.raises(ValueError, match="JSON objects"):
            config_store.save_graph_color_groups(WS_A, [{"ok": True}, "nope"])  # type: ignore[list-item]
