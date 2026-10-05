"""Tests for the workspace-feature map (packages/domain/src/features.ts
parity, §34.55 RESHAPED): the five core class families, the extends-cascade
family sets, the gating walks, and F4 base-only routing.

The GTK client has no seed manifest of its own (seeding is server-side);
this module is the static seed knowledge the STORE's feature applier
resolves through — the family sets must match the monorepo domain exactly,
or the archival re-derivation diverges across clients.
"""

from __future__ import annotations

from notees_gtk.core.protocol.features import (
    ALWAYS_ON_SYSTEM_CLASSES,
    TASK_FAMILY_SEED,
    WORKSPACE_FEATURE_MAP,
    WORKSPACE_FEATURES,
    family_class_names,
    feature_for_managed_class,
    gating_features_for_class,
    is_always_on_system_class,
    managed_class_ids,
    system_class_ancestors,
    system_class_uuid,
    task_property_uuid,
)

TASK = "00000000-0000-0000-0001-000000000012"
EVENT = "00000000-0000-0000-0001-000000000040"
MEETING = "00000000-0000-0000-0001-000000000039"
BIRTHDAY = "00000000-0000-0000-0001-000000000041"
SOURCE = "00000000-0000-0000-0001-000000000023"
PERSON = "00000000-0000-0000-0001-000000000030"
BOOK = "00000000-0000-0000-0001-000000000024"
AGENT = "00000000-0000-0000-0001-000000000029"


class TestFiveFamilyMap:
    def test_the_reshaped_enum_is_exactly_the_five_families(self) -> None:
        assert WORKSPACE_FEATURES == ("tasks", "events", "meetings", "sources", "persons")
        assert set(WORKSPACE_FEATURE_MAP) == set(WORKSPACE_FEATURES)

    def test_family_specs(self) -> None:
        assert WORKSPACE_FEATURE_MAP["tasks"].base_class == "task"
        assert WORKSPACE_FEATURE_MAP["events"].base_class == "event"
        assert WORKSPACE_FEATURE_MAP["meetings"].base_class == "meeting"
        assert WORKSPACE_FEATURE_MAP["sources"].base_class == "source"
        assert WORKSPACE_FEATURE_MAP["persons"].base_class == "person"


class TestFamilySets:
    def test_events_family_cascades_through_the_extends_children(self) -> None:
        assert family_class_names("events") == ("event", "birthday", "meeting")
        assert managed_class_ids("events") == (EVENT, BIRTHDAY, MEETING)

    def test_sources_family_is_the_ten_strong_family(self) -> None:
        assert family_class_names("sources") == (
            "source",
            "article",
            "book",
            "conference",
            "document",
            "movie",
            "paper",
            "song",
            "thesis",
            "tv_series",
        )
        assert len(managed_class_ids("sources")) == 10

    def test_single_class_families(self) -> None:
        assert family_class_names("tasks") == ("task",)
        assert family_class_names("meetings") == ("meeting",)
        assert family_class_names("persons") == ("person",)


class TestGatingWalk:
    def test_meeting_gates_on_meetings_and_events(self) -> None:
        assert gating_features_for_class("meeting") == ("meetings", "events")

    def test_birthday_gates_on_events_only(self) -> None:
        assert gating_features_for_class("birthday") == ("events",)

    def test_family_bases_gate_on_their_own_feature(self) -> None:
        assert gating_features_for_class("task") == ("tasks",)
        assert gating_features_for_class("source") == ("sources",)

    def test_unmanaged_classes_have_no_gating(self) -> None:
        assert gating_features_for_class("agent") == ()
        assert gating_features_for_class("organization") == ()
        assert gating_features_for_class("asset") == ()


class TestF4Routing:
    def test_only_the_five_bases_route(self) -> None:
        assert feature_for_managed_class(TASK) == "tasks"
        assert feature_for_managed_class(EVENT) == "events"
        assert feature_for_managed_class(MEETING) == "meetings"
        assert feature_for_managed_class(SOURCE) == "sources"
        assert feature_for_managed_class(PERSON) == "persons"

    def test_family_children_do_not_route(self) -> None:
        # Children keep plain delete semantics (the owner's exact mapping).
        assert feature_for_managed_class(BOOK) is None
        assert feature_for_managed_class(BIRTHDAY) is None
        assert feature_for_managed_class(AGENT) is None
        assert feature_for_managed_class("0192a000-0000-7000-8000-000000000099") is None


class TestAlwaysOnAndAncestors:
    def test_always_on_list(self) -> None:
        for name in ("year", "month", "day", "asset", "highlight", "weblink", "collection", "agent", "organization"):
            assert is_always_on_system_class(name)
        for name in ("task", "event", "meeting", "source", "person", "birthday", "book"):
            assert not is_always_on_system_class(name)
        assert set(ALWAYS_ON_SYSTEM_CLASSES).isdisjoint(
            name for feature in WORKSPACE_FEATURES for name in family_class_names(feature)
        )

    def test_static_extends_closure(self) -> None:
        assert system_class_ancestors("meeting") == frozenset({"event"})
        assert system_class_ancestors("birthday") == frozenset({"event"})
        assert system_class_ancestors("book") == frozenset({"source"})
        assert system_class_ancestors("person") == frozenset({"agent"})
        assert system_class_ancestors("event") == frozenset()
        assert system_class_uuid("task") == TASK


class TestTaskFamilySeed:
    def test_six_schemas_at_the_fixed_ids(self) -> None:
        names = [entry.name for entry in TASK_FAMILY_SEED]
        assert names == ["Status", "Scheduled", "Deadline", "Priority", "Closed", "Recurrence"]
        assert task_property_uuid("taskStatus") == "00000000-0000-0000-0003-000000000001"
        assert task_property_uuid("taskRecurrence") == "00000000-0000-0000-0003-000000000006"

    def test_deterministic_option_ids(self) -> None:
        status = TASK_FAMILY_SEED[0]
        assert status.type == "select"
        assert [option.id for option in status.options] == [
            "00000000-0000-0000-0004-000000000008",
            "00000000-0000-0000-0004-000000000009",
            "00000000-0000-0000-0004-00000000000a",
            "00000000-0000-0000-0004-00000000000b",
            "00000000-0000-0000-0004-00000000000c",
            "00000000-0000-0000-0004-00000000000d",
        ]
        priority = TASK_FAMILY_SEED[3]
        assert [option.id for option in priority.options] == [
            "00000000-0000-0000-0004-00000000000e",
            "00000000-0000-0000-0004-00000000000f",
            "00000000-0000-0000-0004-000000000010",
            "00000000-0000-0000-0004-000000000011",
        ]

    def test_status_options_carry_the_designed_icons_and_colors(self) -> None:
        """§34.89 lockstep (seeds.ts ``TASK_STATUS_OPTIONS``): the six status
        options carry the circle-family MDI glyphs with a distinct color
        each; the priority options stay decoration-free."""
        status = TASK_FAMILY_SEED[0]
        assert [(option.label, option.icon, option.color) for option in status.options] == [
            ("Backlog", "mdiCircleOutline", "gray"),
            ("Pending", "mdiCircle", "yellow"),
            ("Doing", "mdiCircleHalfFull", "orange"),
            ("Reviewing", "mdiEyeCircleOutline", "blue"),
            ("Done", "mdiCheckCircle", "green"),
            ("Cancelled", "mdiCloseCircle", "red"),
        ]
        priority = TASK_FAMILY_SEED[3]
        assert all(option.icon is None and option.color is None for option in priority.options)

    def test_only_the_status_binding_defaults_to_bullet_display(self) -> None:
        """§34.89: the Status value rides the block bullet; every other
        binding keeps display None ('panel', the properties section)."""
        assert TASK_FAMILY_SEED[0].display == "bullet"
        assert all(entry.display is None for entry in TASK_FAMILY_SEED[1:])
