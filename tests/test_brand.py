"""Tests for the Margin Green brand token projection (``notees_gtk.ui.brand``)."""

from notees_gtk.ui import brand
from notees_gtk.ui.brand import ContentColors, content_colors, ink_bbox


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


def _rgba_buffer(width: int, height: int, *, rowstride_pad: int = 0) -> tuple[bytearray, int]:
    """A transparent RGBA buffer; returns (pixels, rowstride)."""
    rowstride = width * 4 + rowstride_pad
    return bytearray(rowstride * height), rowstride


def test_ink_bbox_crops_clear_space_padding() -> None:
    """The vendored logos' viewBox padding is dropped — only the opaque ink answers."""
    pixels, rowstride = _rgba_buffer(10, 8, rowstride_pad=4)
    for y in range(2, 6):
        for x in range(3, 8):
            at = y * rowstride + x * 4
            pixels[at : at + 4] = bytes((20, 30, 40, 255))
    assert ink_bbox(pixels, 10, 8, rowstride, 4, has_alpha=True) == (3, 2, 5, 4)


def test_ink_bbox_tolerates_stray_rowstride_padding() -> None:
    """Bytes past the row width never read as ink."""
    pixels, rowstride = _rgba_buffer(4, 2, rowstride_pad=4)
    pixels[rowstride - 1] = 255  # trailing pad byte of row 0
    assert ink_bbox(pixels, 4, 2, rowstride, 4, has_alpha=True) is None


def test_ink_bbox_fully_transparent_is_none() -> None:
    pixels, rowstride = _rgba_buffer(4, 4)
    assert ink_bbox(pixels, 4, 4, rowstride, 4, has_alpha=True) is None


def test_ink_bbox_without_alpha_is_the_full_rect() -> None:
    """RGB buffers have no ink notion — nothing to crop."""
    pixels, rowstride = _rgba_buffer(4, 4)
    assert ink_bbox(pixels, 4, 4, rowstride, 4, has_alpha=False) == (0, 0, 4, 4)
