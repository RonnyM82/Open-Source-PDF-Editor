"""Resolve installed system font families to their font files (Windows).

Qt exposes family names but not file paths; the Windows registry font lists
map display names ("Arial Bold (TrueType)") to files under C:\\Windows\\Fonts
(or absolute paths for per-user fonts). Used by the style toolbar to honour an
explicit font choice by EMBEDDING a subset — automatic matching of existing
text never embeds (CLAUDE.md font rule).

Pure Python (no Qt) so the resolver is unit-testable headlessly.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

# TTC collections are excluded: pymupdf's fontfile loading wants a single
# face, and every font we care about ships as .ttf/.otf.
_USABLE_SUFFIXES = {".ttf", ".otf"}


@lru_cache(maxsize=1)
def system_font_map() -> dict[str, str]:
    """Lowercase display name (without "(TrueType)") -> absolute file path."""
    import winreg  # Windows-only, deferred so imports don't break elsewhere

    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    mapping: dict[str, str] = {}
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
        except OSError:
            continue
        with key:
            for i in range(winreg.QueryInfoKey(key)[1]):
                try:
                    name, value, _kind = winreg.EnumValue(key, i)
                except OSError:
                    continue
                if not isinstance(value, str) or not value:
                    continue
                path = Path(value) if ("\\" in value or "/" in value) else fonts_dir / value
                if path.suffix.lower() not in _USABLE_SUFFIXES:
                    continue
                display = name.split("(")[0].strip().lower()
                mapping.setdefault(display, str(path))
    return mapping


_BASE14_STYLED = {
    # family key -> (regular, bold, italic, bold-italic)
    "helv": ("helv", "hebo", "heit", "hebi"),
    "tiro": ("tiro", "tibo", "tiit", "tibi"),
    "cour": ("cour", "cobo", "coit", "cobi"),
}


def font_choice(family: str, bold: bool, italic: bool = False) -> tuple[str, str | None, bool]:
    """(base14 code, fontfile, resolved) for a family + weight + slant.

    Base-14 families stay non-embedded (code only); anything else resolves to
    a system font file for embedding. resolved=False means the fallback code
    is being used because no file was found. Italic flows through both paths
    (dropping it silently was a review finding). Shared by the style toolbar
    and the rich-commit conversion in DocumentView.
    """
    fam = family.lower()
    index = (1 if bold else 0) + (2 if italic else 0)  # (regular, bold, italic, both)
    key = None
    if "helvetica" in fam or "arial" in fam:
        key = "helv"
    elif "times" in fam:
        key = "tiro"
    elif "courier" in fam:
        key = "cour"
    if key is not None:
        return _BASE14_STYLED[key][index], None, True
    fallback = _BASE14_STYLED["helv"][index]
    fontfile = resolve_font_file(family, bold=bold, italic=italic)
    if fontfile is None:
        return fallback, None, False
    return fallback, fontfile, True


def resolve_font_file(family: str, bold: bool = False, italic: bool = False) -> str | None:
    """The best installed font file for a family + style, or None.

    Exact display-name matches first ("arial bold"), then the plain family
    (better an upright face than nothing), then a prefix match.
    """
    fonts = system_font_map()
    fam = family.strip().lower()
    names: list[str] = []
    if bold and italic:
        names += [f"{fam} bold italic", f"{fam} bold oblique"]
    elif bold:
        names.append(f"{fam} bold")
    elif italic:
        names += [f"{fam} italic", f"{fam} oblique"]
    names.append(fam)
    for name in names:
        path = fonts.get(name)
        if path and Path(path).exists():
            return path
    for display, path in fonts.items():
        if display.startswith(fam) and Path(path).exists():
            return path
    return None
