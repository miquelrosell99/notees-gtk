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

from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from notees_gtk.config import ClientConfig
from notees_gtk.core.api import ApiError, AuthenticationError, CardsAlgorithm, NoteesClient, UserPrefs
from notees_gtk.core.cards import (
    CardBucketRow,
    build_review_queue,
    card_due_day,
    card_members,
    card_srs_state,
    partition_cards_into_buckets,
)
from notees_gtk.core.protocol.clock import Clock
from notees_gtk.core.protocol.features import card_property_uuid
from notees_gtk.core.protocol.ids import new_uuid7
from notees_gtk.core.protocol.models import RelayEnvelope
from notees_gtk.core.srs import CardGrade, CardSrsState, next_card_state
from notees_gtk.core.sync.engine import SyncEngine
from notees_gtk.data.errors import CycleError, MoveGuardError
from notees_gtk.data.store import LocalStore, NodeRow
from notees_gtk.ui import config_store
from notees_gtk.ui.alias_ops import alias_repoint_candidates, alias_update_envelope
from notees_gtk.ui.ast_render import ast_to_plaintext, ast_to_view
from notees_gtk.ui.brand import content_colors
from notees_gtk.ui.card_ops import card_review_envelope, date_chain_envelopes, review_property_envelopes
from notees_gtk.ui.cards_hub import CardsHubView
from notees_gtk.ui.editor import EditorView
from notees_gtk.ui.login import LoginView
from notees_gtk.ui.page_view import PageViewWidget
from notees_gtk.ui.review_session import LoadedCard, ReviewSessionDialog
from notees_gtk.ui.tag_ops import tag_assign_candidates, tag_assign_envelope, tag_pills, tag_unassign_envelope
from notees_gtk.ui.tags import build_tags_row
from notees_gtk.ui.tree import NodeTreeSidebar, node_display_name
from notees_gtk.ui.worker import run_in_worker

__all__ = ["NoteesWindow"]

#: Interval-sync period in seconds.
SYNC_INTERVAL_SECONDS = 30




class NoteesWindow(Adw.ApplicationWindow):
    """Root window; switches between the login page and the main split view."""

    def __init__(self, *, application: Adw.Application, config: ClientConfig) -> None:
        super().__init__(application=application, title="Notees", default_width=1040, default_height=720)
        # The Margin Green app icon, resolved by name from the icon theme:
        # packaging installs data/icons/hicolor/**/dev.notees.Gtk.{svg,png}
        # (vendored from the brand/ submodule); the lookup is a no-op where
        # the icon is not installed yet, as before this slice.
        self.set_icon_name("dev.notees.Gtk")
        self._config = config
        self._client: NoteesClient | None = None
        self._store: LocalStore | None = None
        self._engine: SyncEngine | None = None
        self._clock = Clock(config_store.ensure_device_id())
        self._workspace_id: str | None = None
        self._workspace_names: dict[str, str] = {}
        self._sync_source_id = 0
        # The memorization-cards scheduler (Task-3 prefs seam): fetched once
        # per login from GET /api/me/prefs, cached device-locally, broadcast
        # to registered chrome listeners (the hub selector lands on top).
        self._cards_algorithm: CardsAlgorithm | None = None
        self._cards_algorithm_listeners: list[Callable[[CardsAlgorithm], None]] = []

        self._toast_overlay = Adw.ToastOverlay(hexpand=True, vexpand=True)
        self.set_content(self._toast_overlay)
        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self._toast_overlay.set_child(self._stack)

        self._login_view = LoginView(on_logged_in=self._on_logged_in)
        self._stack.add_named(self._login_view, "login")

        if (
            config.token or config.api_key
        ):  # compat — key-only config skips the login page until the login UI lands
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
        engine = self._engine
        if engine is not None:
            engine.stop_realtime()
        self._engine = None
        self._client = None
        self._stack.set_visible_child_name("login")
        if message:
            self._login_view.show_toast(message)

    # -------------------------------------------------------------- main UI

    def _build_main(self) -> None:
        """Build the split view bound to the current config/client."""
        assert self._config.token is not None or self._config.api_key is not None  # login page shown otherwise
        old_main = self._stack.get_child_by_name("main")
        if old_main is not None:
            self._stack.remove(old_main)
        if self._client is None:
            # compat — API key (v2 relay auth) passes through; login-token path stays until the login UI lands
            self._client = NoteesClient(self._config.server_url, token=self._config.token, api_key=self._config.api_key)
        self._config.data_dir.mkdir(parents=True, exist_ok=True)
        if self._store is None:
            self._store = LocalStore(self._config.data_dir / "notees.db")

        self._sidebar = NodeTreeSidebar(
            on_workspace_selected=self._on_workspace_selected,
            on_node_selected=self._show_node,
            on_cards_selected=self._show_cards_hub,
        )
        sidebar_view = Adw.ToolbarView()
        sidebar_view.add_top_bar(Adw.HeaderBar(show_title=False))
        sidebar_view.set_content(self._sidebar)
        sidebar_page = Adw.NavigationPage(title="Notees", child=sidebar_view)

        self._page_view = PageViewWidget()
        actor_id = config_store.load_actor_id() or config_store.GTK_ACTOR_ID
        self._editor = EditorView(
            self._store,
            actor_id,
            self._clock,
            on_saved=self._on_editor_saved,
        )
        self._content_stack = Gtk.Stack()
        self._content_stack.add_named(self._page_view, "view")
        self._content_stack.add_named(self._editor, "edit")
        self._cards_hub = CardsHubView(
            on_review=self._start_review,
            on_algorithm_selected=self.set_cards_algorithm,
        )
        self._content_stack.add_named(self._cards_hub, "cards")
        # The login-time prefs fetch may resolve after the hub was built —
        # keep the selector honest when it does.
        self.connect_cards_algorithm(self._cards_hub.set_algorithm)

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
        run_in_worker(client.get_prefs, on_done=self._on_prefs_loaded, on_error=self._on_prefs_unreachable)

    # ------------------------------------------------------------------ prefs

    def _on_prefs_loaded(self, prefs: UserPrefs) -> None:
        """The GET /api/me/prefs answer: refresh the device cache and the
        in-memory value (the web's getPrefs write-through posture)."""
        config_store.save_cards_algorithm(prefs.cards_algorithm)
        self._cards_algorithm = prefs.cards_algorithm
        self._notify_cards_algorithm()

    def _on_prefs_unreachable(self, _exc: Exception) -> None:
        """Offline/unreachable at login: the cached value answers, else the
        ``"fsrs"`` default — silently, like the web (an offline start is not
        an error worth a toast)."""
        self._cards_algorithm = config_store.load_cards_algorithm()
        self._notify_cards_algorithm()

    def cards_algorithm(self) -> CardsAlgorithm:
        """The scheduler the memorization-cards chrome schedules with:
        in-memory when the prefs fetch answered, else the device cache,
        else ``"fsrs"``."""
        if self._cards_algorithm is not None:
            return self._cards_algorithm
        return config_store.load_cards_algorithm()

    def connect_cards_algorithm(self, listener: Callable[[CardsAlgorithm], None]) -> None:
        """Register a chrome listener (the hub selector) notified on every
        change, including the login-time fetch resolution."""
        self._cards_algorithm_listeners.append(listener)

    def set_cards_algorithm(self, value: CardsAlgorithm) -> None:
        """The selector's write-through (the web's patchPrefs posture): the
        device cache updates first (fail loud on a non-grammar value, before
        anything mutates), the in-memory value broadcasts, and the PUT is
        best-effort — when the server cannot be reached the cached value is
        the honest offline answer and no error surfaces."""
        config_store.save_cards_algorithm(value)
        self._cards_algorithm = value
        self._notify_cards_algorithm()
        client = self._client
        if client is not None:
            run_in_worker(
                partial(client.patch_prefs, cards_algorithm=value),
                on_done=lambda _prefs: None,
                on_error=lambda _exc: None,
            )

    def _notify_cards_algorithm(self) -> None:
        value = self.cards_algorithm()
        for listener in self._cards_algorithm_listeners:
            listener(value)

    # ------------------------------------------------------------ cards hub

    def _show_cards_hub(self) -> None:
        """The hub surface: a sibling of view/edit in the content stack,
        never a node — the pinned sidebar row selects it, a node selection
        switches back (``_show_node``)."""
        self._refresh_hub()
        self._sidebar.select_cards()
        self._content_stack.set_visible_child_name("cards")

    def _refresh_hub(self) -> None:
        """Recompute the hub from the live mirror: card members → bucket
        rows over the effective cardDue read → the pure partition. Runs on
        workspace sync, interval sync, and post-session close (the last via
        ``_show_cards_hub``)."""
        store = self._require_store()
        if store is None or self._workspace_id is None:
            return
        rows = store.nodes(self._workspace_id)
        members = card_members(rows, store.class_extends_edges())
        due_schema = card_property_uuid("cardDue")
        bucket_rows = [
            CardBucketRow(member.id, card_due_day(store.get_effective_properties(member.id), due_schema))
            for member in members
        ]
        today = datetime.now().date().isoformat()
        buckets = partition_cards_into_buckets(bucket_rows, today)
        names = {member.id: node_display_name(member) for member in members}
        self._cards_hub.show_hub(buckets=buckets, names=names, algorithm=self.cards_algorithm())

    def _start_review(self, card_ids: list[str]) -> None:
        """Open the review session over the hand-off ids (the hub's Review
        action): the queue is the pure due-then-fresh build over the Task-2
        reads — a direct open with no hand-off falls back to all card
        members (the web deep-link fallback)."""
        store = self._require_store()
        if store is None or self._workspace_id is None:
            return
        members = card_members(store.nodes(self._workspace_id), store.class_extends_edges())
        ids = card_ids if card_ids else [member.id for member in members]
        due_schema = card_property_uuid("cardDue")
        queue_rows = [
            CardBucketRow(card_id, card_due_day(store.get_effective_properties(card_id), due_schema))
            for card_id in ids
        ]
        today = datetime.now().date().isoformat()
        queue = build_review_queue(queue_rows, today)
        if not queue:
            self.show_toast("Nothing to review — no due or new cards")
            return
        # The session's scheduler is read ONCE here (the web's mount-time
        # read; every card.review op records which algorithm ran).
        algorithm = self.cards_algorithm()
        session_id = new_uuid7()
        dialog = ReviewSessionDialog(
            queue=queue,
            load_card=self._load_session_card,
            on_grade=lambda card_id, grade, before: self._grade_session_card(
                session_id, algorithm, card_id, grade, before
            ),
            on_close=self._show_cards_hub,
        )
        dialog.present(self)

    def _load_session_card(self, card_id: str) -> LoadedCard | None:
        """The session's lazy card read: the node, its embedded children
        (the back), and the derived SRS state before grading — the
        JOURNAL-derived state (``card_srs_state`` over ``list_card_reviews``),
        the plan-sanctioned GTK divergence from the web session's
        effective-properties read (``core.cards.card_srs_state`` is the
        journal twin of that read)."""
        store = self._require_store()
        if store is None or self._workspace_id is None:
            return None
        row = store.node(self._workspace_id, card_id)
        if row is None:
            return None
        children = [child for child in store.children(self._workspace_id, card_id) if child.is_embedded]
        return LoadedCard(
            title=node_display_name(row),
            front=ast_to_view(row.content, resolve_name=self._resolve_name),
            back_children=tuple(ast_to_view(child.content, resolve_name=self._resolve_name) for child in children),
            state=card_srs_state(store.list_card_reviews(card_id)),
        )

    def _grade_session_card(
        self,
        session_id: str,
        algorithm: CardsAlgorithm,
        card_id: str,
        grade: CardGrade,
        state_before: CardSrsState,
    ) -> None:
        """The grade's write path (owner ruling 3 — enqueue + one sync per
        grade; ruling 7 — no undo journal): the Task-4 envelopes in order —
        the due-day date-chain ensure, the property batch at the row's
        existing idx, the ``card.review`` history op last — all enqueued with
        the optimistic mirror apply (the ``_enqueue_tag_write`` pattern).
        A structural guard failure toasts and stops the loop; the envelopes
        ALREADY enqueued stay in the outbox (the fail-loud house posture —
        the loud report, never a silent partial undo)."""
        store = self._require_store()
        workspace_id = self._workspace_id
        if store is None or workspace_id is None:
            return
        actor_id = config_store.load_actor_id() or config_store.GTK_ACTOR_ID
        device_id = config_store.ensure_device_id()
        today = datetime.now().date().isoformat()
        state_after = next_card_state(state_before, grade, algorithm, today)

        envelopes: list[RelayEnvelope] = []
        if state_after.due is not None:
            envelopes.extend(
                date_chain_envelopes(
                    workspace_id=workspace_id,
                    actor_id=actor_id,
                    device_id=device_id,
                    iso_day=state_after.due,
                    store_reads=lambda node_id: store.node(workspace_id, node_id),
                    clock=self._clock,
                )
            )
        envelopes.extend(
            review_property_envelopes(
                workspace_id=workspace_id,
                actor_id=actor_id,
                device_id=device_id,
                card_id=card_id,
                state_after=state_after,
                algorithm=algorithm,
                props=store.get_effective_properties(card_id),
                clock=self._clock,
            )
        )
        envelopes.append(
            card_review_envelope(
                workspace_id=workspace_id,
                actor_id=actor_id,
                device_id=device_id,
                card_id=card_id,
                grade=grade,
                algorithm=algorithm,
                state_before=state_before,
                state_after=state_after,
                session_id=session_id,
                reviewed_at=datetime.now(UTC).isoformat(),
                clock=self._clock,
            )
        )
        for envelope in envelopes:
            store.enqueue(envelope)
            try:
                store.apply_remote(envelope)
            except (CycleError, MoveGuardError) as exc:
                self.show_toast(str(exc))
                return
        engine = self._engine
        if engine is not None:
            run_in_worker(engine.sync, on_done=lambda _result: None, on_error=self._on_sync_error)

    def _on_workspaces_loaded(self, workspaces: list[Any]) -> None:
        self._workspace_names = {ws.id: str(ws.name or ws.id) for ws in workspaces}
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
        old_engine = self._engine
        if old_engine is not None:
            old_engine.stop_realtime()
        actor_id = config_store.load_actor_id() or config_store.GTK_ACTOR_ID
        engine = SyncEngine(client, store, actor_id=actor_id, workspace_id=workspace_id, clock=self._clock)
        engine.start_realtime()
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
        # The editor already applied the envelope to the local mirror before
        # this callback, so the toggle-off refresh below renders fresh content;
        # the sync round only needs to push the outbox and pick up the echo.
        self.show_toast("Saved — syncing…")
        self._edit_toggle.set_active(False)
        engine = self._engine
        if engine is not None:
            run_in_worker(
                engine.sync, on_done=lambda _result: self._after_save_sync(node_id), on_error=self._on_sync_error
            )
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
        self._refresh_hub()

    def _show_node(self, node_id: str) -> None:
        store = self._require_store()
        if store is None or self._workspace_id is None:
            return
        row = store.node(self._workspace_id, node_id)
        if row is None:
            return
        self._sidebar.unselect_cards()
        self._content_stack.set_visible_child_name("view")
        title = node_display_name(row)
        self._page_view.show_page(
            title,
            ast_to_view(row.content, resolve_name=self._resolve_name),
            aliases=self._alias_entries(row),
            aliased_main=self._aliased_main_entry(row),
            repoint_candidates=self._repoint_candidates(node_id) if row.aliased_node_id is not None else (),
            tags_row=self._tags_row(row),
            on_open_node=self._open_node,
            on_repoint=partial(self._on_alias_repoint, node_id),
        )
        self._editor.open_node(node_id, ast_to_plaintext(row.content))

    def _tags_row(self, row: NodeRow) -> Gtk.Widget:
        """The window-built Tags row for ``row``: pill records alphabetical
        by tag title with effective colors, candidate pages for the add
        picker (label-sorted), both from the pure
        :mod:`notees_gtk.ui.tag_ops` filters over the live rows."""
        store = self._require_store()
        assert store is not None and self._workspace_id is not None
        rows = store.nodes(self._workspace_id)
        by_id = {node_row.id: node_row for node_row in rows}
        extends = store.class_extends_edges()
        candidates = [
            (candidate.id, node_display_name(candidate))
            for candidate in tag_assign_candidates(rows, row.id, by_id, extends)
        ]
        candidates.sort(key=lambda entry: (entry[1].lower(), entry[0]))
        return build_tags_row(
            pills=tag_pills(row.tag_ids, by_id, extends, node_display_name),
            candidates=candidates,
            colors=content_colors(dark=Adw.StyleManager.get_default().get_dark()),
            on_open_tag=self._open_node,
            on_assign=partial(self._on_tag_assign, row.id),
            on_unassign=partial(self._on_tag_unassign, row.id),
        )

    def _alias_entries(self, row: NodeRow) -> list[tuple[str, str]]:
        """(id, label) for every live node whose alias-terminal is this one
        (the store's reverse read, chains included)."""
        store = self._require_store()
        if store is None or self._workspace_id is None:
            return []
        entries: list[tuple[str, str]] = []
        for alias_id in store.alias_nodes_of(self._workspace_id, row.id):
            alias_row = store.node(self._workspace_id, alias_id)
            if alias_row is not None:
                entries.append((alias_id, node_display_name(alias_row)))
        return entries

    def _aliased_main_entry(self, row: NodeRow) -> tuple[str, str] | None:
        """(id, label) of the alias's main, or ``None`` for an ordinary node."""
        if row.aliased_node_id is None:
            return None
        store = self._require_store()
        if store is None or self._workspace_id is None:
            return None
        main_row = store.node(self._workspace_id, row.aliased_node_id)
        label = node_display_name(main_row) if main_row is not None else row.aliased_node_id
        return (row.aliased_node_id, label)

    def _repoint_candidates(self, node_id: str) -> list[tuple[str, str]]:
        """The Change picker's (id, label) rows: every live node that may
        receive this alias's field (the pure filter), label-sorted."""
        store = self._require_store()
        if store is None or self._workspace_id is None:
            return []
        entries = [(r.id, node_display_name(r)) for r in alias_repoint_candidates(store.nodes(self._workspace_id), node_id)]
        entries.sort(key=lambda entry: (entry[1].lower(), entry[0]))
        return entries

    def _open_node(self, node_id: str) -> None:
        """Navigate to ``node_id`` — the alias chrome's Open/Navigate seam.

        Selects the node in the sidebar (expanding ancestors as needed; the
        selection signal drives the show) and shows it here too, which covers
        the already-selected case where no selection signal fires.
        """
        self._sidebar.select_node(node_id)
        self._show_node(node_id)

    def _on_alias_repoint(self, node_id: str, target_id: str | None) -> None:
        """Write the alias's OWN ``aliasedNodeId`` (re-point or present-null
        clear) through ``object.update``, then push the outbox."""
        store = self._require_store()
        if store is None or self._workspace_id is None:
            return
        envelope = alias_update_envelope(
            workspace_id=self._workspace_id,
            actor_id=config_store.load_actor_id() or config_store.GTK_ACTOR_ID,
            device_id=config_store.ensure_device_id(),
            node_id=node_id,
            target_id=target_id,
            clock=self._clock,
        )
        try:
            store.enqueue(envelope)
            # Optimistic mirror apply, the editor-save precedent: the
            # toggle-free view refresh after the sync already renders the new
            # target; op-id dedupe makes the server echo harmless.
            store.apply_remote(envelope)
        except CycleError as exc:
            self.show_toast(str(exc))
            return
        self.show_toast("Alias updated — syncing…")
        engine = self._engine
        if engine is not None:
            run_in_worker(
                engine.sync, on_done=lambda _result: self._after_alias_write(node_id), on_error=self._on_sync_error
            )
        else:
            self._show_node(node_id)

    def _after_alias_write(self, node_id: str) -> None:
        self._refresh_sidebar()
        self._show_node(node_id)

    # ------------------------------------------------------------------ tags

    def _on_tag_assign(self, node_id: str, tag_id: str) -> None:
        """Assign a page as a tag: the OR-Set add carrier (a re-issued
        ``object.create`` over the node's own id), built from the row the
        pure helper reads."""
        store = self._require_store()
        if store is None or self._workspace_id is None:
            return
        row = store.node(self._workspace_id, node_id)
        if row is None:
            return
        envelope = tag_assign_envelope(
            workspace_id=self._workspace_id,
            actor_id=config_store.load_actor_id() or config_store.GTK_ACTOR_ID,
            device_id=config_store.ensure_device_id(),
            node=row,
            tag_id=tag_id,
            clock=self._clock,
        )
        self._enqueue_tag_write(node_id, envelope, "Tag assigned")

    def _on_tag_unassign(self, node_id: str, tag_id: str) -> None:
        """Remove a tag: the ``tag.unassign`` OR-Set tombstone."""
        store = self._require_store()
        if store is None or self._workspace_id is None:
            return
        envelope = tag_unassign_envelope(
            workspace_id=self._workspace_id,
            actor_id=config_store.load_actor_id() or config_store.GTK_ACTOR_ID,
            device_id=config_store.ensure_device_id(),
            node_id=node_id,
            tag_id=tag_id,
            clock=self._clock,
        )
        self._enqueue_tag_write(node_id, envelope, "Tag removed")

    def _enqueue_tag_write(self, node_id: str, envelope: RelayEnvelope, message: str) -> None:
        """The tag-write outbox seam — exactly the alias write's flow:
        enqueue, optimistically apply to the local mirror (the toggle-free
        refresh after the sync already renders the new membership; op-id
        dedupe makes the server echo harmless), then push the outbox on a
        worker thread. The applier's structural guards (the date-chain lock
        a tag carrier could trip on a fresh replica) fail loud to a toast.
        """
        store = self._require_store()
        if store is None:
            return
        store.enqueue(envelope)
        try:
            store.apply_remote(envelope)
        except (CycleError, MoveGuardError) as exc:
            self.show_toast(str(exc))
            return
        self.show_toast(f"{message} — syncing…")
        engine = self._engine
        if engine is not None:
            run_in_worker(
                engine.sync, on_done=lambda _result: self._after_alias_write(node_id), on_error=self._on_sync_error
            )
        else:
            self._show_node(node_id)

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
