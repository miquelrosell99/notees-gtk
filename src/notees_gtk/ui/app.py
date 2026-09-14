"""GtkApplication subclass wiring the window lifecycle."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio

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
        config = config_store.load_config()
        window = NoteesWindow(application=self, config=config)
        window.present()
