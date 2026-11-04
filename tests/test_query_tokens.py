"""Query-token content-helper tests — the ``apps/web/src/ui/queryTokens.ts``
port (``notees_gtk.ui.query_tokens``): the token address space (indexes),
the AST splice/append semantics, the fail-loud pins, and the content-update
envelope — plus a live round-trip over an envelopes-built world (the write
lands in the mirror through the same op path the editor uses, so the
section's next render sees the new AST through its own store notify).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from notees_gtk.core.protocol.clock import Clock, Hlc
from notees_gtk.core.protocol.ids import new_uuid7
from notees_gtk.core.protocol.models import PROTOCOL_VERSION, RelayEnvelope
from notees_gtk.data.store import LocalStore
from notees_gtk.ui.ast_render import ast_to_view
from notees_gtk.ui.query_tokens import (
    append_query_token,
    content_update_envelope,
    query_token_indexes,
    read_query_token,
    update_query_token_ast,
)

WS = "0192a000-0000-7000-8000-000000000001"
ACTOR = "0192a000-0000-7000-8000-000000000002"
DEVICE = "test-device-query-tokens"

HOST = "0192a000-0000-7000-8000-0000000000a1"

T0 = 1727200000000

FACTORY_AST = {"version": 2, "scope": {"type": "entire_workspace"}, "root": {"type": "group", "logic": "and", "children": []}}


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


def make_world(tmp_path: Any) -> LocalStore:
    """A hosting page whose content carries a text row and a query token."""
    store = LocalStore(tmp_path / "mirror.db")
    envelopes = [
        env("object.create", {"objectId": HOST, "contentAst": text("Hosting")}, T0),
        env(
            "object.update",
            {"objectId": HOST, "contentAst": [*text("Intro"), {"type": "query", "queryAst": FACTORY_AST}]},
            T0 + 1000,
        ),
    ]
    for envelope in envelopes:
        store.apply_remote(envelope)
    return store


class TestTokenAddressSpace:
    def test_query_token_indexes_finds_every_query_token(self) -> None:
        content = [
            {"type": "text", "text": "a"},
            {"type": "query", "queryAst": FACTORY_AST},
            {"type": "text", "text": "b"},
            {"type": "query", "queryAst": FACTORY_AST},
        ]
        assert query_token_indexes(content) == [1, 3]
        assert query_token_indexes(json.dumps(content)) == [1, 3]

    def test_query_token_indexes_over_legacy_plaintext_is_empty(self) -> None:
        assert query_token_indexes("legacy plaintext content") == []
        assert query_token_indexes(None) == []

    def test_read_query_token_returns_the_token_verbatim(self) -> None:
        token = {"type": "query", "queryAst": FACTORY_AST, "extra": "rides"}
        assert read_query_token([token], 0) == token

    def test_read_query_token_fail_loud_off_range_or_wrong_kind(self) -> None:
        with pytest.raises(IndexError):
            read_query_token([{"type": "text", "text": "x"}], 4)
        with pytest.raises(ValueError):
            read_query_token([{"type": "text", "text": "x"}], 0)


class TestTokenWrites:
    def test_update_query_token_ast_splices_at_the_index_only(self) -> None:
        content = [
            {"type": "text", "text": "intro"},
            {"type": "query", "queryAst": FACTORY_AST},
            {"type": "text", "text": "outro"},
        ]
        next_ast = {"version": 2, "scope": {"type": "pages"}, "root": {"type": "group", "logic": "and", "children": []}}
        next_content = update_query_token_ast(content, 1, next_ast)
        assert next_content[0] == content[0]
        assert next_content[1] == {"type": "query", "queryAst": next_ast}
        assert next_content[2] == content[2]
        assert content[1] == {"type": "query", "queryAst": FACTORY_AST}  # input untouched

    def test_update_query_token_ast_fail_loud_on_a_non_query_token(self) -> None:
        with pytest.raises(ValueError):
            update_query_token_ast([{"type": "text", "text": "x"}], 0, FACTORY_AST)

    def test_append_query_token_appends_and_reports_the_index(self) -> None:
        content = [{"type": "text", "text": "intro"}]
        next_content, index = append_query_token(content, FACTORY_AST)
        assert index == 1
        assert next_content == [*content, {"type": "query", "queryAst": FACTORY_AST}]
        # A classed block's first Apply: empty content appends at 0.
        empty_content, empty_index = append_query_token(None, FACTORY_AST)
        assert empty_index == 0
        assert empty_content == [{"type": "query", "queryAst": FACTORY_AST}]


class TestContentUpdateEnvelope:
    def test_the_envelope_is_the_editor_save_shape(self) -> None:
        clock = Clock(DEVICE)
        envelope = content_update_envelope(
            workspace_id=WS,
            actor_id=ACTOR,
            device_id=DEVICE,
            node_id=HOST,
            content_ast=text("next"),
            clock=clock,
        )
        assert envelope.op_type == "object.update"
        assert envelope.payload["objectId"] == HOST
        assert envelope.payload["contentAst"] == text("next")


class TestLiveRoundTrip:
    """The commit lands through the real applier: the mirror's content
    column carries the new AST, the render plan rebuilds the section run
    request from it, and the store's own notify fires (the section's live
    re-run seam — a second apply converges like the server echo)."""

    def test_the_write_lands_in_the_mirror_and_the_render_plan(self, tmp_path: Any) -> None:
        store = make_world(tmp_path)
        row = store.node(WS, HOST)
        assert row is not None

        next_ast = {
            "version": 2,
            "scope": {"type": "entire_workspace"},
            "root": {
                "type": "group",
                "logic": "and",
                "children": [{"type": "content", "op": "contains", "value": "zebra"}],
            },
        }
        next_content = update_query_token_ast(row.content, 1, next_ast)
        envelope = content_update_envelope(
            workspace_id=WS,
            actor_id=ACTOR,
            device_id=DEVICE,
            node_id=HOST,
            content_ast=next_content,
            clock=Clock(DEVICE),
        )
        store.enqueue(envelope)
        notified: list[str] = []
        unsubscribe = store.subscribe(lambda: notified.append("notify"))
        store.apply_remote(envelope)
        unsubscribe()

        updated = store.node(WS, HOST)
        assert updated is not None
        tokens = json.loads(updated.content or "[]")
        assert tokens[1] == {"type": "query", "queryAst": next_ast}
        assert notified == ["notify"]

        # The render plan: the section carries the new AST + its write address.
        view = ast_to_view(updated.content, self_node_id=HOST)
        (section,) = [item for item in view.items if item.__class__.__name__ == "QuerySectionView"]
        assert section.query_ast == next_ast
        assert section.token_index == 1
        assert section.self_node_id == HOST

    def test_the_token_content_plaintext_derives_from_the_whole_array(self, tmp_path: Any) -> None:
        """Title-is-content: the query token contributes nothing to the
        plaintext; the applier's excerpt derivation keeps the host title."""
        store = make_world(tmp_path)
        row = store.node(WS, HOST)
        assert row is not None
        assert row.content_plain == "Intro"
