"""The graph-view topology projection (the ``packages/store/src/graph.ts``
port, reduced per the minimal-surface ruling).

One derived, read-only projection over the mirror:

- the node set is classes + standalone (``is_embedded = 0``) pages ONLY —
  the blocks-never-render law; node aliases are not vertices either; a
  parentless member of an effectively parentlessRole-"block" class is
  excluded (the class-level render contract, extends-aware via
  ``class_hierarchy``);
- every edge endpoint rolls up to the node set via its nearest node-set
  ancestor: the walk steps THROUGH embedded parents (a mention authored on a
  block lands between the block's page and the target) and through aliases
  (an endpoint resolves to the alias-terminal, never the alias);
- five structural edge families: ``mention`` / ``property`` / ``tag`` from
  the derived edge index (targets ride the applier-materialized
  ``resolved_target_id`` — the alias terminal), ``parent`` (main-node
  parentage within the set), ``class`` (OR-Set membership, present rows
  only); parallels merge into one weighted edge whose stamp is the earliest
  contributing row's.

Deviations from the TS reference (each deliberate — the deferred slice's
minimal-surface ruling): the ``semantic`` / ``temporal`` co-occurrence
families, the ``mass`` / ``contentSize`` metrics, the per-node ``color`` /
``icon`` chrome fields' consumers, and the ``anchor``/``depth`` local scope
are NOT ported; nodes carry their graph ``degree`` instead. The input is
never mutated; output ordering is deterministic (nodes by id, edges by
kind/source/target) so every replica renders identically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "ALIAS_GRAPH_DEPTH_CAP",
    "GraphEdge",
    "GraphEdgeKind",
    "GraphNode",
    "GraphTopology",
    "NodeSetRow",
    "build_topology",
]

#: The alias-chain cap for the in-memory terminal walk (the store walker's
#: precedent — chains are user-built and tiny; the cap bounds pathological
#: data that predates the write-time acyclicity check).
ALIAS_GRAPH_DEPTH_CAP = 32

#: The five structural edge families (``graph.ts`` ``GraphEdgeKind`` minus
#: the unported co-occurrence families).
GraphEdgeKind = str
GRAPH_EDGE_KINDS: tuple[str, ...] = ("mention", "property", "tag", "parent", "class")


@dataclass(frozen=True)
class NodeSetRow:
    """One node-set member as the store seam hands it over."""

    id: str
    is_class: bool
    parent_id: str | None
    class_ids: tuple[str, ...]
    color: str | None
    icon: str | None
    created_at: str | None


@dataclass(frozen=True)
class GraphNode:
    """A graph vertex: a class or a standalone page."""

    id: str
    is_class: bool
    parent_id: str | None
    class_ids: tuple[str, ...]
    color: str | None
    icon: str | None
    created_at: str | None
    degree: int


@dataclass(frozen=True)
class GraphEdge:
    """A weighted edge between two vertices; ``weight`` is the merged
    multiplicity of the contributing rows (the earliest row's stamp)."""

    source: str
    target: str
    kind: GraphEdgeKind
    weight: int
    created_at: str | None


@dataclass(frozen=True)
class GraphTopology:
    """The full projection: the vertex set + the weighted edge list."""

    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]


def _earlier_stamp(a: str | None, b: str | None) -> str | None:
    """Earliest non-null ISO stamp (lexical order = chronological for the UTC
    wire format); a null never wins (``graph.ts`` ``earlierStamp``)."""
    if a is None:
        return b
    if b is None:
        return a
    return a if a <= b else b


def build_topology(
    *,
    node_rows: list[NodeSetRow],
    edge_rows: list[tuple[str, str, str, str | None]],
    membership_rows: list[tuple[str, str]],
    active_rows: list[tuple[str, str | None, bool, bool]],
    alias_rows: list[tuple[str, str]],
) -> GraphTopology:
    """Project the graph topology from raw mirror rows (all rows already
    workspace-scoped by the store seam).

    Args:
        node_rows: The node-set members (classes + standalone pages).
        edge_rows: The structural reference edges — ``(source_id, target_id,
            type, created_at)`` rows of types mention/property/tag with a
            non-null resolved target.
        membership_rows: ``(node_id, class_id)`` present OR-Set rows.
        active_rows: Every live node — ``(id, parent_id, is_embedded,
            is_class)`` — the rollup/metric maps walk parents through blocks.
        alias_rows: Every alias mapping — ``(id, aliased_node_id)``.
    """
    in_set = {row.id for row in node_rows}

    children_of: dict[str, list[str]] = {}
    is_main_child: dict[str, bool] = {}
    parent_of: dict[str, str | None] = {}
    for node_id, parent_id, is_embedded, is_class in active_rows:
        if parent_id is not None:
            children_of.setdefault(parent_id, []).append(node_id)
        is_main_child[node_id] = is_embedded is False or is_class is True
        parent_of[node_id] = parent_id
    alias_target_of = dict(alias_rows)

    def terminal_of(node_id: str) -> str:
        """The alias-terminal of ``node_id`` (self when not an alias) —
        cycle-safe, depth-capped (``graph.ts`` ``terminalOf``)."""
        current = node_id
        seen: set[str] = set()
        for _depth in range(ALIAS_GRAPH_DEPTH_CAP):
            target = alias_target_of.get(current)
            if target is None or target in seen:
                break
            seen.add(current)
            current = target
        return current

    resolve_cache: dict[str, str | None] = {}

    def resolve_to_set(node_id: str) -> str | None:
        """Nearest node-set ancestor, stepping THROUGH aliases: blocks roll
        up; node-set nodes resolve to self; an alias resolves to its
        terminal. None when the walk leaves the live tree (a parentless
        block has no graph node above it)."""
        cached = resolve_cache.get(node_id)
        if cached is not None or node_id in resolve_cache:
            return cached
        current: str | None = node_id
        seen: set[str] = set()
        result: str | None = None
        while current is not None and current not in seen:
            seen.add(current)
            if current in in_set:
                result = current
                break
            alias_target = alias_target_of.get(current)
            if alias_target is not None:
                current = alias_target
                continue
            current = parent_of.get(current)
        for visited in seen:
            resolve_cache[visited] = result
        return result

    edges: list[GraphEdge] = []
    edge_index: dict[tuple[str, str, str], int] = {}

    def add_edge(source: str, target: str, kind: str, created_at: str | None) -> None:
        """Roll both endpoints up to the node set; drop self-loops and
        out-of-tree endpoints; merge parallels into one weighted edge with
        the earliest contributing stamp."""
        if kind not in GRAPH_EDGE_KINDS:
            return
        from_id = resolve_to_set(source)
        to_id = resolve_to_set(target)
        if from_id is None or to_id is None or from_id == to_id:
            return
        key = (kind, from_id, to_id)
        position = edge_index.get(key)
        if position is not None:
            existing = edges[position]
            edges[position] = GraphEdge(
                source=existing.source,
                target=existing.target,
                kind=existing.kind,
                weight=existing.weight + 1,
                created_at=_earlier_stamp(existing.created_at, created_at),
            )
            return
        edge_index[key] = len(edges)
        edges.append(GraphEdge(source=from_id, target=to_id, kind=kind, weight=1, created_at=created_at))

    # Structural reference families (mention/property/tag) from the edge
    # index — multiplicity rides in the weight.
    for source_id, target_id, edge_type, created_at in edge_rows:
        add_edge(source_id, target_id, edge_type, created_at)

    # Parent family: main-node parentage within the set (no underlying row
    # stamp — the family is synthesized).
    for row in node_rows:
        if row.parent_id is not None and row.parent_id in in_set:
            add_edge(row.parent_id, row.id, "parent", None)

    # Class family: OR-Set membership (present rows only), both ends in the
    # set.
    for node_id, class_id in membership_rows:
        if node_id in in_set and class_id in in_set:
            add_edge(node_id, class_id, "class", None)

    degree: dict[str, int] = {row.id: 0 for row in node_rows}
    for edge in edges:
        degree[edge.source] += 1
        degree[edge.target] += 1

    nodes = tuple(
        GraphNode(
            id=row.id,
            is_class=row.is_class,
            parent_id=row.parent_id,
            class_ids=row.class_ids,
            color=row.color,
            icon=row.icon,
            created_at=row.created_at,
            degree=degree[row.id],
        )
        for row in sorted(node_rows, key=lambda r: r.id)
    )
    ordered_edges = tuple(sorted(edges, key=lambda e: (e.kind, e.source, e.target)))
    return GraphTopology(nodes=nodes, edges=ordered_edges)


def topology_counts(topology: GraphTopology) -> dict[str, Any]:
    """Honest counts for a chrome status line (``graphCounts`` parity)."""
    return {"nodes": len(topology.nodes), "edges": len(topology.edges)}
