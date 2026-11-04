"""Tests for the derived reference-edge index (the ``edge`` + ``node_link``
tables — ``packages/store/src/edges.ts`` parity).

The edge index is never authored: the appliers rebuild a source node's edges
from its stored content (mentions, typed links — top level and inside
quote/heading children), its visible-set property values (node-typed refs,
verb = propertySchemaId, datetime refs fanning out to the deterministic
chain ancestors), and its tag membership (the 2026-11-03 "tags are links"
ruling). Edge ids are deterministic (sha256 over source/type/target/verb/
metadata/occurrence), so wipe -> replay -> identical rows.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from notees_gtk.core.protocol.clock import Hlc
from notees_gtk.core.protocol.dates import day_node_id, month_node_id, year_node_id
from notees_gtk.core.protocol.ids import new_uuid7
from notees_gtk.core.protocol.models import PROTOCOL_VERSION, RelayEnvelope
from notees_gtk.data.store import LocalStore

WS_A = "ws-a"
ACTOR = "actor-1"
BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)


def uid(label: str) -> str:
    """Deterministic test UUID for an id label (test_store.py parity)."""
    return str(uuid5(NAMESPACE_URL, f"notees-gtk/test/{label}"))


def make_env(
    op_type: str,
    payload: dict[str, Any],
    *,
    hlc: tuple[int, int] = (1, 0),
    env_id: str | None = None,
    workspace_id: str = WS_A,
    actor_id: str = ACTOR,
    affected: tuple[str, ...] = (),
) -> RelayEnvelope:
    """Build a minimal valid envelope for store tests."""
    return RelayEnvelope(
        id=env_id or new_uuid7(),
        protocolVersion=PROTOCOL_VERSION,
        workspaceId=workspace_id,
        actorId=actor_id,
        deviceId="edges-test-device",
        hlc=Hlc(physical=hlc[0], logical=hlc[1]),
        affectedNodeIds=list(affected),
        opType=op_type,
        payload=payload,
        timestamp=BASE_TS,
    )


def create_env(
    node_id: str,
    *,
    parent_id: str | None = None,
    content: Any = None,
    tag_ids: tuple[str, ...] = (),
    hlc: tuple[int, int] = (1, 0),
) -> RelayEnvelope:
    """An ``object.create`` envelope; ``content`` is a raw token list."""
    payload: dict[str, Any] = {"objectId": node_id, "parentId": parent_id}
    if tag_ids:
        payload["tagIds"] = list(tag_ids)
    if content is not None:
        payload["contentAst"] = content
    return make_env("object.create", payload, hlc=hlc, affected=(node_id,))


def update_env(node_id: str, *, hlc: tuple[int, int], **fields: Any) -> RelayEnvelope:
    payload: dict[str, Any] = {"objectId": node_id}
    payload.update(fields)
    return make_env("object.update", payload, hlc=hlc, affected=(node_id,))


def delete_env(node_id: str, *, permanent: bool = False, hlc: tuple[int, int]) -> RelayEnvelope:
    payload: dict[str, Any] = {"objectId": node_id}
    if permanent:
        payload["permanent"] = True
    return make_env("object.delete", payload, hlc=hlc, affected=(node_id,))


def property_set_env(
    node_id: str, schema_id: str, *, value: Any, idx: int = 0, hlc: tuple[int, int]
) -> RelayEnvelope:
    return make_env(
        "property.set",
        {"objectId": node_id, "propertySchemaId": schema_id, "value": value, "idx": idx},
        hlc=hlc,
        affected=(node_id,),
    )


def mention(target: str, *, label: str = "m", link_id: str | None = None, embedded: bool = False) -> dict[str, Any]:
    token: dict[str, Any] = {"type": "mention", "targetNodeId": target, "customLabel": label}
    if link_id is not None:
        token["linkId"] = link_id
    if embedded:
        token["isEmbedded"] = True
    return token


def edges_of(store: LocalStore, source_id: str) -> list[tuple[Any, ...]]:
    """The source's edge rows in id order."""
    with sqlite3.connect(store._conn.execute("PRAGMA database_list").fetchone()[2]) as raw:  # noqa: SLF001
        return raw.execute(
            "SELECT type, target_id, resolved_target_id, verb, metadata, is_embedded, created_at"
            " FROM edge WHERE source_id = ? ORDER BY id",
            (source_id,),
        ).fetchall()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[LocalStore]:
    instance = LocalStore(tmp_path / "store.db")
    try:
        yield instance
    finally:
        instance.close()


def make_embedded_source(
    store: LocalStore, source_id: str, *, content: list[Any], hlc: tuple[int, int] = (1, 0)
) -> str:
    """A parent page + an embedded child block carrying ``content``; the
    parent id is returned. The child is the edge source under test."""
    parent = uid(f"{source_id}-parent")
    store.apply_remote(create_env(parent, content=[{"type": "text", "text": "Page"}], hlc=hlc))
    store.apply_remote(create_env(source_id, parent_id=parent, content=content, hlc=(hlc[0] + 1, 0)))
    return parent


class TestMentionEdges:
    def test_mention_edge_derives_on_create_and_content_update_replaces_it(self, store: LocalStore) -> None:
        target = uid("target-a")
        source = uid("source-a")
        make_embedded_source(store, source, content=[mention(target)])
        assert edges_of(store, source) == [
            ("mention", target, target, None, None, 0, BASE_TS.isoformat())
        ]
        # A content write re-derives: the mention goes away with its token.
        store.apply_remote(update_env(source, hlc=(5, 0), contentAst=[{"type": "text", "text": "plain"}]))
        assert edges_of(store, source) == []
        # ... and a new mention lands on the next write.
        other = uid("target-b")
        store.apply_remote(update_env(source, hlc=(6, 0), contentAst=[mention(other)]))
        assert [row[:3] for row in edges_of(store, source)] == [("mention", other, other)]

    def test_two_identical_mentions_yield_two_distinct_edge_rows(self, store: LocalStore) -> None:
        target = uid("target-a")
        source = uid("source-a")
        make_embedded_source(store, source, content=[mention(target, label="x"), mention(target, label="y")])
        rows = edges_of(store, source)
        assert len(rows) == 2
        edge_ids = [
            row[0]
            for row in store._conn.execute(  # noqa: SLF001
                "SELECT id FROM edge WHERE source_id = ? ORDER BY id", (source,)
            )
        ]
        assert len(set(edge_ids)) == 2  # the occurrence counter separates them

    def test_mention_inside_quote_and_heading_children_derives(self, store: LocalStore) -> None:
        in_quote = uid("target-quote")
        in_heading = uid("target-heading")
        source = uid("source-a")
        make_embedded_source(
            store,
            source,
            content=[
                {"type": "quote", "children": [mention(in_quote, label="q")]},
                {"type": "heading", "children": [mention(in_heading, label="h")]},
            ],
        )
        targets = {row[1] for row in edges_of(store, source)}
        assert targets == {in_quote, in_heading}

    def test_mention_is_embedded_flag_and_link_id_assertion_row(self, store: LocalStore) -> None:
        target = uid("target-a")
        link = uid("link-1")
        source = uid("source-a")
        make_embedded_source(store, source, content=[mention(target, link_id=link, embedded=True)])
        rows = edges_of(store, source)
        assert rows == [("mention", target, target, None, None, 1, BASE_TS.isoformat())]
        with sqlite3.connect(store._conn.execute("PRAGMA database_list").fetchone()[2]) as raw:  # noqa: SLF001
            assert raw.execute(
                "SELECT id, target_id, click_count FROM node_link WHERE source_id = ?", (source,)
            ).fetchall() == [(link, target, 0)]
        # The assertion dies with the token (stale rows are swept).
        store.apply_remote(update_env(source, hlc=(5, 0), contentAst=[mention(target, label="anon")]))
        with sqlite3.connect(store._conn.execute("PRAGMA database_list").fetchone()[2]) as raw:  # noqa: SLF001
            assert raw.execute("SELECT id FROM node_link WHERE source_id = ?", (source,)).fetchall() == []


class TestTypedLinkEdges:
    def test_string_verb_records_without_resolution(self, store: LocalStore) -> None:
        source = uid("source-a")
        make_embedded_source(
            store,
            source,
            content=[{"type": "typed_link", "text": "supports", "verb": "supports"}],
        )
        # target NULL by design (RECORD, DON'T RESOLVE); metadata merges text.
        assert edges_of(store, source) == [
            ("typed_link", None, None, "supports", '{"text":"supports"}', 0, BASE_TS.isoformat())
        ]

    def test_bound_verb_and_merged_metadata(self, store: LocalStore) -> None:
        bound_schema = uid("schema-verb")
        source = uid("source-a")
        make_embedded_source(
            store,
            source,
            content=[
                {
                    "type": "typed_link",
                    "text": "see",
                    "verb": {"propertySchemaId": bound_schema},
                    "metadata": {"locator": "sec-2"},
                }
            ],
        )
        assert edges_of(store, source) == [
            (
                "typed_link",
                None,
                None,
                bound_schema,
                '{"locator":"sec-2","text":"see"}',
                0,
                BASE_TS.isoformat(),
            )
        ]


class TestPropertyEdges:
    def test_node_typed_value_projects_with_schema_verb(self, store: LocalStore) -> None:
        schema = uid("schema-1")
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {"propertySchemaId": schema, "name": "Ref", "type": "object"},
                hlc=(1, 0),
            )
        )
        target = uid("target-a")
        source = uid("source-a")
        store.apply_remote(create_env(target, content=[{"type": "text", "text": "Target"}], hlc=(3, 0)))
        make_embedded_source(store, source, content=[], hlc=(2, 0))
        store.apply_remote(property_set_env(source, schema, value={"nodeId": target}, hlc=(5, 0)))
        rows = edges_of(store, source)
        assert rows == [
            (
                "property",
                target,
                target,
                schema,
                None,
                0,
                datetime.fromtimestamp(5 / 1000, tz=UTC).isoformat(),
            )
        ]

    def test_datetime_day_ref_fans_out_to_month_and_year(self, store: LocalStore) -> None:
        schema = uid("schema-date")
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {"propertySchemaId": schema, "name": "When", "type": "datetime"},
                hlc=(1, 0),
            )
        )
        source = uid("source-a")
        day = day_node_id("2026-03-15")
        # PG6 existence: the chain nodes must exist (the day is parent-locked
        # to its month, the month to its year).
        store.apply_remote(create_env(year_node_id("2026-03-15"), hlc=(3, 0)))
        store.apply_remote(
            create_env(month_node_id("2026-03-15"), parent_id=year_node_id("2026-03-15"), hlc=(4, 0))
        )
        store.apply_remote(create_env(day, parent_id=month_node_id("2026-03-15"), hlc=(5, 0)))
        make_embedded_source(store, source, content=[], hlc=(2, 0))
        store.apply_remote(property_set_env(source, schema, value={"nodeId": day}, hlc=(6, 0)))
        targets = {row[1] for row in edges_of(store, source)}
        assert targets == {day, month_node_id("2026-03-15"), year_node_id("2026-03-15")}

    def test_range_value_projects_each_present_side(self, store: LocalStore) -> None:
        schema = uid("schema-date")
        store.apply_remote(
            make_env(
                "propertySchema.create",
                {"propertySchemaId": schema, "name": "When", "type": "datetime"},
                hlc=(1, 0),
            )
        )
        source = uid("source-a")
        start = day_node_id("2026-03-15")
        end = day_node_id("2026-03-20")
        year = year_node_id("2026-03-15")
        month = month_node_id("2026-03-15")
        store.apply_remote(create_env(year, hlc=(3, 0)))
        store.apply_remote(create_env(month, parent_id=year, hlc=(4, 0)))
        store.apply_remote(create_env(start, parent_id=month, hlc=(5, 0)))
        store.apply_remote(create_env(end, parent_id=month, hlc=(6, 0)))
        make_embedded_source(store, source, content=[], hlc=(2, 0))
        store.apply_remote(
            property_set_env(source, schema, value={"start": {"nodeId": start}, "end": {"nodeId": end}}, hlc=(7, 0))
        )
        targets = {row[1] for row in edges_of(store, source)}
        expected = {
            start,
            end,
            month_node_id("2026-03-15"),
            year_node_id("2026-03-15"),
            month_node_id("2026-03-20"),
            year_node_id("2026-03-20"),
        }
        assert targets == expected


class TestTagEdges:
    def test_tag_assign_unassign_and_reassign(self, store: LocalStore) -> None:
        tag_a = uid("tag-a")
        tag_b = uid("tag-b")
        source = uid("source-a")
        store.apply_remote(create_env(source, content=[{"type": "text", "text": "Page"}], tag_ids=(tag_a, tag_b)))
        assert {row[0] for row in edges_of(store, source)} == {"tag"}
        assert {row[1] for row in edges_of(store, source)} == {tag_a, tag_b}
        # tag.unassign re-derives (the "tags are links" ruling).
        store.apply_remote(make_env("tag.unassign", {"objectId": source, "tagId": tag_a}, hlc=(2, 0)))
        assert {row[1] for row in edges_of(store, source)} == {tag_b}
        # The re-issued object.create add carrier restores the membership.
        store.apply_remote(create_env(source, tag_ids=(tag_a,), hlc=(3, 0)))
        assert {row[1] for row in edges_of(store, source)} == {tag_a, tag_b}


class TestResolutionAndLifecycle:
    def test_alias_write_reresolves_to_the_terminal(self, store: LocalStore) -> None:
        target = uid("target-a")
        alias = uid("alias-a")
        source = uid("source-a")
        make_embedded_source(store, source, content=[mention(alias)])
        assert edges_of(store, source)[0][:3] == ("mention", alias, alias)
        # The alias write re-points every chain through it: the edge to the
        # alias resolves to the terminal.
        store.apply_remote(create_env(alias, content=[{"type": "text", "text": "Alias"}], hlc=(2, 0)))
        store.apply_remote(update_env(alias, hlc=(3, 0), aliasedNodeId=target))
        assert edges_of(store, source)[0][:3] == ("mention", alias, target)

    def test_trashed_alias_terminal_resolves_to_itself(self, store: LocalStore) -> None:
        target = uid("target-a")
        alias = uid("alias-a")
        source = uid("source-a")
        store.apply_remote(create_env(target, content=[{"type": "text", "text": "Terminal"}], hlc=(1, 0)))
        store.apply_remote(create_env(alias, content=[{"type": "text", "text": "Alias"}], hlc=(2, 0)))
        make_embedded_source(store, source, content=[mention(alias)], hlc=(3, 0))
        store.apply_remote(update_env(alias, hlc=(4, 0), aliasedNodeId=target))
        assert edges_of(store, source)[0][:3] == ("mention", alias, target)
        # Liveness: a trashed alias never resolves through — the edge stays
        # on the alias, inert.
        store.apply_remote(delete_env(alias, hlc=(5, 0)))
        assert edges_of(store, source)[0][:3] == ("mention", alias, alias)

    def test_permanent_delete_purges_outgoing_but_keeps_incoming(self, store: LocalStore) -> None:
        target = uid("target-a")
        source = uid("source-a")
        other = uid("source-b")
        make_embedded_source(store, source, content=[mention(target)], hlc=(1, 0))
        make_embedded_source(store, other, content=[mention(source)], hlc=(3, 0))
        store.apply_remote(delete_env(source, permanent=True, hlc=(5, 0)))
        # The purged source's outgoing rows die with it ...
        assert edges_of(store, source) == []
        # ... but a surviving source's INCOMING rows keep theirs (PB1), and
        # the deleted target resolves to itself (a missing row is its own
        # terminal).
        with sqlite3.connect(store._conn.execute("PRAGMA database_list").fetchone()[2]) as raw:  # noqa: SLF001
            rows = raw.execute(
                "SELECT type, target_id, resolved_target_id FROM edge WHERE source_id = ?", (other,)
            ).fetchall()
        assert rows == [("mention", source, source)]


class TestDeterminism:
    def test_same_envelopes_through_a_second_store_produce_identical_edge_ids(
        self, store: LocalStore, tmp_path: Path
    ) -> None:
        target = uid("target-a")
        source = uid("source-a")
        make_embedded_source(store, source, content=[mention(target), mention(target, label="second")])
        second = LocalStore(tmp_path / "second.db")
        try:
            make_embedded_source(second, source, content=[mention(target), mention(target, label="second")])
            first_ids = [
                row[0]
                for row in store._conn.execute("SELECT id FROM edge ORDER BY id")  # noqa: SLF001
            ]
            second_ids = [
                row[0]
                for row in second._conn.execute("SELECT id FROM edge ORDER BY id")  # noqa: SLF001
            ]
            assert first_ids == second_ids and len(first_ids) == 2
        finally:
            second.close()
