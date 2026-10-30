"""Seed-parity lockstep: the source-library roster (monorepo
source-library branch — the renames/port slice).

The GTK client has NO seed manifest of its own — seeding is server-side
(``buildSeedEnvelopes`` in ``apps/server/src/seed.ts`` driven by
``@notees/domain`` seeds.ts); clients receive seed envelopes through
catch-up like any other ops. This test is the client-side half of the
lockstep: it replays the seed-op SHAPES the changed manifest emits through
the store appliers and pins the fixed UUIDs (never reuse), the
system-class block prefix, the extends parents, and the property specs:

- the source-library renames: ``article`` /
  ``weblink`` / ``movie`` become ``journal_article`` / ``web_page`` /
  ``film`` — the KEYS change, the fixed UUIDs stay (…0026/…0034/…0035);
  ``paper`` (…0025) is WITHDRAWN (Zotero has no "paper" — nodes migrate to
  journal_article), the id a dead slot;
- 30 NEW source type-classes (…0048-…0065, hex, alphabetical by key), all
  ``extends ["source"]``, with the 18-name archived-by-default long tail
  (``SYSTEM_ARCHIVED_BY_DEFAULT_CLASSES``);
- the authorship model: ``authors`` RETYPED to free
  text (``{type: text, multi: true, bindTo: source}`` at the unchanged
  …0012), ``linkedAuthors`` REINSTATED as the node-typed experiment at its
  NEW UUID ``…0006-…0001`` (``{type: object, multi: true, bindTo: source,
  targetClassFilter: [agent]}``), ``editors`` new at ``…0006-…0002``
  (``{type: text, multi: true, bindTo: source}``) — the retired …0025
  linkedAuthors id stays burned;
- withdrawn ids are never reused (the ``locator`` …0018 precedent): the
  property slot …0025 (the 2026-09-27 ``linkedAuthors`` withdrawal) and the
  class slot …0025 (``paper``, withdrawn 2026-10-23), plus the retired
  `class` meta class …0001 and `cover` …0042;
- the #14 follow-up five: ``definition`` /
  ``idea`` / ``place`` / ``project`` / ``trip`` (…0043-…0047), plain seeds
  with their mdi icons + display titles; ``trip`` extends ``event`` so the
  events-family toggle cascades to it — the gating/family groupings mirror
  ``features.ts`` (the four plain ones stay unmanaged: no gating, not
  always-on);
- the workflows slice 2 pair: ``question`` / ``plan`` (…0066/…0067), the
  everyday classes the research workflow reuses — deploy-catalog plain
  seeds per the #14 follow-up's ruling (no extends, unmanaged, the same
  static-map coverage guard).

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

#: Withdrawn system property ids — never reuse (seeds.ts registers them as
#: dead slots): the ``locator`` …0018, and …0025 — WITHDRAWN 2026-09-27
#: with the old ``linkedAuthors``; the 2026-10-23 reinstatement mints a NEW
#: id in the …0006- bibliographic pool instead of reusing it.
WITHDRAWN_PROPERTY_IDS = (
    "00000000-0000-0000-0000-000000000018",
    "00000000-0000-0000-0000-000000000025",
)

#: WITHDRAWN 2026-10-07: the seeded `class` META class …0001
#: is retired — nodes bound to it become REAL classes (the class.create
#: conversion capability) and the seed no longer emits it. Never reuse
#: (the seeds.ts withdrawal comment, the locator …0018 precedent).
RETIRED_CLASS_META_ID = "00000000-0000-0000-0001-000000000001"

#: The weblink class joined the source tree:
#: …0034 extends source, so the SOURCES family toggle archives it. The
#: source-library rename re-keyed it ``web_page`` — the
#: fixed UUID is unchanged.
WEB_PAGE = "00000000-0000-0000-0001-000000000034"
JOURNAL_ARTICLE = "00000000-0000-0000-0001-000000000026"
FILM = "00000000-0000-0000-0001-000000000035"
#: WITHDRAWN 2026-10-23 (source-library roster): ``paper`` — Zotero has no
#: "paper", nodes migrate to journal_article. Dead slot, never reused.
PAPER = "00000000-0000-0000-0001-000000000025"
ASSET = "00000000-0000-0000-0001-000000000009"

#: The retyped authorship schemas: ``authors`` keeps its
#: long-standing …0012 id but flips object→text (multi); ``linkedAuthors``
#: is REINSTATED at its new pool UUID …0006-…0001 (the 2026-09-27 …0025
#: withdrawal stays burned) as the node-typed experiment, filtered to
#: agent; ``editors`` is new at …0006-…0002 (text, multi).
AUTHORS_SCHEMA = "00000000-0000-0000-0000-000000000012"
LINKED_AUTHORS_SCHEMA = "00000000-0000-0000-0006-000000000001"
EDITORS_SCHEMA = "00000000-0000-0000-0006-000000000002"

#: The #14 follow-up five (seeds.ts …0043-…0047):
#: the deploy catalog's missing everyday classes, plain seeds (zero wire
#: cost, seed convergence only). trip extends event — a trip is
#: calendar-bound, so the events toggle cascades to it.
DEFINITION = "00000000-0000-0000-0001-000000000043"
IDEA = "00000000-0000-0000-0001-000000000044"
PLACE = "00000000-0000-0000-0001-000000000045"
PROJECT = "00000000-0000-0000-0001-000000000046"
TRIP = "00000000-0000-0000-0001-000000000047"
EVENT = "00000000-0000-0000-0001-000000000040"

#: Workflows slice 2 (2026-10-30): the everyday `question`/`plan` classes
#: (seeds.ts …0066/…0067) — deploy-catalog plain seeds per the #14
#: follow-up's ruling (zero wire cost, seed convergence only). No extends
#: (neither IS anything else), unmanaged like the four plain #14 seeds.
QUESTION = "00000000-0000-0000-0001-000000000066"
PLAN = "00000000-0000-0000-0001-000000000067"

#: An unrelated user class authored after the seed (the later-extends
#: rebuild probe).
USER_CLASS = "0192a000-0000-7000-8000-0000000005c1"

#: The WITHDRAWN class slots the five must never collide with: …0001 (the
#: seeded `class` meta class, retired 2026-10-07), …0042 (`cover`,
#: withdrawn 2026-10-04 the day it was minted), and …0025 (`paper`,
#: withdrawn 2026-10-23 with the source-library roster — Zotero has no
#: "paper") — dead slots, never reused (the seeds.ts withdrawal comments,
#: the locator …0018 precedent).
WITHDRAWN_CLASS_IDS = (
    "00000000-0000-0000-0001-000000000001",
    "00000000-0000-0000-0001-000000000025",
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


def _seed_authorship_schemas(store: LocalStore) -> None:
    """The 2026-10-23 authorship trio as the seed emits it (source-library
    slice): authors retyped to free text (multi), linkedAuthors reinstated
    node-typed to agent, editors plain text multi — all LOCAL to source
    (the seed-scope audit)."""
    for offset, payload in enumerate(
        (
            {
                "propertySchemaId": AUTHORS_SCHEMA,
                "name": "authors",
                "type": "text",
                "multi": True,
                "scopeClassIds": [SOURCE],
            },
            {
                "propertySchemaId": LINKED_AUTHORS_SCHEMA,
                "name": "linkedAuthors",
                "type": "object",
                "multi": True,
                "scopeClassIds": [SOURCE],
                "targetClassFilter": [AGENT],
            },
            {
                "propertySchemaId": EDITORS_SCHEMA,
                "name": "editors",
                "type": "text",
                "multi": True,
                "scopeClassIds": [SOURCE],
            },
        )
    ):
        assert store.apply_remote(seed_env("propertySchema.create", payload, 2 + offset)) is True


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
            assert row.is_embedded is False  # classes converge to the standalone 0
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
        schema_ids = [AUTHORS_SCHEMA, LINKED_AUTHORS_SCHEMA, EDITORS_SCHEMA]
        assert len(set(class_ids + schema_ids)) == len(class_ids) + len(schema_ids)
        for class_id in class_ids:
            assert class_id.startswith(SYSTEM_CLASS_BLOCK_PREFIX)
        for schema_id in schema_ids:
            assert schema_id.startswith("00000000-0000-0000-000")
        # Withdrawn ids (locator …0018; the 2026-09-27 linkedAuthors …0025)
        # are dead slots: never assigned to an active schema — the 2026-10-23
        # reinstatement minted …0006-…0001 instead.
        assert not set(WITHDRAWN_PROPERTY_IDS) & set(schema_ids)


class TestAuthorshipSpecs:
    """The 2026-10-23 authorship model (source-library slice): ``authors``
    is free TEXT (multi) — the node-typed experiment moved to its own
    reinstated ``linkedAuthors`` schema; ``editors`` joins as plain text.
    The 2026-09-27 ``linkedAuthors`` id (…0025) stays burned — the
    reinstatement mints …0006-…0001."""

    def test_authors_is_free_text_multi_local_to_source(self, store: LocalStore) -> None:
        _seed_authorship_schemas(store)
        with sqlite3.connect(_db_path(store)) as raw:
            row = raw.execute(
                "SELECT name, type, multi, scope_class_ids, target_class_filter FROM property_schema WHERE id = ?",
                (AUTHORS_SCHEMA,),
            ).fetchone()
        # Text schemas take no targetClassFilter — the object filter is
        # superseded by the retype (seeds.ts SYSTEM_PROPERTY_SPECS parity).
        assert row == ("authors", "text", 1, json.dumps([SOURCE]), None)

    def test_linked_authors_is_reinstated_node_typed_filtered_to_agent(self, store: LocalStore) -> None:
        _seed_authorship_schemas(store)
        with sqlite3.connect(_db_path(store)) as raw:
            row = raw.execute(
                "SELECT name, type, multi, scope_class_ids, target_class_filter FROM property_schema WHERE id = ?",
                (LINKED_AUTHORS_SCHEMA,),
            ).fetchone()
        assert row == ("linkedAuthors", "object", 1, json.dumps([SOURCE]), json.dumps([AGENT]))

    def test_editors_is_text_multi(self, store: LocalStore) -> None:
        _seed_authorship_schemas(store)
        with sqlite3.connect(_db_path(store)) as raw:
            row = raw.execute(
                "SELECT name, type, multi, scope_class_ids, target_class_filter FROM property_schema WHERE id = ?",
                (EDITORS_SCHEMA,),
            ).fetchone()
        assert row == ("editors", "text", 1, json.dumps([SOURCE]), None)

    def test_withdrawn_linked_authors_id_is_never_seeded(self, store: LocalStore) -> None:
        """…0025 (linkedAuthors, 2026-09-27 withdrawal) stays burned; the
        2026-10-23 reinstatement minted …0006-…0001 instead."""
        _seed_authorship_schemas(store)
        with sqlite3.connect(_db_path(store)) as raw:
            active = {str(row_id) for (row_id,) in raw.execute("SELECT id FROM property_schema")}
        assert active.isdisjoint(WITHDRAWN_PROPERTY_IDS)


class TestRetiredClassMetaClass:
    """M47 seed convergence: the seeded `class`
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
        class_ids = [SOURCE, AGENT, SONG, TV_SERIES, CONFERENCE, WEB_PAGE, ASSET]
        assert RETIRED_CLASS_META_ID not in class_ids
        assert len(set(class_ids)) == len(class_ids)
        for class_id in class_ids:
            assert class_id.startswith(SYSTEM_CLASS_BLOCK_PREFIX)


class TestWebPageExtendsSource:
    """The 2026-10-07 seed convergence: the web page IS a source — …0034
    extends source, inherits the family's bibliographic bindings, and the
    SOURCES toggle archives it (it left the always-on manifest). The
    source-library rename re-keyed the class
    ``weblink`` → ``web_page``; the fixed UUID is unchanged."""

    def test_web_page_fixed_id_extends_source_and_gates_on_sources(self) -> None:
        from notees_gtk.core.protocol.features import (  # noqa: PLC0415
            gating_features_for_class,
            is_always_on_system_class,
            system_class_ancestors,
            system_class_uuid,
        )

        assert system_class_uuid("web_page") == WEB_PAGE
        assert system_class_ancestors("web_page") == frozenset({"source"})
        assert "web_page" not in system_class_ancestors("source")
        assert gating_features_for_class("web_page") == ("sources",)
        assert not is_always_on_system_class("web_page")

    def test_web_page_replays_as_a_source_family_class(self, store: LocalStore) -> None:
        """The seed-op shape the changed manifest emits: web_page is created
        and extends source like any source-family child (the class-convert
        capability made the registry path a declaration, so the replay
        converges on replicas that never saw the old upsert half-state)."""
        _seed_source_family(store)
        assert (
            store.apply_remote(
                seed_env(
                    "class.create",
                    {"classId": WEB_PAGE, "contentAst": [{"type": "text", "text": "Web page"}], "icon": "mdiLinkVariant"},
                    2,
                )
            )
            is True
        )
        assert (
            store.apply_remote(seed_env("class.setExtends", {"classId": WEB_PAGE, "parentClassIds": [SOURCE]}, 10))
            is True
        )
        row = store.node(WS, WEB_PAGE)
        assert row is not None
        assert row.is_class is True and row.parent_id is None
        assert row.content_plain == "Web page"  # the seed authors the display title
        with sqlite3.connect(_db_path(store)) as raw:
            closure = raw.execute(
                "SELECT ancestor_id FROM class_hierarchy WHERE class_id = ? AND ancestor_id = ?",
                (WEB_PAGE, SOURCE),
            ).fetchall()
        assert closure == [(SOURCE,)]


class TestDeployCatalogFive:
    """The #14 follow-up seed convergence: the
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
            assert row.is_embedded is False  # classes converge to the standalone 0
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


class TestWorkflowsEverydayPair:
    """The workflows slice 2 seed convergence: the everyday ``question`` /
    ``plan`` classes (seeds.ts …0066/…0067) the research workflow reuses.
    Deploy-catalog plain seeds per the #14 follow-up's ruling — zero wire
    cost, seed convergence only: no extends (neither IS anything else), no
    parentlessRole flag, not archived by default, unmanaged (no gating, not
    always-on) exactly like the four plain #14 seeds."""

    PAIR: tuple[tuple[str, str, str, str], ...] = (
        # (seed key, uuid, mdi icon, display title) — the seeds.ts record, pinned.
        ("question", QUESTION, "mdiHelpCircleOutline", "Question"),
        ("plan", PLAN, "mdiClipboardTextOutline", "Plan"),
    )

    def test_fixed_ids_continue_the_roster_block_contiguously(self) -> None:
        ids = [class_id for _name, class_id, _icon, _title in self.PAIR]
        assert len(set(ids)) == len(ids)
        for class_id in ids:
            assert class_id.startswith(SYSTEM_CLASS_BLOCK_PREFIX)
        assert set(ids).isdisjoint(WITHDRAWN_CLASS_IDS)
        # …0066/…0067 are the two slots right after the source-library
        # roster's …0065 — no gaps, no collisions with the withdrawn slots.
        suffixes = [int(class_id.rsplit("-", 1)[1], 16) for class_id in ids]
        assert suffixes == [0x66, 0x67]

    def test_static_map_carries_the_pair_with_seeds_icons_and_titles(self) -> None:
        """The drift guard: both resolve through the GTK static map at their
        fixed id, with the seeds.ts icon + display title — a future
        main-repo seed change that skips this port fails here."""
        from notees_gtk.core.protocol.features import (  # noqa: PLC0415
            SYSTEM_CLASS_DISPLAY_NAMES,
            SYSTEM_CLASS_ICONS,
            system_class_uuid,
        )

        for name, class_id, icon, title in self.PAIR:
            assert system_class_uuid(name) == class_id
            assert SYSTEM_CLASS_ICONS[name] == icon
            assert SYSTEM_CLASS_DISPLAY_NAMES[name] == title

    def test_seed_replay_creates_the_classes_with_display_titles(self, store: LocalStore) -> None:
        """The seed-op shape ``buildSeedEnvelopes`` emits for the pair
        (class.create with the display title as content + the mdi icon —
        the server seed authors ``SYSTEM_CLASS_DISPLAY_NAMES`` into the
        text content, title-is-content)."""
        for offset, (_name, class_id, icon, title) in enumerate(self.PAIR):
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
        for _name, class_id, _icon, title in self.PAIR:
            row = store.node(WS, class_id)
            assert row is not None
            assert row.is_class is True
            assert row.is_embedded is False  # classes converge to the standalone 0
            assert row.content_plain == title  # title-is-content: the display title IS the content
            assert row.parent_id is None  # classes are always roots
        with sqlite3.connect(_db_path(store)) as raw:
            registry = {
                class_id: (name, icon)
                for class_id, name, icon in raw.execute("SELECT id, name, icon FROM class WHERE active = 1").fetchall()
            }
        for _name, class_id, icon, title in self.PAIR:
            assert registry[class_id] == (title, icon)

    def test_gating_and_family_groupings_mirror_features_ts(self) -> None:
        """features.ts parity: the pair are plain seeds — no extends, so
        no family cascade and no gating, and absent from the always-on set
        exactly like the TS record."""
        from notees_gtk.core.protocol.features import (  # noqa: PLC0415
            SYSTEM_ARCHIVED_BY_DEFAULT_CLASSES,
            SYSTEM_CLASS_PARENTLESS_ROLE_BLOCK,
            gating_features_for_class,
            is_always_on_system_class,
            system_class_ancestors,
        )

        for name in ("question", "plan"):
            assert system_class_ancestors(name) == frozenset()
            assert gating_features_for_class(name) == ()
            assert not is_always_on_system_class(name)
            assert name not in SYSTEM_ARCHIVED_BY_DEFAULT_CLASSES
            assert name not in SYSTEM_CLASS_PARENTLESS_ROLE_BLOCK


#: The 30 NEW source type-classes (source-library slice):
#: seeds.ts …0048–…0065, assigned alphabetically by key — pinned verbatim.
NEW_SOURCE_CLASSES: tuple[tuple[str, str], ...] = (
    ("artwork", "00000000-0000-0000-0001-000000000048"),
    ("audio_recording", "00000000-0000-0000-0001-000000000049"),
    ("bill", "00000000-0000-0000-0001-00000000004a"),
    ("blog_post", "00000000-0000-0000-0001-00000000004b"),
    ("book_section", "00000000-0000-0000-0001-00000000004c"),
    ("computer_program", "00000000-0000-0000-0001-00000000004d"),
    ("conference_paper", "00000000-0000-0000-0001-00000000004e"),
    ("dataset", "00000000-0000-0000-0001-00000000004f"),
    ("dictionary_entry", "00000000-0000-0000-0001-000000000050"),
    ("email", "00000000-0000-0000-0001-000000000051"),
    ("encyclopedia_article", "00000000-0000-0000-0001-000000000052"),
    ("forum_post", "00000000-0000-0000-0001-000000000053"),
    ("hearing", "00000000-0000-0000-0001-000000000054"),
    ("instant_message", "00000000-0000-0000-0001-000000000055"),
    ("interview", "00000000-0000-0000-0001-000000000056"),
    ("letter", "00000000-0000-0000-0001-000000000057"),
    ("magazine_article", "00000000-0000-0000-0001-000000000058"),
    ("manuscript", "00000000-0000-0000-0001-000000000059"),
    ("map", "00000000-0000-0000-0001-00000000005a"),
    ("newspaper_article", "00000000-0000-0000-0001-00000000005b"),
    ("patent", "00000000-0000-0000-0001-00000000005c"),
    ("podcast", "00000000-0000-0000-0001-00000000005d"),
    ("preprint", "00000000-0000-0000-0001-00000000005e"),
    ("presentation", "00000000-0000-0000-0001-00000000005f"),
    ("radio_broadcast", "00000000-0000-0000-0001-000000000060"),
    ("report", "00000000-0000-0000-0001-000000000061"),
    ("standard", "00000000-0000-0000-0001-000000000062"),
    ("statute", "00000000-0000-0000-0001-000000000063"),
    ("tv_broadcast", "00000000-0000-0000-0001-000000000064"),
    ("video_recording", "00000000-0000-0000-0001-000000000065"),
)

#: The renamed classes keep their fixed UUIDs: the seed
#: KEYS become Zotero-faithful, the ids never change.
RENAMED_CLASSES: tuple[tuple[str, str, str], ...] = (
    # (new key, fixed uuid, old key)
    ("journal_article", JOURNAL_ARTICLE, "article"),
    ("web_page", WEB_PAGE, "weblink"),
    ("film", FILM, "movie"),
)


class TestSourceLibraryRoster:
    """The source-library roster seed convergence:
    30 new source type-classes (…0048–…0065, all extends source), the
    article/weblink/movie → journal_article/web_page/film renames (UUIDs
    kept), and paper (…0025) withdrawn. The static map gains the fixed ids
    + extends edges + the seeds.ts icons/display titles, and the roster
    replays through the appliers like the server seed emits it — class
    creates with the display titles, setExtends [source] edges, then the
    archived-by-default flips (``object.update {active: false}``), which
    archival is VISIBILITY state: the archived classes must still resolve
    through the extends closure."""

    def test_fixed_ids_are_unique_block_prefixed_and_withdrawn_slots_untouched(self) -> None:
        ids = [class_id for _name, class_id in NEW_SOURCE_CLASSES]
        assert len(ids) == 30
        assert len(set(ids)) == 30
        for class_id in ids:
            assert class_id.startswith(SYSTEM_CLASS_BLOCK_PREFIX)
        assert set(ids).isdisjoint(WITHDRAWN_CLASS_IDS)
        # The roster continues the …0043–…0047 follow-up block contiguously
        # (…0048–…0065 hex) — no gaps, no collisions with the withdrawn slots.
        suffixes = [int(class_id.rsplit("-", 1)[1], 16) for class_id in ids]
        assert suffixes == list(range(0x48, 0x66))

    def test_the_static_map_carries_the_roster_with_seeds_icons_and_titles(self) -> None:
        """The drift guard: every roster class resolves through the GTK
        static map at its fixed id, extends source, and carries the
        seeds.ts icon + display title — a future main-repo seed change
        that skips this port fails here."""
        from notees_gtk.core.protocol.features import (  # noqa: PLC0415
            SYSTEM_ARCHIVED_BY_DEFAULT_CLASSES,
            SYSTEM_CLASS_DISPLAY_NAMES,
            SYSTEM_CLASS_ICONS,
            system_class_ancestors,
            system_class_uuid,
        )

        for name, class_id in NEW_SOURCE_CLASSES:
            assert system_class_uuid(name) == class_id
            assert system_class_ancestors(name) == frozenset({"source"})
            assert SYSTEM_CLASS_ICONS[name]  # the seeds.ts mdi icon, pinned in test_features
            assert SYSTEM_CLASS_DISPLAY_NAMES[name]
        # The renamed classes resolve under their new keys at the old ids…
        for name, class_id, _old_key in RENAMED_CLASSES:
            assert system_class_uuid(name) == class_id
            assert system_class_ancestors(name) == frozenset({"source"})
        # …and the old keys left the map entirely.
        with pytest.raises(KeyError):
            system_class_uuid("article")
        with pytest.raises(KeyError):
            system_class_uuid("weblink")
        with pytest.raises(KeyError):
            system_class_uuid("movie")
        with pytest.raises(KeyError):
            system_class_uuid("paper")
        # The archived-by-default long tail: 18 names, all roster members.
        assert len(SYSTEM_ARCHIVED_BY_DEFAULT_CLASSES) == 18
        for name in SYSTEM_ARCHIVED_BY_DEFAULT_CLASSES:
            assert (name, system_class_uuid(name)) in NEW_SOURCE_CLASSES

    def test_roster_replays_through_the_appliers_with_the_archived_bit(self, store: LocalStore) -> None:
        """The seed-op stream ``buildSeedEnvelopes`` emits for the roster
        (the shapes the monorepo's source-seed store test pins): class
        creates with the display titles + icons, setExtends [source], then
        the archived-by-default flips. Archival is a VISIBILITY state, not
        an absence — the archived classes keep their extends closure."""
        from notees_gtk.core.protocol.features import (  # noqa: PLC0415
            SYSTEM_ARCHIVED_BY_DEFAULT_CLASSES,
            SYSTEM_CLASS_DISPLAY_NAMES,
            SYSTEM_CLASS_ICONS,
        )

        archived = set(SYSTEM_ARCHIVED_BY_DEFAULT_CLASSES)
        _seed_source_family(store)
        physical = 2
        for name, class_id in NEW_SOURCE_CLASSES:
            assert (
                store.apply_remote(
                    seed_env(
                        "class.create",
                        {
                            "classId": class_id,
                            "contentAst": [{"type": "text", "text": SYSTEM_CLASS_DISPLAY_NAMES[name]}],
                            "icon": SYSTEM_CLASS_ICONS[name],
                        },
                        physical,
                    )
                )
                is True
            )
            physical += 1
        for _name, class_id in NEW_SOURCE_CLASSES:
            assert (
                store.apply_remote(seed_env("class.setExtends", {"classId": class_id, "parentClassIds": [SOURCE]}, physical))
                is True
            )
            physical += 1
        for name, class_id in NEW_SOURCE_CLASSES:
            if name not in archived:
                continue
            assert (
                store.apply_remote(seed_env("object.update", {"objectId": class_id, "active": False}, physical))
                is True
            )
            physical += 1

        for name, class_id in NEW_SOURCE_CLASSES:
            row = store.node(WS, class_id)
            assert row is not None
            assert row.is_class is True
            assert row.content_plain == SYSTEM_CLASS_DISPLAY_NAMES[name]
        with sqlite3.connect(_db_path(store)) as raw:
            closure = raw.execute(
                "SELECT class_id, ancestor_id FROM class_hierarchy"
                " WHERE ancestor_id = ? AND class_id != ancestor_id ORDER BY class_id",
                (SOURCE,),
            ).fetchall()
            active_map = {
                str(class_id): bool(is_active)
                for class_id, is_active in raw.execute("SELECT id, active FROM class").fetchall()
            }
        assert closure == sorted((class_id, SOURCE) for _name, class_id in NEW_SOURCE_CLASSES)
        for name, class_id in NEW_SOURCE_CLASSES:
            # The 18 long-tail classes seed archived; the 12 everyday types stay enabled.
            assert active_map[class_id] is (name not in archived)

    def test_archived_classes_survive_a_later_extends_rebuild(self, store: LocalStore) -> None:
        """Any live workspace keeps extending classes after the seed — a
        later setExtends rebuilds the whole closure: the archived roster
        classes must still resolve through it (the monorepo source-seed
        test pins the same)."""
        from notees_gtk.core.protocol.features import SYSTEM_ARCHIVED_BY_DEFAULT_CLASSES  # noqa: PLC0415

        archived_name = SYSTEM_ARCHIVED_BY_DEFAULT_CLASSES[0]
        archived_id = dict(NEW_SOURCE_CLASSES)[archived_name]
        _seed_source_family(store)
        assert (
            store.apply_remote(
                seed_env(
                    "class.create",
                    {"classId": archived_id, "contentAst": [{"type": "text", "text": archived_name}]},
                    2,
                )
            )
            is True
        )
        assert (
            store.apply_remote(seed_env("class.setExtends", {"classId": archived_id, "parentClassIds": [SOURCE]}, 3))
            is True
        )
        assert (
            store.apply_remote(seed_env("object.update", {"objectId": archived_id, "active": False}, 4))
            is True
        )
        # An unrelated user class extends source afterwards (the rebuild).
        assert (
            store.apply_remote(
                seed_env(
                    "class.create",
                    {"classId": USER_CLASS, "contentAst": [{"type": "text", "text": "user class"}]},
                    5,
                )
            )
            is True
        )
        assert (
            store.apply_remote(seed_env("class.setExtends", {"classId": USER_CLASS, "parentClassIds": [SOURCE]}, 6))
            is True
        )
        with sqlite3.connect(_db_path(store)) as raw:
            closure = raw.execute(
                "SELECT ancestor_id FROM class_hierarchy WHERE class_id = ? AND ancestor_id = ?",
                (archived_id, SOURCE),
            ).fetchall()
            active = raw.execute("SELECT active FROM class WHERE id = ?", (archived_id,)).fetchone()
        assert closure == [(SOURCE,)]
        assert active == (0,)
