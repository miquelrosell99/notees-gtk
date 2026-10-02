"""Tests for the strict op payload schemas and builders (op-types.ts parity).

This is the GTK client's half of the relay's 422 ``validation_failed`` gate:
every known op type validates against a pydantic model with ``extra="forbid"``
(a retired or renamed wire key fails loudly), uuid fields are format-checked,
and the ``object.update`` refines ride along. The builders are the
write-side conveniences (web ``WorkspaceClient`` parity): the ``name``
parameter wraps into a single text token unless an explicit ``content_ast``
wins — title-is-content means no op payload carries a ``name`` key.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from notees_gtk.core.protocol.op_types import KNOWN_OP_TYPES
from notees_gtk.core.protocol.payloads import (
    PAYLOAD_SCHEMAS,
    build_class_create,
    build_class_reorder,
    build_class_unassign,
    build_class_update,
    build_object_create,
    build_object_move,
    build_object_update,
    build_tag_unassign,
    payload_schema_for,
    validate_payload,
)

UUID_1 = "11111111-1111-7111-8111-111111111111"
UUID_2 = "22222222-2222-7222-8222-222222222222"
UUID_3 = "33333333-3333-7333-8333-333333333333"


class TestRegistry:
    def test_every_known_op_type_has_a_strict_schema(self) -> None:
        assert set(PAYLOAD_SCHEMAS) == set(KNOWN_OP_TYPES)
        for schema in PAYLOAD_SCHEMAS.values():
            assert schema.model_config.get("extra") == "forbid"

    def test_unknown_op_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown opType"):
            validate_payload("legacy.nodeCreate", {"objectId": UUID_1})

    def test_schema_lookup(self) -> None:
        assert payload_schema_for("tag.unassign") is PAYLOAD_SCHEMAS["tag.unassign"]
        assert payload_schema_for("nope") is None


class TestTitleIsContentStrictness:
    """B.1/B.2: object.create/update and class.create/update payloads do NOT
    accept ``name`` — strict validation rejects it like the server's zod
    ``.strict()`` schemas."""

    @pytest.mark.parametrize("op_type", ["object.create", "object.update"])
    def test_object_payloads_reject_name(self, op_type: str) -> None:
        payload: dict[str, Any] = {"objectId": UUID_1, "name": "Legacy"}
        if op_type == "object.update":
            payload["icon"] = "📄"  # satisfy the at-least-one-field refine
        with pytest.raises(ValidationError) as excinfo:
            validate_payload(op_type, payload)
        assert "name" in str(excinfo.value)

    def test_object_create_accepts_content_ast_and_tag_ids(self) -> None:
        validate_payload(
            "object.create",
            {"objectId": UUID_1, "contentAst": [{"type": "text", "text": "Hi"}], "tagIds": [UUID_2]},
        )

    @pytest.mark.parametrize("op_type", ["class.create", "class.update"])
    def test_class_payloads_reject_name_accept_content_ast(self, op_type: str) -> None:
        with pytest.raises(ValidationError):
            validate_payload(op_type, {"classId": UUID_1, "name": "Legacy"})
        validate_payload(op_type, {"classId": UUID_1, "contentAst": [{"type": "text", "text": "Task"}]})

    def test_extra_keys_rejected_for_the_new_ops(self) -> None:
        with pytest.raises(ValidationError):
            validate_payload("class.reorder", {"objectId": UUID_1, "classIds": [UUID_2], "sequence": 1})
        with pytest.raises(ValidationError):
            validate_payload("tag.unassign", {"objectId": UUID_1, "tagId": UUID_2, "hlc": 5})

    def test_uuid_fields_are_format_checked(self) -> None:
        with pytest.raises(ValidationError):
            validate_payload("class.reorder", {"objectId": "not-a-uuid", "classIds": [UUID_2]})
        with pytest.raises(ValidationError):
            validate_payload("class.reorder", {"objectId": UUID_1, "classIds": ["also-not"]})
        with pytest.raises(ValidationError):
            validate_payload("tag.unassign", {"objectId": UUID_1, "tagId": "nope"})


class TestObjectUpdateRefines:
    def test_requires_at_least_one_field(self) -> None:
        with pytest.raises(ValidationError, match="at least one field"):
            validate_payload("object.update", {"objectId": UUID_1})

    def test_exactly_one_content_carrier(self) -> None:
        with pytest.raises(ValidationError, match="exactly one content carrier"):
            validate_payload(
                "object.update",
                {"objectId": UUID_1, "contentAst": [], "contentDeltaB64": "AAAA"},
            )

    def test_single_carrier_accepted(self) -> None:
        validate_payload("object.update", {"objectId": UUID_1, "contentDeltaB64": "AAAA"})


class TestRenderStateModelStrictness:
    """Revision 11: object.create/update dropped the ``nodeType`` enumeration
    and gained the optional ``presentAsMain`` render bit — the retired key is
    rejected outright (``extra="forbid"``), no wire compat of any kind."""

    @pytest.mark.parametrize("op_type", ["object.create", "object.update"])
    def test_node_type_key_rejected(self, op_type: str) -> None:
        payload: dict[str, Any] = {"objectId": UUID_1, "nodeType": "page"}
        if op_type == "object.update":
            payload["icon"] = "📄"  # satisfy the at-least-one-field refine
        with pytest.raises(ValidationError) as excinfo:
            validate_payload(op_type, payload)
        assert "nodeType" in str(excinfo.value)

    @pytest.mark.parametrize("op_type", ["object.create", "object.update"])
    def test_present_as_main_accepted(self, op_type: str) -> None:
        payload: dict[str, Any] = {"objectId": UUID_1, "presentAsMain": True}
        if op_type == "object.update":
            validate_payload(op_type, payload)
        else:
            validate_payload(op_type, {**payload, "parentId": UUID_2})


class TestSiblingAnchorFields:
    """object.create/object.move sibling placement anchors: ``afterId`` and
    ``beforeId`` are accepted by the strict schemas (uuid-checked) on both
    payloads — placement itself lives in the appliers' fractional allocator."""

    @pytest.mark.parametrize("op_type", ["object.create", "object.move"])
    def test_after_id_and_before_id_accepted(self, op_type: str) -> None:
        validate_payload(
            op_type,
            {"objectId": UUID_1, "parentId": UUID_2, "afterId": UUID_3, "beforeId": UUID_2},
        )

    @pytest.mark.parametrize("op_type", ["object.create", "object.move"])
    def test_anchor_fields_are_uuid_checked(self, op_type: str) -> None:
        with pytest.raises(ValidationError):
            validate_payload(op_type, {"objectId": UUID_1, "parentId": UUID_2, "beforeId": "not-a-uuid"})
        with pytest.raises(ValidationError):
            validate_payload(op_type, {"objectId": UUID_1, "parentId": UUID_2, "afterId": "not-a-uuid"})


class TestBuilders:
    """Builder parity with WorkspaceClient.createObject/createClass/
    reorderClasses: ``name`` is a convenience that becomes a single text
    token; an explicit contentAst wins and name is dropped."""

    def test_object_create_name_becomes_content_ast(self) -> None:
        assert build_object_create(UUID_1, name="My Page") == {
            "objectId": UUID_1,
            "contentAst": [{"type": "text", "text": "My Page"}],
        }

    def test_object_create_content_ast_wins_over_name(self) -> None:
        payload = build_object_create(UUID_1, name="Dropped", content_ast=[{"type": "text", "text": "Winner"}])
        assert payload == {"objectId": UUID_1, "contentAst": [{"type": "text", "text": "Winner"}]}

    def test_object_create_full_shape(self) -> None:
        payload = build_object_create(
            UUID_1,
            present_as_main=False,
            class_ids=[UUID_2],
            tag_ids=[UUID_3],
            content_ast=[{"type": "text", "text": "hi"}],
            parent_id=UUID_2,
        )
        assert payload == {
            "objectId": UUID_1,
            "presentAsMain": False,
            "classIds": [UUID_2],
            "tagIds": [UUID_3],
            "contentAst": [{"type": "text", "text": "hi"}],
            "parentId": UUID_2,
        }

    def test_object_create_root_page_sends_null_parent(self) -> None:
        payload = build_object_create(UUID_1, parent_id=None)
        assert payload["parentId"] is None

    def test_object_update_builder(self) -> None:
        payload = build_object_update(UUID_1, content_ast=[{"type": "text", "text": "new"}], icon="📄")
        assert payload == {"objectId": UUID_1, "contentAst": [{"type": "text", "text": "new"}], "icon": "📄"}
        with pytest.raises(ValidationError):
            build_object_update(UUID_1)

    def test_class_create_name_convenience(self) -> None:
        assert build_class_create(UUID_1, name="Task", icon="✅") == {
            "classId": UUID_1,
            "contentAst": [{"type": "text", "text": "Task"}],
            "icon": "✅",
        }

    def test_class_create_content_ast_wins_over_name(self) -> None:
        payload = build_class_create(UUID_1, name="Dropped", content_ast=[{"type": "text", "text": "Winner"}])
        assert payload == {"classId": UUID_1, "contentAst": [{"type": "text", "text": "Winner"}]}

    def test_class_update_name_convenience(self) -> None:
        assert build_class_update(UUID_1, name="Novel") == {
            "classId": UUID_1,
            "contentAst": [{"type": "text", "text": "Novel"}],
        }

    def test_reorder_classes_convenience(self) -> None:
        """Mirrors WorkspaceClient.reorderClasses: the full ordered list."""
        assert build_class_reorder(UUID_1, [UUID_3, UUID_2]) == {
            "objectId": UUID_1,
            "classIds": [UUID_3, UUID_2],
        }

    def test_unassign_builders(self) -> None:
        assert build_class_unassign(UUID_1, UUID_2) == {"objectId": UUID_1, "classId": UUID_2}
        assert build_tag_unassign(UUID_1, UUID_2) == {"objectId": UUID_1, "tagId": UUID_2}

    def test_move_and_delete_builders(self) -> None:
        assert build_object_move(UUID_1, UUID_2, after_id=UUID_3) == {
            "objectId": UUID_1,
            "parentId": UUID_2,
            "afterId": UUID_3,
        }
        assert build_object_move(UUID_1, None) == {"objectId": UUID_1, "parentId": None}
        assert build_object_create(UUID_1)  # minimal create: id only

    def test_sibling_anchor_builders(self) -> None:
        """beforeId rides both payloads; the anchor keys stay absent when unset."""
        assert build_object_move(UUID_1, UUID_2, before_id=UUID_3) == {
            "objectId": UUID_1,
            "parentId": UUID_2,
            "beforeId": UUID_3,
        }
        assert build_object_move(UUID_1, UUID_2, after_id=UUID_3, before_id=UUID_2) == {
            "objectId": UUID_1,
            "parentId": UUID_2,
            "afterId": UUID_3,
            "beforeId": UUID_2,
        }
        assert "beforeId" not in build_object_move(UUID_1, UUID_2)
        assert build_object_create(UUID_1, parent_id=UUID_2, after_id=UUID_3, before_id=UUID_2) == {
            "objectId": UUID_1,
            "parentId": UUID_2,
            "afterId": UUID_3,
            "beforeId": UUID_2,
        }
        assert "afterId" not in build_object_create(UUID_1)
        assert "beforeId" not in build_object_create(UUID_1)
