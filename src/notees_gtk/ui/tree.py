"""Sidebar for the main window: workspace switcher + flat-indented node list.

``LocalStore.nodes()`` returns *all* nodes in a workspace (``parent_id=None``
is no filter), so the tree is built client-side: rows are depth-first walked
from the roots, indented by depth, with chevron buttons expanding/collapsing
children. Archived nodes never reach this widget (the store excludes them).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Pango

from notees_gtk.core.api import WorkspaceRef
from notees_gtk.data.store import NodeRow
from notees_gtk.ui.ast_render import ast_to_plaintext

__all__ = ["NodeTreeSidebar", "node_display_name"]


def node_display_name(row: NodeRow) -> str:
    """Derive the sidebar label from the node's content AST (first line).

    Node links fall back to label → target UUID inside ``ast_to_plaintext``,
    never an "…" placeholder; empty content renders as "Untitled".
    """
    if not row.content:
        return "Untitled"
    first_line = ast_to_plaintext(row.content).split("\n", 1)[0].strip()
    return first_line or "Untitled"


class NodeTreeSidebar(Gtk.Box):
    """Workspace drop-down above a scrollable, flat-indented node list."""

    def __init__(
        self,
        on_workspace_selected: Callable[[str], None],
        on_node_selected: Callable[[str], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._on_workspace_selected = on_workspace_selected
        self._on_node_selected = on_node_selected
        self._workspaces: list[WorkspaceRef] = []
        self._rows: list[NodeRow] = []
        self._expanded: set[str] = set()
        self._updating_dropdown = False

        self._dropdown = Gtk.DropDown(hexpand=True)
        self._dropdown.connect("notify::selected", self._on_dropdown_changed)
        dropdown_frame = Gtk.Box(margin_start=12, margin_end=12, margin_top=12)
        dropdown_frame.append(self._dropdown)
        self.append(dropdown_frame)

        scrolled = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.append(scrolled)

        self._list = Gtk.ListBox(css_classes=["navigation-sidebar"], selection_mode=Gtk.SelectionMode.SINGLE)
        self._list.connect("row-selected", self._on_row_selected)
        scrolled.set_child(self._list)

    # ------------------------------------------------------------------ public

    def set_workspaces(self, workspaces: Sequence[WorkspaceRef]) -> None:
        """Populate the workspace drop-down; selects the first entry."""
        self._workspaces = list(workspaces)
        names = Gtk.StringList()
        for workspace in self._workspaces:
            names.append(workspace.name or workspace.uuid)
        self._updating_dropdown = True
        self._dropdown.set_model(names)
        self._dropdown.set_selected(0 if self._workspaces else Gtk.INVALID_LIST_POSITION)
        self._updating_dropdown = False

    def selected_workspace(self) -> str | None:
        """Return the selected workspace uuid, or ``None`` when no model."""
        index = int(self._dropdown.get_selected())
        if 0 <= index < len(self._workspaces):
            return self._workspaces[index].uuid
        return None

    def set_nodes(self, rows: Sequence[NodeRow]) -> None:
        """Rebuild the flat-indented list from the mirrored node rows."""
        selected_before = self.selected_node()
        self._rows = list(rows)
        while child := self._list.get_first_child():
            self._list.remove(child)
        selection_target: Gtk.ListBoxRow | None = None
        for row, depth, has_children in self._visible_rows():
            list_row = self._build_row(row, depth, has_children)
            self._list.append(list_row)
            if row.id == selected_before:
                selection_target = list_row
        if selection_target is None:
            selection_target = self._list.get_row_at_index(0)
        if selection_target is not None:
            self._list.select_row(selection_target)

    def selected_node(self) -> str | None:
        """Return the selected node id, or ``None``."""
        row = self._list.get_selected_row()
        if row is None:
            return None
        return str(row.node_id)

    # ----------------------------------------------------------------- private

    def _on_dropdown_changed(self, *_args: object) -> None:
        if self._updating_dropdown:
            return
        workspace_id = self.selected_workspace()
        if workspace_id is not None:
            self._on_workspace_selected(workspace_id)

    def _on_row_selected(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is not None:
            self._on_node_selected(str(row.node_id))

    def _visible_rows(self) -> list[tuple[NodeRow, int, bool]]:
        """Depth-first walk computing ``(row, depth, has_children)`` pairs.

        Nodes whose parent is missing locally are treated as roots; cyclic
        parent chains are cut off so corrupt data cannot hang the UI.
        """
        children: dict[str | None, list[NodeRow]] = {}
        for row in sorted(self._rows, key=lambda r: r.id):
            children.setdefault(row.parent_id, []).append(row)

        visible: list[tuple[NodeRow, int, bool]] = []

        def walk(parent_id: str | None, depth: int, path: set[str]) -> None:
            for row in children.get(parent_id, []):
                if row.id in path:
                    continue
                kids = [kid for kid in children.get(row.id, []) if kid.id not in path]
                visible.append((row, depth, bool(kids)))
                if row.id in self._expanded:
                    walk(row.id, depth + 1, path | {row.id})

        walk(None, 0, set())
        return visible

    def _build_row(self, row: NodeRow, depth: int, has_children: bool) -> Gtk.ListBoxRow:
        list_row = Gtk.ListBoxRow()
        list_row.node_id = row.id

        box = Gtk.Box(spacing=6, margin_start=12 + depth * 18, margin_end=12, margin_top=3, margin_bottom=3)
        list_row.set_child(box)

        if has_children:
            expanded = row.id in self._expanded
            chevron = Gtk.Button(
                icon_name="pan-down-symbolic" if expanded else "pan-end-symbolic",
                has_frame=False,
                tooltip_text="Collapse" if expanded else "Expand",
            )
            chevron.connect("clicked", self._on_chevron_clicked, row.id)
            box.append(chevron)
        else:
            box.append(Gtk.Box(width_request=28))

        if row.icon:
            icon_label = Gtk.Label(label=row.icon)
            box.append(icon_label)

        name_label = Gtk.Label(label=node_display_name(row), xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        box.append(name_label)
        return list_row

    def _on_chevron_clicked(self, _button: object, node_id: str) -> None:
        if node_id in self._expanded:
            self._expanded.discard(node_id)
        else:
            self._expanded.add(node_id)
        self.set_nodes(self._rows)
