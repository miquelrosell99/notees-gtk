"""Entry point for the Notees GTK client."""


def run() -> int:
    """Run the Notees GTK client.

    Imports the UI lazily so machines without PyGObject get an actionable
    message instead of an import traceback.

    Returns:
        The GTK application exit status.
    """
    try:
        from notees_gtk.ui.app import NoteesApp
    except ImportError as exc:
        raise SystemExit(
            "The Notees GTK UI requires PyGObject. Install it with: pip install 'notees-gtk[ui]'"
        ) from exc
    status: int = NoteesApp().run()
    return status
