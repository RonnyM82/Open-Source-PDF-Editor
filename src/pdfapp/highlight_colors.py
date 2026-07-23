"""Industry-standard highlighter colours (the restricted picker palette).

Qt-free by design (like ``portable.py`` / ``recent_files.py``): just the palette
data and a pure hex→float-tuple helper, so it is unit-testable and can feed the
engine's ``(r, g, b)`` 0-1 highlight API without crossing the pdfcore/Qt
boundary. The Qt side (QColor / painted swatch) lives entirely in the UI.

The set is the classic 6-marker highlighter pack (user decision 2026-07-23);
values are vivid but light enough that black text stays legible under the
translucent PDF highlight annotation. Yellow is the default.
"""

from __future__ import annotations

# (display name, "#RRGGBB"). Order is the picker order.
HIGHLIGHTER_COLORS: list[tuple[str, str]] = [
    ("Yellow", "#FFEB3B"),
    ("Green", "#76FF03"),
    ("Blue", "#40C4FF"),
    ("Pink", "#FF4081"),
    ("Orange", "#FFAB40"),
    ("Purple", "#B388FF"),
]

DEFAULT_HIGHLIGHT = "#FFEB3B"

# Uppercased hexes for membership checks (a persisted colour must be one of the
# palette entries — the picker is deliberately restricted).
PALETTE_HEXES = {hexstr.upper() for _name, hexstr in HIGHLIGHTER_COLORS}


def is_palette_hex(hexstr: object) -> bool:
    """True when ``hexstr`` is one of the palette colours (case-insensitive)."""
    return isinstance(hexstr, str) and hexstr.upper() in PALETTE_HEXES


def hex_to_rgb01(hexstr: str) -> tuple[float, float, float]:
    """``"#RRGGBB"`` → ``(r, g, b)`` floats in 0-1 for the engine highlight API."""
    h = hexstr.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]
