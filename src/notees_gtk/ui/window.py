"""Main application window: navigation split view, sync wiring, edit toggle.

Layout: an :class:`Adw.NavigationSplitView` whose sidebar
(:mod:`notees_gtk.ui.tree`) lists workspaces and nodes from the local store,
and whose content area flips between the read-only page renderer
(:mod:`notees_gtk.ui.page_view`) and the plain-text editor
(:mod:`notees_gtk.ui.editor`).

Sync contract: the blocking client and engine only ever run on worker
threads via :func:`notees_gtk.ui.worker.run_in_worker`; results and errors
marshal back with ``GLib.idle_add``. Opening a workspace restores a snapshot
when worthwhile, syncs once, then re-syncs every 30 seconds.
"""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from notees_gtk.config import ClientConfig
from notees_gtk.core.api import ApiError, AuthenticationError, NoteesClient
from notees_gtk.core.protocol.clock import Clock
from notees_gtk.core.sync.engine import SyncEngine
from notees_gtk.data.store import LocalStore, NodeRow
from notees_gtk.ui import config_store
from notees_gtk.ui.ast_render import ast_to_plaintext, ast_to_view
from notees_gtk.ui.editor import EditorView
from notees_gtk.ui.login import LoginView
from notees_gtk.ui.page_view import PageViewWidget
from notees_gtk.ui.tree import NodeTreeSidebar, node_display_name
from notees_gtk.ui.worker import run_in_worker

__all__ = ["NoteesWindow"]

#: Interval-sync period in seconds.
SYNC_INTERVAL_SECONDS = 30

_ACTOR_FALLBACK = "anonymous"


class NoteesWindow(Adw.ApplicationWindow):
    """Root window; switches between the login page and the main split view."""

    def __init__(self, *, application: Adw.Application, config: ClientConfig) -> None:
        super().__init__(application=application, title="Notees", default_width=1040, default_height=720)
        self._config = config
        self._client: NoteesClient | None = None
        self._store: LocalStore | None = None
        self._engine: SyncEngine | None = None
        self._clock = Clock(config_store.ensure_device_id())
        self._workspace_id: str | None = None
        self._workspace_names: dict[str, str] = {}
        self._sync_source_id = 0

        self._toast_overlay = Adw.ToastOverlay(hexpand=True, vexpand=True)
        self.set_content(self._toast_overlay)
        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self._toast_overlay.set_child(self._stack)

        self._login_view = LoginView(on_logged_in=self._on_logged_in)
        self._stack.add_named(self._login_view, "login")

        if config.token:
            self._build_main()
            self._stack.set_visible_child_name("main")
        else:
            self._stack.set_visible_child_name("login")

    # ----------------------------------------------------------------- login

    def _on_logged_in(self, config: ClientConfig, client: NoteesClient, _user: dict[str, Any]) -> None:
        self._config = config
        self._client = client
        self._build_main()
        self._stack.set_visible_child_name("main")

    def _show_login(self, message: str | None = None) -> None:
        """Return to the login page (session expired or never authenticated)."""
        self._engine = None
        self._client = None
        self._stack.set_visible_child_name("login")
        if message:
            self._login_view.show_toast(message)

    # -------------------------------------------------------------- main UI

    def _build_main(self) -> None:
        """Build the split view bound to the current config/client."""
        assert self._config.token is not None  # login page shown otherwise
        old_main = self._stack.get_child_by_name("main")
        if old_main is not None:
            self._stack.remove(old_main)
        if self._client is None:
            self._client = NoteesClient(self._config.server_url, token=self._config.token)
        self._config.data_dir.mkdir(parents=True, exist_ok=True)
        if self._store is None:
            self._store = LocalStore(self._config.data_dir / "notees.db")

        self._sidebar = NodeTreeSidebar(
            on_workspace_selected=self._on_workspace_selected,
            on_node_selected=self._show_node,
        )
        sidebar_view = Adw.ToolbarView()
        sidebar_view.add_top_bar(Adw.HeaderBar(show_title=False))
        sidebar_view.set_content(self._sidebar)
        sidebar_page = Adw.NavigationPage(title="Notees", child=sidebar_view)

        self._page_view = PageViewWidget()
        actor_id = config_store.load_actor_id() or _ACTOR_FALLBACK
        self._editor = EditorView(
            self._store,
            actor_id,
            self._clock,
            on_saved=self._on_editor_saved,
        )
        self._content_stack = Gtk.Stack()
        self._content_stack.add_named(self._page_view, "view")
        self._content_stack.add_named(self._editor, "edit")

        self._edit_toggle = Gtk.ToggleButton(label="Edit")
        self._edit_toggle.connect("toggled", self._on_edit_toggled)
        content_bar = Adw.HeaderBar(show_title=False)
        content_bar.pack_end(self._edit_toggle)
        content_view = Adw.ToolbarView()
        content_view.add_top_bar(content_bar)
        content_view.set_content(self._content_stack)
        content_page = Adw.NavigationPage(title="", child=content_view)

        split = Adw.NavigationSplitView(sidebar=sidebar_page, content=content_page, hexpand=True, vexpand=True)
        self._stack.add_named(split, "main")

        self._load_workspaces()

    # ------------------------------------------------------------ workspaces

    def _load_workspaces(self) -> None:
        client = self._require_client()
        if client is None:
            return
        run_in_worker(client.list_workspaces, on_done=self._on_workspaces_loaded, on_error=self._on_sync_error)

    def _on_workspaces_loaded(self, workspaces: list[Any]) -> None:
        self._workspace_names = {str(ws.uuid): str(ws.name or ws.uuid) for ws in workspaces}
        self._sidebar.set_workspaces(workspaces)
        workspace_id = self._sidebar.selected_workspace()
        if workspace_id is not None:
            self._on_workspace_selected(workspace_id)

    def _on_workspace_selected(self, workspace_id: str) -> None:
        client = self._require_client()
        store = self._require_store()
        if client is None or store is None:
            return
        self._workspace_id = workspace_id
        self._editor.set_workspace(workspace_id)
        actor_id = config_store.load_actor_id() or _ACTOR_FALLBACK
        engine = SyncEngine(client, store, actor_id=actor_id, workspace_id=workspace_id)
        self._engine = engine
        self._start_interval_sync()

        def work() -> bool:
            restored = engine.maybe_restore_snapshot()
            engine.sync()
            return restored

        run_in_worker(work, on_done=self._on_workspace_synced, on_error=self._on_sync_error)

    def _on_workspace_synced(self, restored: bool) -> None:
        self._refresh_sidebar()
        if restored:
            self.show_toast("Restored the latest workspace snapshot")

    # ------------------------------------------------------------------ sync

    def _start_interval_sync(self) -> None:
        if self._sync_source_id:
            GLib.source_remove(self._sync_source_id)
        self._sync_source_id = GLib.timeout_add_seconds(SYNC_INTERVAL_SECONDS, self._on_interval_sync)

    def _on_interval_sync(self) -> bool:
        engine = self._engine
        if engine is not None:
            run_in_worker(engine.sync, on_done=lambda _result: self._refresh_sidebar(), on_error=self._on_sync_error)
        return True  # keep the GLib timeout source alive (GLib.SOURCE_CONTINUE)

    def _on_editor_saved(self, node_id: str) -> None:
        self.show_toast("Saved — syncing…")
        self._edit_toggle.set_active(False)
        engine = self._engine
        if engine is not None:
            run_in_worker(engine.sync, on_done=lambda _result: self._after_save_sync(node_id), on_error=self._on_sync_error)
        else:
            self._show_node(node_id)

    def _after_save_sync(self, node_id: str) -> None:
        self._refresh_sidebar()
        self._show_node(node_id)

    def _on_sync_error(self, exc: Exception) -> None:
        if isinstance(exc, AuthenticationError):
            self._show_login("Session expired — please sign in again")
        elif isinstance(exc, ApiError):
            self.show_toast(exc.detail)
        else:
            self.show_toast(str(exc))

    # ----------------------------------------------------------------- nodes

    def _refresh_sidebar(self) -> None:
        store = self._require_store()
        if store is None or self._workspace_id is None:
            return
        self._sidebar.set_nodes(store.nodes(self._workspace_id))

    def _show_node(self, node_id: str) -> None:
        store = self._require_store()
        if store is None or self._workspace_id is None:
            return
        row = store.node(self._workspace_id, node_id)
        if row is None:
            return
        title = node_display_name(row)
        self._page_view.show_page(title, ast_to_view(row.content, resolve_name=self._resolve_name))
        self._editor.open_node(node_id, ast_to_plaintext(row.content))

    def _resolve_name(self, target_id: str) -> str | None:
        """Store-backed node-link resolution: first line of the target's content."""
        store = self._require_store()
        if store is None or self._workspace_id is None:
            return None
        row: NodeRow | None = store.node(self._workspace_id, target_id)
        if row is None or not row.content:
            return None
        first_line = ast_to_plaintext(row.content).split("\n", 1)[0].strip()
        return first_line or None

    # ------------------------------------------------------------------ edit

    def _on_edit_toggled(self, toggle: Gtk.ToggleButton) -> None:
        if toggle.get_active():
            self._content_stack.set_visible_child_name("edit")
            self._edit_toggle.set_label("View")
        else:
            node_id = self._editor.current_node()
            self._content_stack.set_visible_child_name("view")
            self._edit_toggle.set_label("Edit")
            if node_id is not None:
                self._show_node(node_id)

    # ----------------------------------------------------------------- misc

    def show_toast(self, message: str) -> None:
        """Display a transient toast over the window."""
        self._toast_overlay.add_toast(Adw.Toast(title=message))

    def _require_client(self) -> NoteesClient | None:
        if self._client is None:
            self._show_login()
        return self._client

    def _require_store(self) -> LocalStore | None:
        if self._store is None:
            self.show_toast("Local store is not available yet")
        return self._store
