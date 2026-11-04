"""The graph surface's viewport math — world↔screen transforms, pan/zoom,
and node hit-testing (the web's ``cameraMath.ts`` reduced port). Pure and
headless: the GTK widget owns no transform logic, so every interaction rule
is unit-testable without a display.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["CLICK_THRESHOLD_PX", "GraphViewport"]

#: A press/release under this screen distance (px) is a click, not a pan.
CLICK_THRESHOLD_PX = 4.0


@dataclass
class GraphViewport:
    """The world→screen camera: a scale plus the world point sitting at the
    widget's center. All mutation is explicit (``pan_by`` / ``zoom_at`` /
    ``fit_bounds``) so chrome gesture handlers stay one-liners."""

    width: float
    height: float
    scale: float = 1.0
    center_x: float = 0.0
    center_y: float = 0.0

    def world_to_screen(self, x: float, y: float) -> tuple[float, float]:
        """Project world coordinates to widget pixels (y grows downward)."""
        return (
            self.width / 2.0 + (x - self.center_x) * self.scale,
            self.height / 2.0 - (y - self.center_y) * self.scale,
        )

    def screen_to_world(self, sx: float, sy: float) -> tuple[float, float]:
        """Invert :meth:`world_to_screen` (a click's pixel → world coords)."""
        return (
            (sx - self.width / 2.0) / self.scale + self.center_x,
            (self.height / 2.0 - sy) / self.scale + self.center_y,
        )

    def pan_by(self, dx: float, dy: float) -> None:
        """Drag-pan: the content follows the cursor, so the world point at
        the screen center shifts opposite the drag, scaled."""
        self.center_x -= dx / self.scale
        self.center_y += dy / self.scale

    def zoom_at(self, factor: float, sx: float, sy: float) -> None:
        """Scale by ``factor`` (clamped to a sane range) keeping the world
        point under ``(sx, sy)`` stationary."""
        clamped = min(max(factor, 0.05), 20.0)
        if abs(clamped - self.scale) < 1e-12:
            return
        anchor_x, anchor_y = self.screen_to_world(sx, sy)
        self.scale = clamped
        # Re-aim so the anchor lands back on (sx, sy).
        self.center_x = anchor_x - (sx - self.width / 2.0) / self.scale
        self.center_y = anchor_y + (sy - self.height / 2.0) / self.scale

    def fit_bounds(
        self,
        min_x: float,
        min_y: float,
        max_x: float,
        max_y: float,
        *,
        padding: float = 40.0,
    ) -> None:
        """Frame the given world rectangle with margin (the initial view and
        the reset-zoom anchor). Degenerate bounds (a single point) frame at
        the default scale."""
        bounds_w = max(max_x - min_x, 1e-6)
        bounds_h = max(max_y - min_y, 1e-6)
        self.center_x = (min_x + max_x) / 2.0
        self.center_y = (min_y + max_y) / 2.0
        available_w = max(self.width - 2.0 * padding, 1.0)
        available_h = max(self.height - 2.0 * padding, 1.0)
        self.scale = min(max(available_w / bounds_w, 0.05), max(available_h / bounds_h, 0.05))
        self.scale = min(self.scale, 2.0)

    def hit_test(
        self,
        positions: dict[str, tuple[float, float]],
        sx: float,
        sy: float,
        *,
        radius: float = 14.0,
    ) -> str | None:
        """The node whose screen position is nearest ``(sx, sy)`` within
        ``radius`` pixels — deterministic (sorted ids break ties); None on
        a miss."""
        best: tuple[float, str] | None = None
        for node_id in sorted(positions):
            x, y = self.world_to_screen(*positions[node_id])
            distance = ((x - sx) ** 2 + (y - sy) ** 2) ** 0.5
            if distance <= radius and (best is None or distance < best[0]):
                best = (distance, node_id)
        return None if best is None else best[1]
