"""The graph view's headless layers — the topology projection, the
deterministic layout, and the color-group model (the web's
``apps/web/src/features/collection/graph/`` reduced port). No GTK imports.
"""

from notees_gtk.core.graph.color_groups import (
    GraphColorGroup,
    color_group_query_ast,
    evaluate_color_groups,
    is_legacy_color_group,
    load_color_groups,
    migrate_color_group,
)
from notees_gtk.core.graph.layout import layout_topology
from notees_gtk.core.graph.model import (
    ALIAS_GRAPH_DEPTH_CAP,
    GRAPH_EDGE_KINDS,
    GraphEdge,
    GraphNode,
    GraphTopology,
    NodeSetRow,
    build_topology,
    topology_counts,
)
from notees_gtk.core.graph.viewport import GraphViewport

__all__ = [
    "ALIAS_GRAPH_DEPTH_CAP",
    "GRAPH_EDGE_KINDS",
    "GraphColorGroup",
    "GraphEdge",
    "GraphNode",
    "GraphTopology",
    "GraphViewport",
    "NodeSetRow",
    "build_topology",
    "color_group_query_ast",
    "evaluate_color_groups",
    "is_legacy_color_group",
    "layout_topology",
    "load_color_groups",
    "migrate_color_group",
    "topology_counts",
]
