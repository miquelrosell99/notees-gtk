"""The Memorization-cards hub surface: bucket strip, algorithm selector,
and the plain member list.

The thin GTK chrome over the pure reads in :mod:`notees_gtk.core.cards` —
the window computes the buckets and feeds this widget; nothing here touches
the store. Mirrors the web Cards hub (``apps/web/src/features/classview/
CardBuckets.tsx`` — the Due/Fresh/Later strip — and the hub's algorithm
selector over the ``/api/me/prefs`` seam, owner ruling 2).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Pango

from notees_gtk.core.api import CardsAlgorithm
from notees_gtk.core.cards import CardBuckets

__all__ = ["CardsHubView"]

#: The selector entries in display order (index → algorithm), FSRS first as
#: the platform default.
_ALGORITHM_ENTRIES: tuple[tuple[str, CardsAlgorithm], ...] = (
    ("FSRS", "fsrs"),
    ("SM-2", "sm2"),
)


class CardsHubView(Gtk.Box):
    """The hub's chrome: header + selector, the buckets row, the member list."""

    def __init__(
        self,
        *,
        on_review: Callable[[list[str]], None],
        on_algorithm_selected: Callable[[CardsAlgorithm], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=18, margin_bottom=18, margin_start=24, margin_end=24)
        self._on_review = on_review
        self._on_algorithm_selected = on_algorithm_selected
        self._updating_algorithm = False
        self._review_ids: list[str] = []

        header = Gtk.Box(spacing=12)
        title = Gtk.Label(label="Memorization cards", xalign=0, hexpand=True)
        title.add_css_class("title-2")
        header.append(title)
        self._algorithm_dropdown = Gtk.DropDown()
        self._algorithm_dropdown.connect("notify::selected", self._on_algorithm_changed)
        header.append(Gtk.Label(label="Scheduler:", xalign=1))
        header.append(self._algorithm_dropdown)
        self.append(header)
        self.append(Gtk.Separator())

        buckets_row = Gtk.Box(spacing=12)
        self._review_button = Gtk.Button()
        self._review_button.connect("clicked", self._on_review_clicked)
        buckets_row.append(self._review_button)
        self._fresh_label = Gtk.Label(xalign=0)
        self._fresh_label.add_css_class("dim-label")
        buckets_row.append(self._fresh_label)
        self._later_label = Gtk.Label(xalign=0)
        self._later_label.add_css_class("dim-label")
        buckets_row.append(self._later_label)
        self.append(buckets_row)

        self._empty_label = Gtk.Label(label="No memorization cards in this workspace", xalign=0.5, vexpand=True)
        self._empty_label.add_css_class("dim-label")
        self.append(self._empty_label)

        scrolled = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self._members = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        scrolled.set_child(self._members)
        self.append(scrolled)

    # ------------------------------------------------------------------ public

    def set_algorithm(self, algorithm: CardsAlgorithm) -> None:
        """Reflect an externally resolved algorithm (the login-time prefs
        fetch landing after the hub was built) without re-emitting."""
        self._updating_algorithm = True
        self._algorithm_dropdown.set_selected(self._algorithm_index(algorithm))
        self._updating_algorithm = False

    def show_hub(self, *, buckets: CardBuckets, names: Mapping[str, str], algorithm: CardsAlgorithm) -> None:
        """Render one hub refresh: the bucket strip, the member list (due,
        then fresh, then later — each row carrying its due-day label), and
        the selector state."""
        self.set_algorithm(algorithm)
        self._review_ids = [row.id for row in (*buckets.due, *buckets.fresh)]
        review_label = f"Review {len(buckets.due)} due"
        if buckets.fresh:
            review_label += f" · {len(buckets.fresh)} new"
        self._review_button.set_label(review_label)
        self._review_button.set_sensitive(bool(self._review_ids))
        self._fresh_label.set_label(f"{len(buckets.fresh)} fresh")
        self._later_label.set_label(f"{len(buckets.later)} later")

        while child := self._members.get_first_child():
            self._members.remove(child)
        total = len(buckets.due) + len(buckets.fresh) + len(buckets.later)
        self._empty_label.set_visible(total == 0)
        for bucket in (buckets.due, buckets.fresh, buckets.later):
            for row in bucket:
                self._members.append(self._member_row(names.get(row.id, row.id), row.due))

    # ----------------------------------------------------------------- private

    def _algorithm_index(self, algorithm: CardsAlgorithm) -> int:
        for index, (_label, value) in enumerate(_ALGORITHM_ENTRIES):
            if value == algorithm:
                return index
        return 0

    def _on_algorithm_changed(self, *_args: object) -> None:
        if self._updating_algorithm:
            return
        index = int(self._algorithm_dropdown.get_selected())
        if 0 <= index < len(_ALGORITHM_ENTRIES):
            self._on_algorithm_selected(_ALGORITHM_ENTRIES[index][1])

    def _on_review_clicked(self, *_args: object) -> None:
        if self._review_ids:
            self._on_review(list(self._review_ids))

    def _member_row(self, name: str, due: str | None) -> Gtk.ListBoxRow:
        list_row = Gtk.ListBoxRow()
        box = Gtk.Box(spacing=12, margin_start=6, margin_end=6, margin_top=3, margin_bottom=3)
        list_row.set_child(box)
        name_label = Gtk.Label(label=name, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        box.append(name_label)
        due_label = Gtk.Label(label=due if due is not None else "—", xalign=1)
        due_label.add_css_class("dim-label")
        box.append(due_label)
        return list_row
