"""Pure tag-chrome helpers: the write envelopes + the pill/picker logic.

Headless-testable by design: no GTK imports here (the conventions in
``.agents/skills/notees-gtk-development`` keep pure UI logic importable
without the ``ui`` extra, exactly like :mod:`notees_gtk.ui.alias_ops`).

Tags are first-class and node-scoped (SCHEMA.md): ANY page can be assigned
as a tag to pages AND blocks — a tag IS a page, there is no tag kind. The
web client (the reference UX, ``apps/web/src/features/metadata/TagsRow.tsx``
+ ``client-writes.ts assignTag/unassignTag``) renders a Tags row of pills
(alphabetical, the tag's effective color as the pill surface) with an add
picker over pages and a per-pill remove; this module ports the semantics the
GTK chrome needs, staying pure over stored rows:

- :func:`tag_assign_envelope` — the OR-Set add carrier: a re-issued
  ``object.create`` over the node's own id carrying the single new tag (the
  web's ``assignTag``; the applier seeds ``tag_member_set`` and early-returns
  on the existing row, so the re-create never touches the tree);
- :func:`tag_unassign_envelope` — the OR-Set tombstone (``tag.unassign``);
- :func:`tag_pills` — the header pill records: assigned tags alphabetical by
  display name, each carrying its effective color resolved to a concrete
  hex (own color, else the first class-in-order's chain color — the web
  ``effectiveNodeColor`` port) and a contrast foreground;
- :func:`tag_assign_candidates` — the add picker's filter: pages only (the
  pure ``rendersWithDocumentChrome`` port matching the store's
  ``_is_page_row`` rule), minus the node itself and its already-assigned
  tags.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from notees_gtk.core.protocol.clock import Clock
from notees_gtk.core.protocol.content import parse_content_ast
from notees_gtk.core.protocol.models import RelayEnvelope, new_envelope
from notees_gtk.data.store import NodeRow

__all__ = [
    "PRESET_HEX",
    "TagPill",
    "contrast_for",
    "css_color_for",
    "effective_class_color",
    "effective_node_color",
    "renders_with_document_chrome",
    "tag_assign_candidates",
    "tag_assign_envelope",
    "tag_pills",
    "tag_unassign_envelope",
]

#: Preset token → concrete hex, the GTK-side render mapping for the ten
#: normative tokens (``core/protocol/colors.py``): MUST mirror the web
#: ``PRESET_HEX`` (``apps/web/src/components/ui/fleet/colorPresets.ts``,
#: which mirrors ``variables.css --md-sys-color-preset-*``) so a pill's
#: surface converges with the web client for the same stored token.
PRESET_HEX: Mapping[str, str] = {
    "red": "#e34d45",
    "orange": "#ed822b",
    "yellow": "#f3b816",
    "green": "#30a66f",
    "teal": "#27a59c",
    "sky": "#20a9e9",
    "blue": "#4072e7",
    "purple": "#9662da",
    "pink": "#de4996",
    "gray": "#8c857d",
}

#: The extends-edge shape the pure resolvers walk: class id → its direct
#: extends parents (the store's ``class_extends`` table, see
#: :meth:`notees_gtk.data.store.LocalStore.class_extends_edges`).
type ExtendsMap = Mapping[str, Sequence[str]]

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


@dataclass(frozen=True)
class TagPill:
    """One assigned-tag pill record for the Tags row.

    ``color``/``foreground`` are concrete ``#RRGGBB`` hex (Pango spans cannot
    read GTK CSS variables), or ``None`` when the tag has no effective color
    — the chrome then renders the neutral chip surface like the web's grey
    default.
    """

    tag_id: str
    label: str
    icon: str | None
    color: str | None
    foreground: str | None


def css_color_for(value: str) -> str:
    """Resolve a stored color value to a concrete hex (the web
    ``cssColorFor`` port): a preset token maps through :data:`PRESET_HEX`,
    a custom ``#RRGGBB`` passes through.

    Raises:
        ValueError: anything else — fail loud like the strict color grammar
            (``core/protocol/colors.py``); the chrome never guesses.
    """
    if value in PRESET_HEX:
        return PRESET_HEX[value]
    if _HEX_COLOR_RE.match(value):
        return value.lower()
    raise ValueError(f"expected a color preset token or #RRGGBB hex, got {value!r}")


def contrast_for(css_color: str) -> str:
    """Black or white for text sitting on ``css_color`` — the web
    ``contrastFor`` port (``apps/web/src/features/metadata/shared.ts``):
    relative luminance over the sRGB channels, threshold 0.45. Takes a
    CONCRETE hex (resolve preset tokens first with :func:`css_color_for`).

    Raises:
        ValueError: a non-``#RRGGBB`` input — the web falls back to a themed
            CSS variable; GTK Pango spans need a concrete color, so the
            caller must resolve first (fail loud, never guess).
    """
    match = _HEX_COLOR_RE.match(css_color.strip())
    if match is None:
        raise ValueError(f"contrast_for needs a concrete #RRGGBB color, got {css_color!r}")
    rgb = int(match.group(0)[1:], 16)

    def channel(shift: int) -> float:
        return ((rgb >> shift) & 0xFF) / 255

    luminance = 0.2126 * channel(16) + 0.7152 * channel(8) + 0.0722 * channel(0)
    return "#000000" if luminance > 0.45 else "#ffffff"


def _extends_parents(class_id: str, extends: ExtendsMap) -> Sequence[str]:
    return extends.get(class_id, ())


def effective_class_color(class_id: str, by_id: Mapping[str, NodeRow], extends: ExtendsMap) -> str | None:
    """A class's effective color: its own, else the nearest extends-chain
    ancestor's (BFS, parents visited in sorted id order — the web
    ``effectiveClassColor`` port, ``client-reads-refs.ts``)."""
    seen: set[str] = set()
    queue = [class_id]
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        row = by_id.get(current)
        if row is not None and row.color:
            return row.color
        queue.extend(sorted(_extends_parents(current, extends)))
    return None


def _effective_parentless_block(class_id: str, by_id: Mapping[str, NodeRow], extends: ExtendsMap) -> bool:
    """True when the class OR any extends-ancestor carries parentlessRole
    "block" (the one-way demote; the web ``effectiveClassParentlessBlock``
    port, the same BFS reduced to a boolean)."""
    seen: set[str] = set()
    queue = [class_id]
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        row = by_id.get(current)
        if row is not None and row.parentless_role == "block":
            return True
        queue.extend(sorted(_extends_parents(current, extends)))
    return False


def effective_node_color(row: NodeRow, by_id: Mapping[str, NodeRow], extends: ExtendsMap) -> str | None:
    """A node's effective color: its own, else the first assigned class's
    chain color (class order — the web ``effectiveNodeColor`` port)."""
    if row.color:
        return row.color
    for class_id in row.class_ids:
        color = effective_class_color(class_id, by_id, extends)
        if color is not None:
            return color
    return None


def renders_with_document_chrome(
    row: NodeRow,
    by_id: Mapping[str, NodeRow],
    extends: ExtendsMap,
) -> bool:
    """True when the node renders with document chrome — the pure port of the
    store's ``_is_page_row`` rule (SCHEMA.md "Node structure", Revision 12):
    a class never does; a parentless non-class node does unless one of its
    classes effectively carries parentlessRole "block"; a parented node does
    exactly when it claims the standalone role (``is_embedded = 0``) AND its
    parent also renders with document chrome. A missing parent row is a
    defensive "not a page" (the store's precedent).
    """
    if row.is_class:
        return False
    if row.parent_id is None:
        return not any(_effective_parentless_block(class_id, by_id, extends) for class_id in row.class_ids)
    if row.is_embedded:
        return False
    parent = by_id.get(row.parent_id)
    if parent is None:
        return False
    return renders_with_document_chrome(parent, by_id, extends)


def tag_pills(
    tag_ids: Sequence[str],
    by_id: Mapping[str, NodeRow],
    extends: ExtendsMap,
    label_for: Callable[[NodeRow], str],
) -> list[TagPill]:
    """The Tags row's pill records: assigned tags alphabetical by display
    name (the web's ``localeCompare`` order, portably: case-folded label,
    id tiebreak), each with its effective color resolved to a concrete hex
    and a contrast foreground. Unresolvable tag ids are skipped (the web
    filters deleted/unknown tags the same way)."""
    pills: list[TagPill] = []
    for tag_id in tag_ids:
        row = by_id.get(tag_id)
        if row is None:
            continue
        color_value = effective_node_color(row, by_id, extends)
        color = css_color_for(color_value) if color_value is not None else None
        pills.append(
            TagPill(
                tag_id=tag_id,
                label=label_for(row),
                icon=row.icon or None,
                color=color,
                foreground=contrast_for(color) if color is not None else None,
            )
        )
    pills.sort(key=lambda pill: (pill.label.casefold(), pill.tag_id))
    return pills


def tag_assign_candidates(
    rows: Sequence[NodeRow],
    node_id: str,
    by_id: Mapping[str, NodeRow],
    extends: ExtendsMap,
) -> list[NodeRow]:
    """The add picker's candidate set: PAGES only (tags ARE pages — the web
    picker's ``searchMode="pages"`` is this ``renders_with_document_chrome``
    filter), minus the node itself (self-tagging is a no-op at best) and
    minus its already-assigned tags (the web picker marks those
    already-added; dropping them keeps the GTK list honest). Input order is
    preserved; the chrome label-sorts for display."""
    assigned = set(by_id[node_id].tag_ids) if node_id in by_id else set()
    return [
        row
        for row in rows
        if row.id != node_id and row.id not in assigned and renders_with_document_chrome(row, by_id, extends)
    ]


def tag_assign_envelope(
    *,
    workspace_id: str,
    actor_id: str,
    device_id: str,
    node: NodeRow,
    tag_id: str,
    clock: Clock,
) -> RelayEnvelope:
    """Build the tag-assign carrier: a re-issued ``object.create`` over the
    node's OWN id with the single new tag (the web ``assignTag`` port —
    ``client-writes.ts``). The applier treats a create on an existing id as
    the OR-Set seeding carrier only (it early-returns before touching the
    tree), so ``parentId``/``contentAst`` ride along for replicas that never
    saw the original create and are inert everywhere else. ``parentId`` is
    always present (``None`` = the root shape); ``contentAst`` rides only
    when the node carries content (absent = no write, the key-presence rule).
    """
    payload: dict[str, object] = {"objectId": node.id, "parentId": node.parent_id}
    if node.content is not None:
        payload["contentAst"] = parse_content_ast(node.content)
    payload["tagIds"] = [tag_id]
    return new_envelope(
        workspace_id=workspace_id,
        actor_id=actor_id,
        device_id=device_id,
        op_type="object.create",
        payload=payload,
        clock=clock,
        affected_node_ids=(node.id,),
    )


def tag_unassign_envelope(
    *,
    workspace_id: str,
    actor_id: str,
    device_id: str,
    node_id: str,
    tag_id: str,
    clock: Clock,
) -> RelayEnvelope:
    """Build the tag-remove tombstone: ``tag.unassign`` — the OR-Set remove
    complement of the create carrier (the web ``unassignTag`` port)."""
    return new_envelope(
        workspace_id=workspace_id,
        actor_id=actor_id,
        device_id=device_id,
        op_type="tag.unassign",
        payload={"objectId": node_id, "tagId": tag_id},
        clock=clock,
        affected_node_ids=(node_id,),
    )
