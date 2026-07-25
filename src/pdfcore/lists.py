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
