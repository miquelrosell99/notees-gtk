"""Search-text parity pins — the ``content`` condition's plaintext source.

The TS contract (``packages/store/src/content.ts`` ``extractSearchPlaintext``,
folded at index time by ``search.ts`` ``foldSearchText``; mirrored client-side
by the web in ``apps/web/src/features/collection/sectionViewResolve.ts``):
mention-resolved content plaintext + ASSET ORIGINAL NAMES (``asset_ref``
tokens, first name per asset, quote/heading children walked) + text-ish
PROPERTY VALUES (authored visible rows; select option labels, the raw id when
the option is gone; numbers in string form; text carrier content one level
deep) + diacritic folding (NFD, strip marks — the folded row meets the
VERBATIM needle: ``contains "peru"`` matches "Perú" while ``contains "perú"``
misses, exactly the TS SQL compiler's semantics).

The GTK mirror stores the derivation in the derived ``nodes.search_plain``
column (the TS ``search_index`` row's counterpart — ``content_plain`` stays
the display excerpt). These pins mirror the web's spec arms: the needle in
the title / the body / an asset original name / a text property value / a
mention target / nowhere — plus the sync/compiler-equivalent arms on the GTK
path: the applier refresh triggers (``object.create``/``object.update``
content writes, ``class.create``/``class.update``, ``property.set``/
``property.unset`` — the TS ``reindexNode`` site list; ``asset.attach``/
``asset.detach`` deliberately do NOT refresh, so an asset name heals only on
the node's next reindex, the documented online-index staleness) and the v27
migration backfill.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import make_server_snapshot

from notees_gtk.core.protocol.clock import Hlc
from notees_gtk.core.protocol.ids import new_uuid7
from notees_gtk.core.protocol.models import PROTOCOL_VERSION, RelayEnvelope
from notees_gtk.data.store import LocalStore

WS = "ws-a"
ACTOR = "actor-0192a000"
DEVICE = "test-device-search-parity"

T0 = 1727200000000

PAGE = "0192a000-0000-7000-8000-000000000101"
BODY = "0192a000-0000-7000-8000-000000000102"
ASSET_HOST = "0192a000-0000-7000-8000-000000000103"
STALE_ASSET_HOST = "0192a000-0000-7000-8000-000000000104"
TARGET = "0192a000-0000-7000-8000-000000000105"
MENTIONER = "0192a000-0000-7000-8000-000000000106"
CUSTOM_LABEL = "0192a000-0000-7000-8000-000000000107"
PROP_NODE = "0192a000-0000-7000-8000-000000000108"
NUMBER_NODE = "0192a000-0000-7000-8000-000000000109"
EMAIL_NODE = "0192a000-0000-7000-8000-00000000010a"
OBJECT_NODE = "0192a000-0000-7000-8000-00000000010b"
OWNER_CARRIER = "0192a000-0000-7000-8000-00000000010c"
CARRIER = "0192a000-0000-7000-8000-00000000010d"
SELECT_NODE = "0192a000-0000-7000-8000-00000000010e"
EMPTY_CONTENT = "0192a000-0000-7000-8000-00000000010f"
CYCLE_A = "0192a000-0000-7000-8000-000000000110"
CYCLE_B = "0192a000-0000-7000-8000-000000000111"
GHOST_SOURCE = "0192a000-0000-7000-8000-000000000112"

ASSET_A = "0192a000-0000-7000-8000-000000000201"
ASSET_B = "0192a000-0000-7000-8000-000000000202"

SCHEMA_TEXT = "0192a000-0000-7000-8000-000000000301"
SCHEMA_SELECT = "0192a000-0000-7000-8000-000000000302"
SCHEMA_NUMBER = "0192a000-0000-7000-8000-000000000303"
SCHEMA_EMAIL = "0192a000-0000-7000-8000-000000000304"
SCHEMA_OBJECT = "0192a000-0000-7000-8000-000000000305"

OPT_HARD = "opt-hard"
OPT_GONE = "opt-gone"
#: A label that is NOT a substring of the option id (and vice versa) — the
#: contains arms must distinguish label vs raw-id indexing exactly.
LABEL_GONE = "Buckram"


def env(op_type: str, payload: dict[str, Any], physical: int) -> RelayEnvelope:
    return RelayEnvelope(
        id=new_uuid7(),
        protocolVersion=PROTOCOL_VERSION,
        workspaceId=WS,
        actorId=ACTOR,
        deviceId=DEVICE,
        hlc=Hlc(physical=physical, logical=0),
        affectedNodeIds=[],
        opType=op_type,
        payload=payload,
        timestamp=datetime.fromtimestamp(physical / 1000, UTC),
    )


def text(value: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": value}]


def q(*conditions: dict[str, Any]) -> dict[str, Any]:
    """An entire-workspace query over the given content conditions."""
    return {
        "version": 2,
        "scope": {"type": "entire_workspace"},
        "root": {"type": "group", "logic": "and", "children": list(conditions)},
    }


def content(op: str, value: Any = None) -> dict[str, Any]:
    condition: dict[str, Any] = {"type": "content", "op": op}
    if value is not None:
        condition["value"] = value
    return condition


def search_plain_of(store: LocalStore, node_id: str) -> str:
    row = store._conn.execute("SELECT search_plain FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return str(row[0]) if row is not None else ""


@pytest.fixture
def world(tmp_path: Any) -> LocalStore:
    """The parity world, built through the real appliers in log order."""
    store = LocalStore(tmp_path / "mirror.db")
    t = lambda i: T0 + i * 1000  # noqa: E731
    envelopes = [
        # Title + diacritics: the folded row drops the acute, keeps the case.
        env("object.create", {"objectId": PAGE, "contentAst": text("Quarterly Report Perú")}, t(1)),
        env(
            "object.create",
            {"objectId": BODY, "contentAst": text("the body keeps nothing"), "parentId": PAGE, "isEmbedded": True},
            t(2),
        ),
        # Asset host: the attach lands BEFORE the content write carrying the
        # asset_ref token, so the create-path refresh (the TS reindexNode at
        # object.create) sees the node_asset row.
        env(
            "asset.attach",
            {
                "objectId": ASSET_HOST,
                "assetId": ASSET_A,
                "hash": "a" * 64,
                "mimeType": "application/pdf",
                "size": 10,
                "originalName": "scanned-receipt.pdf",
            },
            t(3),
        ),
        env(
            "object.create",
            {
                "objectId": ASSET_HOST,
                "contentAst": [
                    {"type": "text", "text": "Attachment: "},
                    {"type": "asset_ref", "assetId": ASSET_A},
                    # A second ref to the SAME asset: first name per asset.
                    {"type": "asset_ref", "assetId": ASSET_A},
                ],
                "parentId": PAGE,
                "isEmbedded": True,
            },
            t(4),
        ),
        # Stale asset host: the content write lands FIRST, the attach after —
        # TS never reindexes on asset.attach, so the name stays OUT of the row.
        env(
            "object.create",
            {
                "objectId": STALE_ASSET_HOST,
                "contentAst": [{"type": "text", "text": "Empty"}, {"type": "asset_ref", "assetId": ASSET_B}],
                "parentId": PAGE,
                "isEmbedded": True,
            },
            t(5),
        ),
        env(
            "asset.attach",
            {
                "objectId": STALE_ASSET_HOST,
                "assetId": ASSET_B,
                "hash": "b" * 64,
                "mimeType": "application/pdf",
                "size": 20,
                "originalName": "late-invoice.pdf",
            },
            t(6),
        ),
        # Mention target + two mentioners (bare and custom-labeled).
        env("object.create", {"objectId": TARGET, "contentAst": text("Q3 Roadmap")}, t(10)),
        env(
            "object.create",
            {
                "objectId": MENTIONER,
                "contentAst": [{"type": "text", "text": "see "}, {"type": "mention", "targetNodeId": TARGET}],
                "parentId": PAGE,
                "isEmbedded": True,
            },
            t(11),
        ),
        env(
            "object.create",
            {
                "objectId": CUSTOM_LABEL,
                "contentAst": [
                    {"type": "mention", "targetNodeId": TARGET, "customLabel": "the plan"},
                ],
                "parentId": PAGE,
                "isEmbedded": True,
            },
            t(12),
        ),
        # Ghost mention: the target id never resolves — the mention folds to
        # nothing (the no-stored-link-text ruling; no captured text exists).
        env(
            "object.create",
            {
                "objectId": GHOST_SOURCE,
                "contentAst": [
                    {"type": "mention", "targetNodeId": "0192a000-0000-7000-8000-000000009999"},
                ],
                "parentId": PAGE,
                "isEmbedded": True,
            },
            t(13),
        ),
        # Mention cycle: A mentions B, B mentions A — the loop point folds,
        # the diamond resolves (content.ts resolveMentionName parity).
        env(
            "object.create",
            {
                "objectId": CYCLE_A,
                "contentAst": [{"type": "text", "text": "alpha see "}, {"type": "mention", "targetNodeId": CYCLE_B}],
                "parentId": PAGE,
                "isEmbedded": True,
            },
            t(14),
        ),
        env(
            "object.create",
            {
                "objectId": CYCLE_B,
                "contentAst": [{"type": "text", "text": "beta see "}, {"type": "mention", "targetNodeId": CYCLE_A}],
                "parentId": PAGE,
                "isEmbedded": True,
            },
            t(15),
        ),
        # Property schemas (global scope — every node is eligible).
        env(
            "propertySchema.create",
            {"propertySchemaId": SCHEMA_TEXT, "name": "Authors", "type": "text"},
            t(20),
        ),
        env(
            "propertySchema.create",
            {
                "propertySchemaId": SCHEMA_SELECT,
                "name": "Binding",
                "type": "select",
                "options": [{"id": OPT_HARD, "label": "Hardback"}, {"id": OPT_GONE, "label": LABEL_GONE}],
            },
            t(21),
        ),
        env(
            "propertySchema.create",
            {"propertySchemaId": SCHEMA_NUMBER, "name": "Pages", "type": "number"},
            t(22),
        ),
        env(
            "propertySchema.create",
            {"propertySchemaId": SCHEMA_EMAIL, "name": "Contact", "type": "email"},
            t(23),
        ),
        env(
            "propertySchema.create",
            {"propertySchemaId": SCHEMA_OBJECT, "name": "Related", "type": "object"},
            t(24),
        ),
        # Text property value + select + number + email + object (structural).
        env("object.create", {"objectId": PROP_NODE, "contentAst": text("Library Entry")}, t(25)),
        env(
            "property.set",
            {"objectId": PROP_NODE, "propertySchemaId": SCHEMA_TEXT, "value": "Umberto Eco", "idx": 0},
            t(26),
        ),
        env("object.create", {"objectId": SELECT_NODE, "contentAst": text("Shelves")}, t(27)),
        env(
            "property.set",
            {"objectId": SELECT_NODE, "propertySchemaId": SCHEMA_SELECT, "value": OPT_GONE, "idx": 0},
            t(28),
        ),
        # The option disappears from the schema AFTER the value indexed its
        # label: the stale label rides the row until the next reindex (the TS
        # online-index staleness), the raw id is NOT yet indexed.
        env(
            "propertySchema.update",
            {"propertySchemaId": SCHEMA_SELECT, "options": [{"id": OPT_HARD, "label": "Hardback"}]},
            t(29),
        ),
        env("object.create", {"objectId": NUMBER_NODE, "contentAst": text("Specs")}, t(30)),
        env(
            "property.set",
            {"objectId": NUMBER_NODE, "propertySchemaId": SCHEMA_NUMBER, "value": 11607, "idx": 0},
            t(31),
        ),
        env("object.create", {"objectId": EMAIL_NODE, "contentAst": text("Directory")}, t(32)),
        env(
            "property.set",
            {"objectId": EMAIL_NODE, "propertySchemaId": SCHEMA_EMAIL, "value": "ada@example.com", "idx": 0},
            t(33),
        ),
        env("object.create", {"objectId": OBJECT_NODE, "contentAst": text("Inbox Zero")}, t(34)),
        env(
            "property.set",
            {"objectId": OBJECT_NODE, "propertySchemaId": SCHEMA_OBJECT, "value": {"nodeId": TARGET}, "idx": 0},
            t(35),
        ),
        # Node-backed text: the carrier's content plaintext rides the OWNER's
        # row, one level deep.
        env("object.create", {"objectId": OWNER_CARRIER, "contentAst": text("Has carrier")}, t(36)),
        env(
            "object.create",
            {
                "objectId": CARRIER,
                "contentAst": text("Deep carrier prose"),
                "parentId": OWNER_CARRIER,
                "isEmbedded": True,
            },
            t(37),
        ),
        env(
            "property.set",
            {"objectId": OWNER_CARRIER, "propertySchemaId": SCHEMA_TEXT, "value": {"nodeId": CARRIER}, "idx": 0},
            t(38),
        ),
        # A node whose own content is empty: the property value alone makes
        # the search row exist (the TS length(s.content) > 0 exists arm).
        env("object.create", {"objectId": EMPTY_CONTENT, "contentAst": []}, t(39)),
        env(
            "property.set",
            {"objectId": EMPTY_CONTENT, "propertySchemaId": SCHEMA_TEXT, "value": "Phantom Value", "idx": 0},
            t(40),
        ),
        # Rename staleness: the target's rename refreshes the TARGET's row
        # only — the mentioner's indexed label stays at the old name (TS
        # online-path behavior; no rename fan-out).
        env("object.update", {"objectId": TARGET, "contentAst": text("Q4 Strategy")}, t(50)),
    ]
    for envelope in envelopes:
        assert store.apply_remote(envelope) is True
    return store


def hits(store: LocalStore, condition: dict[str, Any]) -> list[str]:
    return list(store.run_query_ast(WS, q(condition)).ids)


class TestSearchPlainColumn:
    """Byte pins on the derived row — the extractSearchPlaintext port."""

    def test_title_folds_diacritics_at_derive_time(self, world: LocalStore) -> None:
        assert search_plain_of(world, PAGE) == "Quarterly Report Peru"

    def test_asset_original_name_joins_first_name_per_asset(self, world: LocalStore) -> None:
        assert search_plain_of(world, ASSET_HOST) == "Attachment: scanned-receipt.pdf"

    def test_attach_after_the_content_write_does_not_refresh(self, world: LocalStore) -> None:
        # TS never reindexes on asset.attach/detach — the late name stays out.
        assert search_plain_of(world, STALE_ASSET_HOST) == "Empty"

    def test_bare_mention_resolves_the_targets_current_name(self, world: LocalStore) -> None:
        assert search_plain_of(world, MENTIONER) == "see Q3 Roadmap"

    def test_custom_label_wins_over_the_resolved_name(self, world: LocalStore) -> None:
        assert search_plain_of(world, CUSTOM_LABEL) == "the plan"

    def test_ghost_mention_folds_to_nothing(self, world: LocalStore) -> None:
        assert search_plain_of(world, GHOST_SOURCE) == ""

    def test_mention_cycle_folds_at_the_loop_point(self, world: LocalStore) -> None:
        # B was created after A and derives inside its own create transaction
        # (the row is visible to the same connection): B's mention of A
        # resolves A's name — whose own mention of B resolves ONE hop deep
        # (B's row exists by then) and folds only when the cycle REvisits A
        # (the path guard, content.ts resolveMentionName parity). A's row
        # predates B entirely: its mention folded to nothing at create time.
        assert search_plain_of(world, CYCLE_B) == "beta see alpha see beta see"
        assert search_plain_of(world, CYCLE_A) == "alpha see"

    def test_rename_does_not_fan_out_to_mentioners(self, world: LocalStore) -> None:
        assert search_plain_of(world, TARGET) == "Q4 Strategy"
        assert search_plain_of(world, MENTIONER) == "see Q3 Roadmap"

    def test_text_property_value_appends_to_the_row(self, world: LocalStore) -> None:
        assert search_plain_of(world, PROP_NODE) == "Library Entry Umberto Eco"

    def test_select_value_indexes_the_option_label(self, world: LocalStore) -> None:
        # Indexed at property.set time as the label; the schema update that
        # dropped the option does not reindex (staleness, TS parity).
        assert search_plain_of(world, SELECT_NODE) == f"Shelves {LABEL_GONE}"

    def test_number_value_indexes_in_string_form(self, world: LocalStore) -> None:
        assert search_plain_of(world, NUMBER_NODE) == "Specs 11607"

    def test_email_value_indexes_as_is(self, world: LocalStore) -> None:
        assert search_plain_of(world, EMAIL_NODE) == "Directory ada@example.com"

    def test_object_refs_are_structural_and_stay_out(self, world: LocalStore) -> None:
        assert search_plain_of(world, OBJECT_NODE) == "Inbox Zero"

    def test_text_carrier_content_rides_the_owner_row_one_level(self, world: LocalStore) -> None:
        assert search_plain_of(world, OWNER_CARRIER) == "Has carrier Deep carrier prose"


class TestContainsArms:
    """The compiler's content-contains arms over the parity world."""

    def test_needle_in_title(self, world: LocalStore) -> None:
        assert hits(world, content("contains", "quarterly")) == [PAGE]

    def test_needle_in_body(self, world: LocalStore) -> None:
        assert hits(world, content("contains", "nothing")) == [BODY]

    def test_needle_in_asset_original_name(self, world: LocalStore) -> None:
        assert hits(world, content("contains", "receipt")) == [ASSET_HOST]

    def test_late_asset_name_does_not_match(self, world: LocalStore) -> None:
        assert hits(world, content("contains", "invoice")) == []

    def test_needle_in_text_property_value(self, world: LocalStore) -> None:
        assert hits(world, content("contains", "umberto")) == [PROP_NODE]

    def test_needle_in_select_label_and_the_stale_raw_id_arm(self, world: LocalStore) -> None:
        # The label indexed at set time still matches after the option left
        # the schema; the raw option id is not in the row yet.
        assert hits(world, content("contains", LABEL_GONE.lower())) == [SELECT_NODE]
        assert hits(world, content("contains", OPT_GONE)) == []

    def test_rewriting_the_select_value_reindexes_to_the_raw_id(self, world: LocalStore) -> None:
        newer = env(
            "property.set",
            {"objectId": SELECT_NODE, "propertySchemaId": SCHEMA_SELECT, "value": OPT_GONE, "idx": 0},
            T0 + 60 * 1000,
        )
        assert world.apply_remote(newer) is True
        assert hits(world, content("contains", OPT_GONE)) == [SELECT_NODE]
        assert hits(world, content("contains", LABEL_GONE.lower())) == []

    def test_needle_in_number_string_form(self, world: LocalStore) -> None:
        assert hits(world, content("contains", "11607")) == [NUMBER_NODE]

    def test_needle_in_email_value(self, world: LocalStore) -> None:
        assert hits(world, content("contains", "@example.com")) == [EMAIL_NODE]

    def test_structural_object_refs_do_not_match(self, world: LocalStore) -> None:
        assert OBJECT_NODE not in hits(world, content("contains", "strategy"))

    def test_needle_in_carrier_content_hits_the_owner(self, world: LocalStore) -> None:
        # The carrier block's own row matches too — its content is its row.
        assert hits(world, content("contains", "deep carrier")) == sorted([CARRIER, OWNER_CARRIER])

    def test_needle_in_mention_target_name(self, world: LocalStore) -> None:
        # The target's OLD name is what the mentioner indexed.
        assert hits(world, content("contains", "roadmap")) == [MENTIONER]
        assert hits(world, content("contains", "strategy")) == [TARGET]

    def test_custom_label_mention_matches_the_label_only(self, world: LocalStore) -> None:
        assert hits(world, content("contains", "the plan")) == [CUSTOM_LABEL]
        assert CUSTOM_LABEL not in hits(world, content("contains", "roadmap"))

    def test_folded_needle_matches_diacritics(self, world: LocalStore) -> None:
        assert hits(world, content("contains", "peru")) == [PAGE]

    def test_verbatim_diacritic_needle_misses_the_folded_row(self, world: LocalStore) -> None:
        # The needle is NOT folded at compare time (the TS SQL compiler
        # passes it verbatim; folding is index-time only, one mirror pair).
        assert hits(world, content("contains", "perú")) == []

    def test_nowhere(self, world: LocalStore) -> None:
        assert hits(world, content("contains", "zzzz-absent")) == []

    def test_like_is_ascii_case_insensitive(self, world: LocalStore) -> None:
        assert hits(world, content("contains", "QUARTERLY")) == [PAGE]


class TestUnsetRefreshes:
    def test_property_unset_leaves_the_row_with_the_element(self, world: LocalStore) -> None:
        unset = env(
            "property.unset",
            {"objectId": PROP_NODE, "propertySchemaId": SCHEMA_TEXT, "idx": 0},
            T0 + 70 * 1000,
        )
        assert world.apply_remote(unset) is True
        assert search_plain_of(world, PROP_NODE) == "Library Entry"
        assert hits(world, content("contains", "umberto")) == []


class TestCompareOps:
    def test_eq_compares_the_folded_row_case_sensitively(self, world: LocalStore) -> None:
        assert hits(world, content("eq", "Quarterly Report Peru")) == [PAGE]
        assert hits(world, content("eq", "quarterly report peru")) == []

    def test_exists_reads_the_property_superset_row(self, world: LocalStore) -> None:
        # EMPTY_CONTENT has no content text at all — the property value alone
        # carries the row (the TS length(s.content) > 0 arm). The ghost
        # mention's row folded to empty: exists misses it.
        existing = hits(world, content("exists"))
        assert EMPTY_CONTENT in existing
        assert GHOST_SOURCE not in existing
        assert hits(world, content("contains", "phantom")) == [EMPTY_CONTENT]

    def test_fts_arms_run_over_the_folded_row_with_verbatim_terms(self, world: LocalStore) -> None:
        assert hits(world, content("fts", "peru")) == [PAGE]
        assert hits(world, content("fts", "perú")) == []
        assert hits(world, content("fts", "receipt")) == [ASSET_HOST]
        assert hits(world, content("fts", "umberto")) == [PROP_NODE]


class TestMigrationBackfill:
    def test_v27_backfill_derives_the_column_for_existing_rows(self, tmp_path: Any) -> None:
        path = tmp_path / "s.db"
        store = LocalStore(path)
        store.apply_remote(env("object.create", {"objectId": PAGE, "contentAst": text("Perú Backfill")}, T0 + 1000))
        store.close()
        with sqlite3.connect(path) as raw:
            # Simulate a pre-slice (v26) database: no search_plain column at
            # all — the guarded ALTER and the backfill both run on open.
            raw.execute("ALTER TABLE nodes DROP COLUMN search_plain")
            raw.execute("PRAGMA user_version = 26")
        upgraded = LocalStore(path)
        try:
            columns = {row[1] for row in upgraded._conn.execute("PRAGMA table_info(nodes)")}
            assert "search_plain" in columns
            assert search_plain_of(upgraded, PAGE) == "Peru Backfill"
            assert hits(upgraded, content("contains", "backfill")) == [PAGE]
        finally:
            upgraded.close()

    def test_snapshot_restore_derives_the_column_for_restored_rows(self, tmp_path: Any) -> None:
        store = LocalStore(tmp_path / "mirror.db")
        blob = make_server_snapshot(
            [
                {
                    "id": "snap-node",
                    "workspace_id": WS,
                    "is_class": 0,
                    "is_embedded": 0,
                    "content": '[{"type": "text", "text": "Restored Page"}]',
                    "updated_at": "2026-01-01T00:00:00.000Z",
                }
            ]
        )
        try:
            assert store.restore_snapshot(blob, workspace_id=WS) is True
            assert search_plain_of(store, "snap-node") == "Restored Page"
            assert hits(store, content("contains", "restored")) == ["snap-node"]
        finally:
            store.close()
