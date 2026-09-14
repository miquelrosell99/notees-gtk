"""Known operation types for the relay sync protocol (SPEC §3)."""

from __future__ import annotations

__all__ = ["KNOWN_OP_TYPES"]

KNOWN_OP_TYPES: frozenset[str] = frozenset(
    [
        # Structural
        "node.create",
        "node.delete",
        "node.move",
        "node.updateContent",
        "node.updateIcon",
        "node.updateColor",
        "node.addAlias",
        "node.removeAlias",
        "node.archive",
        "node.restore",
        "node.permanentDelete",
        "node.convert",
        "class.assign",
        "class.unassign",
        # Properties
        "property.set",
        "property.unset",
        # Schema
        "propertySchema.create",
        "propertySchema.update",
        "propertySchema.delete",
        "classPropertyEdge.create",
        "classPropertyEdge.update",
        "classPropertyEdge.delete",
        "classPropertyEdge.reorder",
        "class.create",
        "class.update",
        "class.delete",
        "class.setExtends",
        # NodeViews
        "nodeView.create",
        "nodeView.update",
        "nodeView.delete",
        "nodeView.reorder",
        # Tasks
        "task.recordCompletion",
        "task.deleteCompletion",
        "task.setRecurrence",
        "task.deleteRecurrence",
        # Assets
        "asset.upload",
        "asset.delete",
        # Activity
        "activity.record",
        "activity.delete",
        "link.click",
        # Shares
        "share.public.create",
        "share.public.revoke",
        "share.user.grant",
        "share.user.revoke",
        # User preferences
        "user.favorite.add",
        "user.favorite.remove",
        "user.favorite.reorder",
        # Plugins
        "plugin.op",
    ]
)
