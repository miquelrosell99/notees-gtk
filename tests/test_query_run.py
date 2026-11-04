"""Tests for the live query-section run (``notees_gtk.ui.query_run``) — the
``apps/web/src/ui/queryRun.ts`` port — plus the store's change-notification
seam and the render-plan → run-request contract.

Headless by design (no ``gi`` import anywhere here): the GTK chrome in
``notees_gtk.ui.query_section`` is import-smoke-tested on GTK hosts only;
every behavior pin below rides the headless controller, the pure navigation
helper, or a real envelopes-built ``LocalStore`` world (the same topology
builders the slice-1 execution suite uses).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from notees_gtk.core.protocol.clock import Hlc
from notees_gtk.core.protocol.ids import new_uuid7
from notees_gtk.core.protocol.models import PROTOCOL_VERSION, RelayEnvelope
from notees_gtk.core.query.execute import AggregateResult, QueryResult
from notees_gtk.data.store import LocalStore, NodeRow
from notees_gtk.ui.ast_render import QuerySectionView, ast_to_view
from notees_gtk.ui.query_run import (
    AggregateRunState,
    PlainRunState,
    QuerySectionRun,
    aggregate_column_label,
    format_aggregate_cell,
    is_unspecified_query_ast,
    open_query_result,
    parse_run_ast,
)

WS = "0192a000-0000-7000-8000-000000000001"
ACTOR = "0192a000-0000-7000-8000-000000000002"
DEVICE = "test-device-query-section"

HOST = "0192a000-0000-7000-8000-0000000000a1"
ALPHA = "0192a000-0000-7000-8000-0000000000a2"
BETA = "0192a000-0000-7000-8000-0000000000a3"
BLOCK = "0192a000-0000-7000-8000-0000000000a4"

T0 = 1727200000000


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
    """A hosting page with two standalone children and one embedded block,
    built through the real appliers (the live-section seam's substrate)."""
    store = LocalStore(tmp_path / "mirror.db")
    t = lambda i: T0 + i * 1000  # noqa: E731
    envelopes = [
        env("object.create", {"objectId": HOST, "contentAst": text("Hosting")}, t(0)),
        env(
            "object.create",
            {"objectId": ALPHA, "contentAst": text("Alpha"), "parentId": HOST, "isEmbedded": False},
            t(1),
        ),
        env(
            "object.create",
            {"objectId": BETA, "contentAst": text("Beta"), "parentId": HOST, "isEmbedded": False},
            t(2),
        ),
        env(
            "object.create",
            {"objectId": BLOCK, "contentAst": text("Note"), "parentId": HOST, "isEmbedded": True},
            t(3),
        ),
    ]
    for envelope in envelopes:
        assert store.apply_remote(envelope) is True
    return store


def children_of_self_ast() -> dict[str, Any]:
    """The ``self`` member-target AST — the first GTK surface exercising the
    hosting-page contract (fails loud at compile without ``self_node_id``).
    Family ``parent``: the member's parent is in the target set, so this
    selects the hosting page's children."""
    return {
        "version": 2,
        "scope": {"type": "entire_workspace"},
        "root": {
            "type": "group",
            "logic": "and",
            "children": [{"type": "member", "family": "parent", "op": "in", "target": {"mode": "self"}}],
        },
    }


def contains_ast(value: str) -> dict[str, Any]:
    return {
        "version": 2,
        "scope": {"type": "entire_workspace"},
        "root": {
            "type": "group",
            "logic": "and",
            "children": [{"type": "content", "op": "contains", "value": value}],
        },
    }


def aggregate_ast() -> dict[str, Any]:
    return {
        "version": 2,
        "scope": {"type": "entire_workspace"},
        "root": {"type": "group", "logic": "and", "children": []},
        "aggregation": {
            "dimensions": [{"kind": "isClass"}],
            "measures": [{"function": "count", "kind": "node"}],
        },
    }


# --------------------------------------------------------------------- fakes


class FakeSubscriber:
    """A hand-rolled subscribe seam: captures the registered listener so a
    test can deliver notifies on demand."""

    def __init__(self) -> None:
        self.listener: Callable[[], None] | None = None
        self.unsubscribed = False

    def __call__(self, listener: Callable[[], None]) -> Callable[[], None]:
        self.listener = listener

        def unsubscribe() -> None:
            self.unsubscribed = True

        return unsubscribe

    def notify(self) -> None:
        assert self.listener is not None
        self.listener()


def counting_runner(
    results: list[QueryResult] | None = None,
) -> tuple[Callable[[object, str | None], QueryResult], list[tuple[object, str | None]]]:
    """A plain-run seam recording (raw_ast, self_node_id) per call, serving
    the canned results in order (the last one repeats)."""
    calls: list[tuple[object, str | None]] = []

    def runner(raw: object, self_node_id: str | None) -> QueryResult:
        calls.append((raw, self_node_id))
        if results is None:
            return QueryResult(ids=(), rows=())
        return results[min(len(calls) - 1, len(results) - 1)]

    return runner, calls


def aggregate_runner_sink() -> tuple[
    Callable[[object, str | None], AggregateResult], list[tuple[object, str | None]]
]:
    calls: list[tuple[object, str | None]] = []

    def runner(raw: object, self_node_id: str | None) -> AggregateResult:
        calls.append((raw, self_node_id))
        return AggregateResult(columns=("isClass", "count"), rows=())

    return runner, calls


def make_controller(
    raw: object,
    *,
    self_node_id: str | None = HOST,
    enabled: bool = True,
    results: list[QueryResult] | None = None,
) -> tuple[QuerySectionRun, FakeSubscriber, list[tuple[object, str | None]], list[tuple[object, str | None]]]:
    subscriber = FakeSubscriber()
    runner, calls = counting_runner(results)
    aggregate_runner, aggregate_calls = aggregate_runner_sink()
    controller = QuerySectionRun(
        runner,
        aggregate_runner,
        subscriber,
        raw_ast=raw,
        self_node_id=self_node_id,
        enabled=enabled,
    )
    return controller, subscriber, calls, aggregate_calls


# --------------------------------------------------------------- render plan


class TestRenderPlan:
    """The ast_render → run-request contract: query tokens become live
    sections carrying the right ``self_node_id`` (part of the run identity —
    the compiler fails loud without the host)."""

    def test_query_token_renders_as_section_with_hosting_page_id(self) -> None:
        ast = {"version": 2, "scope": {"type": "pages"}, "root": {"type": "group", "logic": "and", "children": []}}
        content = json.dumps([{"type": "query", "queryAst": ast}])
        (item,) = ast_to_view(content, self_node_id=HOST).items
        assert isinstance(item, QuerySectionView)
        assert item.self_node_id == HOST
        assert item.query_ast == ast

    def test_self_hosted_plan_runs_through_the_store_seam(self, tmp_path: Any) -> None:
        """End-to-end render-plan → seam pin: the section's AST, run with the
        hosting page id through ``run_query_ast``, resolves the self target
        against the host (its children, not the host itself)."""
        store = make_world(tmp_path)
        content = json.dumps([{"type": "query", "queryAst": children_of_self_ast()}])
        (section,) = ast_to_view(content, self_node_id=HOST).items
        assert isinstance(section, QuerySectionView)
        result = store.run_query_ast(WS, section.query_ast, self_node_id=section.self_node_id)
        assert set(result.ids) == {ALPHA, BETA, BLOCK}

    def test_self_hosted_plan_without_host_fails_loud(self, tmp_path: Any) -> None:
        store = make_world(tmp_path)
        with pytest.raises(ValueError):
            store.run_query_ast(WS, children_of_self_ast())


# -------------------------------------------------------- the coalesced run


class TestQuerySectionRun:
    """The useQueryRun contract: run identity (AST, version, self), the
    dirty/trailing-run coalescing, the error state, the unspecified arm."""

    def test_start_subscribes_and_runs_the_initial_pass(self) -> None:
        controller, subscriber, calls, _ = make_controller(contains_ast("x"))
        controller.start()
        assert subscriber.listener is not None
        assert len(calls) == 1
        assert calls[0][1] == HOST  # the self node id rides every run
        assert isinstance(controller.result, PlainRunState)
        assert controller.error is None

    def test_run_identity_includes_the_self_node_id(self) -> None:
        controller, _subscriber, calls, _ = make_controller(contains_ast("x"))
        controller.start()
        controller.update(self_node_id=ALPHA)  # a new host → a new identity
        assert len(calls) == 2
        assert calls[1][1] == ALPHA

    def test_notify_reruns_and_replaces_the_cached_result(self) -> None:
        first = QueryResult(ids=(ALPHA,), rows=({"id": ALPHA},))
        second = QueryResult(ids=(), rows=())
        controller, subscriber, calls, _ = make_controller(contains_ast("x"), results=[first, second])
        controller.start()
        assert controller.result is not None and controller.result.result is first
        subscriber.notify()
        assert len(calls) == 2
        assert controller.result is not None and controller.result.result is second

    def test_unchanged_triple_skips_outright(self) -> None:
        controller, _subscriber, calls, _ = make_controller(contains_ast("x"))
        controller.start()
        controller.update()  # no input changed
        assert len(calls) == 1

    def test_notify_burst_mid_flight_costs_at_most_two_runs(self) -> None:
        """Notifications landing while a run is in flight mark it dirty and
        schedule exactly ONE trailing run (the web's coalescing law)."""
        subscriber = FakeSubscriber()
        runs: list[str] = []
        burst_done = False

        def runner(raw: object, self_node_id: str | None) -> QueryResult:
            nonlocal burst_done
            runs.append("run")
            if not burst_done:
                # Three notifications land mid-flight (a burst of applies),
                # once — the trailing run sees a quiet store.
                burst_done = True
                subscriber.notify()
                subscriber.notify()
                subscriber.notify()
            return QueryResult(ids=(), rows=())

        aggregate_runner, _ = aggregate_runner_sink()
        controller = QuerySectionRun(
            runner,
            aggregate_runner,
            subscriber,
            raw_ast=contains_ast("x"),
            self_node_id=HOST,
        )
        controller.start()
        assert runs == ["run", "run"]  # the initial run + exactly one trailing

    def test_notify_burst_when_idle_reruns_per_notify(self) -> None:
        """The web coalesces only IN-FLIGHT notifications (React batches the
        idle burst into one tick before the effect sees it); per-notify
        re-runs when idle mirror the hook's version-bump semantics — the
        chrome's main-loop marshalling collapses real bursts before they
        reach here."""
        controller, subscriber, calls, _ = make_controller(contains_ast("x"))
        controller.start()
        subscriber.notify()
        subscriber.notify()
        assert len(calls) == 3

    def test_invalid_ast_surfaces_as_the_error_state(self, tmp_path: Any) -> None:
        """A malformed AST never crashes the section: the strict parse inside
        the seam raises and the controller caches the message (the web's
        loose-parse → run-error shape)."""
        store = make_world(tmp_path)
        subscriber = FakeSubscriber()
        aggregate_runner, _ = aggregate_runner_sink()
        controller = QuerySectionRun(
            lambda raw, self_id: store.run_query_ast(WS, raw, self_node_id=self_id),
            aggregate_runner,
            subscriber,
            raw_ast={"version": 2, "scope": {"type": "nope"}, "root": {"type": "group", "logic": "and", "children": []}},
            self_node_id=HOST,
        )
        controller.start()
        assert controller.result is None
        assert controller.error is not None

    def test_aggregation_ast_takes_the_aggregate_arm(self) -> None:
        controller, _subscriber, _calls, aggregate_calls = make_controller(aggregate_ast())
        controller.start()
        assert aggregate_calls == [(aggregate_ast(), HOST)]
        assert isinstance(controller.result, AggregateRunState)
        assert controller.result.dimension_count == 1

    def test_unspecified_never_runs_and_clears_a_stale_result(self) -> None:
        unspecified = {
            "version": 2,
            "scope": {"type": "entire_workspace"},
            "root": {"type": "group", "logic": "and", "children": []},
        }
        controller, subscriber, calls, _ = make_controller(contains_ast("x"))
        controller.start()
        assert len(calls) == 1
        # The host derives the enabled arm from the AST (the web passes
        # ``enabled: !unspecified`` into the hook) — an emptied query never
        # runs and clears its stale result.
        controller.update(raw_ast=unspecified, enabled=False)
        assert len(calls) == 1
        assert controller.result is None
        subscriber.notify()  # unspecified stays inert across notifies
        assert len(calls) == 1

    def test_disabled_clears_and_holds(self) -> None:
        controller, subscriber, calls, _ = make_controller(contains_ast("x"))
        controller.start()
        controller.update(enabled=False)
        assert controller.result is None and controller.error is None
        subscriber.notify()
        assert len(calls) == 1

    def test_stop_unsubscribes(self) -> None:
        controller, subscriber, _calls, _ = make_controller(contains_ast("x"))
        controller.start()
        controller.stop()
        assert subscriber.unsubscribed is True


class TestUnspecified:
    """The isUnspecifiedQueryAst port."""

    @pytest.mark.parametrize(
        "raw",
        [
            {
                "version": 2,
                "scope": {"type": "entire_workspace"},
                "root": {"type": "group", "logic": "and", "children": []},
            },
            {
                "version": 2,
                "scope": {"type": "entire_workspace"},
                "root": {"type": "group", "logic": "or", "children": []},
            },
        ],
    )
    def test_empty_match_all_is_unspecified(self, raw: object) -> None:
        assert is_unspecified_query_ast(raw) is True

    @pytest.mark.parametrize(
        "raw",
        [
            contains_ast("x"),
            {"version": 2, "scope": {"type": "pages"}, "root": {"type": "group", "logic": "and", "children": []}},
            aggregate_ast(),
            {"version": 2, "scope": {"type": "nope"}, "root": {"type": "group", "logic": "and", "children": []}},
            None,
            "garbage",
        ],
    )
    def test_anything_else_is_not(self, raw: object) -> None:
        assert is_unspecified_query_ast(raw) is False

    def test_parse_run_ast_is_loose(self) -> None:
        assert parse_run_ast(contains_ast("x")) is not None
        assert parse_run_ast("garbage") is None
        assert parse_run_ast(None) is None


# ------------------------------------------------------ open-on-activate nav


def make_node_row(node_id: str, **overrides: Any) -> NodeRow:
    base = NodeRow(
        id=node_id,
        workspace_id=WS,
        parent_id=None,
        is_class=False,
        is_embedded=False,
        name=None,
        class_ids=(),
        tag_ids=(),
        icon=None,
        color=None,
        cover_asset_id=None,
        banner_asset_id=None,
        aliased_node_id=None,
        description=None,
        list_style=None,
        parentless_role=None,
        is_active=True,
        content=None,
        content_plain=node_id,
    )
    return replace(base, **overrides)


class TestOpenQueryResult:
    """The openQueryResult port: document-chrome rows open directly; an
    inline block resolves to its containing page (the ancestor walk with the
    parent-page condition)."""

    def test_page_row_opens_directly(self) -> None:
        page = make_node_row(HOST)
        row = {"id": HOST, "parent_id": None, "is_class": 0, "is_embedded": 0}
        assert open_query_result(row, lambda _id: page) == HOST

    def test_embedded_block_resolves_to_the_containing_page(self) -> None:
        block = make_node_row(BLOCK, parent_id=HOST, is_embedded=True)
        host = make_node_row(HOST)
        by_id = {HOST: host, BLOCK: block}
        row = {"id": BLOCK, "parent_id": HOST, "is_class": 0, "is_embedded": 1}
        assert open_query_result(row, by_id.get) == HOST

    def test_standalone_child_page_opens_directly(self) -> None:
        child = make_node_row(ALPHA, parent_id=HOST, is_embedded=False)
        host = make_node_row(HOST)
        by_id = {HOST: host, ALPHA: child}
        row = {"id": ALPHA, "parent_id": HOST, "is_class": 0, "is_embedded": 0}
        assert open_query_result(row, by_id.get) == ALPHA

    def test_nested_block_walks_past_the_inert_middle(self) -> None:
        """A block under a non-page parent continues past it to the real
        page (the web's seen-set ancestor walk)."""
        block = make_node_row(BLOCK, parent_id=ALPHA, is_embedded=True)
        middle = make_node_row(ALPHA, parent_id=HOST, is_embedded=True)
        host = make_node_row(HOST)
        by_id = {HOST: host, ALPHA: middle, BLOCK: block}
        row = {"id": BLOCK, "parent_id": ALPHA, "is_class": 0, "is_embedded": 1}
        assert open_query_result(row, by_id.get) == HOST

    def test_parentless_block_class_member_falls_back_to_its_own_id(self) -> None:
        """A parentless member of a parentlessRole "block" class has no
        chrome anywhere: no ancestor is a page, so the honest fallback is
        the row's own id."""
        class_id = "0192a000-0000-7000-8000-0000000000c1"
        member = make_node_row(ALPHA, class_ids=(class_id,))
        by_id = {ALPHA: member}
        row = {"id": ALPHA, "parent_id": None, "is_class": 0, "is_embedded": 0, "class_ids": json.dumps([class_id])}
        assert open_query_result(row, by_id.get, parentless_block=lambda _c: True) == ALPHA

    def test_json_and_list_class_ids_both_read(self) -> None:
        class_id = "0192a000-0000-7000-8000-0000000000c1"
        member = make_node_row(ALPHA, class_ids=(class_id,))
        by_id = {ALPHA: member}
        as_json = {"id": ALPHA, "parent_id": None, "is_class": 0, "is_embedded": 0, "class_ids": json.dumps([class_id])}
        as_list = {"id": ALPHA, "parent_id": None, "is_class": 0, "is_embedded": 0, "class_ids": [class_id]}
        assert open_query_result(as_json, by_id.get) == ALPHA
        assert open_query_result(as_list, by_id.get) == ALPHA


class TestAggregateLabels:
    """The aggregateColumnLabel / formatAggregateCell ports."""

    def test_known_columns(self) -> None:
        assert aggregate_column_label("isClass", {}, {}) == "Class"
        assert aggregate_column_label("isEmbedded", {}, {}) == "Present as main"
        assert aggregate_column_label("count", {}, {}) == "Count"
        assert aggregate_column_label("countDistinct", {}, {}) == "Count distinct"

    def test_resolved_and_fallback_ids(self) -> None:
        class_id = "0192a000-0000-7000-8000-0000000000c1"
        prop_id = "0192a000-0000-7000-8000-0000000000d1"
        assert aggregate_column_label(f"class:{class_id}", {class_id: "City"}, {}) == "City"
        assert aggregate_column_label(f"class:{class_id}", {}, {}) == f"Class {class_id[:8]}"
        assert aggregate_column_label(f"property:{prop_id}", {}, {prop_id: "priority"}) == "priority"
        assert aggregate_column_label(f"sum:{prop_id}", {}, {prop_id: "priority"}) == "sum(priority)"
        assert aggregate_column_label("mystery", {}, {}) == "mystery"

    def test_cell_formatting(self) -> None:
        assert format_aggregate_cell(None) == "—"
        assert format_aggregate_cell({"a": 1}) == '{"a": 1}'
        assert format_aggregate_cell(3) == "3"


# ---------------------------------------------------- the store notify seam


class TestStoreSubscribe:
    """The subscribe seam: a mutating apply notifies after commit; dedupe
    hits and LWW drops stay silent; unsubscribe detaches; wipe notifies; a
    failing listener never breaks the apply."""

    def _world(self, tmp_path: Any) -> tuple[LocalStore, list[str]]:
        store = make_world(tmp_path)
        log: list[str] = []
        store.subscribe(lambda: log.append("changed"))
        return store, log

    def test_mutating_apply_notifies_after_commit(self, tmp_path: Any) -> None:
        store, log = self._world(tmp_path)
        assert log == []  # the world's own build ran before subscribing
        created = env("object.create", {"objectId": new_uuid7(), "contentAst": text("New")}, T0 + 9000)
        assert store.apply_remote(created) is True
        assert log == ["changed"]

    def test_reapply_is_a_dedupe_hit_and_stays_silent(self, tmp_path: Any) -> None:
        store, log = self._world(tmp_path)
        envelope = env("object.create", {"objectId": new_uuid7(), "contentAst": text("New")}, T0 + 9000)
        store.apply_remote(envelope)
        log.clear()
        assert store.apply_remote(envelope) is False
        assert log == []

    def test_lww_drop_stays_silent(self, tmp_path: Any) -> None:
        store, log = self._world(tmp_path)
        stale = env("object.create", {"objectId": ALPHA, "contentAst": text("Old")}, T0 - 9000)
        assert store.apply_remote(stale) is False  # older HLC loses to the world
        assert log == []

    def test_unsubscribe_detaches(self, tmp_path: Any) -> None:
        store, log = self._world(tmp_path)
        unsubscribe = store.subscribe(lambda: log.append("second"))
        store.apply_remote(env("object.create", {"objectId": new_uuid7(), "contentAst": text("New")}, T0 + 9000))
        assert log == ["changed", "second"]
        unsubscribe()
        store.apply_remote(env("object.create", {"objectId": new_uuid7(), "contentAst": text("Other")}, T0 + 9100))
        assert log == ["changed", "second", "changed"]  # only the survivor fires

    def test_wipe_notifies(self, tmp_path: Any) -> None:
        store, log = self._world(tmp_path)
        store.wipe(WS)
        assert log == ["changed"]

    def test_a_failing_listener_never_breaks_the_apply(self, tmp_path: Any) -> None:
        store, _log = self._world(tmp_path)

        def boom() -> None:
            raise RuntimeError("listener exploded")

        store.subscribe(boom)
        created = env("object.create", {"objectId": new_uuid7(), "contentAst": text("New")}, T0 + 9000)
        assert store.apply_remote(created) is True

    def test_notify_sees_committed_state(self, tmp_path: Any) -> None:
        """The re-derive contract: a subscriber re-reading the mirror inside
        the notification sees the just-applied row."""
        store = make_world(tmp_path)
        seen: list[str | None] = []
        node_id = new_uuid7()

        def listener() -> None:
            row = store.node(WS, node_id)
            seen.append(None if row is None else row.content_plain)

        store.subscribe(listener)
        store.apply_remote(env("object.create", {"objectId": node_id, "contentAst": text("Fresh")}, T0 + 9000))
        assert seen == ["Fresh"]

    def test_property_schema_names_lists_active_schemas(self, tmp_path: Any) -> None:
        """The aggregate grid's label read: active schemas by id → name."""
        store = make_world(tmp_path)
        assert store.property_schema_names(WS) == {}
        schema_id = new_uuid7()
        store.apply_remote(
            env(
                "propertySchema.create",
                {
                    "propertySchemaId": schema_id,
                    "name": "priority",
                    "type": "select",
                    "options": [{"id": "low", "label": "low"}, {"id": "high", "label": "high"}],
                },
                T0 + 9000,
            )
        )
        assert store.property_schema_names(WS) == {schema_id: "priority"}


# -------------------------------------------------- the live seam end-to-end


class TestLiveSectionSeam:
    """The controller over a real envelopes-built mirror: the self-hosted
    section re-runs on every store change and converges — a deletion or a
    rename replaces the rows with no manual refresh."""

    def _controller(self, store: LocalStore, raw: object) -> QuerySectionRun:
        return QuerySectionRun(
            lambda raw_ast, self_id: store.run_query_ast(WS, raw_ast, self_node_id=self_id),
            lambda raw_ast, self_id: store.run_aggregate_ast(WS, raw_ast, self_node_id=self_id),
            store.subscribe,
            raw_ast=raw,
            self_node_id=HOST,
        )

    def test_self_hosted_section_runs_the_hosts_children(self, tmp_path: Any) -> None:
        store = make_world(tmp_path)
        controller = self._controller(store, children_of_self_ast())
        controller.start()
        assert isinstance(controller.result, PlainRunState)
        assert set(controller.result.result.ids) == {ALPHA, BETA, BLOCK}

    def test_deletion_converges_on_notify(self, tmp_path: Any) -> None:
        store = make_world(tmp_path)
        controller = self._controller(store, children_of_self_ast())
        controller.start()
        deleted = env("object.delete", {"objectId": BETA}, T0 + 9000)
        assert store.apply_remote(deleted) is True  # the apply itself notifies
        assert isinstance(controller.result, PlainRunState)
        assert set(controller.result.result.ids) == {ALPHA, BLOCK}

    def test_rename_converges_on_notify(self, tmp_path: Any) -> None:
        store = make_world(tmp_path)
        controller = self._controller(store, contains_ast("Beta"))
        controller.start()
        assert controller.result is not None and tuple(controller.result.result.ids) == (BETA,)
        renamed = env("object.update", {"objectId": BETA, "contentAst": text("Renamed")}, T0 + 9000)
        assert store.apply_remote(renamed) is True
        assert controller.result is not None and tuple(controller.result.result.ids) == ()

    def test_aggregation_runs_the_grid(self, tmp_path: Any) -> None:
        store = make_world(tmp_path)
        controller = self._controller(store, aggregate_ast())
        controller.start()
        assert isinstance(controller.result, AggregateRunState)
        assert controller.result.result.columns == ("isClass", "count")
        assert (0, 4) in controller.result.result.rows  # the four non-class nodes
