"""The structured query-condition builder — the ``FilterBlockBuilder.tsx``
chrome ported to a GTK dialog, deliberately adapted (not pixel-ported): one
modal :class:`Adw.Dialog` a query-token section opens ("Edit query"), the
web's toolbar/panel chrome compressed into the dialog's head/foot rows.

Every behavior decision rides the headless register in
:mod:`notees_gtk.core.query.builder` (the family/operator/value columns,
the mode switch, the boolean single-selector ruling, the not-wrapper, the
prune) — this module only turns model records into widgets and model
morphs into signal handlers. Chrome adaptations, each a deliberate GTK
idiom rather than a web mirror:

- the row is ``[Exclude] [Type] [Operator] [Value] [Mode] [×]`` in one
  line (the web's columns compress at dialog width; the property family
  rides the type slot exactly like the web's PropertySchemaPick);
- structural edits (add/remove/operator/mode/exclude/schema pick/group
  logic) rebuild the editor body from the draft — the draft is the single
  source of truth; plain-text value edits write the draft silently (no
  rebuild) so typing never loses focus (the web is per-keystroke
  controlled; GTK commits ride the draft directly);
- reorder chevrons are scoped out (delete + re-add covers ordering for a
  first GTK builder — the model is order-faithful either way);
- the Raw-AST exchange mode is not ported (web-optional; the structured
  builder + the aggregation JSON hatch are the GTK surface);
- AGGREGATION AUTHORING is the documented slice-4 escape hatch: an
  "Aggregation (JSON)" expander pre-filled from the token's AST, strict-
  validated through the wire parser on Apply — the grouped grid itself
  already renders through the slice-3 aggregate arm.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any, Literal, cast

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from notees_gtk.core.protocol.query_ast import (
    Aggregation,
    Child,
    Condition,
    ContentCondition,
    DateCondition,
    DatePeriod,
    EntireWorkspaceScope,
    Group,
    IdsTarget,
    MemberCondition,
    MemberFamily,
    NameCondition,
    Not,
    PropertyCondition,
    QueryAst,
    QueryTarget,
    Scope,
    SelfTarget,
    dump_query_ast,
    parse_query_ast,
)
from notees_gtk.core.query.builder import (
    FAMILY_LABELS,
    AddMenuEntry,
    BuilderFacts,
    TargetMode,
    apply_boolean_value,
    apply_operator,
    apply_property_schema_pick,
    apply_row_value,
    apply_target_mode,
    block_view,
    create_condition,
    draft_or_factory_ast,
    family_of,
    filter_kind_options_for_config,
    operator_of,
    operator_options,
)
from notees_gtk.ui.node_picker import NodeEntry, pop_node_picker

__all__ = ["QueryBuilderDialog"]

#: The pick-candidates seam: family → sorted (id, label) rows for the
#: Manual target-mode picker (the chrome's store read, label-sorted).
type PickCandidates = Callable[[MemberFamily], Sequence[NodeEntry]]

#: The dialog's apply seam — the committed wire AST (pruned, dump-shaped).
type ApplyCallback = Callable[[dict[str, Any]], None]

#: The value-axis boolean labels (the single-selector ruling's two values).
_TYPE_VALUES: tuple[tuple[str, str], ...] = (("true", "A class"), ("false", "Not a class"))
_PLACEMENT_VALUES: tuple[tuple[str, str], ...] = (("false", "Page"), ("true", "Block"))

#: The relative-period register (the date/property within arms).
_PERIODS: tuple[tuple[str, str], ...] = (
    ("today", "Today"),
    ("this_week", "This week"),
    ("this_month", "This month"),
    ("this_year", "This year"),
)

#: Group logic labels (the Match ALL/ANY toggle).
_LOGIC_VALUES: tuple[tuple[str, str], ...] = (("and", "all of"), ("or", "any of"))


def _group_writer(child_change: Callable[[Child], None]) -> Callable[[Group], None]:
    """The nested group's write closure: a group morphs in place through
    the parent's replace verb (typed for mypy — the lambda form cannot
    annotate its parameter)."""

    def write(group: Group) -> None:
        child_change(group)

    return write


def _try_parse(raw: object) -> QueryAst | None:
    """Loose parse for the edit path: an invalid/absent token AST starts
    from the factory draft instead of failing (the web's unspecified arm)."""
    if raw is None:
        return None
    try:
        return parse_query_ast(raw)
    except ValueError:
        return None


def _dump_aggregation(aggregation: Aggregation) -> dict[str, Any]:
    """The aggregation's wire shape (the dump serializer's private arm,
    re-derived here so the hatch round-trips what the token carries)."""
    return cast(
        dict[str, Any],
        dump_query_ast(QueryAst(scope=EntireWorkspaceScope(), root=Group(logic="and", children=()), aggregation=aggregation))[
            "aggregation"
        ],
    )


def _add_menu_child(entry: AddMenuEntry) -> Child:
    """The fresh draft child one add-menu entry appends (``createCondition``
    for the families; the group/not constructors for the last three)."""
    if entry == "group-and":
        return Group(logic="and", children=())
    if entry == "group-or":
        return Group(logic="or", children=())
    if entry == "not":
        return Not(child=Group(logic="and", children=()))
    return create_condition(entry)


def _dropdown(
    options: Sequence[tuple[str, str]],
    current: str,
    on_change: Callable[[str], None],
    *,
    tooltip: str | None = None,
) -> Gtk.DropDown:
    """A string dropdown over (value, label) pairs; ``on_change`` fires only
    on user selection (the guard skips the render-time ``set_selected``)."""
    drop = Gtk.DropDown.new_from_strings([label for _, label in options])
    if tooltip is not None:
        drop.set_tooltip_text(tooltip)
    for index, (value, _) in enumerate(options):
        if value == current:
            drop.set_selected(index)
            break

    def on_selected(widget: Gtk.DropDown, _param: object) -> None:
        selected = widget.get_selected()
        if 0 <= selected < len(options):
            value = options[selected][0]
            if value != current:
                on_change(value)

    drop.connect("notify::selected", on_selected)
    return drop


def _entry(text: str, on_commit: Callable[[str], None], *, placeholder: str | None = None) -> Gtk.Entry:
    """A text entry writing the draft directly on every change (no rebuild —
    typing never loses focus; structural widgets rebuild around it)."""
    entry = Gtk.Entry(text=text)
    if placeholder is not None:
        entry.set_placeholder_text(placeholder)
    entry.connect("changed", lambda widget: on_commit(widget.get_text()))
    return entry


class QueryBuilderDialog(Adw.Dialog):
    """The modal condition builder over one query token's AST.

    Args:
        raw_ast: The token's own ``queryAst`` (any JSON value — invalid or
            absent starts from the factory draft, exactly the web's
            unspecified-query arm).
        facts: The bound-property facts (the operator/value register).
        self_node_id: The hosting page id — gates the Self target mode
            (the web rule: the option shows when a host exists, or when the
            row already rides it).
        pick_candidates: The Manual-mode picker source, per member family.
        on_apply: The commit callback — receives the dumped wire AST.
    """

    def __init__(
        self,
        *,
        raw_ast: object,
        facts: BuilderFacts,
        self_node_id: str | None,
        pick_candidates: PickCandidates,
        on_apply: ApplyCallback,
    ) -> None:
        super().__init__(title="Edit query", content_width=760, content_height=600)
        self._facts = facts
        self._self_node_id = self_node_id
        self._pick_candidates = pick_candidates
        self._on_apply = on_apply

        parsed = _try_parse(raw_ast)
        self._scope: Scope
        if parsed is None:
            self._draft = Group(logic="and", children=())
            self._scope = EntireWorkspaceScope()
            aggregation_prefill = ""
        else:
            self._draft = parsed.root
            self._scope = parsed.scope
            aggregation_prefill = (
                json.dumps(_dump_aggregation(parsed.aggregation), ensure_ascii=False, indent=1)
                if parsed.aggregation is not None
                else ""
            )

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        apply_button = Gtk.Button(label="Apply")
        apply_button.add_css_class("suggested-action")
        apply_button.connect("clicked", self._on_apply_clicked)
        header.pack_end(apply_button)
        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.connect("clicked", lambda _btn: self.close())
        header.pack_start(cancel_button)
        toolbar.add_top_bar(header)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scrolled = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self._body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        scrolled.set_child(self._body)
        root.append(scrolled)
        toolbar.set_content(root)
        self.set_content(toolbar)

        # The aggregation JSON hatch (the documented slice-4 escape hatch).
        self._aggregation_buffer = Gtk.TextBuffer()
        if aggregation_prefill:
            self._aggregation_buffer.set_text(aggregation_prefill, -1)
        self._error_label = Gtk.Label(xalign=0, wrap=True)
        self._error_label.add_css_class("error")

        self._render()

    # ------------------------------------------------------------------ rendering

    def _render(self) -> None:
        """Rebuild the editor body from the draft (structural edits only —
        text value writes touch the draft without a rebuild)."""
        while child := self._body.get_first_child():
            self._body.remove(child)
        self._body.append(self._group_box(self._draft, self._set_root_draft, depth=0, is_root=True))
        self._body.append(self._aggregation_box())
        self._error_label.set_visible(False)
        self._body.append(self._error_label)

    def _set_root_draft(self, group: Group) -> None:
        self._draft = group
        self._render()

    # --------------------------------------------------------------------- groups

    def _group_box(
        self,
        group: Group,
        on_change: Callable[[Group], None],
        *,
        depth: int,
        is_root: bool = False,
        on_remove: Callable[[], None] | None = None,
    ) -> Gtk.Widget:
        """One group's editor: the ALL/ANY head, the child rows, the add
        menu (nested groups indent — the web's nested-group card is a GTK
        margin). ``on_remove`` is the parent's splice verb — absent on the
        root, where removal is not a choice."""
        frame = Gtk.Frame()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_bottom=6)
        if depth > 0:
            box.set_margin_start(12)
        frame.set_child(box)

        head = Gtk.Box(spacing=6, margin_start=6, margin_end=6)
        head.append(Gtk.Label(label="Match" if is_root else "Group"))
        head.append(
            _dropdown(
                _LOGIC_VALUES,
                group.logic,
                lambda logic: on_change(replace(group, logic=cast(Literal["and", "or"], logic))),
                tooltip="Group logic",
            )
        )
        if on_remove is not None:
            remove = Gtk.Button(label="×")
            remove.set_tooltip_text("Remove group (and its conditions)")
            remove.connect("clicked", lambda _btn: on_remove())
            head.append(remove)
        box.append(head)

        for index, child in enumerate(group.children):
            child_change, child_remove = self._child_verbs(group, index, on_change)
            if isinstance(child, Group):
                box.append(
                    self._group_box(
                        child,
                        _group_writer(child_change),
                        depth=depth + 1,
                        on_remove=child_remove,
                    )
                )
            elif isinstance(child, Not) and isinstance(child.child, Group):
                box.append(
                    self._excluded_group_box(
                        child.child,
                        child_change,
                        depth=depth + 1,
                        on_remove=child_remove,
                    )
                )
            else:
                box.append(self._row_box(child, child_change, child_remove, depth=depth))
        box.append(self._add_menu_box(group, on_change))
        return frame

    def _child_verbs(
        self, group: Group, index: int, on_change: Callable[[Group], None]
    ) -> tuple[Callable[[Child], None], Callable[[], None]]:
        """The two verbs one child edits through: replace (rows/groups morph
        in place) and remove (the × button splices the child out)."""

        def change(child: Child) -> None:
            on_change(replace(group, children=group.children[:index] + (child,) + group.children[index + 1 :]))

        def remove() -> None:
            on_change(replace(group, children=group.children[:index] + group.children[index + 1 :]))

        return change, remove

    def _excluded_group_box(
        self,
        group: Group,
        child_change: Callable[[Child], None],
        *,
        depth: int,
        on_remove: Callable[[], None],
    ) -> Gtk.Widget:
        """A NOT-around-a-GROUP child: the group editor with an Exclude head
        (unchecking unwraps to the bare group — the not-wrapper is sugar)."""
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        toggle = Gtk.CheckButton(label="Exclude group (NOT)", active=True)
        toggle.connect("toggled", lambda btn: child_change(Not(child=group) if btn.get_active() else group))
        outer.append(toggle)
        outer.append(
            self._group_box(
                group,
                lambda g: child_change(Not(child=g)),
                depth=depth,
                is_root=False,
                on_remove=on_remove,
            )
        )
        return outer

    def _add_menu_box(self, group: Group, on_change: Callable[[Group], None]) -> Gtk.Widget:
        """The add-menu register (the v2 families + the group constructors),
        a popover list like the repo's pickers."""
        button = Gtk.MenuButton(label="Add condition…")
        button.set_halign(Gtk.Align.START)
        popover = Gtk.Popover()
        list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        entries: list[tuple[AddMenuEntry, str, str]] = [
            (option.value, option.label, option.description) for option in filter_kind_options_for_config()
        ]
        for _, label, description in entries:
            row = Gtk.ListBoxRow()
            row_label = Gtk.Label(label=label, xalign=0, margin_start=6, margin_end=6, margin_top=3, margin_bottom=3)
            row_label.set_tooltip_text(description)
            row.set_child(row_label)
            list_box.append(row)

        def on_activated(_list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
            popover.popdown()
            entry_value, _, _ = entries[row.get_index()]
            on_change(replace(group, children=group.children + (_add_menu_child(entry_value),)))

        list_box.connect("row-activated", on_activated)
        popover.set_child(list_box)
        button.set_popover(popover)
        return button

    # ------------------------------------------------------------------------ rows

    def _row_box(
        self,
        child: Child,
        child_change: Callable[[Child], None],
        child_remove: Callable[[], None],
        *,
        depth: int,
    ) -> Gtk.Widget:
        """One condition row: ``[Exclude] [Type] [Operator] [Value] [Mode] [×]``."""
        view = block_view(child)
        assert view is not None  # groups arrive through _group_box, never here
        condition = view.condition
        family = family_of(condition)

        def write(next_condition: Condition) -> None:
            """The value write preserving the row's not-wrap polarity."""
            child_change(apply_row_value(child, next_condition))

        row = Gtk.Box(spacing=6, margin_start=6, margin_end=6)

        exclude = Gtk.CheckButton(label="Exclude", active=view.negated)
        exclude.set_tooltip_text("Negate this condition (NOT)")
        exclude.connect("toggled", lambda btn: child_change(Not(child=condition) if btn.get_active() else condition))
        row.append(exclude)

        # The TYPE column — the row's identity, readonly once added (the
        # property family is the exception: the schema pick rides this slot).
        if isinstance(condition, PropertyCondition):
            row.append(self._property_schema_pick(condition, write))
        else:
            label = Gtk.Label(label=FAMILY_LABELS[family], xalign=0)
            label.set_tooltip_text(FAMILY_LABELS[family])
            row.append(label)

        # The OPERATOR column — absent for the boolean single-selector rows
        # (the owner's ruling: an empty register renders no dropdown).
        options = operator_options(family, condition, self._facts)
        if options:
            current = operator_of(condition, view.negated)
            row.append(
                _dropdown(
                    [(option.value, option.label) for option in options],
                    current,
                    lambda op: child_change(apply_operator(condition, op)),
                    tooltip="Operator",
                )
            )

        # The VALUE column.
        value_widget = self._value_box(condition, write, depth=depth)
        if value_widget is not None:
            row.append(value_widget)

        # The MODE column — the member target families only (the set bits
        # read as Manual until the switch re-materializes an "in").
        if isinstance(condition, MemberCondition):
            row.append(self._mode_switch(condition, write))

        spacer = Gtk.Box(hexpand=True)
        row.append(spacer)
        delete = Gtk.Button(label="×")
        delete.set_tooltip_text("Remove condition")
        delete.connect("clicked", lambda _btn: child_remove())
        row.append(delete)
        return row

    def _property_schema_pick(self, condition: PropertyCondition, write: Callable[[Condition], None]) -> Gtk.Widget:
        """The property family's type slot: the schema dropdown (the
        PropertySchemaPick port — a re-pick normalizes the row back to the
        plain shape, seeding the truth value for booleans)."""
        properties = self._facts.bound_properties
        options: list[tuple[str, str]] = [("", "Pick a property")] + [(prop.id, prop.name) for prop in properties]

        def on_pick(schema_id: str) -> None:
            schema = next((prop for prop in properties if prop.id == schema_id), None)
            write(apply_property_schema_pick(schema_id, schema.type if schema is not None else None))

        return _dropdown(options, condition.schema_id, on_pick, tooltip="Property")

    # --------------------------------------------------------------- value column

    def _value_box(self, condition: Condition, write: Callable[[Condition], None], *, depth: int) -> Gtk.Widget | None:
        """The value column per family — None where no value control exists
        (the cover/banner/alias set bits ARE the condition)."""
        if isinstance(condition, MemberCondition):
            return self._member_value(condition, write, depth=depth)
        if isinstance(condition, (ContentCondition, NameCondition)):
            return _entry(condition.value or "", lambda text: write(replace(condition, value=text)), placeholder="Text")
        if isinstance(condition, PropertyCondition):
            return self._property_value(condition, write)
        if isinstance(condition, DateCondition):
            return self._date_value(condition, write)
        # nodeField: the boolean rows ride the single value selector; the
        # cover/banner/alias rows carry no value at all.
        if condition.field == "isClass":
            return _dropdown(_TYPE_VALUES, condition.value or "true", lambda v: write(replace(condition, value=v)))
        if condition.field == "isEmbedded":
            return _dropdown(_PLACEMENT_VALUES, condition.value or "false", lambda v: write(replace(condition, value=v)))
        return None

    def _member_value(self, condition: MemberCondition, write: Callable[[Condition], None], *, depth: int) -> Gtk.Widget:
        """The member family's value column: the Manual picker + picked
        chips, the nested group editor (Query mode), or the self label."""
        if condition.op in ("in", "not-in", "exact") and isinstance(condition.target, QueryTarget):
            return self._group_box(
                condition.target.root,
                lambda group: write(replace(condition, target=QueryTarget(root=group))),
                depth=depth + 1,
            )
        if condition.op in ("in", "not-in", "exact") and isinstance(condition.target, SelfTarget):
            label = Gtk.Label(label="the page hosting this query", xalign=0)
            label.add_css_class("dim-label")
            return label
        # Manual (an empty ids pick renders the same control).
        ids = condition.target.ids if isinstance(condition.target, IdsTarget) else ()
        candidates = list(self._pick_candidates(condition.family))
        label_for = dict(candidates)

        box = Gtk.Box(spacing=6)
        for picked in ids:
            chip = Gtk.Box(spacing=3)
            chip_label = Gtk.Label(label=label_for.get(picked, picked[:8]))
            chip.append(chip_label)
            remove = Gtk.Button(label="×")

            def on_remove(_btn: Gtk.Widget, remove_id: str = picked) -> None:
                next_ids = tuple(item for item in ids if item != remove_id)
                write(replace(condition, target=IdsTarget(ids=next_ids)))

            remove.connect("clicked", on_remove)
            chip.append(remove)
            box.append(chip)

        add = Gtk.MenuButton(label="Add…")
        add.set_tooltip_text(f"Pick {FAMILY_LABELS[cast(Any, condition.family)].lower()} nodes")

        def on_pick(node_id: str) -> None:
            if node_id not in ids:
                write(replace(condition, target=IdsTarget(ids=tuple(ids) + (node_id,))))

        # The picker rides a click on the button rather than a popover menu:
        # a MenuButton's own popover cannot host the searchable picker, so
        # the click pops the shared picker over the button.
        add.connect("clicked", lambda btn: pop_node_picker(btn, candidates, on_pick))
        box.append(add)
        return box

    def _property_value(self, condition: PropertyCondition, write: Callable[[Condition], None]) -> Gtk.Widget:
        """The property value control, adapted to the SELECTED schema's type
        (the Odoo register: select → options dropdown, boolean → the
        true/false selector that IS the predicate, between → two inputs,
        within → the period dropdown, everything else → the plain input)."""
        schema = next((prop for prop in self._facts.bound_properties if prop.id == condition.schema_id), None)
        schema_type = schema.type if schema is not None else "text"
        if condition.op == "within":
            return _dropdown(
                _PERIODS,
                condition.period or "today",
                lambda period: write(replace(condition, period=cast(DatePeriod, period))),
            )
        if condition.op == "between":
            box = Gtk.Box(spacing=6)
            box.append(
                _entry(
                    "" if condition.from_ is None else str(condition.from_),
                    lambda text: write(replace(condition, from_=text)),
                    placeholder="From",
                )
            )
            box.append(
                _entry(
                    "" if condition.to is None else str(condition.to),
                    lambda text: write(replace(condition, to=text)),
                    placeholder="To",
                )
            )
            return box
        if schema_type == "boolean":
            # The value IS the predicate (the ruling's fold): picking
            # true/false re-materializes the positive eq row from any shape.
            current = "true" if condition.value is True else "false"
            return _dropdown(
                (("true", "True"), ("false", "False")),
                current,
                lambda picked: write(apply_boolean_value(condition, picked == "true")),
            )
        if schema_type == "select" and schema is not None:
            current = condition.value if isinstance(condition.value, str) else ""
            return _dropdown(
                schema.options,
                current,
                lambda option: write(replace(condition, value=option)),
                tooltip="Value",
            )
        if condition.op == "exists":
            # The set bit carries no value.
            label = Gtk.Label(label="—", xalign=0)
            label.add_css_class("dim-label")
            return label
        return _entry(
            "" if condition.value is None else str(condition.value),
            lambda text: write(replace(condition, value=text)),
            placeholder="Value",
        )

    def _date_value(self, condition: DateCondition, write: Callable[[Condition], None]) -> Gtk.Widget:
        """The created/edited value control: the timestamp input, the two
        bound inputs (between), or the period dropdown (within)."""
        if condition.op == "within":
            return _dropdown(
                _PERIODS,
                condition.period or "today",
                lambda period: write(replace(condition, period=cast(DatePeriod, period))),
            )
        if condition.op == "between":
            box = Gtk.Box(spacing=6)
            box.append(
                _entry(
                    condition.from_ or "",
                    lambda text: write(replace(condition, from_=text)),
                    placeholder="From (YYYY-MM-DD)",
                )
            )
            box.append(
                _entry(
                    condition.to or "",
                    lambda text: write(replace(condition, to=text)),
                    placeholder="To (YYYY-MM-DD)",
                )
            )
            return box
        return _entry(
            condition.timestamp or "",
            lambda text: write(replace(condition, timestamp=text)),
            placeholder="YYYY-MM-DD",
        )

    def _mode_switch(self, condition: MemberCondition, write: Callable[[Condition], None]) -> Gtk.Widget:
        """The three-way target-mode switch: Manual / Query / Self. The Self
        option needs a hosting page to resolve against; a row already IN
        Self mode keeps its option even at a host-less surface (the web's
        rule — the switch must show its value)."""
        if condition.op in ("in", "not-in", "exact"):
            if isinstance(condition.target, QueryTarget):
                current: TargetMode = "query"
            elif isinstance(condition.target, SelfTarget):
                current = "self"
            else:
                current = "manual"
        else:
            current = "manual"  # the set bits read as Manual until the switch re-materializes an "in"
        options: list[tuple[str, str]] = [("manual", "Manual"), ("query", "Query")]
        if self._self_node_id is not None or current == "self":
            options.append(("self", "Self"))
        return _dropdown(
            options,
            current,
            lambda mode: write(apply_target_mode(condition, cast(TargetMode, mode))),
            tooltip="Target mode — Manual: picked nodes · Query: a nested query · Self: the page hosting this query",
        )

    # ------------------------------------------------------------------ aggregation

    def _aggregation_box(self) -> Gtk.Widget:
        """The aggregation JSON hatch (the documented slice-4 escape hatch):
        strict-validated through the wire parser on Apply; empty = no
        aggregation. The grouped grid renders through the slice-3 arm."""
        expander = Gtk.Expander(label="Aggregation (JSON — advanced)")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        hint = Gtk.Label(
            label='{"dimensions": [{"kind": "class", "id": "…"}], "measures": [{"function": "count"}]}',
            xalign=0,
        )
        hint.add_css_class("dim-label")
        box.append(hint)
        view = Gtk.TextView(buffer=self._aggregation_buffer, monospace=True, top_margin=6, bottom_margin=6)
        view.set_size_request(-1, 120)
        box.append(view)
        expander.set_child(box)
        return expander

    # ----------------------------------------------------------------------- apply

    def _on_apply_clicked(self, *_args: object) -> None:
        """The commit: prune the draft (the draft → wire boundary), validate
        the aggregation hatch, dump, hand the wire AST to the caller."""
        aggregation = self._parse_aggregation_hatch()
        if aggregation == "ERROR":
            return
        ast = draft_or_factory_ast(self._draft, self._scope)
        final = replace(ast, aggregation=cast(Any, aggregation))
        self._on_apply(dump_query_ast(final))
        self.close()

    def _parse_aggregation_hatch(self) -> Aggregation | Literal["ERROR"] | None:
        """The hatch's strict validation: empty → None; a JSON doc that
        fails the wire parser (or parses to a non-aggregation) surfaces the
        error inline and blocks the commit — never a half-typed AST lands."""
        buffer = self._aggregation_buffer
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True).strip()
        if not text:
            return None
        try:
            decoded = json.loads(text)
            wrapper = parse_query_ast(
                {
                    "version": 2,
                    "scope": {"type": "entire_workspace"},
                    "root": {"type": "group", "logic": "and", "children": []},
                    "aggregation": decoded,
                }
            )
        except (ValueError, TypeError) as exc:
            self._error_label.set_label(f"Aggregation is not a valid query AST aggregation: {exc}")
            self._error_label.set_visible(True)
            return "ERROR"
        assert wrapper.aggregation is not None
        return wrapper.aggregation
