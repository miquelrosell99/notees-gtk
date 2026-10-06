"""Data-level color grammar (owner 2026-10-03).

``colors.ts`` parity: a node/class ``color`` is ONE string field carrying
either a preset token or a custom hex color. This replaces the first v3
encoding — CSS variable references (``var(--color-preset-red)``) — which
leaked a web technology onto the wire and drifted between clients. The
token/hex split is client-neutral, strictly validatable, keeps theme
remapping (the concrete hex lives client-side, keyed by token), and keeps
freeform custom colors without a schema change.

``null`` on a color field means CLEAR (``object.update`` gains the
capability here — the UI's "No color" was a protocol no-op until now;
``class.update`` documented "null clears" but the old string-only schema
never accepted it). Stored logs still carrying the retired
``var(--color-preset-*)`` encoding are rewritten in place by the one-time
migration in the monorepo; payload schemas reject that encoding outright —
no backward compatibility (owner directive).

The token SET is normative here; display ORDER is a client concern (web:
hue order then gray — variables.css / colorPresets.ts).
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import AfterValidator

__all__ = ["COLOR_PRESET_TOKENS", "ColorValue", "is_color_value", "validate_color"]

#: The ten preset tokens, hue order (red → pink) then gray.
COLOR_PRESET_TOKENS: tuple[str, ...] = (
    "red",
    "orange",
    "yellow",
    "green",
    "teal",
    "sky",
    "blue",
    "purple",
    "pink",
    "gray",
)

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def validate_color(value: str) -> str:
    """Return ``value`` when it is a preset token or ``#RRGGBB`` hex.

    Raises:
        ValueError: anything else — including the retired
            ``var(--color-preset-<token>)`` encoding (zod parity: the
            ``.regex(/^#[0-9a-fA-F]{6}$/)`` hex branch and the enum
            token branch both fail).
    """
    if value in COLOR_PRESET_TOKENS or _HEX_COLOR_RE.match(value):
        return value
    raise ValueError(f"expected a color preset token or #RRGGBB hex, got {value!r}")


def _validate_optional_color(value: str | None) -> str | None:
    # ``null`` CLEARS the field (zod ``.nullish()`` parity) — presence on
    # the payload, not the value, decides whether the applier writes.
    if value is None:
        return None
    return validate_color(value)


def is_color_value(value: Any) -> bool:
    """True when ``value`` is a stored color (preset token or #RRGGBB hex)."""
    return isinstance(value, str) and (value in COLOR_PRESET_TOKENS or _HEX_COLOR_RE.match(value) is not None)


#: A stored color value: preset token or custom ``#RRGGBB`` hex — or ``None``
#: to clear (payload fields keep ``None`` as their absent default; the appliers
#: test key presence, so an explicit ``null`` still reaches them as a clear).
ColorValue = Annotated[str | None, AfterValidator(_validate_optional_color)]
