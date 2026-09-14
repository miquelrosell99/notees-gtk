"""Plain-text editor for node content (the honest MVP round-trip).

The read-only view renders the rich AST; editing goes through a plain-text
``Gtk.TextView`` seeded with :func:`ast_to_plaintext` — the same non-CRDT
form the Flutter client uses. Saving rebuilds the paragraph AST, enqueues a
``node.updateContent`` envelope, and hands off to the window, which runs the
sync engine on a worker thread.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk

from notees_gtk.core.protocol.clock import Clock
from notees_gtk.core.protocol.models import new_envelope
from notees_gtk.data.store import LocalStore
from notees_gtk.ui.ast_render import paragraphs_from_plaintext

__all__ = ["EditorView"]


class EditorView(Gtk.Box):
    """Text view + save action producing ``node.updateContent`` envelopes."""

    def __init__(
        self,
        store: LocalStore,
        actor_id: str,
        clock: Clock,
        on_saved: Callable[[str], None],
    ) -> None:
        """Wire the editor to ``store``; ``on_saved`` fires with the node id
        after the envelope is enqueued (the window then triggers a sync)."""
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._store = store
        self._actor_id = actor_id
        self._clock = clock
        self._on_saved = on_saved
        self._workspace_id = ""
        self._node_id: str | None = None

        header = Gtk.Box(spacing=6, margin_start=6, margin_end=6, margin_top=6)
        self.append(header)
        hint = Gtk.Label(label="Plain-text editing — rich formatting is preserved in the read-only view only.", xalign=0, hexpand=True)
        hint.add_css_class("dim-label")
        header.append(hint)
        save_button = Gtk.Button(label="Save", halign=Gtk.Align.END)
        save_button.add_css_class("suggested-action")
        save_button.connect("clicked", self._on_save_clicked)
        header.append(save_button)

        scrolled = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.append(scrolled)
        self._text_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD, top_margin=12, bottom_margin=12, left_margin=12, right_margin=12)
        scrolled.set_child(self._text_view)

    # ------------------------------------------------------------------ public

    def set_workspace(self, workspace_id: str) -> None:
        """Bind saves to ``workspace_id`` (called on workspace switches)."""
        self._workspace_id = workspace_id

    def open_node(self, node_id: str, plaintext: str) -> None:
        """Load ``plaintext`` into the buffer for ``node_id``."""
        self._node_id = node_id
        self._text_view.get_buffer().set_text(plaintext, -1)

    def current_node(self) -> str | None:
        """Return the node currently loaded in the buffer."""
        return self._node_id

    # ----------------------------------------------------------------- private

    def _on_save_clicked(self, *_args: object) -> None:
        if self._node_id is None or not self._workspace_id:
            return
        buffer = self._text_view.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        ast = paragraphs_from_plaintext(text)
        envelope = new_envelope(
            workspace_id=self._workspace_id,
            actor_id=self._actor_id,
            op_type="node.updateContent",
            payload={"nodeId": self._node_id, "content": json.dumps(ast)},
            clock=self._clock,
        )
        self._store.enqueue(envelope)
        self._on_saved(self._node_id)
