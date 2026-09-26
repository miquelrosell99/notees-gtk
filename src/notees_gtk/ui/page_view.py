"""Read-only rendering of :class:`PageView` records into GTK widgets.

The heavy lifting (token parsing, mention/chip resolution) lives in the pure
:mod:`notees_gtk.ui.ast_render` module; this file only turns view records into
labels: Pango markup for marked runs and pills, monospace frames for math,
dimmed placeholders for block-scale tokens (asset/embed/query/whiteboard).
"""

from __future__ import annotations

from collections.abc import Iterable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from notees_gtk.ui.ast_render import (
    ClassChipRun,
    ExternalLinkRun,
    HardBreakRun,
    MathRun,
    MentionRun,
    PageView,
    PlaceholderView,
    QuoteView,
    TextRun,
    TypedLinkRun,
)

__all__ = ["PageViewWidget", "run_markup", "runs_to_markup"]


def run_markup(run: object) -> str:
    """Convert one inline view record to Pango markup."""
    if isinstance(run, TextRun):
        text = f"{GLib.markup_escape_text(run.text, -1)}"
        if "code" in run.marks:
            text = f"<tt>{text}</tt>"
        if "highlight" in run.marks:
            text = f'<span background="yellow" color="black">{text}</span>'
        if "strike" in run.marks:
            text = f"<s>{text}</s>"
        if "italic" in run.marks:
            text = f"<i>{text}</i>"
        if "bold" in run.marks:
            text = f"<b>{text}</b>"
        return text
    if isinstance(run, MentionRun):
        return f'<span foreground="#3584E4" underline="single">{GLib.markup_escape_text(run.text, -1)}</span>'
    if isinstance(run, ClassChipRun):
        return f'<span background="#D3D3D3" color="black">{GLib.markup_escape_text(run.text, -1)}</span>'
    if isinstance(run, TypedLinkRun):
        return f"<u>{GLib.markup_escape_text(run.text, -1)}</u>"
    if isinstance(run, ExternalLinkRun):
        return f'<span foreground="#1B5FBF" underline="single">{GLib.markup_escape_text(run.text, -1)}</span>'
    if isinstance(run, MathRun):
        return f"<tt>{GLib.markup_escape_text(run.expression, -1)}</tt>"
    if isinstance(run, HardBreakRun):
        return ""
    return ""


def runs_to_markup(runs: Iterable[object]) -> str:
    """Convert a line's inline view records to one Pango markup string."""
    return "".join(run_markup(run) for run in runs)


class PageViewWidget(Gtk.Box):
    """Scrollable read-only rendering of one node's content."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        scrolled = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.append(scrolled)
        self._content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=18,
            margin_bottom=18,
            margin_start=24,
            margin_end=24,
        )
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

        line: list[object] = []
        for item in view.items:
            if isinstance(item, QuoteView):
                self._flush_line(line)
                label = Gtk.Label(xalign=0, wrap=True, use_markup=True, selectable=True)
                label.set_markup(f"“{runs_to_markup(item.children)}”")
                label.add_css_class("dim-label")
                self._content.append(label)
            elif isinstance(item, PlaceholderView):
                self._flush_line(line)
                self._content.append(self._placeholder_widget(item))
            else:
                line.append(item)
        self._flush_line(line)

    # ----------------------------------------------------------------- private

    def _flush_line(self, line: list[object]) -> None:
        """Render the buffered inline records as one wrapped paragraph."""
        if not line:
            return
        label = Gtk.Label(xalign=0, wrap=True, use_markup=True, selectable=True)
        label.set_markup(runs_to_markup(line))
        self._content.append(label)
        line.clear()

    def _placeholder_widget(self, block: PlaceholderView) -> Gtk.Widget:
        label = Gtk.Label(label=f"[{block.kind}]", xalign=0)
        label.add_css_class("dim-label")
        return label
