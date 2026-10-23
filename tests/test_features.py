"""Tests for the workspace-feature map (packages/domain/src/features.ts
parity, RESHAPED): the five core class families, the
extends-cascade family sets, the gating walks, the class-node delete
routing (base-only), and the locked-system-class core.

The GTK client has no seed manifest of its own (seeding is server-side);
this module is the static seed knowledge the STORE's feature applier
resolves through — the family sets must match the monorepo domain exactly,
or the archival re-derivation diverges across clients.
"""

from __future__ import annotations

from notees_gtk.core.protocol.features import (
    ALWAYS_ON_SYSTEM_CLASSES,
    LOCKED_SYSTEM_CLASSES,
    TASK_FAMILY_SEED,
    WORKSPACE_FEATURE_MAP,
    WORKSPACE_FEATURES,
    family_class_names,
    feature_for_managed_class,
    gating_features_for_class,
    is_always_on_system_class,
    is_family_feature,
    is_locked_system_class_id,
    managed_class_ids,
    system_class_ancestors,
    system_class_name_for_id,
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
TRIP = "00000000-0000-0000-0001-000000000047"


class TestFiveFamilyMap:
    def test_the_reshaped_enum_carries_the_five_families_plus_journal(self) -> None:
        """The five family entries + the surfaces-only ``journal`` toggle
       ; the family MAP stays the five exactly."""
        assert WORKSPACE_FEATURES == ("tasks", "events", "meetings", "sources", "persons", "journal")
        assert set(WORKSPACE_FEATURE_MAP) == {"tasks", "events", "meetings", "sources", "persons"}

    def test_family_specs(self) -> None:
        assert WORKSPACE_FEATURE_MAP["tasks"].base_class == "task"
        assert WORKSPACE_FEATURE_MAP["events"].base_class == "event"
        assert WORKSPACE_FEATURE_MAP["meetings"].base_class == "meeting"
        assert WORKSPACE_FEATURE_MAP["sources"].base_class == "source"
        assert WORKSPACE_FEATURE_MAP["persons"].base_class == "person"

    def test_is_family_feature_excludes_journal(self) -> None:
        for feature in ("tasks", "events", "meetings", "sources", "persons"):
            assert is_family_feature(feature) is True
        assert is_family_feature("journal") is False


class TestFamilySets:
    def test_events_family_cascades_through_the_extends_children(self) -> None:
        """The #14 follow-up: trip extends event —
        a trip is calendar-bound, so the events family set archives it with
        meeting and birthday (the four-strong cascade)."""
        assert family_class_names("events") == ("event", "birthday", "meeting", "trip")
        assert managed_class_ids("events") == (EVENT, BIRTHDAY, MEETING, TRIP)

    def test_sources_family_is_the_forty_strong_family(self) -> None:
        """The source-library roster: web_page (the renamed
        weblink, a source child since 2026-10-07) plus the 30 new Zotero-faithful
        type-classes all extend source — the family the SOURCES toggle archives
        is the base + 39 children, and paper (…0025) left the roster with its
        2026-10-23 withdrawal (nodes migrate to journal_article)."""
        assert family_class_names("sources") == (
            "source",
            "artwork",
            "audio_recording",
            "bill",
            "blog_post",
            "book",
            "book_section",
            "computer_program",
            "conference",
            "conference_paper",
            "dataset",
            "dictionary_entry",
            "document",
            "email",
            "encyclopedia_article",
            "film",
            "forum_post",
            "hearing",
            "instant_message",
            "interview",
            "journal_article",
            "letter",
            "magazine_article",
            "manuscript",
            "map",
            "newspaper_article",
            "patent",
            "podcast",
            "preprint",
            "presentation",
            "radio_broadcast",
            "report",
            "song",
            "standard",
            "statute",
            "thesis",
            "tv_broadcast",
            "tv_series",
            "video_recording",
            "web_page",
        )
        assert len(managed_class_ids("sources")) == 40
        assert system_class_uuid("web_page") == "00000000-0000-0000-0001-000000000034"
        assert system_class_uuid("journal_article") == "00000000-0000-0000-0001-000000000026"
        assert system_class_uuid("film") == "00000000-0000-0000-0001-000000000035"

    def test_archived_by_default_set_is_the_eighteen_strong_long_tail(self) -> None:
        """SYSTEM_ARCHIVED_BY_DEFAULT_CLASSES parity (seeds.ts): the 18 new
        type-classes that seed archived — the everyday types stay enabled."""
        from notees_gtk.core.protocol.features import SYSTEM_ARCHIVED_BY_DEFAULT_CLASSES  # noqa: PLC0415

        assert SYSTEM_ARCHIVED_BY_DEFAULT_CLASSES == (
            "artwork",
            "bill",
            "blog_post",
            "computer_program",
            "dataset",
            "dictionary_entry",
            "email",
            "encyclopedia_article",
            "forum_post",
            "hearing",
            "instant_message",
            "interview",
            "letter",
            "manuscript",
            "map",
            "patent",
            "radio_broadcast",
            "statute",
        )
        archived = set(SYSTEM_ARCHIVED_BY_DEFAULT_CLASSES)
        assert archived <= set(family_class_names("sources"))
        # The everyday types stay enabled by default.
        assert archived.isdisjoint({"book_section", "magazine_article", "newspaper_article", "preprint", "report", "standard", "podcast", "conference_paper", "audio_recording", "video_recording", "tv_broadcast", "film", "web_page", "journal_article"})

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

    def test_web_page_gates_on_sources(self) -> None:
        """The source-family child: its chrome hides when SOURCES is off
        (web_page is the renamed weblink — same fixed UUID …0034)."""
        assert gating_features_for_class("web_page") == ("sources",)
        for name in ("artwork", "journal_article", "film", "patent"):
            assert gating_features_for_class(name) == ("sources",)

    def test_trip_gates_on_events(self) -> None:
        """The #14 follow-up: trip extends event, so its chrome hides when
        EVENTS is off — exactly like meeting and birthday."""
        assert gating_features_for_class("trip") == ("events",)

    def test_the_four_plain_deploy_catalog_seeds_have_no_gating(self) -> None:
        """features.ts parity: definition/idea/place/project are plain seeds —
        unmanaged, like the TS record (absent from ALWAYS_ON_SYSTEM_CLASSES,
        no family base ancestor, gate on nothing)."""
        for name in ("definition", "idea", "place", "project"):
            assert gating_features_for_class(name) == ()


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


class TestLockedSystemClasses:
    """The locked core (the classes-gallery batch):
    the date chain + table, asset, query, code, template, comment can never
    be archived or deleted (features.ts ``LOCKED_SYSTEM_CLASSES`` parity)."""

    def test_the_locked_list_is_the_always_on_core(self) -> None:
        assert LOCKED_SYSTEM_CLASSES == (
            "year",
            "month",
            "day",
            "table",
            "asset",
            "query",
            "code",
            "template",
            "comment",
        )
        assert set(LOCKED_SYSTEM_CLASSES) <= set(ALWAYS_ON_SYSTEM_CLASSES)

    def test_locked_ids_resolve_through_the_seed_map(self) -> None:
        assert is_locked_system_class_id("00000000-0000-0000-0001-000000000003") is True  # year
        assert is_locked_system_class_id("00000000-0000-0000-0001-000000000015") is True  # table
        # Gallery-toggleable always-on classes are NOT locked; user classes neither.
        assert is_locked_system_class_id("00000000-0000-0000-0001-000000000006") is False  # quote
        assert is_locked_system_class_id("00000000-0000-0000-0001-000000000010") is False  # whiteboard
        assert is_locked_system_class_id("0192a000-0000-7000-8000-000000000099") is False

    def test_system_class_name_for_id(self) -> None:
        assert system_class_name_for_id(TASK) == "task"
        assert system_class_name_for_id("00000000-0000-0000-0001-000000000003") == "year"
        assert system_class_name_for_id("0192a000-0000-7000-8000-000000000099") is None


class TestAlwaysOnAndAncestors:
    def test_always_on_list(self) -> None:
        for name in ("year", "month", "day", "asset", "highlight", "collection", "agent", "organization"):
            assert is_always_on_system_class(name)
        for name in ("task", "event", "meeting", "source", "person", "birthday", "book", "web_page", "trip"):
            assert not is_always_on_system_class(name)
        # The #14 follow-up's four plain seeds are unmanaged, not always-on
        # (features.ts parity — they are absent from the TS always-on list).
        for name in ("definition", "idea", "place", "project"):
            assert not is_always_on_system_class(name)
        assert set(ALWAYS_ON_SYSTEM_CLASSES).isdisjoint(
            name for feature in WORKSPACE_FEATURE_MAP for name in family_class_names(feature)
        )

    def test_the_retired_class_meta_class_left_the_manifest(self) -> None:
        """The seeded `class` meta class (…0001) is retired 2026-10-07:
        never feature-managed, never reused (the seeds.ts withdrawal)."""
        assert "class" not in ALWAYS_ON_SYSTEM_CLASSES
        all_family_names = {name for feature in WORKSPACE_FEATURE_MAP for name in family_class_names(feature)}
        assert "class" not in all_family_names

    def test_static_extends_closure(self) -> None:
        assert system_class_ancestors("meeting") == frozenset({"event"})
        assert system_class_ancestors("birthday") == frozenset({"event"})
        assert system_class_ancestors("trip") == frozenset({"event"})
        assert system_class_ancestors("book") == frozenset({"source"})
        assert system_class_ancestors("web_page") == frozenset({"source"})
        assert system_class_ancestors("person") == frozenset({"agent"})
        assert system_class_ancestors("event") == frozenset()
        # The source-library roster: every new type-class
        # extends source; paper's edge left the map with its withdrawal.
        for name in ("artwork", "audio_recording", "patent", "tv_broadcast", "video_recording"):
            assert system_class_ancestors(name) == frozenset({"source"})
        # The #14 follow-up's four plain seeds extend nothing.
        for name in ("definition", "idea", "place", "project"):
            assert system_class_ancestors(name) == frozenset()
        assert system_class_uuid("task") == TASK
        assert system_class_uuid("trip") == TRIP


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

    def test_the_date_bindings_are_datetime_typed(self) -> None:
        """The unified-datetime seed convergence: the
        task family's three date bindings (Scheduled/Deadline/Closed) are
        DATETIME-typed per the amended time-of-day law (SCHEMA.md
        "Datetime") — the seeds.ts ``TASK_FAMILY_SEED`` retype parity."""
        by_property = {entry.property: entry for entry in TASK_FAMILY_SEED}
        for name in ("taskScheduled", "taskDeadline", "taskClosedDate"):
            assert by_property[name].type == "datetime"
        assert by_property["taskStatus"].type == "select"
        assert by_property["taskPriority"].type == "select"
        assert by_property["taskRecurrence"].type == "select"

    def test_status_options_carry_the_designed_icons_and_colors(self) -> None:
        """The status options (seeds.ts ``TASK_STATUS_OPTIONS`` lockstep):
        the six status
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

    def test_only_the_status_entry_defaults_to_bullet_display(self) -> None:
        """The Status schema defaults to "bullet" (the manifest's
        display rides the SCHEMA row the ensure authors); every other entry
        keeps display None ('panel', the properties section)."""
        assert TASK_FAMILY_SEED[0].display == "bullet"
        assert all(entry.display is None for entry in TASK_FAMILY_SEED[1:])
