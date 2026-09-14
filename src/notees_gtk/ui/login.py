"""Login page for the Notees GTK client.

Server URL (default ``http://localhost:8001``), email, password, and an
optional TOTP field for 2FA-gated accounts. The blocking
:meth:`NoteesClient.login` call runs on a worker thread (see
:mod:`notees_gtk.ui.worker`); failures surface as ``Adw.Toast`` overlays.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from notees_gtk.config import ClientConfig
from notees_gtk.core.api import ApiError, NoteesClient, TwoFactorRequired
from notees_gtk.ui import config_store
from notees_gtk.ui.worker import run_in_worker

__all__ = ["LoginView"]

#: Callback invoked after a successful login with the persisted config, the
#: authenticated client, and the server's public user record.
LoggedInCallback = Callable[[ClientConfig, NoteesClient, dict[str, Any]], None]


class LoginView(Gtk.Box):
    """Credentials form; reports errors through an :class:`Adw.Toast`."""

    def __init__(self, on_logged_in: LoggedInCallback) -> None:
        """Build the form; ``on_logged_in`` receives ``(config, client, user)``."""
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._on_logged_in = on_logged_in

        self._toast_overlay = Adw.ToastOverlay(hexpand=True, vexpand=True)
        self.append(self._toast_overlay)

        clamp = Adw.Clamp(maximum_size=380, vexpand=True)
        self._toast_overlay.set_child(clamp)

        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, valign=Gtk.Align.CENTER)
        clamp.set_child(form)

        group = Adw.PreferencesGroup(title="Sign in to Notees", description="Connect to your self-hosted server")
        form.append(group)

        self._server_row = Adw.EntryRow(title="Server URL", text=config_store.DEFAULT_SERVER_URL)
        group.add(self._server_row)

        self._email_row = Adw.EntryRow(title="Email")
        group.add(self._email_row)

        self._password_row = Adw.PasswordEntryRow(title="Password")
        group.add(self._password_row)

        self._totp_row = Adw.EntryRow(title="TOTP code (if 2FA is enabled)")
        group.add(self._totp_row)

        self._login_button = Gtk.Button(label="Log In", halign=Gtk.Align.CENTER, width_request=160)
        self._login_button.add_css_class("pill")
        self._login_button.add_css_class("suggested-action")
        self._login_button.connect("clicked", self._on_login_clicked)
        form.append(self._login_button)

    # ------------------------------------------------------------------ public

    def show_toast(self, message: str) -> None:
        """Display ``message`` as a transient toast over the form."""
        self._toast_overlay.add_toast(Adw.Toast(title=message))

    # ----------------------------------------------------------------- private

    def _on_login_clicked(self, *_args: object) -> None:
        server_url = self._server_row.get_text().strip() or config_store.DEFAULT_SERVER_URL
        email = self._email_row.get_text().strip()
        password = self._password_row.get_text()
        totp = self._totp_row.get_text().strip() or None
        if not email or not password:
            self.show_toast("Email and password are required")
            return

        self._set_busy(True)
        client = NoteesClient(server_url)

        def work() -> tuple[dict[str, Any], str]:
            result = client.login(email, password, totp=totp)
            return result.user, result.access_token

        run_in_worker(
            work,
            on_done=lambda user_and_token: self._on_login_done(server_url, client, *user_and_token),
            on_error=self._on_login_error,
        )

    def _on_login_done(self, server_url: str, client: NoteesClient, user: dict[str, Any], token: str) -> None:
        self._set_busy(False)
        config = ClientConfig(server_url=server_url, data_dir=config_store.data_dir(), token=token)
        config_store.save_config(config)
        # Envelopes are stamped with the user's public uuid (frontend: actorId = user.uuid).
        config_store.save_actor_id(str(user.get("uuid") or "anonymous"))
        self._on_logged_in(config, client, user)

    def _on_login_error(self, exc: Exception) -> None:
        self._set_busy(False)
        if isinstance(exc, TwoFactorRequired):
            self.show_toast("Two-factor authentication required — enter the TOTP code and try again")
        elif isinstance(exc, ApiError):
            self.show_toast(exc.detail)
        else:
            self.show_toast(str(exc))

    def _set_busy(self, busy: bool) -> None:
        self._login_button.set_sensitive(not busy)
        self._login_button.set_label("Signing in…" if busy else "Log In")
