"""The shared node picker popover (search entry over labeled rows).

Extracted from :mod:`notees_gtk.ui.aliases`' alias re-point picker so every
chrome that needs "search pages/nodes, pick one" reuses ONE implementation
(the alias row's Change picker and the Tags row's add picker today). Pure
candidates filters live beside each chrome's ops module
(:mod:`notees_gtk.ui.alias_ops`, :mod:`notees_gtk.ui.tag_ops`); this widget
only turns the ``(node id, label)`` records into the popover UI.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk

__all__ = ["NodeEntry", "pop_node_picker"]

#: (node id, display label) pairs — the picker's row records.
NodeEntry = tuple[str, str]


def pop_node_picker(
    anchor: Gtk.Widget,
    entries: Sequence[NodeEntry],
    on_pick: Callable[[str], None],
    *,
    empty_label: str = "No available nodes",
    search_placeholder: str = "Search…",
) -> None:
    """Show the picker over ``anchor``: a search entry filtering the row
    list; activating a row pops the popover down and hands the id to
    ``on_pick``. An empty candidate set renders an explicit empty state
    rather than a dead list."""
    popover = Gtk.Popover()
    popover.set_parent(anchor)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_bottom=6)
    popover.set_child(box)

    if not entries:
        empty = Gtk.Label(label=empty_label, margin_start=12, margin_end=12)
        empty.add_css_class("dim-label")
        box.append(empty)
        popover.popup()
        return

    search = Gtk.SearchEntry(margin_start=6, margin_end=6, placeholder_text=search_placeholder)
    box.append(search)
    list_box = Gtk.ListBox(css_classes=["navigation-sidebar"], selection_mode=Gtk.SelectionMode.NONE)
    for entry_id, label in entries:
        entry_row = Gtk.ListBoxRow()
        entry_row.node_id = entry_id
        entry_label = Gtk.Label(label=label, xalign=0, margin_start=6, margin_end=6)
        entry_row.set_child(entry_label)
        list_box.append(entry_row)
    scrolled = Gtk.ScrolledWindow(max_content_height=320, min_content_height=120, propagate_natural_height=True)
    scrolled.set_child(list_box)
    box.append(scrolled)

    def on_row_activated(_list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        popover.popdown()
        on_pick(str(row.node_id))

    def on_search_changed(entry: Gtk.SearchEntry) -> None:
        needle = entry.get_text().strip().lower()
        child = list_box.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.ListBoxRow):
                label = child.get_child()
                text = label.get_text().lower() if isinstance(label, Gtk.Label) else ""
                child.set_visible(needle == "" or needle in text)
            child = child.get_next_sibling()

    list_box.connect("row-activated", on_row_activated)
    search.connect("search-changed", on_search_changed)
    popover.popup()
