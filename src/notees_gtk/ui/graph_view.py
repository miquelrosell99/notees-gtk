"""The graph surface — the deferred slice's chrome: a minimal graph view
(the web GraphView's Graphite-style stand-in) plus the color-groups
popover.

The surface is deliberately thin: every piece of logic — the topology
projection, the deterministic layout, the viewport math, the color-group
model and evaluation — lives in the headless ``core.graph`` layers, and the
store seams (``graph_topology`` / ``run_query_ast`` /
``parse_query_language``) do the mirror reads. This module owns only draw
and gesture chrome: nodes render as labeled circles, edges as family-colored
lines, left-drag pans, the scroll wheel zooms at the pointer, and a click
under the click threshold opens the node through the window's navigate
seam. The color-groups popover lists / adds / edits / removes groups (label
+ color + a query-DSL text, the slice-2 grammar — the condition-builder
dialog stays the query-token editor; a future slice can embed it here) and
every commit re-persists the device cache and live-recolors.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk

from notees_gtk.core.graph.color_groups import (
    GraphColorGroup,
    evaluate_color_groups,
    group_to_raw,
    load_color_groups,
)
from notees_gtk.core.graph.layout import layout_topology
from notees_gtk.core.graph.model import GraphTopology
from notees_gtk.core.graph.viewport import CLICK_THRESHOLD_PX, GraphViewport
from notees_gtk.core.protocol.ids import new_uuid7
from notees_gtk.data.store import LocalStore
from notees_gtk.ui import config_store
from notees_gtk.ui.tree import node_display_name

__all__ = ["GraphViewWidget"]

#: The node circle radius in screen pixels (a constant — the one dimension
#: that is genuinely fixed, the tap-target floor).
_NODE_RADIUS = 12.0

#: Edge colors per structural family (rgba triples; the chrome's minimal
#: palette — the web renderer's shader tiers are engine territory).
_EDGE_RGB: dict[str, tuple[float, float, float]] = {
    "parent": (0.56, 0.56, 0.60),
    "class": (0.56, 0.56, 0.60),
    "mention": (0.36, 0.56, 0.86),
    "property": (0.36, 0.76, 0.56),
    "tag": (0.86, 0.66, 0.32),
}
_FALLBACK_EDGE_RGB = (0.50, 0.50, 0.54)
_FALLBACK_FILL = (0.76, 0.76, 0.80)


def _hex_rgb(color: str) -> tuple[float, float, float] | None:
    """Parse a ``#RRGGBB`` color to rgb triples; preset tokens (the wire
    color grammar's named presets) and anything else read as None — the
    fallback fill applies."""
    if len(color) == 7 and color.startswith("#"):
        try:
            return (
                int(color[1:3], 16) / 255.0,
                int(color[3:5], 16) / 255.0,
                int(color[5:7], 16) / 255.0,
            )
        except ValueError:
            return None
    return None


class GraphViewWidget(Gtk.Box):
    """The graph surface: toolbar (the color-groups popover) over a
    drawing area. ``show_graph`` binds it to a workspace; the store's
    change notify rebuilds topology, layout, and colors until ``stop``.
    """

    def __init__(self, *, on_open_node: Callable[[str], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_open_node = on_open_node
        self._store: LocalStore | None = None
        self._workspace_id: str | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._topology: GraphTopology | None = None
        self._positions: dict[str, tuple[float, float]] = {}
        self._labels: dict[str, str] = {}
        self._fills: dict[str, tuple[float, float, float]] = {}
        self._viewport = GraphViewport(width=800.0, height=600.0)
        self._framed = False
        self._drag_origin: tuple[float, float] | None = None
        self._last_drag: tuple[float, float] = (0.0, 0.0)
        self._pointer: tuple[float, float] = (0.0, 0.0)
        self._groups_popover: Gtk.Popover | None = None

        bar = Gtk.Box(spacing=6, margin_start=6, margin_end=6, margin_top=6, margin_bottom=6)
        groups_button = Gtk.Button(label="Color groups…")
        groups_button.connect("clicked", self._on_groups_clicked)
        bar.append(groups_button)
        self.append(bar)

        self._area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self._area.set_draw_func(self._on_draw)
        self.append(self._area)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self._area.add_controller(motion)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self._area.add_controller(drag)
        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self._area.add_controller(scroll)

    # ------------------------------------------------------------------ public

    def show_graph(self, *, store: LocalStore, workspace_id: str) -> None:
        """Bind the surface to a workspace and render it live: the store's
        change notify rebuilds the projection, the layout, and the colors
        (the live-recolor contract — one rebuild per notify, cheap at this
        surface's scale)."""
        self.stop()
        self._store = store
        self._workspace_id = workspace_id
        self._framed = False
        self._unsubscribe = store.subscribe(self._rebuild)
        self._rebuild()

    def stop(self) -> None:
        """Drop the store subscription (a workspace switch, teardown)."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    # --------------------------------------------------------------- internals

    def _rebuild(self) -> None:
        store = self._store
        workspace_id = self._workspace_id
        if store is None or workspace_id is None:
            return
        topology = store.graph_topology(workspace_id)
        self._topology = topology
        self._positions = layout_topology(topology)
        rows = {row.id: row for row in store.nodes(workspace_id)}
        self._labels = {
            node.id: node_display_name(rows[node.id]) if node.id in rows else node.id for node in topology.nodes
        }
        self._fills = self._resolve_fills(store, workspace_id, topology)
        if not self._framed and self._positions:
            xs = [position[0] for position in self._positions.values()]
            ys = [position[1] for position in self._positions.values()]
            margin = _NODE_RADIUS * 2.0 / max(self._viewport.scale, 0.05)
            self._viewport.fit_bounds(
                min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin
            )
            self._framed = True
        self._area.queue_draw()

    def _groups(self, store: LocalStore, workspace_id: str) -> list[GraphColorGroup]:
        """The migrated device-cache groups (the at-load migration runs on
        every read — legacy rows converge in memory and re-persist on the
        next commit)."""
        raws = config_store.load_graph_color_groups(workspace_id)
        return load_color_groups(store, workspace_id, raws)

    def _resolve_fills(
        self, store: LocalStore, workspace_id: str, topology: GraphTopology
    ) -> dict[str, tuple[float, float, float]]:
        """Node fill colors: the first matching color group wins; a node
        with no group keeps its own ``#RRGGBB`` color when it carries one;
        everything else takes the fallback fill."""
        groups = self._groups(store, workspace_id)
        assignment = evaluate_color_groups(store, workspace_id, groups)
        node_colors = {node.id: node.color for node in topology.nodes}
        fills: dict[str, tuple[float, float, float]] = {}
        for node in topology.nodes:
            rgb: tuple[float, float, float] | None = None
            if node.id in assignment:
                rgb = _hex_rgb(assignment[node.id])
            own = node_colors.get(node.id)
            if rgb is None and own:
                rgb = _hex_rgb(own)
            fills[node.id] = rgb if rgb is not None else _FALLBACK_FILL
        return fills

    def _persist(self, groups: list[GraphColorGroup]) -> None:
        workspace_id = self._workspace_id
        assert workspace_id is not None
        config_store.save_graph_color_groups(workspace_id, [group_to_raw(group) for group in groups])

    # -------------------------------------------------------------------- draw

    def _on_draw(self, _area: Gtk.DrawingArea, cr: Any, width: int, height: int) -> None:
        self._viewport.width = float(width)
        self._viewport.height = float(height)
        topology = self._topology
        if topology is None:
            return
        for edge in topology.edges:
            rgb = _EDGE_RGB.get(edge.kind, _FALLBACK_EDGE_RGB)
            x1, y1 = self._viewport.world_to_screen(*self._positions[edge.source])
            x2, y2 = self._viewport.world_to_screen(*self._positions[edge.target])
            cr.set_source_rgba(rgb[0], rgb[1], rgb[2], 0.55)
            cr.set_line_width(1.2)
            cr.move_to(x1, y1)
            cr.line_to(x2, y2)
            cr.stroke()
        for node in topology.nodes:
            sx, sy = self._viewport.world_to_screen(*self._positions[node.id])
            fill = self._fills.get(node.id, _FALLBACK_FILL)
            cr.arc(sx, sy, _NODE_RADIUS, 0.0, 2.0 * math.pi)
            cr.set_source_rgb(*fill)
            cr.fill_preserve()
            cr.set_source_rgb(0.30, 0.30, 0.34)
            cr.set_line_width(1.0)
            cr.stroke()
            label = self._labels.get(node.id, node.id)
            cr.set_source_rgb(0.16, 0.16, 0.20)
            cr.set_font_size(11.0)
            extents = cr.text_extents(label)
            cr.move_to(sx - extents.width / 2.0 - extents.x_bearing, sy + _NODE_RADIUS + extents.height + 2.0)
            cr.show_text(label)

    # ----------------------------------------------------------------- gestures

    def _on_motion(self, _controller: Gtk.EventControllerMotion, x: float, y: float) -> None:
        self._pointer = (x, y)

    def _on_drag_begin(self, _gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        self._drag_origin = (x, y)
        self._last_drag = (0.0, 0.0)

    def _on_drag_update(self, _gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        self._viewport.pan_by(dx - self._last_drag[0], dy - self._last_drag[1])
        self._last_drag = (dx, dy)
        self._area.queue_draw()

    def _on_drag_end(self, _gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        origin = self._drag_origin
        self._drag_origin = None
        if origin is None:
            return
        if math.hypot(dx, dy) >= CLICK_THRESHOLD_PX:
            return  # it was a pan
        node_id = self._viewport.hit_test(self._positions, origin[0], origin[1], radius=_NODE_RADIUS + 2.0)
        if node_id is not None:
            self._on_open_node(node_id)

    def _on_scroll(self, _controller: Gtk.EventControllerScroll, _dx: float, dy: float) -> None:
        self._viewport.zoom_at(1.1 ** (-dy), self._pointer[0], self._pointer[1])
        self._area.queue_draw()

    # ------------------------------------------------------------ color groups

    def _on_groups_clicked(self, button: Gtk.Button) -> None:
        if self._groups_popover is not None:
            self._groups_popover.unparent()
        popover = Gtk.Popover(has_arrow=True)
        popover.set_parent(button)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        hint = Gtk.Label(xalign=0, css_classes=["dim-label"])
        hint.set_text("First matching group wins.")
        body.append(hint)
        list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        store = self._store
        workspace_id = self._workspace_id
        if store is not None and workspace_id is not None:
            for group in self._groups(store, workspace_id):
                list_box.append(self._build_group_row(group))
        body.append(list_box)
        add_button = Gtk.Button(label="Add group…")
        add_button.connect("clicked", self._on_add_group_clicked, popover)
        body.append(add_button)
        popover.set_child(body)
        self._groups_popover = popover
        popover.popup()

    def _build_group_row(self, group: GraphColorGroup) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(spacing=8, margin_start=6, margin_end=6, margin_top=3, margin_bottom=3)
        row.set_child(box)
        swatch = Gtk.DrawingArea(content_width=18, content_height=18, valign=Gtk.Align.CENTER)
        rgb = _hex_rgb(group.color) or _FALLBACK_FILL
        swatch.set_draw_func(lambda area, cr, w, h, rgb=rgb: _draw_swatch(cr, w, h, rgb))
        box.append(swatch)
        label = Gtk.Label(label=group.label, xalign=0, hexpand=True)
        if group.error is not None:
            label.set_tooltip_text(f"Erroneous group (paints nothing): {group.error}")
        box.append(label)
        edit_button = Gtk.Button(label="Edit…")
        edit_button.connect("clicked", self._on_edit_group_clicked, group)
        box.append(edit_button)
        remove_button = Gtk.Button(label="Remove")
        remove_button.connect("clicked", self._on_remove_group_clicked, group.id)
        box.append(remove_button)
        return row

    def _on_add_group_clicked(self, _button: Gtk.Button, popover: Gtk.Popover) -> None:
        popover.popdown()
        self._open_group_dialog(None)

    def _on_edit_group_clicked(self, _button: Gtk.Button, group: GraphColorGroup) -> None:
        if self._groups_popover is not None:
            self._groups_popover.popdown()
        self._open_group_dialog(group)

    def _on_remove_group_clicked(self, _button: Gtk.Button, group_id: str) -> None:
        store = self._store
        workspace_id = self._workspace_id
        if store is None or workspace_id is None:
            return
        self._persist([group for group in self._groups(store, workspace_id) if group.id != group_id])
        if self._groups_popover is not None:
            self._groups_popover.popdown()
        self._rebuild()

    def _open_group_dialog(self, group: GraphColorGroup | None) -> None:
        store = self._store
        if store is None:
            return

        def apply_edit(
            label: str, color: str, query: str, existing: GraphColorGroup | None
        ) -> str | None:
            workspace_id = self._workspace_id
            assert workspace_id is not None
            if not query and existing is not None and existing.ast is not None:
                ast = existing.ast  # an empty query keeps the current AST (label/color edit)
            else:
                try:
                    ast = store.parse_query_language(workspace_id, query).root
                except ValueError as exc:
                    return str(exc)
            groups = self._groups(store, workspace_id)
            updated = GraphColorGroup(
                id=existing.id if existing is not None else new_uuid7(),
                label=label,
                color=color,
                ast=ast,
            )
            replaced = False
            for index, current in enumerate(groups):
                if current.id == updated.id:
                    groups[index] = updated
                    replaced = True
            if not replaced:
                groups.append(updated)
            self._persist(groups)
            self._rebuild()
            return None

        dialog = _GroupDialog(group=group, on_apply=apply_edit)
        dialog.present(self)

    # --------------------------------------------------------------- teardown

    def do_unroot(self) -> None:  # noqa: N802 — the GTK vfunc name is camelCase
        self.stop()
        Gtk.Box.do_unroot(self)


def _draw_swatch(cr: Any, width: int, height: int, rgb: tuple[float, float, float]) -> None:
    """The color-groups row swatch: a filled rounded square."""
    cr.set_source_rgb(*rgb)
    cr.rectangle(1.0, 1.0, float(width) - 2.0, float(height) - 2.0)
    cr.fill()


class _GroupDialog(Adw.Dialog):
    """The add/edit dialog: label + color + the query-DSL text (the slice-2
    grammar — names resolve against the mirror at Apply). A parse failure
    surfaces inline and keeps the dialog open."""

    def __init__(
        self,
        *,
        group: GraphColorGroup | None,
        on_apply: Callable[[str, str, str, GraphColorGroup | None], str | None],
    ) -> None:
        super().__init__(title="Edit color group" if group is not None else "Add color group", content_width=520)
        self._group = group
        self._on_apply = on_apply

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        apply_button = Gtk.Button(label="Apply")
        apply_button.add_css_class("suggested-action")
        apply_button.connect("clicked", self._on_apply_clicked)
        header.pack_end(apply_button)
        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.connect("clicked", lambda _btn: self.close())
        header.pack_start(cancel_button)
        toolbar.add_top_bar(header)

        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        self._label_entry = Gtk.Entry(hexpand=True)
        self._label_entry.set_placeholder_text("Label")
        if group is not None:
            self._label_entry.set_text(group.label)
        body.append(self._label_entry)

        color_row = Gtk.Box(spacing=8)
        color_row.append(Gtk.Label(label="Color", xalign=0))
        self._color_button = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(), valign=Gtk.Align.CENTER)
        rgba = Gdk.RGBA()
        rgba.parse(group.color if group is not None else "#808080")
        self._color_button.set_rgba(rgba)
        color_row.append(self._color_button)
        body.append(color_row)

        self._query_entry = Gtk.Entry(hexpand=True)
        if group is not None:
            # The DSL round-trip of a builder-authored AST does not always
            # exist (nested queries have no DSL spelling) — an existing
            # group starts blank and an empty query on Apply keeps the
            # current AST (a label/color edit).
            self._query_entry.set_placeholder_text("Query (DSL text) — empty keeps the current query")
        else:
            self._query_entry.set_placeholder_text("Query, e.g. class: Person")
        body.append(self._query_entry)

        self._error_label = Gtk.Label(xalign=0, wrap=True, css_classes=["error"])
        body.append(self._error_label)

        toolbar.set_content(body)
        self.set_content(toolbar)

    def _on_apply_clicked(self, _button: Gtk.Button) -> None:
        label = self._label_entry.get_text().strip()
        if not label:
            self._error_label.set_text("The label must not be empty.")
            return
        rgba = self._color_button.get_rgba()
        color = f"#{round(rgba.red * 255):02x}{round(rgba.green * 255):02x}{round(rgba.blue * 255):02x}"
        query = self._query_entry.get_text().strip()
        if not query and self._group is None:
            self._error_label.set_text("The query must not be empty.")
            return
        error = self._on_apply(label, color, query, self._group)
        if error is not None:
            self._error_label.set_text(error)
            return
        self.close()
