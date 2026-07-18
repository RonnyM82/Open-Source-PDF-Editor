"""Point-of-need gesture hints (U2b): ONE source for every hover hint.

Later milestones change wording here and nowhere else — U6 adds
click-to-select phrasing, U8 makes the double-click target follow the
active sub-mode. Until U6 ships the wording stays gesture-only (nothing
may imply click-to-select before it exists).
"""

from __future__ import annotations

_TEXT_HINT_LINE = (
    "Click to select · drag moves the selection · double-click edits"
    " · Ctrl+double-click edits the paragraph"
)
_TEXT_HINT_PARAGRAPH = (
    "Click to select · drag moves the selection · double-click edits the paragraph"
    " · Ctrl+double-click edits one line"
)
_HINTS = {
    "image": (
        "Click to select · drag moves, a corner drag resizes · double-click replaces"
        " · Delete removes · right-click for more"
    ),
    "image_corner": ("Select the image, then drag the corner to resize (Ctrl+drag is direct)"),
    "comment": (
        "Comment (doesn't print) · double-click edits · Ctrl+drag moves"
        " · Delete removes · right-click for more"
    ),
}


def hover_hint(kind: str, dblclick_paragraph: bool = True) -> str:
    """The status-bar hint for a hover kind ('' for none/unknown).

    ``dblclick_paragraph`` is the U8 sub-mode: the text hint always names
    what a PLAIN double-click currently edits (the mode must stay visible).
    The default matches the app default (paragraph-first).
    """
    if kind == "text":
        return _TEXT_HINT_PARAGRAPH if dblclick_paragraph else _TEXT_HINT_LINE
    return _HINTS.get(kind, "")
