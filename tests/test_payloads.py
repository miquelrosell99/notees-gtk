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

from notees_gtk.core.protocol.colors import COLOR_PRESET_TOKENS
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
    build_workspace_feature_set,
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


class TestColorGrammar:
    """node/class ``color`` (owner 2026-10-03) is a preset token or a
    custom ``#RRGGBB`` hex (colors.py grammar), or ``null`` to clear. The
    retired ``var(--color-preset-*)`` encoding and freeform strings are
    rejected outright by the strict schemas — no wire compat."""

    @pytest.mark.parametrize("op_type", ["object.update", "class.create", "class.update"])
    @pytest.mark.parametrize("token", COLOR_PRESET_TOKENS)
    def test_every_preset_token_accepted(self, op_type: str, token: str) -> None:
        key = "objectId" if op_type == "object.update" else "classId"
        validate_payload(op_type, {key: UUID_1, "color": token})

    @pytest.mark.parametrize("op_type", ["object.update", "class.create", "class.update"])
    @pytest.mark.parametrize("hex_color", ["#abcdef", "#ABCDEF", "#123abc", "#0f9D8e"])
    def test_six_digit_hex_accepted(self, op_type: str, hex_color: str) -> None:
        key = "objectId" if op_type == "object.update" else "classId"
        validate_payload(op_type, {key: UUID_1, "color": hex_color})

    @pytest.mark.parametrize("op_type", ["object.update", "class.create", "class.update"])
    def test_retired_css_variable_encoding_rejected(self, op_type: str) -> None:
        key = "objectId" if op_type == "object.update" else "classId"
        with pytest.raises(ValidationError):
            validate_payload(op_type, {key: UUID_1, "color": "var(--color-preset-red)"})

    @pytest.mark.parametrize("op_type", ["object.update", "class.create", "class.update"])
    @pytest.mark.parametrize("bad", ["#12345", "#1234567", "red ", " not-a-color", "not-a-color", ""])
    def test_garbage_rejected(self, op_type: str, bad: str) -> None:
        key = "objectId" if op_type == "object.update" else "classId"
        with pytest.raises(ValidationError):
            validate_payload(op_type, {key: UUID_1, "color": bad})

    def test_null_clear_accepted_on_object_update(self) -> None:
        """NEW capability: an explicit null on the wire must parse, and it
        still satisfies the at-least-one-writable-field refine."""
        validate_payload("object.update", {"objectId": UUID_1, "color": None})

    def test_null_clear_accepted_on_class_update(self) -> None:
        validate_payload("class.update", {"classId": UUID_1, "color": None})

    def test_absent_color_still_validates(self) -> None:
        validate_payload("object.update", {"objectId": UUID_1, "icon": "📄"})
        validate_payload("class.update", {"classId": UUID_1, "description": "d"})

    def test_builder_sends_null_clear_only_when_asked(self) -> None:
        """``color=None`` emits an explicit null (the UI's "No color");
        omitting the parameter leaves the key off the wire entirely."""
        assert build_object_update(UUID_1, color=None) == {"objectId": UUID_1, "color": None}
        assert build_class_update(UUID_1, color=None) == {"classId": UUID_1, "color": None}
        assert "color" not in build_object_update(UUID_1, icon="📄")
        assert "color" not in build_class_update(UUID_1)
        assert build_object_update(UUID_1, color="sky") == {"objectId": UUID_1, "color": "sky"}
        assert build_class_update(UUID_1, color="#123abc") == {"classId": UUID_1, "color": "#123abc"}

    def test_builder_rejects_out_of_grammar_colors(self) -> None:
        with pytest.raises(ValidationError):
            build_object_update(UUID_1, color="var(--color-preset-red)")
        with pytest.raises(ValidationError):
            build_class_update(UUID_1, color="not-a-color")


class TestWireNodeFields:
    """M27: ``object.update`` carries the optional nullable ``coverAssetId``
    / ``bannerAssetId`` / ``aliasedNodeId`` node fields — presence writes,
    present-null clears (the zod ``uuid.nullish()`` parity). The 2026-10-09
    batch adds ``description`` — the page subtitle, a plain string max 512
    chars with the same nullish semantics (zod ``z.string().max(512).nullish()``
    parity). ``object.create`` carries none of them (the strict schema
    rejects the keys outright, no wire compat)."""

    @pytest.mark.parametrize(
        "key", ["coverAssetId", "bannerAssetId", "aliasedNodeId"]
    )
    def test_object_update_accepts_the_field_and_its_null_clear(self, key: str) -> None:
        validate_payload("object.update", {"objectId": UUID_1, key: UUID_2})
        # A null-only update still satisfies the at-least-one-field refine
        # (key presence, the zod ``Object.keys(p).length > 1`` parity).
        validate_payload("object.update", {"objectId": UUID_1, key: None})

    @pytest.mark.parametrize(
        "key", ["coverAssetId", "bannerAssetId", "aliasedNodeId"]
    )
    def test_the_fields_are_uuid_format_checked(self, key: str) -> None:
        with pytest.raises(ValidationError):
            validate_payload("object.update", {"objectId": UUID_1, key: "not-a-uuid"})

    @pytest.mark.parametrize(
        "key", ["coverAssetId", "bannerAssetId", "aliasedNodeId"]
    )
    def test_object_create_rejects_the_update_only_fields(self, key: str) -> None:
        with pytest.raises(ValidationError):
            validate_payload("object.create", {"objectId": UUID_1, key: UUID_2})

    def test_description_accepts_text_and_its_null_clear(self) -> None:
        validate_payload("object.update", {"objectId": UUID_1, "description": "Subtitle text"})
        # A null-only update still satisfies the at-least-one-field refine
        # (key presence, the zod ``Object.keys(p).length > 1`` parity).
        validate_payload("object.update", {"objectId": UUID_1, "description": None})

    def test_description_is_max_512_chars(self) -> None:
        validate_payload("object.update", {"objectId": UUID_1, "description": "x" * 512})
        with pytest.raises(ValidationError):
            validate_payload("object.update", {"objectId": UUID_1, "description": "x" * 513})

    def test_object_create_rejects_description(self) -> None:
        with pytest.raises(ValidationError):
            validate_payload("object.create", {"objectId": UUID_1, "description": "Subtitle text"})

    def test_builder_sends_clears_only_when_asked(self) -> None:
        """``None`` emits an explicit null (the clear); omitting the
        parameter leaves the key off the wire entirely (absence = no write)."""
        assert build_object_update(UUID_1, aliased_node_id=None) == {"objectId": UUID_1, "aliasedNodeId": None}
        assert build_object_update(UUID_1, cover_asset_id=UUID_2, banner_asset_id=UUID_3) == {
            "objectId": UUID_1,
            "coverAssetId": UUID_2,
            "bannerAssetId": UUID_3,
        }
        payload = build_object_update(UUID_1, icon="📄")
        for key in ("coverAssetId", "bannerAssetId", "aliasedNodeId"):
            assert key not in payload
        assert build_object_update(UUID_1, description=None) == {"objectId": UUID_1, "description": None}
        assert build_object_update(UUID_1, description="Subtitle text") == {
            "objectId": UUID_1,
            "description": "Subtitle text",
        }
        assert "description" not in build_object_update(UUID_1, icon="📄")


class TestAssetPropertyType:
    """M38: the property-schema type enum gains ``asset`` (node-typed values
    whose target must carry the asset class — the filter is implicit in the
    type)."""

    def test_property_schema_create_accepts_asset(self) -> None:
        validate_payload(
            "propertySchema.create",
            {"propertySchemaId": UUID_1, "name": "Attachment", "type": "asset", "multi": True, "scope": "class"},
        )

    def test_property_schema_create_rejects_unknown_types(self) -> None:
        with pytest.raises(ValidationError):
            validate_payload(
                "propertySchema.create",
                {"propertySchemaId": UUID_1, "name": "X", "type": "asset_node"},
            )


class TestUnifiedDatetimePropertyType:
    """The unified datetime property type (owner 2026-10-09, SCHEMA.md
    "Datetime"): the ``propertySchema.create`` type enum retires
    ``date``/``date_range`` and adds ``datetime`` — strict, the retired
    values are rejected outright (the zod enum parity)."""

    def test_property_schema_create_accepts_datetime(self) -> None:
        validate_payload(
            "propertySchema.create",
            {"propertySchemaId": UUID_1, "name": "When", "type": "datetime"},
        )
        # datePrecision/dateQualified ride the datetime schema (the TS parity).
        validate_payload(
            "propertySchema.create",
            {
                "propertySchemaId": UUID_1,
                "name": "When",
                "type": "datetime",
                "datePrecision": "year",
                "dateQualified": True,
            },
        )

    @pytest.mark.parametrize("retired", ["date", "date_range"])
    def test_property_schema_create_rejects_the_retired_types(self, retired: str) -> None:
        with pytest.raises(ValidationError):
            validate_payload(
                "propertySchema.create",
                {"propertySchemaId": UUID_1, "name": "x", "type": retired},
            )

    def test_property_schema_update_keeps_patchable_date_fields(self) -> None:
        validate_payload("propertySchema.update", {"propertySchemaId": UUID_1, "datePrecision": "month"})
        validate_payload("propertySchema.update", {"propertySchemaId": UUID_1, "datePrecision": None})


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


class TestWorkspaceFeatureSet:
    """The workspace.feature.set payload — the strict five-
    family enum (tasks|events|meetings|sources|persons); the retired pre-
    reshape ids are rejected outright, no wire compat."""

    def test_builder_emits_the_toggle(self) -> None:
        assert build_workspace_feature_set("tasks", True) == {"feature": "tasks", "enabled": True}
        assert build_workspace_feature_set("events", False) == {"feature": "events", "enabled": False}

    @pytest.mark.parametrize("feature", ["tasks", "events", "meetings", "sources", "persons"])
    def test_every_family_id_accepted(self, feature: str) -> None:
        validate_payload("workspace.feature.set", {"feature": feature, "enabled": True})

    @pytest.mark.parametrize(
        "feature",
        [
            "journals",
            "readItLater",
            "read_it_later",
            "library",
            "people",
            "collections",
            "tasks ",
            "TASKS",
            "whiteboard",
        ],
    )
    def test_retired_and_unknown_feature_ids_rejected(self, feature: str) -> None:
        with pytest.raises(ValidationError):
            validate_payload("workspace.feature.set", {"feature": feature, "enabled": True})

    def test_extra_keys_rejected(self) -> None:
        with pytest.raises(ValidationError):
            validate_payload("workspace.feature.set", {"feature": "tasks", "enabled": True, "workspaceId": UUID_1})

    def test_enabled_is_required_and_typed(self) -> None:
        with pytest.raises(ValidationError):
            validate_payload("workspace.feature.set", {"feature": "tasks"})
        with pytest.raises(ValidationError):
            validate_payload("workspace.feature.set", {"feature": "tasks", "enabled": []})


class TestPropertyWirePayloads:
    """The property-wire batch: the optional PG5 elementId (UUID, explicit
    null rejected — zod ``optional()`` parity) and the PC4 binding active
    flag (omitted = keep; explicit null rejected)."""

    def test_property_set_element_id_accepted(self) -> None:
        validate_payload(
            "property.set",
            {"objectId": UUID_1, "propertySchemaId": UUID_2, "value": "x", "elementId": UUID_3, "idx": 1},
        )

    def test_property_set_element_id_must_be_uuid(self) -> None:
        with pytest.raises(ValidationError):
            validate_payload(
                "property.set",
                {"objectId": UUID_1, "propertySchemaId": UUID_2, "value": "x", "elementId": "not-a-uuid"},
            )

    def test_property_set_explicit_null_element_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="elementId"):
            validate_payload(
                "property.set",
                {"objectId": UUID_1, "propertySchemaId": UUID_2, "value": "x", "elementId": None},
            )

    def test_property_unset_element_id_accepted_absent_default(self) -> None:
        validate_payload("property.unset", {"objectId": UUID_1, "propertySchemaId": UUID_2})
        validate_payload("property.unset", {"objectId": UUID_1, "propertySchemaId": UUID_2, "elementId": UUID_3})

    def test_property_unset_explicit_null_element_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="elementId"):
            validate_payload(
                "property.unset",
                {"objectId": UUID_1, "propertySchemaId": UUID_2, "elementId": None},
            )

    def test_class_property_set_active_accepted(self) -> None:
        validate_payload(
            "class.property.set",
            {"classId": UUID_1, "propertySchemaId": UUID_2, "active": False},
        )

    def test_class_property_set_explicit_null_active_rejected(self) -> None:
        with pytest.raises(ValidationError, match="active"):
            validate_payload(
                "class.property.set",
                {"classId": UUID_1, "propertySchemaId": UUID_2, "active": None},
            )

    def test_class_property_set_active_false_is_a_real_write_shape(self) -> None:
        """Omitted keeps the stored flag; the payload layer must not collapse
        an explicit False into 'absent' (model_fields_set is the guard)."""
        payload = PAYLOAD_SCHEMAS["class.property.set"].model_validate(
            {"classId": UUID_1, "propertySchemaId": UUID_2, "active": False}
        )
        assert payload.active is False
        assert "active" in payload.model_fields_set
        kept = PAYLOAD_SCHEMAS["class.property.set"].model_validate({"classId": UUID_1, "propertySchemaId": UUID_2})
        assert kept.active is None
        assert "active" not in kept.model_fields_set


class TestPropertySchemaNumberFormats:
    """numberPad/numberDecimals/numberRounding ride the
    propertySchema payloads (additive-optional, nullable); the keep-vs-clear
    contract distinguishes absent from explicit null via model_fields_set."""

    def test_create_accepts_and_validates_the_format_fields(self) -> None:
        schema = PAYLOAD_SCHEMAS["propertySchema.create"]
        payload = schema.model_validate(
            {
                "propertySchemaId": UUID_1,
                "name": "Dex number",
                "type": "number",
                "numberPad": 4,
                "numberDecimals": 1,
                "numberRounding": "floor",
            }
        )
        assert payload.number_pad == 4
        assert payload.number_decimals == 1
        assert payload.number_rounding == "floor"
        minimal = schema.model_validate({"propertySchemaId": UUID_1, "name": "x", "type": "number"})
        assert minimal.number_pad is None
        # Range + enum enforcement mirrors the TS reference.
        with pytest.raises(ValidationError):
            schema.model_validate({"propertySchemaId": UUID_1, "name": "x", "type": "number", "numberPad": 0})
        with pytest.raises(ValidationError):
            schema.model_validate({"propertySchemaId": UUID_1, "name": "x", "type": "number", "numberDecimals": 11})
        with pytest.raises(ValidationError):
            schema.model_validate(
                {"propertySchemaId": UUID_1, "name": "x", "type": "number", "numberRounding": "sideways"}
            )

    def test_update_keep_vs_clear_via_fields_set(self) -> None:
        schema = PAYLOAD_SCHEMAS["propertySchema.update"]
        kept = schema.model_validate({"propertySchemaId": UUID_1})
        assert "number_pad" not in kept.model_fields_set
        cleared = schema.model_validate({"propertySchemaId": UUID_1, "numberPad": None})
        assert "number_pad" in cleared.model_fields_set
        assert cleared.number_pad is None


class TestPropertyDisplayPositions:
    """The render contracts ``display``/``readonly``/``hideWhenEmpty`` are
    PROPERTY-level (owner review, correcting the binding-level experiment) —
    they live on ``propertySchema.create/update``
    (nullable-optional; update-side absent keeps, null clears), and the
    strict ``class.property.set`` schema rejects all three like any retired
    key. ``required`` is the deliberate per-class exception that stays on
    the binding. The select option ``icon`` (MDI camelCase, max 64) stays on
    the deliberately NON-strict option record: unknown keys inside an option
    STRIP instead of rejecting, so icon-carrying envelopes sync through
    pre-batch parsers."""

    def test_display_valid_positions_parse_on_the_schema_payloads(self) -> None:
        for op_type in ("propertySchema.create", "propertySchema.update"):
            schema = PAYLOAD_SCHEMAS[op_type]
            base = {"propertySchemaId": UUID_1}
            if op_type == "propertySchema.create":
                base["name"] = "stage"
                base["type"] = "select"
            for position in ("panel", "bullet", "inline"):
                payload = schema.model_validate({**base, "display": position})
                assert payload.display == position
                assert "display" in payload.model_fields_set
            # Nullable — an explicit null parses (the update-side
            # applier clears the stored value).
            nulled = schema.model_validate({**base, "display": None})
            assert nulled.display is None
            assert "display" in nulled.model_fields_set
            minimal = schema.model_validate(base)
            assert minimal.display is None
            assert "display" not in minimal.model_fields_set

    def test_display_unknown_position_rejected_on_the_schema_payloads(self) -> None:
        with pytest.raises(ValidationError, match="display"):
            validate_payload(
                "propertySchema.create",
                {"propertySchemaId": UUID_1, "name": "stage", "type": "select", "display": "sideways"},
            )
        with pytest.raises(ValidationError, match="display"):
            validate_payload("propertySchema.update", {"propertySchemaId": UUID_1, "display": "sideways"})

    @pytest.mark.parametrize("key", ["display", "readonly", "hideWhenEmpty"])
    def test_class_property_set_render_contract_keys_rejected(self, key: str) -> None:
        """The binding payload carries ONLY the genuinely per-class
        mechanics — display/readonly/hideWhenEmpty are retired keys there,
        rejected outright by the strict schema (required stays)."""
        with pytest.raises(ValidationError, match=key):
            validate_payload(
                "class.property.set",
                {"classId": UUID_1, "propertySchemaId": UUID_2, key: "bullet" if key == "display" else True},
            )

    def test_schema_render_contract_flags_parse_on_create_and_update(self) -> None:
        create = PAYLOAD_SCHEMAS["propertySchema.create"].model_validate(
            {
                "propertySchemaId": UUID_1,
                "name": "stage",
                "type": "select",
                "readonly": True,
                "hideWhenEmpty": False,
            }
        )
        assert create.readonly is True
        assert create.hide_when_empty is False
        update = PAYLOAD_SCHEMAS["propertySchema.update"].model_validate(
            {"propertySchemaId": UUID_1, "readonly": False, "hideWhenEmpty": True}
        )
        assert update.readonly is False
        assert update.hide_when_empty is True

    def test_option_icon_parses_on_create_and_update(self) -> None:
        create = PAYLOAD_SCHEMAS["propertySchema.create"].model_validate(
            {
                "propertySchemaId": UUID_1,
                "name": "stage",
                "type": "select",
                "options": [{"id": "a", "label": "A", "icon": "mdiCircle"}],
            }
        )
        assert create.options is not None
        assert create.options[0].icon == "mdiCircle"
        update = PAYLOAD_SCHEMAS["propertySchema.update"].model_validate(
            {"propertySchemaId": UUID_1, "options": [{"id": "a", "label": "A", "icon": "mdiCircle"}]}
        )
        assert update.options is not None
        assert update.options[0].icon == "mdiCircle"
        with pytest.raises(ValidationError):
            PAYLOAD_SCHEMAS["propertySchema.create"].model_validate(
                {
                    "propertySchemaId": UUID_1,
                    "name": "x",
                    "type": "select",
                    "options": [{"id": "a", "label": "A", "icon": "x" * 65}],
                }
            )

    def test_option_record_is_non_strict_unknown_keys_strip(self) -> None:
        """The convergence contract: an option carrying keys a
        pre-batch parser does not know (the color grammar, a future
        decoration) validates — the parsed model DROPS the unknown keys
        instead of rejecting the envelope."""
        payload = PAYLOAD_SCHEMAS["propertySchema.create"].model_validate(
            {
                "propertySchemaId": UUID_1,
                "name": "stage",
                "type": "select",
                "options": [{"id": "a", "label": "A", "icon": "mdiCircle", "color": "yellow", "futureKey": 1}],
            }
        )
        option = payload.options[0]
        assert option.icon == "mdiCircle"
        assert set(option.model_dump()) == {"id", "label", "icon"}  # stripped, never rejected
