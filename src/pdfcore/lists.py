"""List-marker recognition (bulleted & numbered lists).

Pure Python, no Qt and no PyMuPDF at import — a small classifier over text that
the paragraph layer (``pdfcore/textedit.py``) uses to group a marker with its
item body and to reproduce a hanging indent on edit.

Per the design decision (docs/list-feature-plan.md, "Option B") the marker is
ordinary EDITABLE TEXT, not a managed property: this module only RECOGNISES a
leading marker so the item can be grouped and re-laid with a hanging indent; it
never owns or renumbers it.

Two on-page encodings (both seen in the samples):

- a BULLET is usually its own text span (a SymbolMT ``•``) sitting to the left
  of the body at a lesser indent, sharing the body's baseline; the paragraph
  layer folds it onto the front of the body text.
- a NUMBER is INLINE — ``"1. "`` is the leading text of the body span itself.

so a marker is recognised both as a whole-line bullet (for folding) and as a
leading token of a line's text (for the hanging-indent split).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Bullet glyphs we treat as list markers. Kept deliberately tight — only real
# bullet characters, never a lone hyphen/asterisk (which appear mid-content and
# would mis-fold ordinary text). The SymbolMT bullet extracts as U+2022.
BULLET_GLYPHS = frozenset("•◦▪‣⁃·●○■")

# An ordinal marker: decimal / latin-letter / roman, optionally paren-wrapped,
# followed by ``.`` or ``)`` (or a closing paren for the ``(1)`` form). The
# trailing separator is REQUIRED so a sentence starting with a bare letter or
# number is never mistaken for a list marker.
_ROMAN = r"(?:[ivxlcdm]+|[IVXLCDM]+)"
_ORDINAL_CORE = rf"(?:\d{{1,3}}|[a-zA-Z]|{_ROMAN})"
_ORDINAL_RE = re.compile(rf"^\(?({_ORDINAL_CORE})[.)]")


@dataclass(frozen=True)
class ListMarker:
    """A recognised leading marker.

    - ``kind``: ``"bullet"`` | ``"decimal"`` | ``"alpha"`` | ``"roman"``.
    - ``text``: the marker exactly as it appears, INCLUDING its trailing
      separator but NOT the whitespace that follows it (``"•"``, ``"1."``,
      ``"a)"``, ``"(iv)"``).
    - ``ordinal``: 1-based value for a number/letter, ``None`` for a bullet.
    - ``end``: index in the source string just past the marker's whitespace —
      i.e. where the body text begins.
    """

    kind: str
    text: str
    ordinal: int | None
    end: int


def _roman_value(s: str) -> int | None:
    values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    total = 0
    prev = 0
    for ch in reversed(s.lower()):
        if ch not in values:
            return None
        v = values[ch]
        total += -v if v < prev else v
        prev = max(prev, v)
    return total or None


def leading_marker(text: str) -> ListMarker | None:
    """Recognise a list marker at the START of ``text``, or ``None``.

    Handles a bullet glyph and an ordinal token (``1.`` ``2)`` ``a.`` ``(iv)``).
    Requires whitespace (or end of string) after the marker so a decimal like
    ``3.14`` or a word is never taken as a marker. ``.end`` points past the
    trailing whitespace, so ``text[:m.end]`` is the marker + gutter and
    ``text[m.end:]`` is the body.
    """
    if not text:
        return None
    stripped = text.lstrip()
    lead = len(text) - len(stripped)
    if not stripped:
        return None

    first = stripped[0]
    if first in BULLET_GLYPHS:
        end = lead + 1
        while end < len(text) and text[end].isspace():
            end += 1
        return ListMarker(kind="bullet", text=first, ordinal=None, end=end)

    match = _ORDINAL_RE.match(stripped)
    if match:
        marker_text = match.group(0)  # e.g. "1.", "(iv)" missing close paren?
        core = match.group(1)
        # A closing paren for the "(1)" form: accept it into the marker text.
        after = lead + match.end()
        if marker_text.startswith("(") and after < len(text) and text[after] == ")":
            marker_text += ")"
            after += 1
        # Require a space (or end) after the marker: "1.x" is not a list item.
        if after < len(text) and not text[after].isspace():
            return None
        end = after
        while end < len(text) and text[end].isspace():
            end += 1
        if core.isdigit():
            kind, ordinal = "decimal", int(core)
        elif len(core) == 1 and core.isalpha():
            kind, ordinal = "alpha", (ord(core.lower()) - ord("a") + 1)
        else:
            kind, ordinal = "roman", _roman_value(core)
        marker_full = text[lead:after]
        return ListMarker(kind=kind, text=marker_full.strip(), ordinal=ordinal, end=end)
    return None


# Letters that are BOTH a latin ordinal and a roman numeral: "i." is alpha 9
# and roman 1, and the marker text alone cannot say which.
_ROMAN_ALPHA_AMBIGUOUS = frozenset("ivxlcdm")


def ordinal_at_level(mk: ListMarker, level: int) -> int | None:
    """``mk``'s ordinal read at ``level``'s ladder rung.

    ``leading_marker`` parses a single letter as ALPHA, so a third-level
    roman "i." came back as ordinal 9 — the editor then seeded the list at 9
    and the items renumbered ix, x, xi (user report, 2026-08-18: "i, ii"
    became "ix, x" and a new item "xi" instead of "iii"). The marker alone is
    genuinely ambiguous (Word shares this); the CALLER knows the block's
    level from the box's indent geometry, and the ladder names each rung's
    style (1. a. i., cycling), so a marker whose parse disagrees with its
    rung is re-read in the rung's style when the text supports it: "i." at a
    roman rung means 1, not 9. Unambiguous markers keep their parsed value —
    "b." at a roman rung stays ordinal 2 (and regenerates as "ii.", the
    renumber-on-edit rule). Bullets have no ordinal.
    """
    if mk.kind == "bullet":
        return None
    if max(0, level) % 3 == 2 and mk.kind == "alpha":
        core = mk.text.strip("()").rstrip(".)")
        if core.lower() in _ROMAN_ALPHA_AMBIGUOUS:
            return _roman_value(core)
    return mk.ordinal


def is_bullet_only(text: str) -> bool:
    """True when ``text`` is JUST a bullet glyph (+ optional whitespace) — the
    lone-marker span the paragraph layer folds onto the following line."""
    stripped = text.strip()
    return len(stripped) == 1 and stripped in BULLET_GLYPHS


def split_leading_marker(text: str) -> tuple[str, str] | None:
    """``(marker_incl_gutter, body)`` if ``text`` starts with a marker, else
    ``None``. ``marker_incl_gutter`` keeps the whitespace after the marker so
    concatenating the two halves reproduces ``text`` exactly."""
    marker = leading_marker(text)
    if marker is None:
        return None
    return text[: marker.end], text[marker.end :]


# --- Marker GENERATION (list v2) -------------------------------------------
# The engine writes markers from structure at commit time. Per-level styles
# are the Office defaults: bullets cycle • ◦ ▪, numbers cycle 1. a. i.

BULLET_LEVEL_GLYPHS = ("•", "◦", "▪")

# Real bullet glyphs need a real font: base-14 helv renders U+2022 as a middot
# speck (the "Format as list has zero effect" report). Probed 2026-08-18:
# Arial and Segoe UI both carry • ◦ ▪. Resolved engine-side the same way
# pdfcore/ocr.py probes for tesseract; a caller can override, and when no
# font resolves the layout falls back to the base-14 code (and the middot)
# rather than refusing.
_MARKER_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
)


def marker_fontfile() -> str | None:
    """A font file whose subset can draw the bullet glyphs, or ``None``."""
    for candidate in _MARKER_FONT_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


def bullet_glyph(level: int) -> str:
    """The bullet glyph for a 0-based indent level (• ◦ ▪, cycling)."""
    return BULLET_LEVEL_GLYPHS[max(0, level) % len(BULLET_LEVEL_GLYPHS)]


def alpha_ordinal(n: int) -> str:
    """1 -> "a", 26 -> "z", 27 -> "aa" (spreadsheet-column style)."""
    if n < 1:
        raise ValueError("ordinal must be >= 1")
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("a") + rem) + out
    return out


_ROMAN_TABLE = (
    (1000, "m"),
    (900, "cm"),
    (500, "d"),
    (400, "cd"),
    (100, "c"),
    (90, "xc"),
    (50, "l"),
    (40, "xl"),
    (10, "x"),
    (9, "ix"),
    (5, "v"),
    (4, "iv"),
    (1, "i"),
)


def roman_ordinal(n: int) -> str:
    """1 -> "i", 4 -> "iv" (lower-case roman)."""
    if n < 1:
        raise ValueError("ordinal must be >= 1")
    out = ""
    for value, glyphs in _ROMAN_TABLE:
        while n >= value:
            out += glyphs
            n -= value
    return out


def number_marker(level: int, ordinal: int) -> str:
    """The numbered marker for a 0-based level: "1." then "a." then "i.",
    cycling at deeper levels."""
    style = max(0, level) % 3
    if style == 0:
        return f"{ordinal}."
    if style == 1:
        return f"{alpha_ordinal(ordinal)}."
    return f"{roman_ordinal(ordinal)}."


def marker_text(kind: str, level: int, ordinal: int) -> str:
    """The literal marker for a list block: kind "bullet" or "number"."""
    if kind == "bullet":
        return bullet_glyph(level)
    if kind == "number":
        return number_marker(level, ordinal)
    raise ValueError(f"kind must be 'bullet' or 'number', not {kind!r}")
