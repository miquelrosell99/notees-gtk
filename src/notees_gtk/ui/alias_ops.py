"""Pure alias-chrome helpers: the write envelope + the repoint candidate set.

Headless-testable by design: no GTK imports here (the conventions in
``.agents/skills/notees-gtk-development`` keep pure UI logic importable
without the ``ui`` extra, exactly like :mod:`notees_gtk.ui.ast_render`).

The node-alias system rides the ``aliasedNodeId`` wire node field
(SCHEMA.md "Node aliases" — many-to-one FROM the alias, mapped by
``object.update``, cycle-checked at the applier). This module carries the
two pieces the page chrome needs that stay pure over stored rows:

- :func:`alias_update_envelope` — the alias-side write (re-point or clear)
  as an ``object.update`` envelope, the same authoring shape the editor
  uses for content (``packages/web`` parity: the alias row's Change/Remove
  write the carrier's OWN field; the main's ADD direction is not part of
  this chrome);
- :func:`alias_repoint_candidates` — the Change picker's filter: every
  live candidate minus the alias itself and every node that already carries
  an alias (the web picker's ``canAdd`` rule — an already-aliased node
  would need the backward write instead).
"""

from __future__ import annotations

from collections.abc import Sequence

from notees_gtk.core.protocol.clock import Clock
from notees_gtk.core.protocol.models import RelayEnvelope, new_envelope
from notees_gtk.data.store import NodeRow

__all__ = ["alias_repoint_candidates", "alias_update_envelope"]


def alias_update_envelope(
    *,
    workspace_id: str,
    actor_id: str,
    device_id: str,
    node_id: str,
    target_id: str | None,
    clock: Clock,
) -> RelayEnvelope:
    """Build the ``object.update`` envelope re-pointing (or clearing) the
    alias: ``aliasedNodeId`` presence-writes the new target, present-null
    clears (the nullish-field rule — a real CLEAR, never swallowed). The
    applier cycle-checks the would-be chain and fails loud on a revisit."""
    return new_envelope(
        workspace_id=workspace_id,
        actor_id=actor_id,
        device_id=device_id,
        op_type="object.update",
        payload={"objectId": node_id, "aliasedNodeId": target_id},
        clock=clock,
        affected_node_ids=(node_id,),
    )


def alias_repoint_candidates(rows: Sequence[NodeRow], node_id: str) -> list[NodeRow]:
    """The Change picker's candidate set over the workspace's live rows:
    the alias node itself excluded (a self-alias is a write-time cycle) and
    every already-aliased node excluded (its field is taken — re-pointing it
    from here would be the main-side backward write, not this row's job).
    Input (id) order is preserved; the chrome labels sort for display."""
    return [row for row in rows if row.id != node_id and row.aliased_node_id is None]
