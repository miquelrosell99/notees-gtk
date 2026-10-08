"""Read-only rendering of :class:`PageView` records into GTK widgets.

The heavy lifting (token parsing, mention/chip resolution) lives in the pure
:mod:`notees_gtk.ui.ast_render` module; this file only turns view records into
labels: Pango markup for marked runs and pills, monospace frames for math,
dimmed placeholders for block-scale tokens (asset/embed/query/whiteboard).

Above the content the widget renders the node-alias chrome (when the window
passes it — see :mod:`notees_gtk.ui.aliases`): the Aliases section on an
aliased node's view and the Aliased-node row on the alias's own view.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from notees_gtk.ui.aliases import AliasEntry, build_aliased_node_row, build_aliases_section
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
from notees_gtk.ui.brand import ContentColors, content_colors

__all__ = ["PageViewWidget", "run_markup", "runs_to_markup"]


def run_markup(run: object, colors: ContentColors) -> str:
    """Convert one inline view record to Pango markup.

    ``colors`` carries the Margin Green roles (``ui.brand``) resolved for the
    current colour scheme — Pango spans cannot read GTK CSS variables.
    """
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
        return f'<span foreground="{colors.link}" underline="single">{GLib.markup_escape_text(run.text, -1)}</span>'
    if isinstance(run, ClassChipRun):
        return (
            f'<span background="{colors.chip_background}" color="{colors.chip_foreground}">'
            f"{GLib.markup_escape_text(run.text, -1)}</span>"
        )
    if isinstance(run, TypedLinkRun):
        return f"<u>{GLib.markup_escape_text(run.text, -1)}</u>"
    if isinstance(run, ExternalLinkRun):
        return f'<span foreground="{colors.link}" underline="single">{GLib.markup_escape_text(run.text, -1)}</span>'
    if isinstance(run, MathRun):
        return f"<tt>{GLib.markup_escape_text(run.expression, -1)}</tt>"
    if isinstance(run, HardBreakRun):
        return ""
    return ""


def runs_to_markup(runs: Iterable[object], colors: ContentColors) -> str:
    """Convert a line's inline view records to one Pango markup string."""
    return "".join(run_markup(run, colors) for run in runs)


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
        # The reading surface: theme.css sets the Newsreader stack on this
        # class (the brand's reading-text typeface, with a system serif
        # fallback) and lets it inherit into every content label.
        self._content.add_css_class("page-content")
        self._colors: ContentColors = content_colors(dark=False)
        clamp = Adw.Clamp(maximum_size=860)
        clamp.set_child(self._content)
        scrolled.set_child(clamp)

    def show_page(
        self,
        title: str,
        view: PageView,
        *,
        aliases: Sequence[AliasEntry] = (),
        aliased_main: AliasEntry | None = None,
        repoint_candidates: Sequence[AliasEntry] = (),
        on_open_node: Callable[[str], None] | None = None,
        on_repoint: Callable[[str | None], None] | None = None,
    ) -> None:
        """Render ``view`` with ``title`` as the page header.

        The alias chrome (both directions of the ``aliasedNodeId`` relation)
        renders above the content when supplied: the Aliases section on the
        aliased node's view, the Aliased-node row on the alias's own view.
        """
        while child := self._content.get_first_child():
            self._content.remove(child)

        # Margin Green content roles resolved for the active colour scheme
        # (tokens.css light/dark); re-resolved on every render.
        self._colors = content_colors(dark=Adw.StyleManager.get_default().get_dark())

        if title:
            heading = Gtk.Label(label=title, xalign=0, wrap=True)
            heading.add_css_class("title-1")
            heading.add_css_class("page-title")
            self._content.append(heading)
            self._content.append(Gtk.Separator())

        if aliased_main is not None and on_repoint is not None:
            self._content.append(
                build_aliased_node_row(
                    main=aliased_main,
                    candidates=repoint_candidates,
                    on_open_node=on_open_node or (lambda _id: None),
                    on_repoint=on_repoint,
                )
            )
        if aliases and on_open_node is not None:
            self._content.append(build_aliases_section(aliases, on_open_node))

        line: list[object] = []
        for item in view.items:
            if isinstance(item, QuoteView):
                self._flush_line(line)
                label = Gtk.Label(xalign=0, wrap=True, use_markup=True, selectable=True)
                label.set_markup(f"“{runs_to_markup(item.children, self._colors)}”")
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
        label.set_markup(runs_to_markup(line, self._colors))
        self._content.append(label)
        line.clear()

    def _placeholder_widget(self, block: PlaceholderView) -> Gtk.Widget:
        label = Gtk.Label(label=f"[{block.kind}]", xalign=0)
        label.add_css_class("dim-label")
        return label
