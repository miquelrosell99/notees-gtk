"""QueryAST color groups — the ``colorGroups.ts`` port.

Each group is a query-AST root group (the v2 grammar — the same conditions
the query surfaces run) plus a color; nodes matching a group render in the
group's color. The persisted list order IS the first-match-wins priority; a
node with no group keeps its own color. Evaluation wraps the group in the
workspace-scope AST and rides the client's own query runner
(:meth:`LocalStore.run_query_ast`), so groups see exactly what the query
surfaces see.

Persistence is the AST itself (JSON-serializable; the device-local cache
lives in ``ui.config_store``). Two one-time migrations run per device at
load (:func:`migrate_color_group`, the web's at-load precedent): a legacy
row holding a text-DSL ``query`` string parses through the slice-2 DSL (names
resolve against the workspace), and a row holding a v1 query AST rewrites
through the pure v1→v2 mapping (``core.query.ast_v1_map`` — no dual-parse
anywhere). Fail-loud errors keep the group but mark it erroneous, painted
nowhere — the same honesty the web's ``error`` row had.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from notees_gtk.core.protocol.query_ast import (
    EntireWorkspaceScope,
    Group,
    QueryAst,
    dump_query_ast,
    parse_query_ast,
)
from notees_gtk.core.query.ast_v1_map import map_query_ast_v1_to_v2
from notees_gtk.core.query.execute import QueryResult

__all__ = [
    "ColorGroupStore",
    "GraphColorGroup",
    "color_group_query_ast",
    "evaluate_color_groups",
    "group_to_raw",
    "is_legacy_color_group",
    "is_v2_condition_tree",
    "load_color_groups",
    "migrate_color_group",
]


@runtime_checkable
class ColorGroupStore(Protocol):
    """The narrow mirror surface color groups ride — the concrete
    :class:`~notees_gtk.data.store.LocalStore` satisfies it."""

    def run_query_ast(
        self,
        workspace_id: str,
        raw_ast: object,
        *,
        now: datetime | None = None,
        self_node_id: str | None = None,
    ) -> QueryResult: ...

    def parse_query_language(self, workspace_id: str, text: str) -> QueryAst: ...


@dataclass(frozen=True)
class GraphColorGroup:
    """One color group: a label, a preset token or ``#RRGGBB`` color, and
    the query-AST root group selecting the group's nodes (None when the
    legacy migration failed — the group stays listed, painted nowhere)."""

    id: str
    label: str
    color: str
    ast: Group | None
    error: str | None = None


#: The v2 condition types — a stored group tree is v2 when every condition
#: rides one (``colorGroups.ts`` ``V2_CONDITION_TYPES``).
_V2_CONDITION_TYPES = frozenset({"member", "content", "name", "property", "date", "nodeField"})


def is_legacy_color_group(raw: object) -> bool:
    """True when a stored row is a pre-AST legacy group (a ``query`` string,
    no AST) — ``isLegacyColorGroup`` parity."""
    return isinstance(raw, dict) and isinstance(raw.get("query"), str)


def is_v2_condition_tree(children: Any) -> bool:
    """Recursive walk: true when every condition in the tree rides a v2
    shape (``isV2ConditionTree`` parity — groups and nots recurse)."""
    if not isinstance(children, list):
        return False
    for child in children:
        if not isinstance(child, dict):
            return False
        child_type = child.get("type")
        if child_type == "group":
            if not is_v2_condition_tree(child.get("children")):
                return False
        elif child_type == "not":
            inner = child.get("child")
            if isinstance(inner, dict) and inner.get("type") == "group":
                if not is_v2_condition_tree(inner.get("children")):
                    return False
            elif not isinstance(inner, dict) or inner.get("type") not in _V2_CONDITION_TYPES:
                return False
        elif child_type not in _V2_CONDITION_TYPES:
            return False
    return True


def color_group_query_ast(root: Group) -> QueryAst:
    """Wrap a group's root in the workspace-scope AST the query runner
    consumes (``colorGroupQueryAst`` parity)."""
    return QueryAst(scope=EntireWorkspaceScope(), root=root)


def _strict_root(raw_root: Any) -> Group:
    """Validate a raw (migrated or stored) root group through the strict v2
    parser — the fail-loud gate; raises ``QueryAstError`` on any non-v2
    shape."""
    envelope = {"version": 2, "scope": {"type": "entire_workspace"}, "root": raw_root}
    return parse_query_ast(envelope).root


def migrate_color_group(store: ColorGroupStore, workspace_id: str, raw: object) -> GraphColorGroup:
    """The one-time legacy migrations, run per device at load (``migrateColorGroup``
    parity).

    A text-DSL ``query`` string parses through the DSL (names resolve against
    the workspace through the store's parse seam) and the group re-persists
    as an AST. A stored v1 AST rewrites through the pure v1→v2 mapping. A
    fail-loud error KEEPS the group but marks it erroneous (``ast: None`` +
    the message). Already-migrated (v2) rows pass through the strict parse.
    Malformed rows (missing id/label/color) raise — the device cache is the
    caller's to sanitize.
    """
    if is_legacy_color_group(raw):
        row = raw
        assert isinstance(row, dict)
        group_id = _require_str(row, "id")
        label = _require_str(row, "label")
        color = _require_str(row, "color")
        try:
            parsed = store.parse_query_language(workspace_id, str(row["query"]))
            return GraphColorGroup(id=group_id, label=label, color=color, ast=parsed.root)
        except Exception as exc:  # noqa: BLE001 — the group survives, painted nowhere
            return GraphColorGroup(id=group_id, label=label, color=color, ast=None, error=str(exc))
    if not isinstance(raw, dict):
        raise ValueError(f"color group row must be an object, got {type(raw).__name__}")
    group_id = _require_str(raw, "id")
    label = _require_str(raw, "label")
    color = _require_str(raw, "color")
    raw_ast = raw.get("ast")
    if raw_ast is None:
        # The erroneous-row reload: the failure is cached on the row.
        error = raw.get("error")
        return GraphColorGroup(
            id=group_id,
            label=label,
            color=color,
            ast=None,
            error=str(error) if error is not None else None,
        )
    if not is_v2_condition_tree(raw_ast.get("children") if isinstance(raw_ast, dict) else None):
        try:
            mapped = map_query_ast_v1_to_v2(
                {"version": 1, "scope": {"type": "entire_workspace"}, "root": raw_ast}
            )
            return GraphColorGroup(
                id=group_id, label=label, color=color, ast=_strict_root(mapped["root"])
            )
        except Exception as exc:  # noqa: BLE001 — keep the group, paint nowhere
            return GraphColorGroup(id=group_id, label=label, color=color, ast=None, error=str(exc))
    try:
        return GraphColorGroup(id=group_id, label=label, color=color, ast=_strict_root(raw_ast))
    except Exception as exc:  # noqa: BLE001 — keep the group, paint nowhere
        return GraphColorGroup(id=group_id, label=label, color=color, ast=None, error=str(exc))


def _require_str(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"color group row needs a non-empty {key!r}")
    return value


def group_to_raw(group: GraphColorGroup) -> dict[str, Any]:
    """Serialize a group for the device cache — the AST itself (the dumped
    v2 root group), the error cached so a reload never re-runs a failing
    migration."""
    raw: dict[str, Any] = {
        "id": group.id,
        "label": group.label,
        "color": group.color,
        "ast": dump_query_ast(color_group_query_ast(group.ast))["root"] if group.ast is not None else None,
    }
    if group.error is not None:
        raw["error"] = group.error
    return raw


def load_color_groups(store: ColorGroupStore, workspace_id: str, raws: object) -> list[GraphColorGroup]:
    """Migrate every stored row at load (the at-load migration precedent).
    A non-list cache reads as no groups — the honest answer for a garbled
    device file; malformed rows fail loud (the caller's cache layer decides
    whether to drop or surface)."""
    if raws is None:
        return []
    if not isinstance(raws, list):
        raise ValueError(f"stored color groups must be a list, got {type(raws).__name__}")
    return [migrate_color_group(store, workspace_id, raw) for raw in raws]


def evaluate_color_groups(
    store: ColorGroupStore,
    workspace_id: str,
    groups: list[GraphColorGroup],
    *,
    self_node_id: str | None = None,
) -> dict[str, str]:
    """Evaluate the groups in list order — returns nodeId → color for the
    FIRST group each node matches (the list order IS the priority).
    Erroneous (unmigrated) groups and empty groups match nothing; a runner
    failure skips that group (the web's ``catch { continue }`` parity).
    """
    assignment: dict[str, str] = {}
    for group in groups:
        if group.ast is None or not group.ast.children:
            continue
        try:
            result = store.run_query_ast(
                workspace_id,
                dump_query_ast(color_group_query_ast(group.ast)),
                self_node_id=self_node_id,
            )
        except Exception:  # noqa: BLE001 — one broken group paints nothing, never the rest
            continue
        for node_id in result.ids:
            if node_id not in assignment:
                assignment[node_id] = group.color
    return assignment
