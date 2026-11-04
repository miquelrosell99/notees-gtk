"""The pure v1→v2 query-AST mapping — the ``scripts/lib/map-query-ast-v1-v2.ts``
port (the mechanical half of the query-AST v2 migration).

Walks a v1 AST (Group / Not / nested-query trees) and returns its v2 image:
every v1 condition variant lands on exactly one v2 shape per the mapping
table — the v1 family hats (``descendantOf`` / ``*Any`` / ``*Query`` /
``*Self`` / ``*Set``) become ``member`` conditions with the family/op/target
axes made explicit; ``contentSet``, ``propertyBetween``, ``propertyWithin``,
the ten ``created*``/``updated*`` types, and ``isClass``/``isEmbedded`` fold
into their parameterized v2 types; ``not`` wrappers, group logic, and the
scope pass through unchanged.

Pure data-in/data-out by design: imports nothing from the query model (the
migration feeds it v1 data, and the strict parser validates the rewritten
envelopes separately). Unknown shapes FAIL LOUD with the offending type and
the tree path; the function is total over the v1 grammar and raises on
everything else — including v2 input, which must NOT be re-mapped (the
migration is one-time; double-application is a bug, not a no-op).
"""

from __future__ import annotations

from typing import Any, NoReturn, cast

__all__ = ["QueryAstV1MapError", "is_query_ast_v1", "map_query_ast_v1_to_v2"]

#: Every v1 condition ``type`` → its mapping spec (the table IS the contract,
#: ``map-query-ast-v1-v2.ts`` ``V1_MEMBER_SPECS`` verbatim): (kind, family,
#: id/ids key — the key is None for the query/self/set kinds).
_V1_MEMBER_SPECS: dict[str, tuple[str, str, str | None]] = {
    # kind, family, id/ids key — kinds: one | many | query | self | set | exact
    "class": ("one", "class", "classId"),
    "classAny": ("many", "class", "classIds"),
    "classAnyQuery": ("query", "class", None),
    "classSelf": ("self", "class", None),
    "classSet": ("set", "class", None),
    "classExact": ("exact", "class", None),
    "tag": ("one", "tags", "tagId"),
    "tagAny": ("many", "tags", "tagIds"),
    "tagAnyQuery": ("query", "tags", None),
    "tagSelf": ("self", "tags", None),
    "tagSet": ("set", "tags", None),
    "linkedTo": ("one", "links", "nodeId"),
    "linkedToAny": ("many", "links", "nodeIds"),
    "linkedToQuery": ("query", "links", None),
    "linkedToSelf": ("self", "links", None),
    "linkSet": ("set", "links", None),
    "referencePathOf": ("many", "referencePath", "nodeIds"),
    "referencePathOfQuery": ("query", "referencePath", None),
    "referencePathOfSelf": ("self", "referencePath", None),
    "parentOf": ("many", "parent", "nodeIds"),
    "parentOfQuery": ("query", "parent", None),
    "parentOfSelf": ("self", "parent", None),
    "parentSet": ("set", "parent", None),
    "childOf": ("many", "child", "nodeIds"),
    "childOfQuery": ("query", "child", None),
    "childOfSelf": ("self", "child", None),
    "childSet": ("set", "child", None),
    "descendantOf": ("one", "childPath", "nodeId"),
    "descendantOfAny": ("many", "childPath", "nodeIds"),
    "descendantOfQuery": ("query", "childPath", None),
    "descendantOfSelf": ("self", "childPath", None),
    "descendantSet": ("set", "childPath", None),
    "pageOf": ("many", "page", "nodeIds"),
    "pageOfQuery": ("query", "page", None),
    "pageOfSelf": ("self", "page", None),
    "pageSet": ("set", "page", None),
    "ancestorIn": ("many", "parentPath", "nodeIds"),
    "ancestorInQuery": ("query", "parentPath", None),
    "ancestorInSelf": ("self", "parentPath", None),
    "rootIn": ("many", "root", "nodeIds"),
    "rootInQuery": ("query", "root", None),
    "rootInSelf": ("self", "root", None),
}

#: The v1 date-condition name → (field, op).
_V1_DATE_TYPES: dict[str, tuple[str, str]] = {
    "createdAfter": ("created", "after"),
    "createdBefore": ("created", "before"),
    "createdCmp": ("created", "cmp"),
    "createdBetween": ("created", "between"),
    "createdWithin": ("created", "within"),
    "updatedAfter": ("updated", "after"),
    "updatedBefore": ("updated", "before"),
    "updatedCmp": ("updated", "cmp"),
    "updatedBetween": ("updated", "between"),
    "updatedWithin": ("updated", "within"),
}


class QueryAstV1MapError(ValueError):
    """A v1 AST node failed the mapping — the message carries the tree path."""

    def __init__(self, message: str, path: str) -> None:
        super().__init__(f"{message} (at {path})")
        self.path = path


def _is_object(value: Any) -> bool:
    return isinstance(value, dict)


def _fail(path: str, message: str) -> NoReturn:
    raise QueryAstV1MapError(message, path)


def _expect_object(value: Any, path: str) -> dict[str, Any]:
    if not _is_object(value):
        _fail(path, f"expected an object, got {value!r}")
    return cast(dict[str, Any], value)


def _expect_string(value: Any, path: str, what: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(path, f"expected a non-empty string for {what}")
    return value


def _expect_string_array(value: Any, path: str, what: str) -> list[str]:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
        _fail(path, f"expected a non-empty string array for {what}")
    return list(value)


def _map_member(kind: str, family: str, key: str | None, condition: dict[str, Any], path: str) -> dict[str, Any]:
    if kind == "one":
        assert key is not None
        node_id = _expect_string(condition.get(key), f"{path}.{key}", key)
        return {"type": "member", "family": family, "op": "in", "target": {"mode": "ids", "ids": [node_id]}}
    if kind == "many":
        assert key is not None
        ids = _expect_string_array(condition.get(key), f"{path}.{key}", key)
        return {"type": "member", "family": family, "op": "in", "target": {"mode": "ids", "ids": ids}}
    if kind == "query":
        return {
            "type": "member",
            "family": family,
            "op": "in",
            "target": {
                "mode": "query",
                "root": _map_group(_expect_object(condition.get("root"), f"{path}.root"), f"{path}.root"),
            },
        }
    if kind == "self":
        return {"type": "member", "family": family, "op": "in", "target": {"mode": "self"}}
    if kind == "set":
        return {"type": "member", "family": family, "op": "exists"}
    # exact (class-only): a single-id ids target under the exact op.
    node_id = _expect_string(condition.get("classId"), f"{path}.classId", "classId")
    return {"type": "member", "family": "class", "op": "exact", "target": {"mode": "ids", "ids": [node_id]}}


def _map_condition(condition: dict[str, Any], path: str) -> dict[str, Any]:
    condition_type = _expect_string(condition.get("type"), f"{path}.type", "condition type")
    spec = _V1_MEMBER_SPECS.get(condition_type)
    if spec is not None:
        return _map_member(spec[0], spec[1], spec[2], condition, path)

    if condition_type == "content":
        return {"type": "content", "op": condition.get("op"), "value": condition.get("value")}
    if condition_type == "contentSet":
        return {"type": "content", "op": "exists"}
    if condition_type == "name":
        return {"type": "name", "op": condition.get("op"), "value": condition.get("value")}
    if condition_type == "property":
        return {
            "type": "property",
            "schemaId": condition.get("schemaId"),
            "op": condition.get("op"),
            "value": condition.get("value"),
            "includeDefaults": condition.get("includeDefaults"),
        }
    if condition_type == "propertyBetween":
        return {
            "type": "property",
            "schemaId": condition.get("schemaId"),
            "op": "between",
            "from": condition.get("from"),
            "to": condition.get("to"),
        }
    if condition_type == "propertyWithin":
        return {
            "type": "property",
            "schemaId": condition.get("schemaId"),
            "op": "within",
            "period": condition.get("period"),
        }
    if condition_type in ("isClass", "isEmbedded"):
        value = condition.get(condition_type)
        if not isinstance(value, bool):
            _fail(f"{path}.{condition_type}", f"expected a boolean for {condition_type}")
        return {
            "type": "nodeField",
            "field": condition_type,
            "op": "eq",
            "value": "true" if value else "false",
        }
    if condition_type in ("coverAsset", "bannerAsset", "aliasedNode"):
        return {"type": "nodeField", "field": condition_type, "op": condition.get("op"), "value": condition.get("value")}
    _fail(path, f"unknown v1 condition type {condition_type!r}")


def _map_condition_by_type(condition: dict[str, Any], path: str) -> dict[str, Any]:
    condition_type = _expect_string(condition.get("type"), f"{path}.type", "condition type")
    date_shape = _V1_DATE_TYPES.get(condition_type)
    if date_shape is None:
        return _map_condition(condition, path)
    field, op = date_shape
    mapped: dict[str, Any] = {"type": "date", "field": field}
    if op == "cmp":
        mapped["op"] = condition.get("op")
        mapped["timestamp"] = condition.get("timestamp")
    else:
        mapped["op"] = op
        if op == "between":
            mapped["from"] = condition.get("from")
            mapped["to"] = condition.get("to")
        elif op == "within":
            mapped["period"] = condition.get("period")
        else:
            mapped["timestamp"] = condition.get("timestamp")
    return mapped


def _map_child(child: Any, path: str) -> dict[str, Any]:
    node = _expect_object(child, path)
    child_type = _expect_string(node.get("type"), f"{path}.type", "child type")
    if child_type == "group":
        return _map_group(node, path)
    if child_type == "not":
        inner = _expect_object(node.get("child"), f"{path}.child")
        inner_type = _expect_string(inner.get("type"), f"{path}.child.type", "not child type")
        return {
            "type": "not",
            "child": (
                _map_group(inner, f"{path}.child")
                if inner_type == "group"
                else _map_condition_by_type(inner, f"{path}.child")
            ),
        }
    return _map_condition_by_type(node, path)


def _map_group(group: dict[str, Any], path: str) -> dict[str, Any]:
    if group.get("type") != "group":
        _fail(path, f"expected a group, got type {group.get('type')!r}")
    if group.get("logic") not in ("and", "or"):
        _fail(f"{path}.logic", f"expected 'and' | 'or', got {group.get('logic')!r}")
    children = group.get("children")
    if not isinstance(children, list):
        _fail(f"{path}.children", "expected a children array")
    return {
        "type": "group",
        "logic": group["logic"],
        "children": [_map_child(child, f"{path}.children[{index}]") for index, child in enumerate(children)],
    }


def is_query_ast_v1(ast: Any) -> bool:
    """Structural v1 guard — true only for an object that IS a v1 AST shape
    (``isQueryAstV1`` parity; the device-side migrations use it to decide
    whether a stored AST needs the one-time rewrite)."""
    if not _is_object(ast) or ast.get("version") != 1:
        return False
    root = ast.get("root")
    return _is_object(ast.get("scope")) and _is_object(root) and root.get("type") == "group" and isinstance(
        root.get("children"), list
    )


def map_query_ast_v1_to_v2(ast: Any) -> dict[str, Any]:
    """Map a v1 query AST to its v2 image. Pure and total over the v1
    grammar: raises :class:`QueryAstV1MapError` (with the tree path) on
    anything else — including v2 input, which must NOT be re-mapped."""
    if not _is_object(ast):
        _fail("$", "expected a query AST object")
    if ast.get("version") != 1:
        _fail("$.version", f"expected version 1, got {ast.get('version')!r}")
    scope = ast.get("scope")
    if not _is_object(scope):
        _fail("$.scope", "expected a scope object")
    if not scope:
        _fail("$.scope", "scope must not be empty")
    mapped: dict[str, Any] = {
        "version": 2,
        "scope": dict(scope),
        "root": _map_group(_expect_object(ast.get("root"), "$.root"), "$.root"),
    }
    if "sort" in ast:
        mapped["sort"] = ast["sort"]
    if "aggregation" in ast:
        mapped["aggregation"] = ast["aggregation"]
    return mapped
