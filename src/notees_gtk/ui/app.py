"""GtkApplication subclass wiring the window lifecycle."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, Gtk

from notees_gtk.ui import config_store
from notees_gtk.ui.window import NoteesWindow

__all__ = ["NoteesApp"]


class NoteesApp(Adw.Application):
    """Notees desktop client entry point (application id ``dev.notees.Gtk``)."""

    def __init__(self) -> None:
        super().__init__(application_id="dev.notees.Gtk", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        Adw.init()

    def do_activate(self) -> None:
        """Present the main window, restoring the persisted session if any."""
        _install_brand_theme()
        config = config_store.load_config()
        window = NoteesWindow(application=self, config=config)
        window.present()


def _install_brand_theme() -> None:
    """Load the Margin Green theme (``ui/theme.css``) for the default display.

    The CSS ships as a package asset (read through :mod:`importlib.resources`,
    so it lands inside the wheel); it re-skins libadwaita's accent chrome via
    the public ``--accent-*`` variables — Advance Green in both colour
    schemes. A missing display (headless import) simply skips the provider.
    """
    from importlib.resources import files

    css = files("notees_gtk.ui").joinpath("theme.css").read_text(encoding="utf-8")
    provider = Gtk.CssProvider()
    provider.load_from_string(css)
    display = Gdk.Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
