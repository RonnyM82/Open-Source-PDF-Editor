"""Help → "Editing gestures" cheat sheet (U7).

A static themed dialog listing every gesture and mode — built LAST in the
U-series so it documents everything. qt-material styles it app-wide and the
dialog is short-lived (S5 convention: state baked at construction). No
persistence mechanism exists, so there are no first-run coach marks by
design; this dialog and the hover hints are the teaching surfaces.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QScrollArea, QVBoxLayout

_SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "Modes",
        [
            (
                "Markup / Edit mode (Ctrl+E)",
                "Documents open in Markup mode — highlight, comment and callout freely; "
                "Ctrl+E enables editing text and images for this document",
            ),
            ("Show editable areas", "Outlines every editable paragraph and image on the page"),
            (
                "Double-click edits paragraph",
                "Sets what a plain double-click edits — one line, or the whole paragraph",
            ),
        ],
    ),
    (
        "Text",
        [
            ("Hover", "Outlines the paragraph under the cursor; the status bar names the moves"),
            ("Click", "Selects the paragraph — then drag the selection to move it"),
            ("Double-click", "Edits the line (or the paragraph, per the toggle)"),
            ("Ctrl+double-click", "Edits the other one — a momentary override"),
            ("Ctrl+drag", "Moves the paragraph directly, no selection needed"),
            ("Hold Shift while moving", "Snaps the move to one axis (keeps a row/column aligned)"),
            ("Ctrl+click / Shift+click", "Adds the box to a multi-selection (click again removes)"),
            (
                "Drag a box (empty area)",
                "Marquee-select boxes — left→right encloses, right→left crosses; "
                "Shift adds, Ctrl removes",
            ),
            ("Drag a multi-selected box", "Moves the whole group, spacing preserved"),
            (
                "Right-click (multi-selected)",
                "Merge · Align (6-way) · Distribute (equal gaps) · Delete all selected",
            ),
            (
                "Right-click",
                "Edit text · Edit paragraph · Highlight · Duplicate text box · Delete text box",
            ),
            ("Duplicate text box", "Drops a selected copy alongside — drag it into place"),
        ],
    ),
    (
        "In an editor",
        [
            ("Enter / Ctrl+Enter", "Commits a line edit / applies a paragraph edit"),
            ("Esc", "Cancels the edit"),
            ("Style toolbar", "Formats the selected characters (bold two words, colour one)"),
            (
                "Alignment button",
                "Left / centre / right for the WHOLE box — the dropdown remembers your last pick",
            ),
            ("Drag right edge / corner", "Sets the paragraph wrap width / resizes the box"),
        ],
    ),
    (
        "Images",
        [
            ("Click", "Selects the image (corner handles appear)"),
            ("Drag selected", "The body moves it · a corner resizes it (aspect kept)"),
            ("Delete", "Removes the selected image"),
            ("Double-click", "Replaces the image with a new file"),
            ("Ctrl+drag", "Moves or resizes directly, no selection needed"),
            ("Right-click", "Replace image · Delete image"),
        ],
    ),
    (
        "Insert & highlight",
        [
            (
                "Insert text / image",
                "Arms click-to-place — a chip shows what to do next; Esc cancels",
            ),
            (
                "Bulleted / numbered list",
                "With an editor open (or a text box selected), the toolbar "
                "toggles format it — clicking the checked one removes the "
                "formatting; Enter starts the next item, Shift+Enter breaks a "
                "line inside one, an empty item ends the list",
            ),
            (
                "List indent",
                "Tab / Shift+Tab at an item's start (or the toolbar indent "
                "buttons) nest and un-nest it; outdenting at the top level "
                "removes the list formatting",
            ),
            (
                "Highlight text (Ctrl+Shift+H)",
                "Highlights the current text selection, or arms a window drag across text",
            ),
            ("Highlighter colour", "Pick from the Annotate toolbar swatch or the Annotate menu"),
            ("Right-click the background", "Insert text, an image, or a comment at that spot"),
            (
                "Insert comment / callout",
                "Review markup — works in Markup mode; never prints unless chosen at print time",
            ),
            ("On a comment", "Double-click edits · drag moves · Delete removes"),
        ],
    ),
    (
        "General",
        [
            ("Ctrl+F", "Finds text anywhere in the document (OCR offered for scanned pages)"),
            ("Esc", "Cancels an armed mode, then clears the selection, then closes search"),
            ("Ctrl+Z / Ctrl+Y", "Undo / redo any edit"),
            ("Ctrl+wheel", "Zooms the page"),
            ("Scroll at a page edge", "Continues to the next or previous page"),
        ],
    ),
]


def gestures_html() -> str:
    parts: list[str] = []
    for title, rows in _SECTIONS:
        parts.append(f"<h3>{title}</h3><table cellspacing='0' cellpadding='3'>")
        for gesture, meaning in rows:
            parts.append(f"<tr><td><b>{gesture}</b>&nbsp;&nbsp;</td><td>{meaning}</td></tr>")
        parts.append("</table>")
    return "".join(parts)


class GestureHelpDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Editing gestures")
        label = QLabel(gestures_html(), self)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setContentsMargins(8, 4, 8, 4)
        scroll = QScrollArea(self)
        scroll.setWidget(label)
        scroll.setWidgetResizable(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(scroll)
        layout.addWidget(buttons)
        self.resize(600, 540)
