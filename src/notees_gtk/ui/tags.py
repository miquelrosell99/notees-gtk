"""The Tags row chrome for the page view (SCHEMA.md tags; web ``TagsRow``).

The web client renders a Tags row of pills wherever a node's metadata
surfaces — the page header (always, with the add/remove machinery) and a
dedicated row under a block that carries tags (``apps/web/src/ui/
{PageChrome,BlockRow}.tsx``). The GTK page view renders ONE node's own
content (it has no child-block rows), so both web surfaces collapse into
this single row on the opened node's view: pills for the assigned tags
(alphabetical, the tag's effective color as the pill surface) plus an Add
pill opening the shared page picker; each pill's remove button unassigns.

All decision logic lives in the pure :mod:`notees_gtk.ui.tag_ops`; this
file only turns records into widgets. The write path is the window's
(alias-chrome precedent): enqueue → optimistic mirror apply → outbox sync.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk

from notees_gtk.ui.brand import ContentColors
from notees_gtk.ui.node_picker import NodeEntry, pop_node_picker
from notees_gtk.ui.tag_ops import TagPill

__all__ = ["build_tags_row"]


def build_tags_row(
    *,
    pills: Sequence[TagPill],
    candidates: Sequence[NodeEntry],
    colors: ContentColors,
    on_open_tag: Callable[[str], None],
    on_assign: Callable[[str], None],
    on_unassign: Callable[[str], None],
) -> Gtk.Widget:
    """Build the ``Tags:`` metadata row: one pill per assigned tag (label
    navigates to the tag page, × unassigns) and an Add pill popping the
    page picker (the assign carrier). The row is always rendered — the web
    header behaves the same; a node with no tags shows just the Add pill.
    """
    row_box = Gtk.Box(spacing=6)

    caption = Gtk.Label(label="Tags", xalign=0)
    caption.add_css_class("dim-label")
    row_box.append(caption)

    for pill in pills:
        row_box.append(_build_pill(pill, colors, on_open_tag, on_unassign))

    add_button = Gtk.Button(label="Add tag", has_frame=False, tooltip_text="Assign a page as a tag")
    add_button.connect("clicked", lambda _b: pop_node_picker(add_button, candidates, on_assign))
    row_box.append(add_button)
    return row_box


def _build_pill(
    pill: TagPill,
    colors: ContentColors,
    on_open_tag: Callable[[str], None],
    on_unassign: Callable[[str], None],
) -> Gtk.Widget:
    """One tag pill: the colored label (a navigate button) + the remove ×.

    The surface is a Pango span background — the same mechanism the inline
    class chips use (:func:`notees_gtk.ui.page_view.run_markup`) — colored
    with the tag's effective color when set, else the neutral chip roles.
    """
    box = Gtk.Box(spacing=0)

    if pill.color is not None:
        background = pill.color
        foreground = pill.foreground or "#ffffff"
    else:
        background = colors.chip_background
        foreground = colors.chip_foreground
    markup = (
        f'<span background="{GLib.markup_escape_text(background, -1)}"'
        f' foreground="{GLib.markup_escape_text(foreground, -1)}">'
        f" {GLib.markup_escape_text(pill.label, -1)} </span>"
    )

    text_button = Gtk.Button(has_frame=False, tooltip_text="Open the tag page")
    text_button.connect("clicked", lambda _b, tag_id=pill.tag_id: on_open_tag(tag_id))
    pill_label = Gtk.Label(use_markup=True, selectable=False)
    pill_label.set_markup(markup)
    if pill.icon:
        icon_label = Gtk.Label(label=f"{pill.icon} ")
        icon_box = Gtk.Box()
        icon_box.append(icon_label)
        icon_box.append(pill_label)
        text_button.set_child(icon_box)
    else:
        text_button.set_child(pill_label)
    box.append(text_button)

    remove_button = Gtk.Button(label="×", has_frame=False, tooltip_text=f"Remove tag {pill.label}")
    remove_button.connect("clicked", lambda _b, tag_id=pill.tag_id: on_unassign(tag_id))
    box.append(remove_button)
    return box
