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
- withdrawn ids are never reused (v1 ``locator`` …0018 precedent):
  ``…0025`` (``linkedAuthors``) is WITHDRAWN 2026-09-27, reversed same day.

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
#: dead slots): v1 ``locator`` …0018, and …0025 (``linkedAuthors``) —
#: WITHDRAWN 2026-09-27, reversed same day in the FINAL authors model.
WITHDRAWN_PROPERTY_IDS = (
    "00000000-0000-0000-0000-000000000018",
    "00000000-0000-0000-0000-000000000025",
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
        # Withdrawn ids (v1 locator …0018; linkedAuthors …0025, WITHDRAWN
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
