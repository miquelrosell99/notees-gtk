"""The ``query`` content token's live section chrome — the
``apps/web/src/ui/QueryNodeSection.tsx`` + ``queryResults.tsx`` result-body
port (deliberately thinner: no tabs/toolbar/builder chrome — the GTK client
has no query builder yet; the token's own AST is the only surface, task
ruling: render what the token carries).

The section hosts a :class:`~notees_gtk.ui.query_run.QuerySectionRun` — the
coalesced live re-run: store notifies marshal onto the GTK main loop (the
React-tick batching mirror — a worker-thread apply burst collapses into one
idle callback), the controller's dirty/trailing-run arm covers notifies
mid-flight, and the rows replace in place: deletions/renames converge
without a manual refresh.

Rows render like the web's query-node rows — display name + minimal chrome,
open on activate through :func:`notees_gtk.ui.query_run.open_query_result`
(document-chrome rows open directly, an inline block resolves to its
containing page). An AST carrying an ``aggregation`` renders the grouped
grid (dimension columns + measure columns).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango

from notees_gtk.core.query.execute import AggregateResult, QueryResult
from notees_gtk.data.store import LocalStore
from notees_gtk.ui.ast_render import QuerySectionView
from notees_gtk.ui.query_run import (
    AggregateRunState,
    PlainRunState,
    QuerySectionRun,
    aggregate_column_label,
    format_aggregate_cell,
    is_unspecified_query_ast,
    open_query_result,
)

__all__ = ["QUERY_RESULT_CAP", "QuerySectionWidget"]

#: Result list cap (TS ``QUERY_RESULT_CAP`` — a per-render window on the web,
#: widened by a load-more the GTK chrome does not carry yet; the overflow
#: renders an honest "+N more" line instead).
QUERY_RESULT_CAP = 200


def _row_title(row: Mapping[str, Any]) -> tuple[str, str]:
    """(node id, display title) for one result row — title-is-content: the
    plaintext excerpt, the raw id as the honest fallback."""
    node_id = str(row.get("id") or "")
    title = str(row.get("content_plain") or "").strip() or node_id
    return node_id, title


class QuerySectionWidget(Gtk.Box):
    """The live query section below the content row that carries the token."""

    def __init__(
        self,
        store: LocalStore,
        workspace_id: str,
        section: QuerySectionView,
        *,
        on_open_node: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._store = store
        self._workspace_id = workspace_id
        self._on_open_node = on_open_node
        # Worker-thread notify → one pending main-loop refresh (the batching
        # mirror): the flag flips on both threads; a benign race costs one
        # extra run, never a lost one.
        self._notify_armed = False
        self._listener: Callable[[], None] | None = None
        self._run = QuerySectionRun(
            run_query=lambda raw, self_id: store.run_query_ast(workspace_id, raw, self_node_id=self_id),
            run_aggregate=lambda raw, self_id: store.run_aggregate_ast(workspace_id, raw, self_node_id=self_id),
            subscribe=self._subscribe_main_thread,
            raw_ast=section.query_ast,
            self_node_id=section.self_node_id,
            enabled=not is_unspecified_query_ast(section.query_ast),
        )
        self._run.start()
        self._render()
        self.connect("destroy", self._on_destroy)

    # ------------------------------------------------------------------ wiring

    def _subscribe_main_thread(self, listener: Callable[[], None]) -> Callable[[], None]:
        """The controller's subscribe seam — marshal store notifies onto the
        main loop so the run + row replacement never touch widgets from the
        sync worker thread."""
        self._listener = listener

        def store_listener() -> None:
            if self._notify_armed:
                return
            self._notify_armed = True
            GLib.idle_add(self._on_notify_idle)

        return self._store.subscribe(store_listener)

    def _on_notify_idle(self) -> bool:
        self._notify_armed = False
        if self._listener is not None:
            self._listener()
        self._render()
        return False  # GLib.SOURCE_REMOVE — run once

    def _on_destroy(self, *_args: object) -> None:
        self._run.stop()

    def close(self) -> None:
        """Explicit teardown (the page view stops its sections on re-render;
        the destroy signal is the belt-and-braces arm)."""
        self._run.stop()

    # ------------------------------------------------------------------ chrome

    def _render(self) -> None:
        while child := self.get_first_child():
            self.remove(child)
        state = self._run.result
        if not self._run.enabled:
            self.append(self._dim_label("Set a query to see results."))
            return
        if self._run.error is not None:
            label = self._dim_label("invalid query")
            label.set_tooltip_text(self._run.error)
            self.append(label)
            return
        if state is None:
            return
        if isinstance(state, AggregateRunState):
            self._render_aggregate(state.result, state.dimension_count)
        elif isinstance(state, PlainRunState):
            self._render_rows(state.result)

    def _dim_label(self, text: str) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0)
        label.add_css_class("dim-label")
        return label

    def _render_rows(self, result: QueryResult) -> None:
        count = len(result.ids)
        self.append(self._dim_label(f"Query — {count} result{'s' if count != 1 else ''}"))
        if count == 0:
            self.append(self._dim_label("No results."))
            return
        list_box = Gtk.ListBox(activate_on_single_click=True)
        list_box.connect("row-activated", self._on_row_activated)
        for row in result.rows[:QUERY_RESULT_CAP]:
            node_id, title = _row_title(row)
            label = Gtk.Label(label=title, xalign=0, ellipsize=Pango.EllipsizeMode.END)
            label.set_tooltip_text(node_id)
            list_box.append(Gtk.ListBoxRow(child=label))
        self.append(list_box)
        overflow = count - QUERY_RESULT_CAP
        if overflow > 0:
            self.append(self._dim_label(f"…and {overflow} more"))

    def _render_aggregate(self, result: AggregateResult, dimension_count: int) -> None:
        self.append(self._dim_label(f"Query — {len(result.rows)} groups"))
        if not result.rows:
            self.append(self._dim_label("No results."))
            return
        class_names = {row.id: str(row.content_plain or "") for row in self._store.nodes(self._workspace_id)}
        property_names = self._store.property_schema_names(self._workspace_id)
        grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        for index, column in enumerate(result.columns):
            label = Gtk.Label(label=aggregate_column_label(column, class_names, property_names))
            label.add_css_class("heading")
            label.set_xalign(1.0 if index >= dimension_count else 0.0)
            grid.attach(label, index, 0, 1, 1)
        for row_index, cells in enumerate(result.rows, start=1):
            for column_index, value in enumerate(cells):
                label = Gtk.Label(label=format_aggregate_cell(value))
                label.set_xalign(1.0 if column_index >= dimension_count else 0.0)
                grid.attach(label, column_index, row_index, 1, 1)
        self.append(grid)

    def _on_row_activated(self, _list_box: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        if self._on_open_node is None:
            return
        index = row.get_index()
        state = self._run.result
        if not isinstance(state, PlainRunState) or not 0 <= index < len(state.result.rows):
            return
        result_row = state.result.rows[index]
        target = open_query_result(
            result_row,
            lambda node_id: self._store.node(self._workspace_id, node_id),
            parentless_block=self._parentless_block_resolver(),
        )
        self._on_open_node(target)

    def _parentless_block_resolver(self) -> Callable[[str], bool]:
        """The class facts read for the role predicates: a class effectively
        carries ``parentlessRole "block"`` when it or an extends-ancestor
        does (the store's page predicate, tag_ops' pure resolver shape)."""

        def resolver(class_id: str) -> bool:  # noqa: ARG001 — the extends walk resolves below
            by_id = {row.id: row for row in self._store.nodes(self._workspace_id)}
            extends = self._store.class_extends_edges()
            seen: set[str] = set()
            frontier = [class_id]
            while frontier:
                current = frontier.pop()
                if current in seen:
                    continue
                seen.add(current)
                node_row = by_id.get(current)
                if node_row is not None and node_row.parentless_role == "block":
                    return True
                frontier.extend(extends.get(current, ()))
            return False

        return resolver
