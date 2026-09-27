"""Seed-parity lockstep: the seed-manifest deltas from the citations model
revision (monorepo commit `feat(v2): citations model per owner — source
family subclasses, text authors, explicit linkedAuthors`).

The GTK client has NO seed manifest of its own — seeding is server-side
(``buildSeedEnvelopes`` in ``apps/server/src/seed.ts`` driven by
``@notees/domain`` seeds.ts); clients receive seed envelopes through
catch-up like any other ops. This test is the client-side half of the
lockstep: it replays the seed-op SHAPES the changed manifest emits through
the store appliers and pins the fixed UUIDs (never reuse), the system-class
block prefix, the extends parents, and the revised property specs:

- new system classes ``song`` / ``tv_series`` / ``conference``
  (block ``00000000-0000-0000-0001-…``), all ``extends ["source"]``, with
  their fixed ids and mdi icons;
- ``authors`` CHANGED to ``{type: text, multi: true, bindTo: source}`` —
  verbatim strings, never person nodes — same UUID ``…000000000012``, and
  crucially NO ``targetClassFilter`` anymore (the node-typed assumption is
  gone);
- new ``linkedAuthors`` ``{type: object, multi: true, bindTo: source,
  targetClassFilter: [agent]}`` at UUID ``…000000000025`` (note the block:
  the property lives in ``0000-0000-0000-…``, distinct from the ``paper``
  CLASS at ``0000-0000-0001-…025`` used by the object-create fixture).

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

#: Delta property schemas (0000-0000-0000- block — distinct from classes).
AUTHORS_SCHEMA = "00000000-0000-0000-0000-000000000012"
LINKED_AUTHORS_SCHEMA = "00000000-0000-0000-0000-000000000025"


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
            seed_env("class.create", {"classId": SOURCE, "name": "source", "icon": "mdiBookOpenVariant"}, 1)
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
                    seed_env("class.create", {"classId": class_id, "name": name, "icon": icon}, 2 + offset)
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
            assert row.node_type == "class"
            assert row.name == name
            assert row.parent_id is None  # classes are tree-external
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

    def test_fixed_uuids_are_unique_and_block_prefixed(self) -> None:
        class_ids = [SOURCE, AGENT, SONG, TV_SERIES, CONFERENCE]
        schema_ids = [AUTHORS_SCHEMA, LINKED_AUTHORS_SCHEMA]
        assert len(set(class_ids + schema_ids)) == len(class_ids) + len(schema_ids)
        for class_id in class_ids:
            assert class_id.startswith(SYSTEM_CLASS_BLOCK_PREFIX)


class TestRevisedPropertySpecs:
    def test_authors_is_text_multi_without_target_filter(self, store: LocalStore) -> None:
        """The citations revision: verbatim strings, never person nodes — the
        node-typed targetClassFilter assumption is gone from the seed spec."""
        assert (
            store.apply_remote(
                seed_env(
                    "propertySchema.create",
                    {
                        "propertySchemaId": AUTHORS_SCHEMA,
                        "name": "authors",
                        "type": "text",
                        "multi": True,
                        "scope": "class",
                    },
                    2,
                )
            )
            is True
        )
        with sqlite3.connect(_db_path(store)) as raw:
            row = raw.execute(
                "SELECT name, type, multi, scope, target_class_filter FROM property_schema WHERE id = ?",
                (AUTHORS_SCHEMA,),
            ).fetchone()
        assert row == ("authors", "text", 1, "class", None)

    def test_linked_authors_is_object_multi_filtered_to_agent(self, store: LocalStore) -> None:
        assert (
            store.apply_remote(
                seed_env(
                    "propertySchema.create",
                    {
                        "propertySchemaId": LINKED_AUTHORS_SCHEMA,
                        "name": "linkedAuthors",
                        "type": "object",
                        "multi": True,
                        "scope": "class",
                        "targetClassFilter": [AGENT],
                    },
                    3,
                )
            )
            is True
        )
        with sqlite3.connect(_db_path(store)) as raw:
            row = raw.execute(
                "SELECT name, type, multi, target_class_filter FROM property_schema WHERE id = ?",
                (LINKED_AUTHORS_SCHEMA,),
            ).fetchone()
        assert row == ("linkedAuthors", "object", 1, json.dumps([AGENT]))

    def test_authors_and_linked_authors_do_not_collide_with_class_ids(self) -> None:
        """…0000-…025 (linkedAuthors schema) vs …0001-…025 (paper class) are
        different namespaces — the object-create fixture uses the latter."""
        assert LINKED_AUTHORS_SCHEMA != "00000000-0000-0000-0001-000000000025"
