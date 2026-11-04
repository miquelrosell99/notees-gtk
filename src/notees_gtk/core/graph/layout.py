"""The deterministic force-directed layout — a Fruchterman–Reingold pass
over the topology projection, the web engine's minimal stand-in
(``apps/web/src/features/collection/graph/engine/`` runs a Barnes–Hut
simulation on a worker with seeded RNG; the GTK surface needs nothing that
heavy, but it DOES need zero randomness drift: same topology in, byte-same
positions out, on every run and every machine).

Determinism contract:

- vertices are processed in sorted-id order everywhere (forces accumulate
  over sorted adjacency), so the input's row order is irrelevant;
- the initial placement is a golden-angle spiral (``i * 2.399963...``,
  ``r = spacing * sqrt(i + 1)``) — no RNG at all, and insertion-stable:
  adding one vertex does not reshuffle the spiral's earlier points;
- the temperature cools linearly to zero over the fixed iteration count.

Pure function — no store, no GTK, no clock.
"""

from __future__ import annotations

import math

from notees_gtk.core.graph.model import GraphTopology

__all__ = ["DEFAULT_ITERATIONS", "layout_topology"]

#: The fixed pass count (the web config's ``iterations`` stand-in — high
#: enough for the small graphs this surface renders, fixed so runs never
#: diverge).
DEFAULT_ITERATIONS = 250

#: The golden angle in radians — the spiral's neighbor angle; irrational,
#: so successive points never align on a ray.
_GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))


def _initial_positions(ids: list[str], spacing: float) -> dict[str, tuple[float, float]]:
    """Golden-angle spiral over the sorted ids — deterministic, spread, and
    insertion-stable (point i depends only on i, not on the total count)."""
    positions: dict[str, tuple[float, float]] = {}
    for index, node_id in enumerate(ids):
        radius = spacing * math.sqrt(index + 1)
        angle = index * _GOLDEN_ANGLE
        positions[node_id] = (radius * math.cos(angle), radius * math.sin(angle))
    return positions


def layout_topology(
    topology: GraphTopology,
    *,
    iterations: int = DEFAULT_ITERATIONS,
    spacing: float = 80.0,
) -> dict[str, tuple[float, float]]:
    """Place every vertex on a deterministic force-directed layout.

    Fruchterman–Reingold: all pairs repel with ``k²/d``, edges attract with
    ``d²/k`` per unit weight, displacement is capped by a linearly-cooled
    temperature. Returns ``node_id → (x, y)``; the empty topology maps to
    ``{}`` and a single vertex sits at the origin.
    """
    ids = sorted(node.id for node in topology.nodes)
    if not ids:
        return {}
    if len(ids) == 1:
        return {ids[0]: (0.0, 0.0)}
    if iterations <= 0:
        return _initial_positions(ids, spacing)

    area = spacing * spacing * max(len(ids), 1)
    k = math.sqrt(area / len(ids))
    positions = _initial_positions(ids, spacing)
    adjacency: dict[str, list[tuple[str, int]]] = {node_id: [] for node_id in ids}
    for edge in topology.edges:
        adjacency[edge.source].append((edge.target, edge.weight))
        adjacency[edge.target].append((edge.source, edge.weight))
    for neighbors in adjacency.values():
        neighbors.sort()  # force summation order follows the sorted ids, never the input's row order

    temperature = spacing * 2.0
    cooling = temperature / max(iterations, 1)
    for _round in range(iterations):
        displacement = {node_id: [0.0, 0.0] for node_id in ids}
        # Repulsion over all pairs (sorted order — O(n²) is fine for the
        # minimal surface; the web's Barnes–Hut cutoff is engine territory).
        for i, a in enumerate(ids):
            ax, ay = positions[a]
            for b in ids[i + 1 :]:
                bx, by = positions[b]
                dx, dy = ax - bx, ay - by
                distance = math.hypot(dx, dy)
                if distance < 1e-6:
                    # Coincident points (identical initials can't happen —
                    # the spiral is injective — but a zero distance after
                    # rounding gets a deterministic nudge along the sorted
                    # pair's angle).
                    dx, dy = 1.0, 0.0
                    distance = 1e-6
                force = (k * k) / distance
                ux, uy = dx / distance, dy / distance
                displacement[a][0] += ux * force
                displacement[a][1] += uy * force
                displacement[b][0] -= ux * force
                displacement[b][1] -= uy * force
        # Attraction along the edges (weight-scaled).
        for a in ids:
            ax, ay = positions[a]
            for b, weight in adjacency[a]:
                if a >= b:  # each undirected edge once (sorted-id convention)
                    continue
                bx, by = positions[b]
                dx, dy = ax - bx, ay - by
                distance = math.hypot(dx, dy)
                if distance < 1e-6:
                    continue
                force = ((distance * distance) / k) * max(1, min(weight, 4))
                ux, uy = dx / distance, dy / distance
                displacement[a][0] -= ux * force
                displacement[a][1] -= uy * force
                displacement[b][0] += ux * force
                displacement[b][1] += uy * force
        # Apply, capped by the temperature.
        for node_id in ids:
            dx, dy = displacement[node_id]
            magnitude = math.hypot(dx, dy)
            if magnitude < 1e-9:
                continue
            capped = min(magnitude, max(temperature, 0.01))
            scale = capped / magnitude
            x, y = positions[node_id]
            positions[node_id] = (x + dx * scale, y + dy * scale)
        temperature = max(temperature - cooling, 0.0)
    return positions
