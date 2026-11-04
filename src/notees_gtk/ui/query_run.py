"""The live query-section run — the ``apps/web/src/ui/queryRun.ts`` port.

Headless-testable by design: no GTK imports here (the conventions keep pure
UI logic importable without the ``ui`` extra, exactly like
:mod:`notees_gtk.ui.ast_render`). The GTK chrome lives in
:mod:`notees_gtk.ui.query_section`.

Ports the web hook's contract, method for method:

- the run identity is the ``(AST key, version, selfNodeId)`` triple — the
  hosting page id is PART of the identity (the compiler's
  fail-loud-without-host contract for ``self`` member targets), so the
  section always carries it (the render plan's ``self_node_id``, the window's
  shown-node id);
- every store-notify bumps the version and re-runs, but a notification
  arriving while a run is in flight marks the run dirty and schedules
  exactly ONE trailing run — a burst costs at most two executions instead of
  one per envelope;
- a run whose triple is already the latest skips outright;
- the last result caches between runs; an invalid AST surfaces as the error
  state (the "invalid query" line), never a crash;
- the empty match-all is UNSPECIFIED (``is_unspecified_query_ast``): it
  renders the "Set a query to see results." state instead of loading the
  whole graph — the user has not yet said what they are looking for.

Runs are synchronous here (the mirror is a local SQLite query) — the
"in flight" window is the run call itself, which covers re-entrant notifies
(a run callable that itself writes the store) and cross-thread notifies
landing mid-run. The chrome coalesces worker-thread notify bursts onto the
main loop before they reach :meth:`QuerySectionRun.notify` (the React-tick
batching mirror).

Also here: :func:`open_query_result`, the render-cascade navigation for a
result row — document-chrome rows open directly; an inline block resolves
to its nearest document-chrome ancestor (the ancestor walk with the
parent-page condition, TS ``openQueryResult``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from notees_gtk.core.protocol.query_ast import (
    EntireWorkspaceScope,
    Group,
    QueryAst,
    parse_query_ast,
)
from notees_gtk.core.query.execute import AggregateResult, QueryResult
from notees_gtk.data.store import NodeRow

__all__ = [
    "AggregateRunState",
    "PlainRunState",
    "QueryRunState",
    "QuerySectionRun",
    "aggregate_column_label",
    "format_aggregate_cell",
    "is_unspecified_query_ast",
    "open_query_result",
    "parse_run_ast",
]

#: The run seam the chrome supplies — the ``run_query_ast``/``run_aggregate_ast``
#: store seams closed over workspace + lock (``QueryRunClient.runQueryAst``).
type QueryRunner = Callable[[object, str | None], QueryResult]
type AggregateRunner = Callable[[object, str | None], AggregateResult]
#: The store's ``subscribe`` seam: registers a listener, returns the unsubscribe.
type SubscribeFn = Callable[[Callable[[], None]], Callable[[], None]]


@dataclass(frozen=True)
class PlainRunState:
    """``kind: "query"`` — a plain run's ids + full rows."""

    result: QueryResult


@dataclass(frozen=True)
class AggregateRunState:
    """``kind: "aggregate"`` — the grouped grid + its dimension count."""

    result: AggregateResult
    dimension_count: int


#: The live run's cached state (the hook's ``QueryRunState``).
type QueryRunState = PlainRunState | AggregateRunState


def parse_run_ast(raw: object) -> QueryAst | None:
    """Loose-token parse for the render path: invalid ASTs surface as the
    error state instead of raising here (TS ``parseRunAst``)."""
    try:
        return parse_query_ast(raw)
    except ValueError:
        return None


def is_unspecified_query_ast(raw: object) -> bool:
    """True when the AST parses and specifies NOTHING: the entire-workspace
    scope, an AND group with zero conditions, no aggregation (TS
    ``isUnspecifiedQueryAst``). A scope-only AST (this page / pages) is a
    deliberate choice and still runs; an unparseable AST is not unspecified
    (the invalid-query state names it)."""
    parsed = parse_run_ast(raw)
    if parsed is None:
        return False
    if parsed.aggregation is not None:
        return False
    if not isinstance(parsed.scope, EntireWorkspaceScope):
        return False
    return isinstance(parsed.root, Group) and not parsed.root.children


def _ast_key(raw: object) -> str:
    """The run-identity AST key (``JSON.stringify(rawAst ?? null)``)."""
    return json.dumps(raw if raw is not None else None, ensure_ascii=False)


#: The aggregate-label prefix split (TS ``aggregateColumnLabel``'s regex).
_AGGREGATE_LABEL_RE = re.compile(r"^(class|property|sum|avg|min|max):(.+)$")


def aggregate_column_label(column: str, class_names: Mapping[str, str], property_names: Mapping[str, str]) -> str:
    """An aggregate column's readable label (TS ``aggregateColumnLabel``):
    the compiler's names are deterministic (``isClass``, ``isEmbedded``,
    ``count``, ``countDistinct``, ``<fn>:<id>``) but bare ids are unreadable —
    resolve class/property names from the class facts, falling back to a
    short id prefix. Unknown shapes stay raw."""
    if column == "isClass":
        return "Class"
    if column == "isEmbedded":
        return "Present as main"
    if column == "count":
        return "Count"
    if column == "countDistinct":
        return "Count distinct"
    match = _AGGREGATE_LABEL_RE.match(column)
    if match is None:
        return column
    kind, raw_id = match.group(1), match.group(2)
    if kind == "class":
        return class_names.get(raw_id) or f"Class {raw_id[:8]}"
    base = property_names.get(raw_id) or raw_id[:8]
    return base if kind == "property" else f"{kind}({base})"


def format_aggregate_cell(value: Any) -> str:
    """Grid cell rendering (TS ``formatAggregateCell``): null reads as a
    dash, objects as JSON."""
    if value is None:
        return "—"
    if isinstance(value, (Mapping, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


_UNSET: object = object()


@dataclass
class _Status:
    """The hook's ``status`` ref: the last EXECUTED triple + flight flags.

    ``self_node_id`` starts as the ``_UNSET`` sentinel (the ref's
    ``undefined``) so a real ``None`` host is distinguishable from "never
    ran"; ``ast_key``/``version`` start equally un-current.
    """

    running: bool = False
    dirty: bool = False
    ast_key: str | None = None
    version: int = -1
    self_node_id: str | None | object = _UNSET


class QuerySectionRun:
    """The coalesced live re-run over one query token's AST.

    Args:
        run_query: The plain-run seam (``store.run_query_ast`` closed over
            the workspace; receives the raw AST + the self node id).
        run_aggregate: The aggregation seam (``store.run_aggregate_ast``).
        subscribe: The store's change-notification seam; ``start`` registers
            :meth:`notify` and keeps the unsubscribe for :meth:`stop`.
        raw_ast: The token's own serialized ``queryAst`` (what the token
            carries — the default-tab vs hosted-view distinction is
            web-specific and does not exist here).
        self_node_id: The hosting page's node id — part of the run identity.
        enabled: ``False`` mirrors the unspecified-query arm: never run, and
            a stale cached result is cleared so a query emptied after a run
            renders nothing.

    The class is thread-agnostic (the web hook is too): the chrome
    marshals store notifies onto the GTK main loop before calling
    :meth:`notify`, so the run callables always execute on the owning
    thread.
    """

    def __init__(
        self,
        run_query: QueryRunner,
        run_aggregate: AggregateRunner,
        subscribe: SubscribeFn,
        *,
        raw_ast: object,
        self_node_id: str | None = None,
        enabled: bool = True,
    ) -> None:
        self._run_query = run_query
        self._run_aggregate = run_aggregate
        self._subscribe = subscribe
        self._raw_ast = raw_ast
        self._self_node_id = self_node_id
        self._enabled = enabled
        self._version = 0
        self._status = _Status()
        self._result: QueryRunState | None = None
        self._error: str | None = None
        self._unsubscribe: Callable[[], None] | None = None

    # ------------------------------------------------------------------ wiring

    def start(self) -> None:
        """Subscribe + run the initial pass (the hook's mount effect)."""
        if self._unsubscribe is None:
            self._unsubscribe = self._subscribe(self.notify)
        self._sync_run()

    def stop(self) -> None:
        """Unsubscribe (the widget teardown — a stopped section never runs)."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def update(
        self,
        *,
        raw_ast: object = _UNSET,
        self_node_id: str | None | object = _UNSET,
        enabled: bool | object = _UNSET,
    ) -> None:
        """An input changed (the effect's dependency arms): re-check the run."""
        if raw_ast is not _UNSET:
            self._raw_ast = raw_ast
        if self_node_id is not _UNSET:
            self._self_node_id = self_node_id  # type: ignore[assignment]
        if enabled is not _UNSET:
            self._enabled = enabled  # type: ignore[assignment]
        self._sync_run()

    def notify(self) -> None:
        """A store change: bump the version and re-run when not current."""
        self._version += 1
        self._sync_run()

    # ------------------------------------------------------------------ state

    @property
    def result(self) -> QueryRunState | None:
        """The last run's cached state (``None`` before the first run /
        while disabled / after an error)."""
        return self._result

    @property
    def error(self) -> str | None:
        """The last run's failure message (the "invalid query" line)."""
        return self._error

    @property
    def enabled(self) -> bool:
        """Whether the section runs at all (``False`` = the unspecified
        query — the chrome renders the empty state, never the run)."""
        return self._enabled

    # ------------------------------------------------------------------- core

    def _sync_run(self) -> None:
        """The effect body: skip when current, defer when in flight, else run."""
        current = self._status
        ast_key = _ast_key(self._raw_ast)
        if not self._enabled:
            # Unspecified query: never run, and clear a stale result so a
            # query emptied after a run renders nothing, not its last result.
            current.ast_key = ast_key
            current.version = self._version
            current.self_node_id = self._self_node_id
            self._result = None
            self._error = None
            return
        if (
            current.ast_key == ast_key
            and current.version == self._version
            and current.self_node_id == self._self_node_id
        ):
            return  # already current
        if current.running:
            current.dirty = True
            return
        current.running = True
        try:
            parsed = parse_run_ast(self._raw_ast)
            if parsed is not None and parsed.aggregation is not None:
                next_state: QueryRunState = AggregateRunState(
                    result=self._run_aggregate(self._raw_ast, self._self_node_id),
                    dimension_count=len(parsed.aggregation.dimensions),
                )
            else:
                # A null parse still takes the query arm — the strict parse
                # inside the seam raises and the catch surfaces the error,
                # exactly the web's shape.
                next_state = PlainRunState(result=self._run_query(self._raw_ast, self._self_node_id))
            current.ast_key = ast_key
            current.version = self._version
            current.self_node_id = self._self_node_id
            self._result = next_state
            self._error = None
        except Exception as exc:  # noqa: BLE001 — the run error is state, not a crash
            current.ast_key = ast_key
            current.version = self._version
            current.self_node_id = self._self_node_id
            self._result = None
            self._error = str(exc)
        finally:
            current.running = False
            if current.dirty:
                # One trailing run for the notifications that landed
                # mid-flight; bumping the version re-arms with a fresh pair.
                current.dirty = False
                self._version += 1
                self._sync_run()


# --------------------------------------------------------- open-on-activate


def _class_ids_of(raw: object) -> list[str]:
    """The row's class ids: the mirror stores the column as JSON text, the
    compiler's ``n.*`` projection hands it through verbatim."""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    if isinstance(raw, Sequence):
        return [str(item) for item in raw]
    return []


def _renders_with_chrome(
    *,
    is_class: bool,
    parent_id: str | None,
    is_embedded: bool,
    class_ids: Sequence[str] = (),
    parent_chrome: bool | None = None,
    parentless_block: Callable[[str], bool] | None = None,
) -> bool:
    """The pure role predicate over loose row fields (the domain's
    ``rendersWithDocumentChrome`` — SCHEMA.md "Node structure", Revision 12)."""
    if is_class:
        return False
    if parent_id is None:
        if parentless_block is None:
            return True
        return not any(parentless_block(class_id) for class_id in class_ids)
    if is_embedded:
        return False
    if parent_chrome is None:
        return True
    return parent_chrome


def _node_chrome(
    node: NodeRow,
    get_node: Callable[[str], NodeRow | None],
    parentless_block: Callable[[str], bool] | None,
) -> bool:
    parent = get_node(node.parent_id) if node.parent_id is not None else None
    return _renders_with_chrome(
        is_class=node.is_class,
        parent_id=node.parent_id,
        is_embedded=node.is_embedded,
        class_ids=node.class_ids,
        parent_chrome=None if parent is None else _node_chrome(parent, get_node, parentless_block),
        parentless_block=parentless_block,
    )


def open_query_result(
    row: Mapping[str, Any],
    get_node: Callable[[str], NodeRow | None],
    *,
    parentless_block: Callable[[str], bool] | None = None,
) -> str:
    """The render-cascade navigation target for a query result row (TS
    ``openQueryResult``): document-chrome rows (pages, classes are NOT —
    the predicate's first arm) open directly; an inline block resolves to
    its containing main node (nearest document-chrome ancestor, the
    parent-page condition respected on every step).

    Args:
        row: One ``n.*`` result row (``id``, ``parent_id``, ``is_class``,
            ``is_embedded``, ``class_ids`` at minimum).
        get_node: The store's node read, closed over workspace + lock.
        parentless_block: The parentlessRole "block" demote resolver (the
            class facts read); ``None` treats every parentless non-class node
            as document chrome (the domain function's undefined arm).
    """
    node_id = str(row.get("id") or "")
    parent_id = row.get("parent_id")
    parent = get_node(str(parent_id)) if parent_id else None
    row_chrome = _renders_with_chrome(
        is_class=bool(row.get("is_class")),
        parent_id=str(parent_id) if parent_id else None,
        is_embedded=bool(row.get("is_embedded")),
        class_ids=_class_ids_of(row.get("class_ids")),
        parent_chrome=None if parent is None else _node_chrome(parent, get_node, parentless_block),
        parentless_block=parentless_block,
    )
    if row_chrome:
        return node_id
    # Ancestor walk with the parent-page condition: a bit under a non-page
    # parent is inert, so the walk continues past it to the real page.
    seen = {node_id}
    current = parent
    while current is not None and current.id not in seen:
        if _node_chrome(current, get_node, parentless_block):
            return current.id
        seen.add(current.id)
        current = get_node(current.parent_id) if current.parent_id is not None else None
    return node_id
