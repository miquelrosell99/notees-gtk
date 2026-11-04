"""The graph viewport — world↔screen transforms, pan, zoom-at-anchor, bounds
fitting, and node hit-testing. Every interaction rule the chrome's gesture
handlers ride, pinned without a display.
"""

from __future__ import annotations

from notees_gtk.core.graph.viewport import CLICK_THRESHOLD_PX, GraphViewport


class TestTransforms:
    def test_world_screen_round_trip(self) -> None:
        viewport = GraphViewport(width=800, height=600, scale=2.5, center_x=10.0, center_y=-20.0)
        for world in [(0.0, 0.0), (123.4, -56.7), (-1.0, 2.0)]:
            screen = viewport.world_to_screen(*world)
            assert viewport.screen_to_world(*screen) == world

    def test_world_y_grows_upward_screen_y_downward(self) -> None:
        viewport = GraphViewport(width=800, height=600)
        above = viewport.world_to_screen(0.0, 10.0)
        below = viewport.world_to_screen(0.0, -10.0)
        assert above[1] < below[1]

    def test_pan_shifts_the_mapping(self) -> None:
        viewport = GraphViewport(width=800, height=600, scale=1.0)
        before = viewport.world_to_screen(0.0, 0.0)
        viewport.pan_by(30.0, -20.0)
        after = viewport.world_to_screen(0.0, 0.0)
        assert after[0] == before[0] + 30.0
        assert after[1] == before[1] - 20.0


class TestZoom:
    def test_zoom_at_keeps_the_anchor_stationary(self) -> None:
        viewport = GraphViewport(width=800, height=600, scale=1.0, center_x=5.0, center_y=5.0)
        anchor_screen = (200.0, 150.0)
        anchor_world = viewport.screen_to_world(*anchor_screen)
        viewport.zoom_at(3.0, *anchor_screen)
        assert viewport.screen_to_world(*anchor_screen) == anchor_world
        assert viewport.scale == 3.0

    def test_zoom_clamps_to_the_sane_range(self) -> None:
        viewport = GraphViewport(width=800, height=600)
        viewport.zoom_at(1000.0, 400.0, 300.0)
        assert viewport.scale == 20.0
        viewport.zoom_at(0.0001, 400.0, 300.0)
        assert viewport.scale == 0.05


class TestFraming:
    def test_fit_bounds_centers_and_scales(self) -> None:
        viewport = GraphViewport(width=800, height=600)
        viewport.fit_bounds(-100.0, -50.0, 100.0, 50.0, padding=40.0)
        assert viewport.center_x == 0.0
        assert viewport.center_y == 0.0
        # 720 px / 200 world units vs 520 px / 100 world units — the tighter
        # axis (3.6) wins, then the default-zoom clamp caps at 2.0 (small
        # graphs must not open over-zoomed).
        assert viewport.scale == 2.0
        corners = [viewport.world_to_screen(x, y) for x in (-100.0, 100.0) for y in (-50.0, 50.0)]
        xs = [corner[0] for corner in corners]
        ys = [corner[1] for corner in corners]
        assert min(xs) >= 200.0 and max(xs) <= 600.0
        assert min(ys) >= 200.0 and max(ys) <= 400.0

    def test_fit_bounds_handles_a_single_point(self) -> None:
        viewport = GraphViewport(width=800, height=600)
        viewport.fit_bounds(3.0, 4.0, 3.0, 4.0)
        assert viewport.center_x == 3.0
        assert viewport.center_y == 4.0
        assert viewport.scale > 0


class TestHitTesting:
    def test_nearest_node_within_radius_wins(self) -> None:
        viewport = GraphViewport(width=800, height=600)
        positions = {"far": (200.0, 0.0), "near": (10.0, 5.0)}
        assert viewport.hit_test(positions, *viewport.world_to_screen(12.0, 6.0), radius=14.0) == "near"

    def test_miss_returns_none(self) -> None:
        viewport = GraphViewport(width=800, height=600)
        positions = {"a": (0.0, 0.0)}
        assert viewport.hit_test(positions, 700.0, 500.0, radius=14.0) is None


class TestClickThreshold:
    def test_click_constant(self) -> None:
        # The chrome compares the drag's total distance against this — the
        # pin exists so a display-less refactor cannot shrink it silently.
        assert CLICK_THRESHOLD_PX == 4.0
