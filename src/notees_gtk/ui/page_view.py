"""Read-only rendering of :class:`PageView` records into GTK widgets.

The heavy lifting (unwrap, block mapping, node-link resolution) lives in the
pure :mod:`notees_gtk.ui.ast_render` module; this file only turns view
records into labels: Pango markup for marked runs, monospace frames for code
and math, checkboxes for todos, dimmed placeholders for whiteboard/query
blocks.
"""

from __future__ import annotations

from collections.abc import Iterable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from notees_gtk.ui.ast_render import (
    CodeView,
    HeadingView,
    MathView,
    PageView,
    ParagraphView,
    PlaceholderView,
    TextRun,
    TodoView,
)

__all__ = ["PageViewWidget", "runs_to_markup"]

#: libadwaita title styles for heading levels 1–4; deeper levels reuse the
#: level-4 style (libadwaita ships title-1 … title-4).
_HEADING_STYLES = {1: "title-1", 2: "title-2", 3: "title-3"}


def runs_to_markup(runs: Iterable[TextRun]) -> str:
    """Convert runs to Pango markup, honoring marks and node-link pills."""
    parts: list[str] = []
    for run in runs:
        text = GLib.markup_escape_text(run.text, -1)
        if run.node_link is not None:
            text = f'<span foreground="#3584E4" underline="single">{text}</span>'
        if "code" in run.marks:
            text = f"<tt>{text}</tt>"
        if "highlight" in run.marks:
            text = f'<span background="yellow" color="black">{text}</span>'
        if "strikethrough" in run.marks:
            text = f"<s>{text}</s>"
        if "underline" in run.marks:
            text = f"<u>{text}</u>"
        if "em" in run.marks:
            text = f"<i>{text}</i>"
        if "strong" in run.marks:
            text = f"<b>{text}</b>"
        parts.append(text)
    return "".join(parts)


class PageViewWidget(Gtk.Box):
    """Scrollable read-only rendering of one node's content."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        scrolled = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.append(scrolled)
        self._content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=18, margin_bottom=18, margin_start=24, margin_end=24)
        clamp = Adw.Clamp(maximum_size=860)
        clamp.set_child(self._content)
        scrolled.set_child(clamp)

    def show_page(self, title: str, view: PageView) -> None:
        """Render ``view`` with ``title`` as the page header."""
        while child := self._content.get_first_child():
            self._content.remove(child)

        if title:
            heading = Gtk.Label(label=title, xalign=0, wrap=True)
            heading.add_css_class("title-1")
            heading.add_css_class("page-title")
            self._content.append(heading)
            self._content.append(Gtk.Separator())

        for block in view.blocks:
            self._content.append(self._block_widget(block))

    # ----------------------------------------------------------------- private

    def _block_widget(self, block: object) -> Gtk.Widget:
        if isinstance(block, HeadingView):
            label = Gtk.Label(xalign=0, wrap=True, use_markup=True)
            label.set_markup(runs_to_markup(block.runs))
            label.add_css_class(_HEADING_STYLES.get(block.level, "title-4"))
            return label
        if isinstance(block, ParagraphView):
            label = Gtk.Label(xalign=0, wrap=True, use_markup=True, selectable=True)
            label.set_markup(runs_to_markup(block.runs))
            return label
        if isinstance(block, TodoView):
            box = Gtk.Box(spacing=6)
            check = Gtk.CheckButton(active=block.checked, sensitive=False)
            box.append(check)
            label = Gtk.Label(xalign=0, wrap=True, use_markup=True, hexpand=True)
            label.set_markup(runs_to_markup(block.runs))
            box.append(label)
            return box
        if isinstance(block, CodeView):
            label = Gtk.Label(label=block.text, xalign=0, wrap=True, selectable=True, use_markup=False)
            label.add_css_class("monospace")
            frame = Gtk.Frame()
            frame.set_child(label)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            label.set_margin_start(12)
            label.set_margin_end(12)
            return frame
        if isinstance(block, MathView):
            label = Gtk.Label(label=block.latex, xalign=0, wrap=True, selectable=True)
            label.add_css_class("monospace")
            return label
        if isinstance(block, PlaceholderView):
            label = Gtk.Label(label=f"[{block.kind}]", xalign=0)
            label.add_css_class("dim-label")
            return label
        return Gtk.Label(label=f"[{type(block).__name__}]", xalign=0)
