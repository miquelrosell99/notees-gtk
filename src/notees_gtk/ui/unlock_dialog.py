"""The in-session unlock re-prompt (the web's W2 ``UnlockWorkspaceModal`` port).

A modal :class:`Adw.Dialog` the locked workspace's banner raises: asks for
the account password, re-authenticates through ``POST /api/auth/unlock``
(session-less — the endpoint answers ``{kdf}`` only, no session minted, the
login lockout applies), derives the master key on the worker thread, and
hands it to the caller — the workspace re-opens unlocked without a
sign-out. Wrong password shows the form error; the server lockout (429,
``account_locked``) surfaces the server's retry detail verbatim.
"""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from notees_gtk.core.api import ApiError, NoteesClient
from notees_gtk.core.protocol.crypto import KdfRecord, unwrap_master_key
from notees_gtk.ui.worker import run_in_worker

__all__ = ["UnlockWorkspaceDialog"]

#: Callback invoked with the re-derived raw master key on success.
UnlockedCallback = Callable[[bytes], None]


class UnlockWorkspaceDialog(Adw.Dialog):
    """Password prompt that unlocks an encrypted workspace in-session."""

    def __init__(self, *, email: str, client: NoteesClient, on_unlocked: UnlockedCallback) -> None:
        """Build the prompt; ``on_unlocked`` receives the derived master key."""
        super().__init__(title="Unlock workspace", content_width=380)
        self._email = email
        self._client = client
        self._on_unlocked = on_unlocked

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_start=24, margin_end=24, margin_bottom=18)
        toolbar.set_content(root)
        self.set_content(toolbar)

        heading = Gtk.Label(
            label="This workspace is encrypted",
            xalign=0,
            css_classes=["title-3"],
        )
        root.append(heading)
        body = Gtk.Label(
            label=f"Enter the password for {email} to open it. Nothing is synced until it unlocks.",
            xalign=0,
            wrap=True,
            css_classes=["body"],
        )
        root.append(body)

        self._password_row = Adw.PasswordEntryRow(title="Password", show_apply_button=False)
        self._password_row.connect("entry-activated", self._on_unlock_clicked)
        root.append(self._password_row)

        self._error_label = Gtk.Label(xalign=0, wrap=True, visible=False, css_classes=["error"])
        root.append(self._error_label)

        self._unlock_button = Gtk.Button(label="Unlock", halign=Gtk.Align.CENTER, width_request=140)
        self._unlock_button.add_css_class("pill")
        self._unlock_button.add_css_class("suggested-action")
        self._unlock_button.connect("clicked", self._on_unlock_clicked)
        root.append(self._unlock_button)

    # ----------------------------------------------------------------- private

    def _on_unlock_clicked(self, *_args: object) -> None:
        password = self._password_row.get_text()
        if not password:
            self._show_error("Enter your password")
            return
        self._set_busy(True)

        def work() -> bytes:
            # Session-less: the endpoint answers {kdf} only — the live
            # credential the client already holds keeps working.
            kdf = self._client.unlock(self._email, password)
            # A correct account password whose record does not open fails at
            # the verifier — a form error, never a silent keyless session.
            return unwrap_master_key(password, KdfRecord.from_dict(kdf))

        run_in_worker(work, on_done=self._on_unlock_done, on_error=self._on_unlock_error)

    def _on_unlock_done(self, master_key: bytes) -> None:
        self.close()
        self._on_unlocked(master_key)

    def _on_unlock_error(self, exc: Exception) -> None:
        self._set_busy(False)
        if isinstance(exc, ApiError):
            # 401 invalid_credentials, 429 rate_limited/account_locked (the
            # detail carries the server's retry) — verbatim on the form.
            self._show_error(exc.detail)
        else:
            self._show_error(str(exc))

    def _show_error(self, message: str) -> None:
        self._error_label.set_label(message)
        self._error_label.set_visible(True)

    def _set_busy(self, busy: bool) -> None:
        self._unlock_button.set_sensitive(not busy)
        self._password_row.set_sensitive(not busy)
        self._unlock_button.set_label("Unlocking…" if busy else "Unlock")
