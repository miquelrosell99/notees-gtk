"""Login page for the Notees GTK client.

Server URL (default ``http://localhost:8001``), email, and password. The blocking
:meth:`NoteesClient.login` call runs on a worker thread (see
:mod:`notees_gtk.ui.worker`); failures surface as ``Adw.Toast`` overlays.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, GdkPixbuf, GLib, Gtk

from notees_gtk.config import ClientConfig
from notees_gtk.core.api import ApiError, NoteesClient
from notees_gtk.ui import config_store
from notees_gtk.ui.brand import ink_bbox
from notees_gtk.ui.config_store import GTK_ACTOR_ID
from notees_gtk.ui.worker import run_in_worker

__all__ = ["LoginView"]

#: Callback invoked after a successful login with the persisted config, the
#: authenticated client, and the server's public user record.
LoggedInCallback = Callable[[ClientConfig, NoteesClient, dict[str, Any]], None]

#: Render width for the oversampled load — headroom for a clean crop + downscale.
_OVERSAMPLE_WIDTH_PX = 512
#: The lockup's render width on the login form (the wordmark stays readable).
_MARK_WIDTH_PX = 96


def _brand_symbol_pixbuf() -> GdkPixbuf.Pixbuf | None:
    """Load the Margin Green lockup cropped to its ink, or ``None``.

    The SVGs ship as package assets (``ui/assets/``, vendored from the brand
    submodule); ``full-color.svg`` carries the light-scheme inks (iron + green),
    ``full-color-dark.svg`` the dark-scheme roles. The SVGs' viewBox includes
    brand clear-space padding around the ink, so a raw at-scale render shrinks
    the mark to a fraction of the budget — the login form showed an illegible
    ~14px squiggle inside its 64px box. Render oversampled, crop to the opaque
    bounding box (:func:`notees_gtk.ui.brand.ink_bbox`), and downscale to the
    mark width. A host without the SVG pixbuf loader yields ``None`` and the
    form simply renders without the mark.
    """
    from importlib.resources import files

    dark = Adw.StyleManager.get_default().get_dark()
    name = "full-color-dark.svg" if dark else "full-color.svg"
    path = files("notees_gtk.ui").joinpath("assets", name)
    try:
        rendered = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), _OVERSAMPLE_WIDTH_PX, _OVERSAMPLE_WIDTH_PX, True)
    except GLib.Error:
        return None
    bbox = ink_bbox(
        rendered.get_pixels(),
        rendered.get_width(),
        rendered.get_height(),
        rendered.get_rowstride(),
        rendered.get_n_channels(),
        has_alpha=rendered.get_has_alpha(),
    )
    if bbox is None:
        return None
    x, y, w, h = bbox
    cropped = rendered.new_subpixbuf(x, y, w, h)
    mark_height = max(1, round(h * (_MARK_WIDTH_PX / w)))
    return cropped.scale_simple(_MARK_WIDTH_PX, mark_height, GdkPixbuf.InterpType.BILINEAR)


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

        # The brand lockup above the form: the Margin Green symbol in the
        # scheme-correct render (full-color on light, the f4f3f1/6da789
        # variant on dark), vendored from the brand/ submodule into the
        # package assets. Missing SVG loader -> the form renders without it.
        symbol = _brand_symbol_pixbuf()
        if symbol is not None:
            form.append(Gtk.Image.new_from_pixbuf(symbol))

        group = Adw.PreferencesGroup(title="Sign in to Notees", description="Connect to a Notees sync server")
        form.append(group)

        self._server_row = Adw.EntryRow(title="Server URL", text=config_store.DEFAULT_SERVER_URL)
        group.add(self._server_row)

        self._email_row = Adw.EntryRow(title="Email")
        group.add(self._email_row)

        self._password_row = Adw.PasswordEntryRow(title="Password")
        group.add(self._password_row)

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
        if not email or not password:
            self.show_toast("Email and password are required")
            return

        self._set_busy(True)
        client = NoteesClient(server_url)

        def work() -> tuple[dict[str, Any], str]:
            result = client.login(email, password)
            return result.user, result.token

        run_in_worker(
            work,
            on_done=lambda user_and_token: self._on_login_done(server_url, client, *user_and_token),
            on_error=self._on_login_error,
        )

    def _on_login_done(self, server_url: str, client: NoteesClient, user: dict[str, Any], token: str) -> None:
        self._set_busy(False)
        config = ClientConfig(server_url=server_url, data_dir=config_store.data_dir(), token=token)
        config_store.save_config(config)
        # Envelopes ride the GTK system actor (config_store.GTK_ACTOR_ID).
        config_store.save_actor_id(GTK_ACTOR_ID)
        self._on_logged_in(config, client, user)

    def _on_login_error(self, exc: Exception) -> None:
        self._set_busy(False)
        if isinstance(exc, ApiError):
            self.show_toast(exc.detail)
        else:
            self.show_toast(str(exc))

    def _set_busy(self, busy: bool) -> None:
        self._login_button.set_sensitive(not busy)
        self._login_button.set_label("Signing in…" if busy else "Log In")
