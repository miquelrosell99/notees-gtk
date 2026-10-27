"""The node-alias chrome for the page view (SCHEMA.md "Node aliases").

The ``aliasedNodeId`` wire node field rides the alias relation (many-to-one
FROM the alias); this module renders the two honest affordances the web
client carries (``apps/web/src/ui/components/{AliasesButton,AliasedNodeRow}.tsx``),
shaped for this client's chrome idioms:

- the **Aliases** section on the aliased node's view — one row per node
  whose alias-terminal is it (the store's ``alias_nodes_of`` read, chains
  included), each with an Open button that navigates to the ALIAS node's own
  view (the one deliberate bypass of the navigation redirect, as on the
  web);
- the **Aliased node** row on the alias's own view — names the main (Open
  navigates to it), Change… re-points the carrier's OWN field through a
  popover picker (the pure ``alias_repoint_candidates`` filter), Clear
  writes the present-null.

The main-side ADD direction (the backward write) is not part of this chrome
— the minimal honest set on the new field.

All decision logic lives in the pure :mod:`notees_gtk.ui.alias_ops`; this
file only turns records into widgets.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Pango

from notees_gtk.ui.node_picker import NodeEntry, pop_node_picker

__all__ = ["build_aliases_section", "build_aliased_node_row"]

#: (node id, display label) pairs — the chrome's row records.
AliasEntry = NodeEntry


def build_aliases_section(aliases: Sequence[AliasEntry], on_open_node: Callable[[str], None]) -> Gtk.Widget:
    """The main-side Aliases section: one named row per alias + Open each.

    Renders nothing for an empty set (the window only passes live aliases),
    so ordinary pages stay untouched.
    """
    section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    heading = Gtk.Label(label="Aliases", xalign=0)
    heading.add_css_class("heading")
    section.append(heading)
    for alias_id, label in aliases:
        row_box = Gtk.Box(spacing=6)
        name = Gtk.Label(label=label, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        row_box.append(name)
        open_button = Gtk.Button(label="Open", has_frame=False, tooltip_text="Open the alias itself")
        open_button.connect("clicked", lambda _b, node_id=alias_id: on_open_node(node_id))
        row_box.append(open_button)
        section.append(row_box)
    return section


def build_aliased_node_row(
    *,
    main: AliasEntry,
    candidates: Sequence[AliasEntry],
    on_open_node: Callable[[str], None],
    on_repoint: Callable[[str | None], None],
) -> Gtk.Widget:
    """The alias-side pseudo-property row: ``Aliased node: <main>  Change…  Clear``.

    Change… pops the candidate picker over the button and re-points the
    carrier's OWN ``aliasedNodeId``; Clear writes the present-null. The main
    name itself navigates to the main.
    """
    main_id, main_label = main
    row_box = Gtk.Box(spacing=6)

    caption = Gtk.Label(label="Aliased node", xalign=0)
    caption.add_css_class("dim-label")
    row_box.append(caption)

    main_button = Gtk.Button(label=main_label, has_frame=False, tooltip_text="Open the aliased node")
    main_button.connect("clicked", lambda _b: on_open_node(main_id))
    row_box.append(main_button)

    change_button = Gtk.Button(label="Change…", has_frame=False, tooltip_text="Re-point the alias")
    change_button.connect("clicked", lambda _b: pop_node_picker(change_button, candidates, on_repoint))
    row_box.append(change_button)

    clear_button = Gtk.Button(label="Clear", has_frame=False, tooltip_text="Clear the alias target")
    clear_button.connect("clicked", lambda _b: on_repoint(None))
    row_box.append(clear_button)
    return row_box
