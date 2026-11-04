"""The deterministic force-directed layout — pins on the zero-randomness
contract (same topology in, byte-same positions out; input row order is
irrelevant) and on basic force sanity (edges end shorter than they began,
components keep their distance, output is always finite).
"""

from __future__ import annotations

import math
import random

from notees_gtk.core.graph.layout import layout_topology
from notees_gtk.core.graph.model import GraphEdge, GraphNode, GraphTopology


def node(node_id: str) -> GraphNode:
    return GraphNode(
        id=node_id,
        is_class=False,
        parent_id=None,
        class_ids=(),
        color=None,
        icon=None,
        created_at=None,
        degree=0,
    )


def topology(node_ids: list[str], edges: list[tuple[str, str, str, int]]) -> GraphTopology:
    degree = dict.fromkeys(node_ids, 0)
    graph_edges = []
    for source, target, kind, weight in edges:
        degree[source] += 1
        degree[target] += 1
        graph_edges.append(GraphEdge(source=source, target=target, kind=kind, weight=weight, created_at=None))
    return GraphTopology(
        nodes=tuple(
            GraphNode(
                id=n.id,
                is_class=n.is_class,
                parent_id=n.parent_id,
                class_ids=n.class_ids,
                color=n.color,
                icon=n.icon,
                created_at=n.created_at,
                degree=degree[n.id],
            )
            for n in (node(node_id) for node_id in node_ids)
        ),
        edges=tuple(graph_edges),
    )


class TestDeterminism:
    def test_same_topology_yields_identical_positions(self) -> None:
        top = topology(["a", "b", "c", "d"], [("a", "b", "mention", 1), ("b", "c", "tag", 2), ("c", "d", "parent", 1)])
        first = layout_topology(top)
        second = layout_topology(top)
        assert first == second
        assert set(first) == {"a", "b", "c", "d"}

    def test_input_row_order_is_irrelevant(self) -> None:
        nodes = [f"n{i:02d}" for i in range(12)]
        edges = [(nodes[i], nodes[i + 1], "mention", 1) for i in range(len(nodes) - 1)]
        edges += [(nodes[0], nodes[6], "tag", 1), (nodes[3], nodes[9], "class", 1)]
        reference = layout_topology(topology(nodes, edges))
        shuffled_nodes = list(nodes)
        shuffled_edges = list(edges)
        random.Random(7).shuffle(shuffled_nodes)
        random.Random(8).shuffle(shuffled_edges)
        assert layout_topology(topology(shuffled_nodes, shuffled_edges)) == reference

    def test_positions_are_finite(self) -> None:
        nodes = [f"n{i}" for i in range(20)]
        edges = [(nodes[i], nodes[j], "mention", 1) for i in range(20) for j in range(i + 1, 20) if (i + j) % 3 == 0]
        positions = layout_topology(topology(nodes, edges))
        assert len(positions) == 20
        for x, y in positions.values():
            assert math.isfinite(x) and math.isfinite(y)


class TestForceSanity:
    def test_connected_pair_converges_closer_than_it_started(self) -> None:
        top = topology(["a", "b"], [("a", "b", "mention", 1)])
        positions = layout_topology(top)
        final = math.dist(positions["a"], positions["b"])
        initial = math.dist(layout_topology(top, iterations=0)["a"], layout_topology(top, iterations=0)["b"])
        assert final < initial

    def test_empty_and_single_node_topologies(self) -> None:
        assert layout_topology(topology([], [])) == {}
        single = layout_topology(topology(["only"], []))
        assert single == {"only": (0.0, 0.0)}

    def test_disconnected_components_do_not_collapse_onto_each_other(self) -> None:
        nodes = ["a1", "a2", "b1", "b2"]
        edges = [("a1", "a2", "mention", 1), ("b1", "b2", "mention", 1)]
        positions = layout_topology(topology(nodes, edges))

        def component_midpoint(component: list[str]) -> tuple[float, float]:
            xs = [positions[node_id][0] for node_id in component]
            ys = [positions[node_id][1] for node_id in component]
            return (sum(xs) / len(xs), sum(ys) / len(ys))

        within_a = math.dist(positions["a1"], positions["a2"])
        midpoint_distance = math.dist(component_midpoint(["a1", "a2"]), component_midpoint(["b1", "b2"]))
        assert midpoint_distance > within_a
