"""One icon set for the whole app: QtAwesome / Material Design Icons (S4).

Every toolbar / menu icon comes from ``icon(key)`` — no other module may
call qtawesome directly, and no other icon source may be mixed in. Glyph
colour follows the CURRENT theme mode (light glyphs on the dark shell, dark
glyphs in light mode); a QIcon is baked at call time and does not recolour
itself, so long-lived windows re-assign their icons on ``theme.on_change``
while short-lived dialogs simply build with the mode current at
construction.

qtawesome RAISES on an unknown glyph name at call time (no silent
fallback), so every name in ``_NAMES`` was verified against the installed
Material Design Icons 6.9.x font (restyle S4) and the whole table is
exercised by tests.
"""

from __future__ import annotations

import qtawesome as qta
from PySide6.QtGui import QIcon

from pdfapp import theme

# (normal, disabled) glyph colours per theme mode.
_COLORS = {
    theme.DARK: ("#e0e0e0", "#5f6a73"),
    theme.LIGHT: ("#37474f", "#b0bec5"),
}

_NAMES = {
    # File / document
    "open": "mdi6.folder-open-outline",
    "save": "mdi6.content-save-outline",
    "save_as": "mdi6.content-save-edit-outline",
    "print": "mdi6.printer-outline",
    "merge": "mdi6.merge",
    "split": "mdi6.set-split",
    # Navigation
    "prev_page": "mdi6.chevron-left",
    "next_page": "mdi6.chevron-right",
    "first_page": "mdi6.page-first",
    "last_page": "mdi6.page-last",
    # View
    "zoom_in": "mdi6.magnify-plus-outline",
    "zoom_out": "mdi6.magnify-minus-outline",
    "fit_page": "mdi6.fit-to-page-outline",
    "fit_width": "mdi6.arrow-expand-horizontal",
    "thumbnails": "mdi6.view-grid-outline",
    "dark_theme": "mdi6.theme-light-dark",
    "reveal_areas": "mdi6.selection",
    # Edit
    "edit_mode": "mdi6.pencil-outline",
    "edit_text": "mdi6.pencil",
    "edit_paragraph": "mdi6.text-box-edit-outline",
    "dblclick_paragraph": "mdi6.format-pilcrow",
    "undo": "mdi6.undo",
    "redo": "mdi6.redo",
    "rotate_cw": "mdi6.rotate-right",
    "rotate_ccw": "mdi6.rotate-left",
    "move_up": "mdi6.arrow-up",
    "move_down": "mdi6.arrow-down",
    "delete_page": "mdi6.delete-outline",
    "new_window": "mdi6.open-in-new",
    "insert_pages": "mdi6.file-plus-outline",
    "insert_text": "mdi6.text-box-plus-outline",
    "insert_image": "mdi6.image-plus-outline",
    "insert_comment": "mdi6.comment-plus-outline",
    "insert_callout": "mdi6.comment-arrow-left-outline",
    "insert_link": "mdi6.link-plus",
    "link_text": "mdi6.link-variant-plus",
    "edit_link": "mdi6.link-edit",
    "open_link": "mdi6.open-in-new",
    "remove_link": "mdi6.link-off",
    "highlight": "mdi6.marker",
    "replace_image": "mdi6.image-sync-outline",
    "delete_image": "mdi6.image-remove-outline",
    "rotate_image_cw": "mdi6.rotate-right",
    "rotate_image_ccw": "mdi6.rotate-left",
    "copy": "mdi6.content-copy",
    "duplicate_text": "mdi6.content-duplicate",
    # Box arrangement (align/distribute the boxes themselves — distinct from
    # the format-align-* text-justification icons).
    "box_align_left": "mdi6.align-horizontal-left",
    "box_align_hcenter": "mdi6.align-horizontal-center",
    "box_align_right": "mdi6.align-horizontal-right",
    "box_align_top": "mdi6.align-vertical-top",
    "box_align_vcenter": "mdi6.align-vertical-center",
    "box_align_bottom": "mdi6.align-vertical-bottom",
    "box_distribute_v": "mdi6.distribute-vertical-center",
    "box_distribute_h": "mdi6.distribute-horizontal-center",
    # Text style
    "bold": "mdi6.format-bold",
    "italic": "mdi6.format-italic",
    "underline": "mdi6.format-underline",
    "strikethrough": "mdi6.format-strikethrough",
    "superscript": "mdi6.format-superscript",
    "subscript": "mdi6.format-subscript",
    "align_left": "mdi6.format-align-left",
    "align_center": "mdi6.format-align-center",
    "align_right": "mdi6.format-align-right",
    "list_bulleted": "mdi6.format-list-bulleted",
    "list_numbered": "mdi6.format-list-numbered",
    "list_clear": "mdi6.format-list-checkbox",
    "indent_more": "mdi6.format-indent-increase",
    "indent_less": "mdi6.format-indent-decrease",
    # Tools
    "extract_text": "mdi6.text-box-search-outline",
    "detect_links": "mdi6.web",
    # Sign (digital signatures)
    "place_signature": "mdi6.signature-freehand",
    "sign_invisible": "mdi6.file-sign",
    "place_initials": "mdi6.signature-text",
    "manage_signatures": "mdi6.card-account-details-outline",
    "signature_status": "mdi6.shield-check-outline",
    "protect_document": "mdi6.lock-outline",
    "unlock_document": "mdi6.lock-open-variant-outline",
    # Search (SR2)
    "search": "mdi6.magnify",
    "search_prev": "mdi6.chevron-up",
    "search_next": "mdi6.chevron-down",
    "search_close": "mdi6.close",
    # Help
    "help": "mdi6.help-circle-outline",
    # Print preview (S5)
    "portrait": "mdi6.crop-portrait",
    "landscape": "mdi6.crop-landscape",
    "view_single": "mdi6.file-document-outline",
    "view_facing": "mdi6.book-open-outline",
    "view_overview": "mdi6.view-module",
}


def icon(key: str) -> QIcon:
    """A themed QIcon for a known key (KeyError on unknown — fix the table).

    ``color_on`` renders CHECKED buttons' glyphs in the accent colour — the
    other half of the Material selected-state (theme.py paints the tonal
    fill). A disabled checked button stays in the disabled grey.
    """
    color, disabled = _COLORS[theme.current_mode()]
    return qta.icon(
        _NAMES[key],
        color=color,
        color_disabled=disabled,
        color_on=theme.accent(),
        color_on_disabled=disabled,
    )
