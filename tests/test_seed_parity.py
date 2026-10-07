"""Seed-parity lockstep: the FINAL citations-authors model (monorepo commits
``docs(schema): FINAL citations authorship decision`` and ``feat(v2): FINAL
authors model — node-typed to agent nodes; linkedAuthors withdrawn``).

The GTK client has NO seed manifest of its own — seeding is server-side
(``buildSeedEnvelopes`` in ``apps/server/src/seed.ts`` driven by
``@notees/domain`` seeds.ts); clients receive seed envelopes through
catch-up like any other ops. This test is the client-side half of the
lockstep: it replays the seed-op SHAPES the changed manifest emits through
the store appliers and pins the fixed UUIDs (never reuse), the
system-class block prefix, the extends parents, and the FINAL property
specs:

- new system classes ``song`` / ``tv_series`` / ``conference``
  (block ``00000000-0000-0000-0001-…``), all ``extends ["source"]``, with
  their fixed UUIDs (…000000000036/037/038) and mdi icons;
- ``authors`` FINAL: node-typed to agent nodes —
  ``{type: object, multi: true, bindTo: source, targetClassFilter: [agent]}``
  at UUID ``…000000000012``. The text-authors experiment (verbatim strings,
  no targetClassFilter) was reversed by the owner;
- withdrawn ids are never reused (the ``locator`` …0018 precedent):
  ``…0025`` (``linkedAuthors``) is WITHDRAWN 2026-09-27, reversed same day.
- the #14 follow-up five (owner list, 2026-10-06): ``definition`` /
  ``idea`` / ``place`` / ``project`` / ``trip`` (block ``…0001-…``,
  …0043-…0047), plain seeds with their mdi icons + display titles;
  ``trip`` extends ``event`` so the events-family toggle cascades to it —
  the gating/family groupings mirror ``features.ts`` (the four plain ones
  stay unmanaged: no gating, not always-on).

It fails loud if a future registry/applier change breaks seed application.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from notees_gtk.core.protocol.clock import Hlc
from notees_gtk.core.protocol.ids import new_uuid7
from notees_gtk.core.protocol.models import PROTOCOL_VERSION, RelayEnvelope
from notees_gtk.data.store import LocalStore

WS = "0192a000-0000-7000-8000-000000000001"
ACTOR = "0192a000-0000-7000-8000-000000000002"

#: System classes live in this fixed block; properties in 0000-0000-0000-…
SYSTEM_CLASS_BLOCK_PREFIX = "00000000-0000-0000-0001-"

SOURCE = "00000000-0000-0000-0001-000000000023"
AGENT = "00000000-0000-0000-0001-000000000029"

#: Delta classes (citations model revision) — fixed UUIDs, never reuse.
SONG = "00000000-0000-0000-0001-000000000036"
TV_SERIES = "00000000-0000-0000-0001-000000000037"
CONFERENCE = "00000000-0000-0000-0001-000000000038"

#: Active delta property schema (0000-0000-0000- block — distinct from classes).
AUTHORS_SCHEMA = "00000000-0000-0000-0000-000000000012"

#: Withdrawn system property ids — never reuse (seeds.ts registers them as
#: dead slots): the ``locator`` …0018, and …0025 (``linkedAuthors``) —
#: WITHDRAWN 2026-09-27, reversed same day in the FINAL authors model.
WITHDRAWN_PROPERTY_IDS = (
    "00000000-0000-0000-0000-000000000018",
    "00000000-0000-0000-0000-000000000025",
)

#: WITHDRAWN 2026-10-07 (owner ruling): the seeded `class` META class …0001
#: is retired — nodes bound to it become REAL classes (the class.create
#: conversion capability) and the seed no longer emits it. Never reuse
#: (the seeds.ts withdrawal comment, the locator …0018 precedent).
RETIRED_CLASS_META_ID = "00000000-0000-0000-0001-000000000001"

#: The weblink class joined the source tree (owner ruling 2026-10-07):
#: …0034 extends source, so the SOURCES family toggle archives it.
WEBLINK = "00000000-0000-0000-0001-000000000034"
ASSET = "00000000-0000-0000-0001-000000000009"

#: The #14 follow-up five (owner list, 2026-10-06 — seeds.ts …0043-…0047):
#: the deploy catalog's missing everyday classes, plain seeds (zero wire
#: cost, seed convergence only). trip extends event — a trip is
#: calendar-bound, so the events toggle cascades to it.
DEFINITION = "00000000-0000-0000-0001-000000000043"
IDEA = "00000000-0000-0000-0001-000000000044"
PLACE = "00000000-0000-0000-0001-000000000045"
PROJECT = "00000000-0000-0000-0001-000000000046"
TRIP = "00000000-0000-0000-0001-000000000047"
EVENT = "00000000-0000-0000-0001-000000000040"

#: The WITHDRAWN class slots the five must never collide with: …0001 (the
#: seeded `class` meta class, retired 2026-10-07) and …0042 (`cover`,
#: withdrawn 2026-10-04 the day it was minted) — dead slots, never reused
#: (the seeds.ts withdrawal comments, the locator …0018 precedent).
WITHDRAWN_CLASS_IDS = (
    "00000000-0000-0000-0001-000000000001",
    "00000000-0000-0000-0001-000000000042",
)


def seed_env(op_type: str, payload: dict[str, object], physical: int) -> RelayEnvelope:
    """One envelope in the shape ``buildSeedEnvelopes`` emits (client "seed")."""
    return RelayEnvelope(
        id=new_uuid7(),
        protocolVersion=PROTOCOL_VERSION,
        workspaceId=WS,
        actorId=ACTOR,
        deviceId="seed-device",
        client="seed",
        hlc=Hlc(physical=physical, logical=0),
        affectedNodeIds=[str(payload.get("classId") or payload.get("objectId") or "")],
        opType=op_type,
        payload=payload,
        timestamp="2026-09-27T12:00:00Z",
    )


def _db_path(store: LocalStore) -> Path:
    return Path(str(store._conn.execute("PRAGMA database_list").fetchone()[2]))  # noqa: SLF001


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    instance = LocalStore(tmp_path / "store.db")
    yield instance
    instance.close()


def _seed_source_family(store: LocalStore) -> None:
    """The delta classes' extends parent must exist before the edges land."""
    assert (
        store.apply_remote(
            seed_env(
                "class.create",
                {"classId": SOURCE, "contentAst": [{"type": "text", "text": "source"}], "icon": "mdiBookOpenVariant"},
                1,
            )
        )
        is True
    )


def _seed_authors_schema(store: LocalStore) -> None:
    """The FINAL authors schema as the seed emits it (node-typed to agent)."""
    assert (
        store.apply_remote(
            seed_env(
                "propertySchema.create",
                {
                    "propertySchemaId": AUTHORS_SCHEMA,
                    "name": "authors",
                    "type": "object",
                    "multi": True,
                    "scope": "class",
                    "targetClassFilter": [AGENT],
                },
                2,
            )
        )
        is True
    )


class TestNewSourceSubclasses:
    def test_fixed_ids_icons_and_extends_parent(self, store: LocalStore) -> None:
        _seed_source_family(store)
        classes = [
            (SONG, "song", "mdiMusicNote"),
            (TV_SERIES, "tv_series", "mdiTelevisionClassic"),
            (CONFERENCE, "conference", "mdiPresentation"),
        ]
        for offset, (class_id, name, icon) in enumerate(classes):
            assert (
                store.apply_remote(
                    seed_env(
                        "class.create",
                        {"classId": class_id, "contentAst": [{"type": "text", "text": name}], "icon": icon},
                        2 + offset,
                    )
                )
                is True
            )
        for offset, (class_id, _name, _icon) in enumerate(classes):
            assert (
                store.apply_remote(
                    seed_env("class.setExtends", {"classId": class_id, "parentClassIds": [SOURCE]}, 10 + offset)
                )
                is True
            )

        for class_id, name, _icon in classes:
            row = store.node(WS, class_id)
            assert row is not None
            assert row.is_class is True
            assert row.present_as_main is False
            assert row.content_plain == name  # title-is-content: the label IS the content
            assert row.parent_id is None  # classes are always roots
        with sqlite3.connect(_db_path(store)) as raw:
            edges = raw.execute("SELECT class_id, parent_class_id FROM class_extends ORDER BY class_id").fetchall()
            closure = raw.execute(
                "SELECT class_id, ancestor_id FROM class_hierarchy"
                " WHERE ancestor_id = ? AND class_id != ancestor_id ORDER BY class_id",
                (SOURCE,),
            ).fetchall()
            registry = {
                class_id: (name, icon)
                for class_id, name, icon in raw.execute("SELECT id, name, icon FROM class WHERE active = 1").fetchall()
            }
        assert edges == sorted([(class_id, SOURCE) for class_id, _n, _i in classes])
        assert closure == sorted([(class_id, SOURCE) for class_id, _n, _i in classes])
        # Icons land on the registry row (authoritative class config); the
        # class NODE carries them only after a later class.update (the
        # create's own HLC never beats its INSERT — the appliers.ts quirk).
        for class_id, name, icon in classes:
            assert registry[class_id] == (name, icon)

    def test_fixed_uuids_are_unique_block_prefixed_and_withdrawn_never_reused(self) -> None:
        class_ids = [SOURCE, AGENT, SONG, TV_SERIES, CONFERENCE]
        schema_ids = [AUTHORS_SCHEMA]
        assert len(set(class_ids + schema_ids)) == len(class_ids) + len(schema_ids)
        for class_id in class_ids:
            assert class_id.startswith(SYSTEM_CLASS_BLOCK_PREFIX)
        # Withdrawn ids (locator …0018; linkedAuthors …0025, WITHDRAWN
        # 2026-09-27) are dead slots: never assigned to an active schema.
        assert not set(WITHDRAWN_PROPERTY_IDS) & set(schema_ids)


class TestFinalAuthorsSpec:
    def test_authors_is_node_typed_multi_filtered_to_agent(self, store: LocalStore) -> None:
        """FINAL authors model: node-typed to agent nodes. The text-authors
        experiment (verbatim strings, no targetClassFilter) was reversed."""
        _seed_authors_schema(store)
        with sqlite3.connect(_db_path(store)) as raw:
            row = raw.execute(
                "SELECT name, type, multi, scope, target_class_filter FROM property_schema WHERE id = ?",
                (AUTHORS_SCHEMA,),
            ).fetchone()
        assert row == ("authors", "object", 1, "class", json.dumps([AGENT]))

    def test_withdrawn_linked_authors_id_is_never_seeded(self, store: LocalStore) -> None:
        """…0025 (linkedAuthors) was withdrawn with the FINAL authors model;
        no seed envelope may ever carry it again."""
        _seed_authors_schema(store)
        with sqlite3.connect(_db_path(store)) as raw:
            active = {str(row_id) for (row_id,) in raw.execute("SELECT id FROM property_schema")}
        assert active.isdisjoint(WITHDRAWN_PROPERTY_IDS)


class TestRetiredClassMetaClass:
    """M47 seed convergence (owner ruling 2026-10-07): the seeded `class`
    META class …0001 is retired — its instances become real classes (the
    class.create conversion capability, exercised by the class-convert
    fixture) and nothing seeds or hosts on it anymore. The id is a dead
    slot, never reused (the seeds.ts withdrawal)."""

    def test_the_retired_meta_class_id_is_never_seeded(self, store: LocalStore) -> None:
        assert (
            store.apply_remote(
                seed_env(
                    "class.create",
                    {"classId": SOURCE, "contentAst": [{"type": "text", "text": "source"}]},
                    1,
                )
            )
            is True
        )
        with sqlite3.connect(_db_path(store)) as raw:
            seeded = {str(row_id) for (row_id,) in raw.execute("SELECT id FROM nodes WHERE is_class = 1")}
        assert RETIRED_CLASS_META_ID not in seeded

    def test_fixed_uuids_never_collide_with_the_retired_slot(self) -> None:
        class_ids = [SOURCE, AGENT, SONG, TV_SERIES, CONFERENCE, WEBLINK, ASSET]
        assert RETIRED_CLASS_META_ID not in class_ids
        assert len(set(class_ids)) == len(class_ids)
        for class_id in class_ids:
            assert class_id.startswith(SYSTEM_CLASS_BLOCK_PREFIX)


class TestWeblinkExtendsSource:
    """The 2026-10-07 seed convergence: the web link IS a source — weblink
    (…0034) extends source, inherits the family's bibliographic bindings,
    and the SOURCES toggle archives it (it left the always-on manifest)."""

    def test_weblink_fixed_id_extends_source_and_gates_on_sources(self) -> None:
        from notees_gtk.core.protocol.features import (  # noqa: PLC0415
            gating_features_for_class,
            is_always_on_system_class,
            system_class_ancestors,
        )

        assert system_class_ancestors("weblink") == frozenset({"source"})
        assert "weblink" not in system_class_ancestors("source")
        assert gating_features_for_class("weblink") == ("sources",)
        assert not is_always_on_system_class("weblink")

    def test_weblink_replays_as_a_source_family_class(self, store: LocalStore) -> None:
        """The seed-op shape the changed manifest emits: weblink is created
        and extends source like any source-family child (the class-convert
        capability made the registry path a declaration, so the replay
        converges on replicas that never saw the old upsert half-state)."""
        _seed_source_family(store)
        assert (
            store.apply_remote(
                seed_env(
                    "class.create",
                    {"classId": WEBLINK, "contentAst": [{"type": "text", "text": "weblink"}], "icon": "mdiLinkVariant"},
                    2,
                )
            )
            is True
        )
        assert (
            store.apply_remote(seed_env("class.setExtends", {"classId": WEBLINK, "parentClassIds": [SOURCE]}, 10))
            is True
        )
        row = store.node(WS, WEBLINK)
        assert row is not None
        assert row.is_class is True and row.parent_id is None
        assert row.content_plain == "weblink"
        with sqlite3.connect(_db_path(store)) as raw:
            closure = raw.execute(
                "SELECT ancestor_id FROM class_hierarchy WHERE class_id = ? AND ancestor_id = ?",
                (WEBLINK, SOURCE),
            ).fetchall()
        assert closure == [(SOURCE,)]


class TestDeployCatalogFive:
    """The #14 follow-up seed convergence (owner list, 2026-10-06): the
    deploy catalog's missing everyday classes — ``definition`` /
    ``idea`` / ``place`` / ``project`` as plain seeds, ``trip`` extending
    ``event`` so the events-family cascade reaches it. The GTK static map
    never got these five (the seed drift this class pins shut): the map
    gains their fixed UUIDs, icons, and display titles, and the
    gating/family groupings mirror ``features.ts`` exactly (the four plain
    ones stay unmanaged — no gating, not always-on)."""

    FIVE: tuple[tuple[str, str, str, str], ...] = (
        # (seed key, uuid, mdi icon, display title) — the seeds.ts record, pinned.
        ("definition", DEFINITION, "mdiBookOpenPageVariant", "Definition"),
        ("idea", IDEA, "mdiThoughtBubbleOutline", "Idea"),
        ("place", PLACE, "mdiMapMarkerOutline", "Place"),
        ("project", PROJECT, "mdiBriefcaseOutline", "Project"),
        ("trip", TRIP, "mdiAirplane", "Trip"),
    )

    def test_fixed_ids_block_prefixed_unique_and_withdrawn_slots_untouched(self) -> None:
        ids = [class_id for _name, class_id, _icon, _title in self.FIVE]
        assert len(set(ids)) == len(ids)
        for class_id in ids:
            assert class_id.startswith(SYSTEM_CLASS_BLOCK_PREFIX)
        assert set(ids).isdisjoint(WITHDRAWN_CLASS_IDS)
        assert RETIRED_CLASS_META_ID not in ids

    def test_static_map_carries_the_five_with_seeds_icons_and_titles(self) -> None:
        """The drift guard: every one of the five resolves through the GTK
        static map at its fixed id, with the seeds.ts icon + display title —
        a future main-repo seed change that skips this port fails here."""
        from notees_gtk.core.protocol.features import (  # noqa: PLC0415
            SYSTEM_CLASS_DISPLAY_NAMES,
            SYSTEM_CLASS_ICONS,
            system_class_uuid,
        )

        for name, class_id, icon, title in self.FIVE:
            assert system_class_uuid(name) == class_id
            assert SYSTEM_CLASS_ICONS[name] == icon
            assert SYSTEM_CLASS_DISPLAY_NAMES[name] == title

    def test_seed_replay_creates_the_classes_with_display_titles(self, store: LocalStore) -> None:
        """The seed-op shape ``buildSeedEnvelopes`` emits for the five
        (class.create with the display title as content + the mdi icon —
        the server seed authors ``SYSTEM_CLASS_DISPLAY_NAMES`` into the
        text content, title-is-content)."""
        for offset, (_name, class_id, icon, title) in enumerate(self.FIVE):
            assert (
                store.apply_remote(
                    seed_env(
                        "class.create",
                        {"classId": class_id, "contentAst": [{"type": "text", "text": title}], "icon": icon},
                        1 + offset,
                    )
                )
                is True
            )
        for _name, class_id, _icon, title in self.FIVE:
            row = store.node(WS, class_id)
            assert row is not None
            assert row.is_class is True
            assert row.present_as_main is False
            assert row.content_plain == title  # title-is-content: the display title IS the content
            assert row.parent_id is None  # classes are always roots
        with sqlite3.connect(_db_path(store)) as raw:
            registry = {
                class_id: (name, icon)
                for class_id, name, icon in raw.execute("SELECT id, name, icon FROM class WHERE active = 1").fetchall()
            }
        for _name, class_id, icon, title in self.FIVE:
            assert registry[class_id] == (title, icon)

    def test_trip_extends_event_and_replays_through_the_appliers(self, store: LocalStore) -> None:
        assert (
            store.apply_remote(
                seed_env(
                    "class.create",
                    {"classId": EVENT, "contentAst": [{"type": "text", "text": "Event"}], "icon": "mdiCalendar"},
                    1,
                )
            )
            is True
        )
        assert (
            store.apply_remote(
                seed_env(
                    "class.create",
                    {"classId": TRIP, "contentAst": [{"type": "text", "text": "Trip"}], "icon": "mdiAirplane"},
                    2,
                )
            )
            is True
        )
        assert (
            store.apply_remote(seed_env("class.setExtends", {"classId": TRIP, "parentClassIds": [EVENT]}, 10))
            is True
        )
        with sqlite3.connect(_db_path(store)) as raw:
            edges = raw.execute("SELECT class_id, parent_class_id FROM class_extends WHERE class_id = ?", (TRIP,)).fetchall()
            closure = raw.execute(
                "SELECT ancestor_id FROM class_hierarchy WHERE class_id = ? AND ancestor_id = ?",
                (TRIP, EVENT),
            ).fetchall()
        assert edges == [(TRIP, EVENT)]
        assert closure == [(EVENT,)]

    def test_gating_and_family_groupings_mirror_features_ts(self) -> None:
        """features.ts parity: trip joined the events family (the cascade
        set + the gating walk), while the four plain seeds are unmanaged —
        exactly like the TS record, where they are absent from
        ``ALWAYS_ON_SYSTEM_CLASSES`` and gate on nothing."""
        from notees_gtk.core.protocol.features import (  # noqa: PLC0415
            family_class_names,
            gating_features_for_class,
            is_always_on_system_class,
            managed_class_ids,
            system_class_ancestors,
        )

        assert system_class_ancestors("trip") == frozenset({"event"})
        assert family_class_names("events") == ("event", "birthday", "meeting", "trip")
        assert managed_class_ids("events") == (
            EVENT,
            "00000000-0000-0000-0001-000000000041",  # birthday
            "00000000-0000-0000-0001-000000000039",  # meeting
            TRIP,
        )
        assert gating_features_for_class("trip") == ("events",)
        for name in ("definition", "idea", "place", "project"):
            assert system_class_ancestors(name) == frozenset()
            assert gating_features_for_class(name) == ()
            assert not is_always_on_system_class(name)
        assert not is_always_on_system_class("trip")
