"""Worker-thread marshaling for blocking client/store/engine calls.

:class:`~notees_gtk.core.api.NoteesClient` and
:class:`~notees_gtk.core.sync.engine.SyncEngine` perform blocking HTTP and
SQLite work and must never run on the GTK main thread. This helper runs a
callable on a daemon thread and marshals its result (or exception) back onto
the main loop with ``GLib.idle_add``, so windows and widgets never call the
client or the engine directly.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

__all__ = ["run_in_worker"]

_log = logging.getLogger(__name__)


def run_in_worker[T](
    work: Callable[[], T],
    *,
    on_done: Callable[[T], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
) -> threading.Thread:
    """Run ``work`` on a daemon thread, delivering the outcome on the main loop.

    Args:
        work: Blocking call executed off the main thread.
        on_done: Called on the GTK main thread with the result.
        on_error: Called on the GTK main thread with the raised exception;
            without it, failures are logged instead of surfaced.

    Returns:
        The started thread (for diagnostics/tests on GTK hosts).
    """

    def _run() -> None:
        try:
            result = work()
        except Exception as exc:
            _log.debug("Worker task failed", exc_info=True)
            if on_error is not None:
                GLib.idle_add(on_error, exc)
            else:
                _log.warning("Worker task failed: %s", exc)
        else:
            if on_done is not None:
                GLib.idle_add(on_done, result)

    thread = threading.Thread(target=_run, daemon=True, name="notees-worker")
    thread.start()
    return thread
