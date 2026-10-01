"""Known operation types for the relay sync protocol (v2).

Mirrors ``v2/packages/protocol/src/op-types.ts`` (the M1 op registry): exactly
these 21 op types have payload schemas server-side. The envelope schema itself
accepts any non-empty ``opType`` string (see ``envelope.ts``); unknown op types
are rejected at relay ingest with ``validation_failed`` (422). Producers should
stick to this registry — that 422 maps to the quarantine path.
"""

from __future__ import annotations

__all__ = ["KNOWN_OP_TYPES"]

KNOWN_OP_TYPES: frozenset[str] = frozenset(
    [
        # Objects
        "object.create",
        "object.update",
        "object.delete",
        "object.move",
        # Classes
        "class.create",
        "class.update",
        "class.delete",
        "class.unassign",
        "class.reorder",
        "tag.unassign",
        "class.setExtends",
        "class.property.set",
        "class.property.unset",
        # Property schemas
        "propertySchema.create",
        "propertySchema.update",
        "propertySchema.delete",
        # Properties
        "property.set",
        "property.unset",
        # Assets
        "asset.attach",
        "asset.detach",
        # Collections (membership only — collections are nodes)
        "collection.member.add",
        "collection.member.remove",
    ]
)
