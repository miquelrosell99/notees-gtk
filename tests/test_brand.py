"""Tests for the Margin Green brand token projection (``notees_gtk.ui.brand``)."""

from notees_gtk.ui import brand
from notees_gtk.ui.brand import ContentColors, content_colors


def test_identity_core_values() -> None:
    """The core identity constants mirror brand/assets/tokens/tokens.css."""
    assert brand.ADVANCE_GREEN == "#2e5e46"
    assert brand.IRON_INK == "#1c1a16"
    assert brand.PAPER == "#f7f4ec"
    assert brand.NIGHT == "#161412"


def test_light_roles_come_from_tokens_css() -> None:
    """Light roles: the tokens.css link role and surface-alt chip on iron ink."""
    colors = content_colors(dark=False)
    assert colors == ContentColors(link="#3b7357", chip_background="#edeae2", chip_foreground="#1c1a16")


def test_dark_roles_come_from_the_tokens_dark_block() -> None:
    """Dark roles: the [data-theme="dark"] block of tokens.css."""
    colors = content_colors(dark=True)
    assert colors == ContentColors(link="#6da789", chip_background="#312a22", chip_foreground="#f4f3f1")
