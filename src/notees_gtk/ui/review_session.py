"""The review-session dialog: front → reveal → back, the four-grade bar.

A modal :class:`Adw.Dialog` over the Cards hub (owner ruling 5). The
behavioral port of the web ``apps/web/src/features/cards/ReviewSession.tsx``
— the front is the card node's own content read-only through
:func:`notees_gtk.ui.ast_render.ast_to_view` + :class:`PageViewWidget` (no
alias/tags chrome); the reveal adds the card's embedded children (the GTK
condensed-prose equivalent: their view items merged under the front — the
web's ProseView ``condensed`` mode); a front-only card shows the kit empty
state ("No answer on this card") and still grades (Review Focus 1). The
grade keys 1–4 are active ONLY while the back is up (the web's
editor-interaction rule); Enter/Space flips the reveal from the front (a
GTK convenience). The session's position and counters live in the pure
:class:`notees_gtk.ui.card_ops.ReviewSessionState`; the grade's writes
(enqueue + sync) are the caller's callback — owner ruling 3, one sync per
grade; owner ruling 7, no undo journal GTK-side.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk

from notees_gtk.core.srs import CardGrade, CardSrsState
from notees_gtk.ui.ast_render import PageView
from notees_gtk.ui.card_ops import ReviewSessionState
from notees_gtk.ui.page_view import PageViewWidget

__all__ = ["LoadedCard", "ReviewSessionDialog"]

#: The four grades in key order (1–4 on the keyboard) — ReviewSession.tsx
#: ``GRADES`` verbatim.
_GRADES: tuple[tuple[CardGrade, str], ...] = (
    ("again", "Again"),
    ("hard", "Hard"),
    ("good", "Good"),
    ("easy", "Easy"),
)

#: Modifier chords that must NEVER grade (the web's meta/ctrl/alt ignore) —
#: bare 1–4 only.
_GRADE_MODIFIER_MASK = (
    Gdk.ModifierType.CONTROL_MASK
    | Gdk.ModifierType.ALT_MASK
    | Gdk.ModifierType.SUPER_MASK
    | Gdk.ModifierType.HYPER_MASK
    | Gdk.ModifierType.META_MASK
)


@dataclass(frozen=True)
class LoadedCard:
    """One loaded card: the title, the front view, the embedded children's
    views (the back), and the SRS state before grading."""

    title: str
    front: PageView
    back_children: tuple[PageView, ...]
    state: CardSrsState


class ReviewSessionDialog(Adw.Dialog):
    """The modal study session over the Cards hub."""

    def __init__(
        self,
        *,
        queue: list[str],
        load_card: Callable[[str], LoadedCard | None],
        on_grade: Callable[[str, CardGrade, CardSrsState], None],
        on_close: Callable[[], None],
    ) -> None:
        super().__init__(title="Review session", content_width=680, content_height=600)
        self._state = ReviewSessionState(queue=tuple(queue))
        self._load_card = load_card
        self._on_grade = on_grade
        self._on_close = on_close
        self._current_state: CardSrsState | None = None
        self._close_notified = False

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self._position = Gtk.Label()
        header.set_title_widget(self._position)
        toolbar.add_top_bar(header)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._page = PageViewWidget()
        root.append(self._page)
        self._actions = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER, margin_top=6, margin_bottom=12)
        root.append(self._actions)
        toolbar.set_content(root)
        self.set_content(toolbar)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

        # The hub refresh must fire however the dialog goes away — the Close
        # button, Esc, or the window manager — so the callback lives on the
        # `closed` signal; the flag keeps a double close request idempotent.
        self.connect("closed", self._on_dialog_closed)

        self._render_current()

    # ----------------------------------------------------------------- rendering

    def _render_current(self) -> None:
        """Render the current card (front or back) or the end summary."""
        while child := self._actions.get_first_child():
            self._actions.remove(child)
        if self._state.finished:
            self._render_summary()
            return

        card_id = self._state.current_card()
        assert card_id is not None
        card = self._load_card(card_id)
        if card is None:
            # The card vanished from the mirror mid-session: skip it, never
            # grade a ghost.
            self._state.skip()
            self._render_current()
            return
        self._current_state = card.state

        self._position.set_label(f"{self._state.index + 1} / {len(self._state.queue)}")
        if not self._state.revealed:
            self._page.show_page(card.title, card.front)
            reveal = Gtk.Button(label="Reveal answer")
            reveal.add_css_class("suggested-action")
            reveal.connect("clicked", self._on_reveal_clicked)
            self._actions.append(reveal)
        else:
            if card.back_children:
                # The condensed back: the front's items then every embedded
                # child's items under one reading surface.
                items = (*card.front.items, *(item for child in card.back_children for item in child.items))
                self._page.show_page(card.title, PageView(items=items))
            else:
                # Review Focus 1: a front-only card still grades.
                self._page.show_page(card.title, card.front, empty_state="No answer on this card")
            for index, (grade, label) in enumerate(_GRADES):
                button = Gtk.Button(label=f"{label}  ({index + 1})")
                if grade == "again":
                    button.add_css_class("destructive-action")
                button.connect("clicked", self._on_grade_clicked, grade)
                self._actions.append(button)

    def _render_summary(self) -> None:
        """The session-end screen: the reviewed count and the lapses (the
        web's ``reviewed``/``lapses`` counters) + Close."""
        self._position.set_label("")
        reviewed = self._state.reviewed
        lines = [f"{reviewed} card{'s' if reviewed != 1 else ''} reviewed"]
        if self._state.lapses > 0:
            lines.append(f"{self._state.lapses} lapse{'s' if self._state.lapses != 1 else ''}")
        self._page.show_page("\n".join(lines), PageView())
        close = Gtk.Button(label="Close")
        close.add_css_class("suggested-action")
        close.connect("clicked", self._on_close_clicked)
        self._actions.append(close)

    # ------------------------------------------------------------------ actions

    def _on_reveal_clicked(self, *_args: object) -> None:
        self._state.reveal()
        self._render_current()

    def _on_grade_clicked(self, _button: object, grade: CardGrade) -> None:
        self._grade(grade)

    def _grade(self, grade: CardGrade) -> None:
        card_id = self._state.current_card()
        state_before = self._current_state
        if card_id is None or state_before is None:
            return
        # The writes are the caller's seam (envelopes + enqueue + one sync
        # per grade); the session advances on the pure state regardless —
        # a failed guard toasts, the card still counts as seen.
        self._on_grade(card_id, grade, state_before)
        self._state.grade(grade, state_before)
        self._render_current()

    def _on_close_clicked(self, *_args: object) -> None:
        self.close()

    def _on_dialog_closed(self, *_args: object) -> None:
        """The single on_close funnel (Close button, Esc, WM close) — the
        flag keeps a double close request idempotent; the callback (the hub
        refresh) is safe to run twice anyway."""
        if self._close_notified:
            return
        self._close_notified = True
        self._on_close()

    def _on_key_pressed(self, _controller: object, keyval: int, _keycode: int, state: Gdk.ModifierType) -> bool:
        """The key flow: Enter/Space flips the front (modifier-agnostic, the
        GTK submit-chord convention); 1–4 grade the back — active only while
        revealed (the web's editor-interaction rule) AND bare, no modifier
        chord (Ctrl+1 must not grade, the web's meta/ctrl/alt ignore)."""
        if self._state.finished:
            return False
        if not self._state.revealed:
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
                self._state.reveal()
                self._render_current()
                return True
            return False
        if state & _GRADE_MODIFIER_MASK:
            return False
        for index, (grade, _label) in enumerate(_GRADES):
            if keyval == getattr(Gdk, f"KEY_{index + 1}"):
                self._grade(grade)
                return True
        return False
