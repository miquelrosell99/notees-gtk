"""Margin Green brand tokens — the GTK projection of the identity.

The single source of truth is the ``brand`` submodule (``notees-brand``,
pinned at ``v1.0.0``): ``brand/assets/tokens/tokens.css`` for the colour
roles, ``brand/guidelines/fonts.md`` for the typefaces. These constants
mirror the tokens for the chrome that renders outside GTK CSS (Pango markup
spans cannot read CSS variables); re-derive them from a token bump, never
re-pick them.

libadwaita owns the window/surface chrome in both colour schemes (the theme
itself carries the ``#161412``-family dark ground); the content markup
colours resolve per scheme, taking the dark roles from the
``[data-theme="dark"]`` block of ``tokens.css``. The accent chrome (the
``--accent-*`` variables in ``theme.css``) stays Advance Green in both
schemes.
"""

from __future__ import annotations

from typing import NamedTuple

__all__ = [
    "ADVANCE_GREEN",
    "ContentColors",
    "content_colors",
    "ink_bbox",
    "IRON_INK",
    "NIGHT",
    "PAPER",
]

#: Advance Green — annotations, links, active states, the arriving mark.
ADVANCE_GREEN = "#2e5e46"
#: Iron Ink — the margin rule, wordmark, text.
IRON_INK = "#1c1a16"
#: Paper — the warm light ground.
PAPER = "#f7f4ec"
#: Night — the warm near-black dark ground.
NIGHT = "#161412"


class ContentColors(NamedTuple):
    """Resolved Pango-markup colours for one colour scheme."""

    link: str
    chip_background: str
    chip_foreground: str


#: Light roles straight from ``tokens.css`` (``--bi-link``, ``--bi-surface-alt`` on ink).
_LIGHT = ContentColors(link="#3b7357", chip_background="#edeae2", chip_foreground="#1c1a16")
#: Dark roles from the tokens.css ``[data-theme="dark"]`` block.
_DARK = ContentColors(link="#6da789", chip_background="#312a22", chip_foreground="#f4f3f1")


def content_colors(dark: bool) -> ContentColors:
    """Return the content markup colours for the given colour scheme."""
    return _DARK if dark else _LIGHT


def ink_bbox(
    pixels: bytes | memoryview,
    width: int,
    height: int,
    rowstride: int,
    channels: int,
    *,
    has_alpha: bool,
) -> tuple[int, int, int, int] | None:
    """Bounding box ``(x, y, w, h)`` of the non-transparent pixels, or ``None``.

    The vendored logo SVGs carry brand clear-space padding around the ink
    (``data-ink`` in the file metadata); rendered raw, the mark shrinks to a
    fraction of the widget budget — the login lockup came out as an illegible
    ~14px squiggle inside its 64px box. Rendering crops to this box first.
    Fully transparent input (a loader quirk) yields ``None``. Buffers without
    an alpha channel have no ink notion — the full rectangle answers.

    Pure pixel math, deliberately gi-free, so the crop is headless-testable.
    """
    if not has_alpha:
        return (0, 0, width, height)
    view = memoryview(pixels)
    min_x, min_y = width, height
    max_x, max_y = -1, -1
    alpha_at = channels - 1
    for y in range(height):
        row = y * rowstride
        for x in range(width):
            if view[row + x * channels + alpha_at] != 0:
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                if y < min_y:
                    min_y = y
                if y > max_y:
                    max_y = y
    if max_x < 0:
        return None
    return (min_x, min_y, max_x - min_x + 1, max_y - min_y + 1)
