"""Main application window — thin chrome around a tab of DocumentViews.

Owns actions, menus, toolbar, and file-level flows (open / merge / split /
save-as dialogs). Per-document state and operations live in DocumentView; the
window delegates action triggers to the *active* view (the current tab) and
reflects that view's state in the toolbar and title via `_sync_chrome()`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QMimeData, QSize, Qt
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QTextCharFormat,
    QUndoGroup,
)
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QColorDialog,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFontComboBox,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSpinBox,
    QTabWidget,
    QToolBar,
    QToolButton,
)

from pdfapp import diagnostics, highlight_colors, icons, portable, theme
from pdfapp.document_view import DocumentView
from pdfapp.font_files import font_choice
from pdfapp.print_support import PrintDialog, PrintOptions, print_document, show_preview
from pdfapp.protect_dialog import ProtectDialog
from pdfapp.recent_files import RecentFiles
from pdfapp.settings import Settings
from pdfapp.sign_dialog import SignDialog
from pdfapp.signature_manager_dialog import DEFAULT_P12_KEY, SignatureManagerDialog
from pdfapp.signature_store import STORE_FILENAME, SignatureStore
from pdfcore import pages, signing
from pdfcore.document import PdfDocument
from pdfcore.textedit import (
    ALIGNMENTS,
    FLAG_BOLD,
    FLAG_ITALIC,
    SCRIPT_NORMAL,
    SCRIPT_SUB,
    SCRIPT_SUPER,
    TextStyle,
)

# Family + weight/slant -> engine font choice. Lives in font_files (shared
# with DocumentView's rich-commit conversion); aliased for existing tests.
_font_choice = font_choice


def _weight_format(bold: bool) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setFontWeight(QFont.Weight.Bold if bold else QFont.Weight.Normal)
    return fmt


def _flag_format(kind: str, on: bool) -> QTextCharFormat:
    fmt = QTextCharFormat()
    if kind == "italic":
        fmt.setFontItalic(on)
    elif kind == "underline":
        fmt.setFontUnderline(on)
    elif kind == "strikethrough":
        fmt.setFontStrikeOut(on)
    return fmt


def _parse_page_ranges(text: str) -> list[tuple[int, int]]:
    """Parse a 1-based range string like "1-3, 4, 5-8" into 0-based (start, end).

    Raises ValueError on empty input or non-integer parts.
    """
    ranges: list[tuple[int, int]] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a) - 1, int(b) - 1
        else:
            start = end = int(part) - 1
        ranges.append((start, end))
    if not ranges:
        raise ValueError("no page ranges given")
    return ranges


def _dropped_pdf_paths(mime: QMimeData) -> list[Path]:
    """Local ``.pdf`` file paths carried by a drag's mime data (order preserved).

    Drag-and-drop hands us URLs; keep only local files with a ``.pdf`` suffix so
    a stray text/link drag — or a non-PDF file — is silently declined.
    """
    if not mime.hasUrls():
        return []
    paths: list[Path] = []
    for url in mime.urls():
        if url.isLocalFile():
            path = Path(url.toLocalFile())
            if path.suffix.lower() == ".pdf":
                paths.append(path)
    return paths


# Bumped whenever the toolbar/dock layout changes so a stale saved window
# state (restoreState) is cleanly ignored rather than hiding a new toolbar.
# 3: added the "Insert link" toolbar button (hyperlink feature).
# 4: added the "Link text" toolbar button (Word-style text links).
_STATE_VERSION = 4


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PDF Editor")
        self.resize(1000, 800)
        # Drop a PDF onto the window to open it, exactly like File > Open.
        self.setAcceptDrops(True)

        # Persisted preferences (theme, layout, toggles, last highlight colour)
        # live in the app's own data dir — the installed build's %LOCALAPPDATA%
        # spot; the same directory as recent_files.json and the diagnostics log.
        self._settings = Settings(portable.data_dir() / "settings.json")
        self._thumbs_visible = self._settings.get("thumbnails_visible", True)
        self._last_hover_hint = ""
        self._print_options = PrintOptions()
        # Current highlighter colour (persisted; restricted to the palette).
        self._highlight_color = QColor(self._startup_highlight_hex())
        # Recent-files list (File → Open Recent), persisted across launches.
        self._recent_files = RecentFiles(portable.data_dir() / "recent_files.json")
        # Signature library: stored signing identities (Sign menu). Passwords
        # are NEVER stored — the sign dialog prompts at signing time.
        self._signatures = SignatureStore(portable.data_dir() / STORE_FILENAME)
        # One stack per document (owned by its DocumentView); the group routes
        # Undo/Redo to the active tab's stack.
        self._undo_group = QUndoGroup(self)

        self._tabs = QTabWidget(self)
        self._tabs.setDocumentMode(True)
        self._tabs.setMovable(True)
        self._tabs.setTabsClosable(True)
        # Kill the tab bar's native "base" frame — a grey 1px line the Fusion
        # style draws UNDER the tabs (QSS can't reach it; it peeked out past
        # the last tab as an RGB-120 hairline).
        self._tabs.tabBar().setDrawBase(False)
        self._tabs.currentChanged.connect(lambda _idx: self._sync_chrome())
        self._tabs.tabCloseRequested.connect(self._close_tab)
        self.setCentralWidget(self._tabs)

        self._build_actions()
        self._build_menu()
        self._build_toolbar()
        self._build_style_toolbar()
        self._build_annotate_toolbar()
        self._assign_icons()
        # Permanent read-only/editing indicator for the ACTIVE tab (U0).
        # Protection indicator ("Protected" / "Restricted" / pending) sits
        # beside the mode label; tooltip lists what's denied.
        self._protection_label = QLabel("", self)
        self.statusBar().addPermanentWidget(self._protection_label)
        self._mode_label = QLabel("", self)
        self.statusBar().addPermanentWidget(self._mode_label)
        self._sync_chrome()

        # Follow theme switches however they are triggered (menu, tests):
        # keep the toggle in sync and re-pull themed chrome per open view.
        theme.on_change(self._on_theme_changed)

        # Restore the saved window size/position + toolbar layout LAST, once all
        # toolbars exist and carry their objectNames (a saved geometry wins over
        # the default resize above; a no-op on first launch / a version change).
        self._restore_window_layout()

    # --- active view ----------------------------------------------------
    @property
    def active_view(self) -> DocumentView | None:
        widget = self._tabs.currentWidget()
        return widget if isinstance(widget, DocumentView) else None

    def _views(self) -> list[DocumentView]:
        return [self._tabs.widget(i) for i in range(self._tabs.count())]

    # --- construction ---------------------------------------------------
    def _build_actions(self) -> None:
        self._open_action = QAction("&Open…", self)
        self._open_action.setShortcut(QKeySequence.StandardKey.Open)
        self._open_action.triggered.connect(self.open_file_dialog)

        self._new_window_action = QAction("New &Window", self)
        self._new_window_action.setShortcut(QKeySequence("Ctrl+Shift+N"))
        self._new_window_action.setToolTip("Open a separate, independent window (Ctrl+Shift+N)")
        self._new_window_action.triggered.connect(self.new_window)

        self._merge_action = QAction("&Merge PDFs…", self)
        self._merge_action.triggered.connect(self.merge_documents)

        self._split_action = QAction("&Split PDF…", self)
        self._split_action.triggered.connect(self.split_document)

        self._save_action = QAction("&Save", self)
        self._save_action.setShortcut(QKeySequence.StandardKey.Save)
        self._save_action.triggered.connect(self.save)

        self._save_as_action = QAction("Save &As…", self)
        self._save_as_action.setShortcut(QKeySequence.StandardKey.SaveAs)
        self._save_as_action.triggered.connect(self.save_as)

        # Protection is a DOCUMENT PROPERTY applied at every save (Acrobat's
        # model) — changing or removing it is owner-gated.
        self._protect_action = QAction("Pro&tect document…", self)
        self._protect_action.triggered.connect(self.protect_document)

        self._print_action = QAction("&Print…", self)
        self._print_action.setShortcut(QKeySequence.StandardKey.Print)
        self._print_action.triggered.connect(self.print_current)

        self._prev_action = QAction("&Previous page", self)
        self._prev_action.setShortcut(QKeySequence.StandardKey.MoveToPreviousPage)
        self._prev_action.triggered.connect(self.prev_page)

        self._next_action = QAction("&Next page", self)
        self._next_action.setShortcut(QKeySequence.StandardKey.MoveToNextPage)
        self._next_action.triggered.connect(self.next_page)

        self._first_action = QAction("&First page", self)
        self._first_action.setShortcut(QKeySequence.StandardKey.MoveToStartOfDocument)
        self._first_action.triggered.connect(self.first_page)

        self._last_action = QAction("&Last page", self)
        self._last_action.setShortcut(QKeySequence.StandardKey.MoveToEndOfDocument)
        self._last_action.triggered.connect(self.last_page)

        self._zoom_in_action = QAction("Zoom &in", self)
        self._zoom_in_action.setShortcut(QKeySequence.StandardKey.ZoomIn)
        self._zoom_in_action.triggered.connect(self.zoom_in)

        self._zoom_out_action = QAction("Zoom &out", self)
        self._zoom_out_action.setShortcut(QKeySequence.StandardKey.ZoomOut)
        self._zoom_out_action.triggered.connect(self.zoom_out)

        self._fit_page_action = QAction("Fit &page", self)
        self._fit_page_action.setShortcut("Ctrl+0")
        self._fit_page_action.triggered.connect(self.fit_page)

        self._fit_width_action = QAction("Fit &width", self)
        self._fit_width_action.setShortcut("Ctrl+1")
        self._fit_width_action.triggered.connect(self.fit_width)

        self._zoom_actions = (
            self._zoom_in_action,
            self._zoom_out_action,
            self._fit_page_action,
            self._fit_width_action,
        )

        self._thumbs_action = QAction("&Thumbnails", self)
        self._thumbs_action.setCheckable(True)
        self._thumbs_action.setChecked(self._thumbs_visible)  # persisted (settings)
        self._thumbs_action.toggled.connect(self._toggle_thumbnails)

        # Checked = the mode already applied at startup. Theme IS persisted now
        # (settings.json): app.main applies the saved mode before this window
        # builds, so current_mode() already reflects it; runtime toggles persist
        # via _on_theme_changed.
        self._dark_theme_action = QAction("Dar&k theme", self)
        self._dark_theme_action.setCheckable(True)
        self._dark_theme_action.setChecked(theme.current_mode() == theme.DARK)
        self._dark_theme_action.toggled.connect(self._on_dark_theme_toggled)

        # Per-document Markup/Edit switch (U0). Documents open in MARKUP mode
        # (annotate: highlight/comment/callout, select + copy); checked = the
        # ACTIVE tab is in EDIT mode (content editing). _sync_chrome reflects the
        # active view's mode; the toggle handler drives it.
        self._edit_mode_action = QAction("&Edit mode", self)
        self._edit_mode_action.setCheckable(True)
        self._edit_mode_action.setShortcut("Ctrl+E")
        self._edit_mode_action.toggled.connect(self._on_edit_mode_toggled)

        # "Show editable areas" (U5): outline every paragraph/image on the
        # page. Per-document, edit-mode only, default OFF (decided).
        self._show_areas_action = QAction("Show editable &areas", self)
        self._show_areas_action.setCheckable(True)
        self._show_areas_action.toggled.connect(self._on_show_areas_toggled)

        # Double-click sub-mode (U8): checked = a plain double-click edits
        # the whole PARAGRAPH (Ctrl then edits one line); unchecked = the
        # historic line-first default. Per-document, edit-mode only. The
        # checked state IS the visible indicator (plus the hover hints).
        self._dblclick_para_action = QAction("Dou&ble-click edits paragraph", self)
        self._dblclick_para_action.setCheckable(True)
        self._dblclick_para_action.toggled.connect(self._on_dblclick_para_toggled)

        self._undo_action = self._undo_group.createUndoAction(self, "&Undo")
        self._undo_action.setShortcut(QKeySequence.StandardKey.Undo)

        self._redo_action = self._undo_group.createRedoAction(self, "&Redo")
        self._redo_action.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])

        self._rotate_cw_action = QAction("Rotate &clockwise", self)
        self._rotate_cw_action.setShortcut("Ctrl+R")
        self._rotate_cw_action.triggered.connect(self.rotate_clockwise)

        self._rotate_ccw_action = QAction("Rotate counter-clock&wise", self)
        self._rotate_ccw_action.setShortcut("Ctrl+Shift+R")
        self._rotate_ccw_action.triggered.connect(self.rotate_counterclockwise)

        self._move_up_action = QAction("Move page &up", self)
        self._move_up_action.setShortcut("Ctrl+Shift+Up")
        self._move_up_action.triggered.connect(self.move_page_up)

        self._move_down_action = QAction("Move page &down", self)
        self._move_down_action.setShortcut("Ctrl+Shift+Down")
        self._move_down_action.triggered.connect(self.move_page_down)

        self._delete_action = QAction("&Delete page", self)
        self._delete_action.setShortcut("Ctrl+Delete")
        self._delete_action.triggered.connect(self.delete_current_page)

        self._insert_action = QAction("&Insert pages from file…", self)
        self._insert_action.triggered.connect(self.insert_pages_from_file)

        # Help → the U7 gestures cheat sheet. Never gated — read-only users
        # need it to learn that Edit mode exists.
        self._gestures_action = QAction("Editing &gestures…", self)
        self._gestures_action.triggered.connect(self.show_gesture_help)

        # Help → reveal the diagnostics log so a user hitting a bug can find the
        # file to send back (logs are local + never auto-uploaded).
        self._show_log_action = QAction("Show &diagnostics log", self)
        self._show_log_action.triggered.connect(self.show_diagnostics_log)

        # Help → About (app name, release version, component versions, licence).
        # Never gated.
        self._about_action = QAction("&About PDF Editor", self)
        self._about_action.triggered.connect(self.show_about)

        # Tools → Extract text (X1): a READ feature — enabled whenever a
        # document is open, never edit-gated (extraction mutates nothing).
        self._extract_text_action = QAction("Extract &text…", self)
        self._extract_text_action.triggered.connect(self.extract_text)

        # Tools → Detect & link URLs: a CONTENT op (styles + links text), so
        # edit-mode gated.
        self._detect_links_action = QAction("&Detect && link URLs…", self)
        self._detect_links_action.triggered.connect(self.detect_links)

        # Edit → Find (SR2): the other READ feature — never edit-gated.
        # Search is ALWAYS case-insensitive (by design; no toggle exists).
        self._find_action = QAction("&Find…", self)
        self._find_action.setShortcut(QKeySequence.StandardKey.Find)
        self._find_action.triggered.connect(self.find_in_document)

        # Checkable (U4): checked while the mode is armed; clicking a checked
        # action cancels it. _sync_chrome keeps them truthful.
        self._insert_text_action = QAction("Insert te&xt…", self)
        self._insert_text_action.setCheckable(True)
        self._insert_text_action.triggered.connect(self.insert_text)

        self._insert_image_action = QAction("Insert i&mage…", self)
        self._insert_image_action.setCheckable(True)
        self._insert_image_action.triggered.connect(self.insert_image)

        # ONE hyperlink command: over text it selects a run, over blank space or
        # an image it draws a hotspot (two near-identical buttons was a
        # usability fault — user report).
        self._hyperlink_action = QAction("&Hyperlink…", self)
        self._hyperlink_action.setCheckable(True)
        self._hyperlink_action.setShortcut("Ctrl+K")
        self._hyperlink_action.triggered.connect(self.hyperlink)

        self._highlight_action = QAction("High&light text", self)
        self._highlight_action.setCheckable(True)
        self._highlight_action.setShortcut("Ctrl+Shift+H")
        self._highlight_action.triggered.connect(self.highlight_text)

        self._insert_comment_action = QAction("Insert &comment…", self)
        self._insert_comment_action.setCheckable(True)
        self._insert_comment_action.triggered.connect(self.insert_comment)

        self._insert_callout_action = QAction("Insert c&allout…", self)
        self._insert_callout_action.setCheckable(True)
        self._insert_callout_action.triggered.connect(self.insert_callout)

        # Sign menu. Placing/signing writes a signed COPY (terminal operation,
        # never mutates the open document) — available in BOTH modes like
        # save/print. Placing INITIALS is a content stamp (insert_image), so
        # it rides _page_edit_actions.
        self._place_signature_action = QAction("&Place signature…", self)
        self._place_signature_action.setCheckable(True)
        self._place_signature_action.triggered.connect(self.place_signature)

        self._sign_invisible_action = QAction("Sign &without a visible stamp…", self)
        self._sign_invisible_action.triggered.connect(self.sign_invisible)

        self._place_initials_action = QAction("Place &initials…", self)
        self._place_initials_action.setCheckable(True)
        self._place_initials_action.triggered.connect(self.place_initials)

        self._manage_signatures_action = QAction("&Manage signatures…", self)
        self._manage_signatures_action.triggered.connect(self.manage_signatures)

        self._signature_status_action = QAction("Signature stat&us…", self)
        self._signature_status_action.triggered.connect(self.show_signature_status)

        # CONTENT edits — enabled only in edit mode (U0). Annotation actions
        # deliberately are NOT here: highlight/comment/callout are available in
        # Markup mode too (see _annotate_actions).
        self._page_edit_actions = (
            self._rotate_cw_action,
            self._rotate_ccw_action,
            self._move_up_action,
            self._move_down_action,
            self._delete_action,
            self._insert_action,
            self._insert_text_action,
            self._insert_image_action,
            self._hyperlink_action,
            self._place_initials_action,
        )
        # ANNOTATIONS — markup, available whenever a document is open (Markup
        # mode AND edit mode), like the read features. Enabled on `has` alone.
        self._annotate_actions = (
            self._highlight_action,
            self._insert_comment_action,
            self._insert_callout_action,
        )

        # One icon set (icons.py) + a tooltip on every icon button. Icons are
        # (re-)baked by _assign_icons() at build time and on each theme change
        # (glyph colour follows the mode). The style toolbar extends this map.
        self._icon_keys: dict[QAction, str] = {
            self._open_action: "open",
            self._new_window_action: "new_window",
            self._merge_action: "merge",
            self._split_action: "split",
            self._save_action: "save",
            self._save_as_action: "save_as",
            self._print_action: "print",
            self._prev_action: "prev_page",
            self._next_action: "next_page",
            self._first_action: "first_page",
            self._last_action: "last_page",
            self._zoom_in_action: "zoom_in",
            self._zoom_out_action: "zoom_out",
            self._fit_page_action: "fit_page",
            self._fit_width_action: "fit_width",
            self._thumbs_action: "thumbnails",
            self._dark_theme_action: "dark_theme",
            self._edit_mode_action: "edit_mode",
            self._show_areas_action: "reveal_areas",
            self._dblclick_para_action: "dblclick_paragraph",
            self._gestures_action: "help",
            self._extract_text_action: "extract_text",
            self._detect_links_action: "detect_links",
            self._find_action: "search",
            self._undo_action: "undo",
            self._redo_action: "redo",
            self._rotate_cw_action: "rotate_cw",
            self._rotate_ccw_action: "rotate_ccw",
            self._move_up_action: "move_up",
            self._move_down_action: "move_down",
            self._delete_action: "delete_page",
            self._insert_action: "insert_pages",
            self._insert_text_action: "insert_text",
            self._insert_image_action: "insert_image",
            self._hyperlink_action: "insert_link",
            self._highlight_action: "highlight",
            self._insert_comment_action: "insert_comment",
            self._insert_callout_action: "insert_callout",
            self._protect_action: "protect_document",
            self._place_signature_action: "place_signature",
            self._sign_invisible_action: "sign_invisible",
            self._place_initials_action: "place_initials",
            self._manage_signatures_action: "manage_signatures",
            self._signature_status_action: "signature_status",
        }
        tooltips = {
            self._open_action: "Open a PDF (Ctrl+O)",
            self._merge_action: "Merge several PDFs into one file",
            self._split_action: "Split a PDF into page ranges",
            self._save_action: "Save (Ctrl+S)",
            self._save_as_action: "Save as a new file (Ctrl+Shift+S)",
            self._print_action: "Print (Ctrl+P)",
            self._prev_action: "Previous page (Page Up)",
            self._next_action: "Next page (Page Down)",
            self._first_action: "First page (Ctrl+Home)",
            self._last_action: "Last page (Ctrl+End)",
            self._zoom_in_action: "Zoom in (Ctrl++)",
            self._zoom_out_action: "Zoom out (Ctrl+-)",
            self._fit_page_action: "Fit the whole page in the window (Ctrl+0)",
            self._fit_width_action: "Fit the page width (Ctrl+1)",
            self._thumbs_action: "Show or hide the thumbnail sidebar",
            self._dark_theme_action: "Toggle dark / light theme",
            self._edit_mode_action: "Edit mode — allow changing this document (Ctrl+E)",
            self._show_areas_action: "Show editable areas — outline everything editable",
            self._dblclick_para_action: (
                "Double-click edits the whole paragraph (Ctrl+double-click then edits"
                " one line); off: the reverse"
            ),
            self._undo_action: "Undo (Ctrl+Z)",
            self._redo_action: "Redo (Ctrl+Y)",
            self._rotate_cw_action: "Rotate page clockwise (Ctrl+R)",
            self._rotate_ccw_action: "Rotate page counter-clockwise (Ctrl+Shift+R)",
            self._move_up_action: "Move page up (Ctrl+Shift+Up)",
            self._move_down_action: "Move page down (Ctrl+Shift+Down)",
            self._delete_action: "Delete page (Ctrl+Delete)",
            self._insert_action: "Insert pages from another PDF",
            self._gestures_action: "Every editing gesture on one page",
            self._extract_text_action: "Extract the document's text (OCR for scanned pages)",
            self._detect_links_action: (
                "Find web/email addresses in the text and turn them into styled hyperlinks"
            ),
            self._find_action: "Find in document (Ctrl+F)",
            self._insert_text_action: "Insert text — then click the page to place it",
            self._insert_image_action: "Insert an image — then click the page to place it",
            self._hyperlink_action: (
                "Hyperlink (Ctrl+K) — drag over text to link a run (click a word, "
                "triple-click a sentence), or drag a box over an image"
            ),
            self._highlight_action: (
                "Highlight text (Ctrl+Shift+H) — the current selection, or drag a window over text"
            ),
            self._insert_comment_action: (
                "Add a review comment — markup that doesn't print unless chosen at print time"
            ),
            self._insert_callout_action: (
                "Add a callout comment — click the target, then place the box"
            ),
        }
        for action, tip in tooltips.items():
            action.setToolTip(tip)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(self._open_action)
        # Fly-out of the last few opened files; rebuilt each time it's shown so
        # it always reflects the current list (like the Documents menu).
        self._recent_menu = file_menu.addMenu("Open &Recent")
        self._recent_menu.aboutToShow.connect(self._rebuild_recent_menu)
        self._rebuild_recent_menu()  # populate now so tests see it pre-show
        file_menu.addAction(self._new_window_action)
        file_menu.addSeparator()
        file_menu.addAction(self._save_action)
        file_menu.addAction(self._save_as_action)
        file_menu.addAction(self._protect_action)
        file_menu.addSeparator()
        file_menu.addAction(self._print_action)
        file_menu.addSeparator()
        file_menu.addAction(self._merge_action)
        file_menu.addAction(self._split_action)

        go_menu = self.menuBar().addMenu("&Go")
        for action in (
            self._prev_action,
            self._next_action,
            self._first_action,
            self._last_action,
        ):
            go_menu.addAction(action)

        view_menu = self.menuBar().addMenu("&View")
        for action in self._zoom_actions:
            view_menu.addAction(action)
        view_menu.addSeparator()
        view_menu.addAction(self._thumbs_action)
        view_menu.addSeparator()
        view_menu.addAction(self._dark_theme_action)

        edit_menu = self.menuBar().addMenu("&Edit")
        edit_menu.addAction(self._find_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self._edit_mode_action)
        edit_menu.addAction(self._show_areas_action)
        edit_menu.addAction(self._dblclick_para_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self._undo_action)
        edit_menu.addAction(self._redo_action)
        edit_menu.addSeparator()
        for action in self._page_edit_actions:
            edit_menu.addAction(action)

        # Annotations (markup) are their own menu — available in Markup mode,
        # not gated on edit mode. Kept as an attribute (shiboken invalidates
        # transient menu wrappers; A4 adds the highlight-colour submenu here).
        self._annotate_menu = self.menuBar().addMenu("&Annotate")
        self._annotate_menu.addAction(self._highlight_action)
        self._build_highlight_color_menu(self._annotate_menu)
        self._annotate_menu.addSeparator()
        self._annotate_menu.addAction(self._insert_comment_action)
        self._annotate_menu.addAction(self._insert_callout_action)

        # Kept as an attribute (like _window_menu): shiboken invalidates
        # transient wrappers fetched back via QAction.menu(), so tests and
        # future additions use this handle instead.
        self._tools_menu = self.menuBar().addMenu("&Tools")
        self._tools_menu.addAction(self._extract_text_action)
        self._tools_menu.addAction(self._detect_links_action)

        self._sign_menu = self.menuBar().addMenu("&Sign")
        self._sign_menu.addAction(self._place_signature_action)
        self._sign_menu.addAction(self._sign_invisible_action)
        self._sign_menu.addSeparator()
        self._sign_menu.addAction(self._place_initials_action)
        self._sign_menu.addSeparator()
        self._sign_menu.addAction(self._signature_status_action)
        self._sign_menu.addAction(self._manage_signatures_action)

        # Lists the open document TABS of THIS window (populated by
        # _rebuild_window_menu). Named "Documents", not "Window": with File →
        # New Window opening a real separate window, a "Window" menu that only
        # switched tabs read as contradictory (user feedback). The attribute
        # keeps its name — tests and the rebuild helper reference it.
        self._window_menu = self.menuBar().addMenu("&Documents")

        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction(self._gestures_action)
        help_menu.addAction(self._show_log_action)
        help_menu.addSeparator()
        help_menu.addAction(self._about_action)

    # --- theme ------------------------------------------------------------
    def _on_dark_theme_toggled(self, checked: bool) -> None:
        app = QApplication.instance()
        mode = theme.DARK if checked else theme.LIGHT
        if app is not None and theme.current_mode() != mode:
            theme.apply_theme(app, mode)

    def _on_theme_changed(self, mode: str) -> None:
        # No-op re-entry: setChecked fires the toggle handler, which sees
        # current_mode() already equals the target and does nothing.
        self._dark_theme_action.setChecked(mode == theme.DARK)
        self._assign_icons()  # glyph colour follows the mode
        for view in self._views():
            view.refresh_theme()
        # Persist so the next launch starts in this mode (app.main reads it
        # before the window builds). Fires for every apply after construction.
        self._settings.set("theme", mode)

    def _assign_icons(self) -> None:
        """(Re-)bake themed icons for every action — build time + theme change."""
        for action, key in self._icon_keys.items():
            action.setIcon(icons.icon(key))
        # The alignment button borrows the active option's freshly-baked icon
        # (it is a QToolButton, so it isn't in the action map itself).
        self._update_align_button()

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Navigation", self)
        # Unique objectName so QMainWindow.saveState()/restoreState() persists
        # this bar's position (Qt silently drops unnamed toolbars from state).
        toolbar.setObjectName("navigation_toolbar")
        toolbar.setIconSize(QSize(20, 20))  # match the style toolbar
        toolbar.addAction(self._prev_action)

        # A quiet "N / total" cluster: the spinbox is a plain centred number
        # field (no up/down buttons — the chevrons and PgUp/PgDn step pages;
        # type a number + Enter to jump). Styled via theme.py's addendum.
        self._page_spin = QSpinBox(self)
        self._page_spin.setObjectName("page_spin")
        self._page_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self._page_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._page_spin.setToolTip("Current page — type a number and press Enter to jump")
        self._page_spin.setMinimum(1)
        self._page_spin.setMaximum(1)
        self._page_spin.setEnabled(False)
        self._page_spin.valueChanged.connect(self._on_spin_changed)
        toolbar.addWidget(self._page_spin)

        self._page_total_label = QLabel("/ 0", self)
        self._page_total_label.setObjectName("page_total")
        toolbar.addWidget(self._page_total_label)

        toolbar.addAction(self._next_action)
        toolbar.addSeparator()
        toolbar.addAction(self._zoom_out_action)
        toolbar.addAction(self._zoom_in_action)
        toolbar.addAction(self._fit_page_action)
        toolbar.addAction(self._fit_width_action)
        toolbar.addSeparator()
        toolbar.addAction(self._edit_mode_action)
        # Insert text/image are the frequent editing entry points, so they get
        # the toolbar slots (E11.4, user request); the reveal-all and
        # double-click sub-mode TOGGLES moved to menu-only (less used).
        toolbar.addAction(self._insert_text_action)
        toolbar.addAction(self._insert_image_action)
        toolbar.addAction(self._hyperlink_action)
        toolbar.addSeparator()
        toolbar.addAction(self._rotate_ccw_action)
        toolbar.addAction(self._rotate_cw_action)
        toolbar.addAction(self._move_up_action)
        toolbar.addAction(self._move_down_action)
        toolbar.addAction(self._delete_action)
        toolbar.addSeparator()
        toolbar.addAction(self._extract_text_action)
        self.addToolBar(toolbar)

    def _build_style_toolbar(self) -> None:
        """The text-style toolbar: font, size, bold, underline, strike, colour, scripts.

        Drives inserted/replacement text. Opening a span/paragraph editor
        populates it from the clicked text; commit applies the (possibly
        adjusted) style. Base-14 family picks stay non-embedded; any other
        system font is embedded as a subset (deliberate choice — see
        CLAUDE.md font rule).
        """
        bar = QToolBar("Text style", self)
        bar.setObjectName("text_style_toolbar")
        bar.setIconSize(QSize(20, 20))  # match the navigation toolbar

        self._font_combo = QFontComboBox(self)
        self._font_combo.setCurrentFont(QFont("Arial"))
        self._font_combo.setToolTip("Font for inserted or replacement text")
        # The combo's font DATABASE may substitute a family it doesn't know
        # (e.g. a document font that isn't installed) — remember the family we
        # populated from so an untouched combo round-trips the ORIGINAL name;
        # a manual user pick clears the override.
        self._style_family_override: str | None = None
        self._populating_style = False
        self._font_combo.currentFontChanged.connect(self._on_font_combo_changed)
        bar.addWidget(self._font_combo)

        self._size_spin = QDoubleSpinBox(self)
        self._size_spin.setRange(4.0, 96.0)
        self._size_spin.setValue(9.0)  # launch default: the quotes' body size
        self._size_spin.setDecimals(1)
        self._size_spin.setSuffix(" pt")
        self._size_spin.setToolTip("Text size — type a value or use the arrows")
        # Typing "12" must not apply 1pt then 12pt: fire only on Enter/arrows.
        self._size_spin.setKeyboardTracking(False)
        bar.addWidget(self._size_spin)

        self._bold_action = QAction("Bold", self)
        self._bold_action.setCheckable(True)
        self._bold_action.setToolTip("Bold (Ctrl+B)")
        self._bold_action.setShortcut(QKeySequence.StandardKey.Bold)
        bar.addAction(self._bold_action)

        self._italic_action = QAction("Italic", self)
        self._italic_action.setCheckable(True)
        self._italic_action.setToolTip("Italic (Ctrl+I)")
        self._italic_action.setShortcut(QKeySequence.StandardKey.Italic)
        bar.addAction(self._italic_action)

        self._underline_action = QAction("Underline", self)
        self._underline_action.setCheckable(True)
        self._underline_action.setToolTip(
            "Underline (Ctrl+U — drawn as a line; PDF has no underline fonts)"
        )
        self._underline_action.setShortcut(QKeySequence.StandardKey.Underline)
        bar.addAction(self._underline_action)

        self._strike_action = QAction("Strikethrough", self)
        self._strike_action.setCheckable(True)
        self._strike_action.setToolTip(
            "Strikethrough (drawn as a line; PDF has no strikethrough fonts)"
        )
        bar.addAction(self._strike_action)

        self._super_action = QAction("Superscript", self)
        self._super_action.setCheckable(True)
        self._super_action.setToolTip("Superscript")
        self._sub_action = QAction("Subscript", self)
        self._sub_action.setCheckable(True)
        self._sub_action.setToolTip("Subscript")
        self._super_action.toggled.connect(lambda on: on and self._sub_action.setChecked(False))
        self._sub_action.toggled.connect(lambda on: on and self._super_action.setChecked(False))
        bar.addAction(self._super_action)
        bar.addAction(self._sub_action)

        # Justification (user request): ONE button showing the ACTIVE option;
        # its dropdown lists all three. The pick is STICKY — persisted like the
        # highlighter colour, so the button starts on the last-used option at
        # the next launch and every insert uses it until changed. It follows
        # the SAME InstantPopup pattern as the highlighter swatch (click opens
        # the list; picking the already-active one re-applies it — the menu
        # action fires regardless of its check state).
        self._text_align = self._startup_text_align()
        self._align_actions: dict[str, QAction] = {}
        align_menu = QMenu(self)
        align_group = QActionGroup(self)
        align_group.setExclusive(True)
        for key, label in (("left", "Align left"), ("center", "Centre"), ("right", "Align right")):
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(key == self._text_align)
            action.setToolTip(f"{label} (paragraphs and inserted text)")
            action.triggered.connect(lambda _checked=False, k=key: self._pick_text_align(k))
            align_group.addAction(action)
            align_menu.addAction(action)
            self._align_actions[key] = action
            self._icon_keys[action] = f"align_{key}"
        self._align_button = self._make_dropdown_button(align_menu, "Text alignment")
        bar.addWidget(self._align_button)

        self._text_color = QColor(0, 0, 0)
        self._color_button = QToolButton(self)
        self._color_button.setToolTip("Text colour")
        self._color_button.clicked.connect(self._pick_text_color)
        self._update_color_swatch()
        bar.addWidget(self._color_button)

        # The live colour swatch stays a painted pixmap (it IS the meaning);
        # everything else joins the one icon set.
        self._icon_keys.update(
            {
                self._bold_action: "bold",
                self._italic_action: "italic",
                self._underline_action: "underline",
                self._strike_action: "strikethrough",
                self._super_action: "superscript",
                self._sub_action: "subscript",
            }
        )

        # No style control may steal keyboard focus from an open in-place
        # editor — otherwise clicking one would move focus off the editor and
        # break the commit-on-Enter flow (and, before the focus-out fix, it
        # cancelled the edit outright). Mouse clicks still drive every control.
        style_actions = (
            self._bold_action,
            self._italic_action,
            self._underline_action,
            self._strike_action,
            self._super_action,
            self._sub_action,
        )
        # (The align button gets NoFocus from _make_dropdown_button.)
        for widget in (self._font_combo, self._color_button):
            widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # The size spin ACCEPTS focus (click into it and type a size) — the
        # open editor stays open (overlays never cancel on focus-out), and
        # editingFinished hands focus straight back to it.
        self._size_spin.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._size_spin.editingFinished.connect(self._refocus_open_editor)
        for action in style_actions:
            button = bar.widgetForAction(action)
            if button is not None:
                button.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        # With an editor OPEN, toolbar changes format the current selection
        # (make just these words bold); with none open they set the style for
        # the next insert. Guarded so populate-from-context doesn't echo back.
        self._bold_action.toggled.connect(lambda on: self._apply_to_selection(_weight_format(on)))
        self._italic_action.toggled.connect(
            lambda on: self._apply_to_selection(_flag_format("italic", on))
        )
        self._underline_action.toggled.connect(
            lambda on: self._apply_to_selection(_flag_format("underline", on))
        )
        self._strike_action.toggled.connect(
            lambda on: self._apply_to_selection(_flag_format("strikethrough", on))
        )
        self._super_action.toggled.connect(lambda _on: self._apply_script_to_selection())
        self._sub_action.toggled.connect(lambda _on: self._apply_script_to_selection())
        self._size_spin.valueChanged.connect(self._apply_size_to_selection)
        self._font_combo.currentFontChanged.connect(self._apply_family_to_selection)

        self.addToolBar(bar)
        self._capture_global_style()  # launch state IS the initial defaults

    def _make_dropdown_button(self, menu: QMenu, tooltip: str) -> QToolButton:
        """A flat icon button whose click opens ``menu`` — the InstantPopup
        pattern the highlighter swatch established, now shared with the
        alignment button.

        InstantPopup (not MenuButtonPopup) is deliberate: the whole button is
        one flat surface with a small corner dropdown arrow (the base style's
        ``::menu-indicator``), so it is the same width as a plain icon button
        and carries no ``::menu-button`` split region — which is exactly the
        Fusion sub-control that rendered as a raised pill and forced the
        asymmetric padding. NoFocus so an open in-place editor keeps the caret
        (the same rule the other style controls follow). theme.py styles the
        hover / pressed state layers; the arrow is left to the base style
        (stylesheet it and it vanishes)."""
        button = QToolButton(self)
        button.setToolTip(tooltip)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setMenu(menu)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return button

    def _build_annotate_toolbar(self) -> None:
        """The Annotate toolbar: highlight + colour swatch + comment + callout.

        Always visible and enabled outside edit mode (its actions are in
        _annotate_actions, enabled on `has` in _sync_chrome) — the whole point
        of Markup mode. objectName so saveState persists its position.
        """
        bar = QToolBar("Annotate", self)
        bar.setObjectName("annotate_toolbar")
        bar.setIconSize(QSize(20, 20))
        bar.addAction(self._highlight_action)

        # Highlighter-colour swatch: a painted button whose dropdown is the
        # restricted palette — the SAME QActions as the Annotate ▸ Highlight
        # colour submenu, so the checkmarks stay in sync across both.
        swatch_menu = QMenu(self)
        for action in self._highlight_color_actions.values():
            swatch_menu.addAction(action)
        self._highlight_color_button = self._make_dropdown_button(swatch_menu, "Highlighter colour")
        self._update_highlight_swatch()
        bar.addWidget(self._highlight_color_button)

        bar.addSeparator()
        bar.addAction(self._insert_comment_action)
        bar.addAction(self._insert_callout_action)
        self.addToolBar(bar)

    def _refocus_open_editor(self) -> None:
        """After typing a size, put the caret back in the in-place editor."""
        if (view := self.active_view) is not None:
            view.focus_open_editor()

    def _apply_to_selection(self, fmt: QTextCharFormat) -> None:
        if self._populating_style:
            return
        if (view := self.active_view) is not None:
            view.apply_format_to_editor(fmt)
        self._maybe_capture_global_style()  # no editor -> a default change

    def _apply_script_to_selection(self) -> None:
        if self._populating_style:
            return
        fmt = QTextCharFormat()
        if self._super_action.isChecked():
            fmt.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignSuperScript)
        elif self._sub_action.isChecked():
            fmt.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignSubScript)
        else:
            fmt.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignNormal)
        if (view := self.active_view) is not None:
            view.apply_format_to_editor(fmt)
        self._maybe_capture_global_style()

    def _apply_size_to_selection(self, size_pt: float) -> None:
        if self._populating_style:
            return
        if (view := self.active_view) is not None:
            view.apply_size_pt_to_editor(size_pt)
        self._maybe_capture_global_style()

    def _apply_family_to_selection(self, font: QFont) -> None:
        if self._populating_style:
            return
        family_font = QFont()
        family_font.setFamily(font.family())
        fmt = QTextCharFormat()
        fmt.setFont(
            family_font,
            QTextCharFormat.FontPropertiesInheritanceBehavior.FontPropertiesSpecifiedOnly,
        )
        if (view := self.active_view) is not None:
            view.apply_format_to_editor(fmt)
        self._maybe_capture_global_style()

    def _pick_text_color(self) -> None:
        color = QColorDialog.getColor(self._text_color, self, "Text colour")
        if color.isValid():
            self._text_color = color
            self._update_color_swatch()
            fmt = QTextCharFormat()
            fmt.setForeground(color)
            self._apply_to_selection(fmt)

    def _update_color_swatch(self, mixed: bool = False) -> None:
        """The colour button's pixmap IS its state: the current colour, or —
        when the selection spans several colours — a neutral crossed swatch
        (the swatch's version of a blanked field / unchecked toggle)."""
        self._color_swatch_mixed = mixed
        swatch = QPixmap(16, 16)
        if mixed:
            swatch.fill(QColor(255, 255, 255))
            painter = QPainter(swatch)
            painter.setPen(QPen(QColor(128, 128, 128)))
            painter.drawRect(0, 0, 15, 15)
            painter.drawLine(2, 13, 13, 2)
            painter.end()
        else:
            swatch.fill(self._text_color)
        self._color_button.setIcon(swatch)

    # --- text justification -----------------------------------------------
    def _startup_text_align(self) -> str:
        """The persisted last-used justification (a hand-edited/sanitized
        value falls back to the do-no-harm left)."""
        value = self._settings.get("last_text_align", "left")
        return value if value in ALIGNMENTS else "left"

    def current_text_align(self) -> str:
        """The toolbar's justification — what a commit applies (align_provider)."""
        return self._text_align

    def _pick_text_align(self, align: str) -> None:
        """A deliberate user pick: reflect it, persist it as the last-used
        option, and apply it to an open paragraph editor so the change shows
        immediately (the commit reads the same value)."""
        if align not in ALIGNMENTS or self._populating_style:
            return
        self._reflect_text_align(align)
        self._settings.set("last_text_align", align)
        if (view := self.active_view) is not None:
            view.apply_alignment_to_editor(align)
        self._maybe_capture_global_style()  # no editor -> a default change

    def _reflect_text_align(self, align: str) -> None:
        """Show ``align`` as the active option (state + button + checkmarks).
        Display only — reflection from a clicked paragraph must not persist a
        choice the user never made (the E11.3 independence rule)."""
        if align not in ALIGNMENTS:
            return
        self._text_align = align
        for key, action in self._align_actions.items():
            action.setChecked(key == align)
        self._update_align_button()

    def _update_align_button(self) -> None:
        """The button wears the ACTIVE option's icon and name."""
        if not hasattr(self, "_align_button"):
            return
        action = self._align_actions[self._text_align]
        self._align_button.setIcon(action.icon())
        self._align_button.setToolTip(f"{action.text()} — click the arrow for other options")

    # --- highlighter colour (A4) ------------------------------------------
    def _startup_highlight_hex(self) -> str:
        """The persisted highlighter colour, restricted to the palette (a
        sanitized/hand-edited value falls back to the default yellow)."""
        hexstr = self._settings.get("last_highlight_color", highlight_colors.DEFAULT_HIGHLIGHT)
        if highlight_colors.is_palette_hex(hexstr):
            return hexstr
        return highlight_colors.DEFAULT_HIGHLIGHT

    def _swatch_pixmap(self, color: QColor, size: int = 16) -> QPixmap:
        """A rounded filled swatch — reused by the colour menu + toolbar button."""
        pix = QPixmap(size, size)
        pix.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(color)
        painter.setPen(QPen(QColor(128, 128, 128)))
        painter.drawRoundedRect(1, 1, size - 2, size - 2, 3, 3)
        painter.end()
        return pix

    def _build_highlight_color_menu(self, parent) -> None:
        """A 'Highlight colour' submenu of the restricted palette (exclusive
        checkable actions). Reused as the toolbar swatch's dropdown (A5)."""
        submenu = parent.addMenu("Highlight &colour")
        self._highlight_color_menu = submenu
        self._highlight_color_group = QActionGroup(self)
        self._highlight_color_group.setExclusive(True)
        self._highlight_color_actions: dict[str, QAction] = {}
        current = self._highlight_color.name().upper()
        for name, hexstr in highlight_colors.HIGHLIGHTER_COLORS:
            action = submenu.addAction(QIcon(self._swatch_pixmap(QColor(hexstr))), name)
            action.setCheckable(True)
            action.setChecked(hexstr.upper() == current)
            self._highlight_color_group.addAction(action)
            action.triggered.connect(lambda _c=False, h=hexstr: self._pick_highlight_color(h))
            self._highlight_color_actions[hexstr.upper()] = action

    def _pick_highlight_color(self, hexstr: str) -> None:
        """Set the highlighter colour: persist it, reflect it in the chrome, and
        push it to every open view so subsequent highlights use it."""
        self._highlight_color = QColor(hexstr)
        self._settings.set("last_highlight_color", hexstr)
        self._refresh_highlight_color_chrome()
        rgb = self._highlight_color_rgb()
        for view in self._views():
            view.set_highlight_color(rgb)

    def _refresh_highlight_color_chrome(self) -> None:
        """Reflect the current highlighter colour in the menu checkmarks and the
        Annotate toolbar swatch."""
        current = self._highlight_color.name().upper()
        for hexstr, action in self._highlight_color_actions.items():
            action.setChecked(hexstr == current)
        self._update_highlight_swatch()

    def _update_highlight_swatch(self) -> None:
        """Repaint the toolbar swatch button to the current colour. Its painted
        pixmap IS its meaning (like the text-colour swatch), so it is excluded
        from _icon_keys and never re-baked on a theme change."""
        if hasattr(self, "_highlight_color_button"):
            self._highlight_color_button.setIcon(QIcon(self._swatch_pixmap(self._highlight_color)))

    def _highlight_color_rgb(self) -> tuple[float, float, float]:
        """The current highlighter colour as engine ``(r, g, b)`` 0-1 floats."""
        c = self._highlight_color
        return (c.redF(), c.greenF(), c.blueF())

    def _on_font_combo_changed(self, _font) -> None:
        if not self._populating_style:
            self._style_family_override = None  # deliberate user choice wins

    def current_text_style(self) -> tuple[TextStyle, QFont]:
        """The toolbar state as an engine TextStyle plus a preview QFont."""
        family = self._style_family_override or self._font_combo.currentFont().family()
        bold = self._bold_action.isChecked()
        italic = self._italic_action.isChecked()
        code, fontfile, resolved = _font_choice(family, bold, italic)
        if not resolved:
            self.statusBar().showMessage(
                f"No font file found for {family} — using Helvetica.", 8000
            )
        if self._super_action.isChecked():
            script = SCRIPT_SUPER
        elif self._sub_action.isChecked():
            script = SCRIPT_SUB
        else:
            script = SCRIPT_NORMAL
        color = (
            (self._text_color.red() << 16)
            | (self._text_color.green() << 8)
            | self._text_color.blue()
        )
        style = TextStyle(
            code=code,
            fontfile=fontfile,
            size=float(self._size_spin.value()),
            color=color,
            underline=self._underline_action.isChecked(),
            strike=self._strike_action.isChecked(),
            script=script,
        )
        preview = QFont(self._font_combo.currentFont())
        preview.setBold(bold)
        preview.setItalic(italic)
        preview.setUnderline(style.underline)
        preview.setStrikeOut(style.strike)
        return style, preview

    def _set_size_field(self, size) -> None:
        if size is None:
            self._size_spin.setSuffix("")  # clear() keeps the suffix — drop it
            self._size_spin.clear()  # display-only: blank until next change
        else:
            self._size_spin.setSuffix(" pt")
            self._size_spin.setValue(float(size))
            self._size_spin.lineEdit().setText(self._size_spin.textFromValue(float(size)) + " pt")

    def _on_selection_format_changed(self, fmt: dict) -> None:
        """Track the open editor's selection in the toolbar (E10.6 + E11.3):
        uniform values show as the actual size / checked toggle; MIXED
        values blank the field / uncheck the toggle. Reflection never
        re-applies (guard) and never touches the GLOBAL insert defaults."""
        self._populating_style = True
        try:
            self._set_size_field(fmt.get("size"))
            self._bold_action.setChecked(bool(fmt.get("bold")))
            self._italic_action.setChecked(bool(fmt.get("italic")))
            self._underline_action.setChecked(bool(fmt.get("underline")))
            self._strike_action.setChecked(bool(fmt.get("strike")))
            # Scripts and colour follow the SAME rules (user request,
            # 2026-07-18): uniform selection shows its state, mixed (None)
            # unchecks both script toggles / neutralises the swatch.
            script = fmt.get("script")
            self._super_action.setChecked(script == SCRIPT_SUPER)
            self._sub_action.setChecked(script == SCRIPT_SUB)
            color = fmt.get("color")
            if color is None:
                self._update_color_swatch(mixed=True)
            else:
                self._text_color = QColor((color >> 16) & 255, (color >> 8) & 255, color & 255)
                self._update_color_swatch()
        finally:
            self._populating_style = False

    def _capture_global_style(self) -> None:
        """Snapshot the controls as the GLOBAL insert defaults — called when
        they change with NO editor open (a deliberate default change)."""
        if self._super_action.isChecked():
            script = SCRIPT_SUPER
        elif self._sub_action.isChecked():
            script = SCRIPT_SUB
        else:
            script = SCRIPT_NORMAL
        self._global_style = {
            "bold": self._bold_action.isChecked(),
            "italic": self._italic_action.isChecked(),
            "underline": self._underline_action.isChecked(),
            "strike": self._strike_action.isChecked(),
            "script": script,
            "size": float(self._size_spin.value()),
            "family": self._font_combo.currentFont().family(),
            "color": QColor(self._text_color),
            "align": self._text_align,
        }

    def _maybe_capture_global_style(self) -> None:
        view = self.active_view
        if view is None or not view.has_open_editor:
            self._capture_global_style()

    def _on_editor_closed(self) -> None:
        """An editor session ended: restore the GLOBAL defaults to the
        controls (selection reflection must not bleed into the next
        insert's style — E11.3, user request)."""
        style = getattr(self, "_global_style", None)
        if style is None:
            return
        self._populating_style = True
        try:
            self._bold_action.setChecked(style["bold"])
            self._italic_action.setChecked(style["italic"])
            self._underline_action.setChecked(style["underline"])
            self._strike_action.setChecked(style["strike"])
            self._super_action.setChecked(style["script"] == SCRIPT_SUPER)
            self._sub_action.setChecked(style["script"] == SCRIPT_SUB)
            self._set_size_field(style["size"])
            self._font_combo.setCurrentFont(QFont(style["family"]))
            self._text_color = QColor(style["color"])
            self._update_color_swatch()
            self._reflect_text_align(style["align"])
        finally:
            self._populating_style = False

    def _populate_style_from(self, info) -> None:
        """Reflect a clicked span/paragraph in the toolbar (edit flows)."""
        code = info.base14 or ""
        if code.startswith("ti"):
            family = "Times New Roman"
        elif code.startswith("co"):
            family = "Courier New"
        elif code.startswith("he"):
            family = "Arial"
        else:
            family = info.font  # embedded/unmapped: best-effort family name
        self._populating_style = True
        try:
            self._font_combo.setCurrentFont(QFont(family))
            # Reflection, not user intent: EVERYTHING here stays under the
            # guard — an unguarded setChecked fired the apply handler before
            # the editor registered as open and clobbered the GLOBAL defaults
            # (caught by the E11.3 independence test).
            self._size_spin.setValue(float(info.size))
            self._bold_action.setChecked(bool(info.flags & FLAG_BOLD))
            self._italic_action.setChecked(bool(info.flags & FLAG_ITALIC))
            self._text_color = QColor(
                (info.color >> 16) & 255, (info.color >> 8) & 255, info.color & 255
            )
            self._update_color_swatch()
            # Underline/strike ARE detectable (drawn rules found at extraction
            # — TextSpan.underline/strike); a Paragraph carries no such attr, so
            # getattr falls back to unchecked. Scripts stay undetectable.
            self._underline_action.setChecked(bool(getattr(info, "underline", False)))
            self._strike_action.setChecked(bool(getattr(info, "strike", False)))
            self._super_action.setChecked(False)
            self._sub_action.setChecked(False)
            # A Paragraph carries a DETECTED justification; a single span has
            # none (nothing to justify against) — leave the button alone then,
            # so the user's last-used option stays visible.
            self._reflect_text_align(getattr(info, "align", self._text_align))
        finally:
            self._populating_style = False
        self._style_family_override = family
        shown = self._font_combo.currentFont().family()
        if shown.lower() != family.lower():
            # The preview substitutes an installed family; the COMMIT still
            # resolves the original name (falling back honestly if it can't).
            self.statusBar().showMessage(
                f"Font {family} isn't installed — {shown} is shown while editing.", 8000
            )

    # --- open flow ------------------------------------------------------
    def open_file_dialog(self) -> None:
        # getOpenFileNames (plural): ctrl/shift-select several PDFs and they all
        # open as tabs, matching drag-drop and multi-file "Open" from Explorer.
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open PDF", "", "PDF files (*.pdf);;All files (*)"
        )
        for path_str in paths:
            self.open_path(Path(path_str))

    @staticmethod
    def _new_window_command() -> tuple[list[str], dict[str, str]]:
        """Argv + env to launch a SEPARATE, independent app window.

        The child opts out of single-instance (``PDF_EDITOR_NO_SINGLE_INSTANCE``)
        so it gets its own window instead of forwarding a bare launch to us — the
        deliberate multi-window path. Frozen: re-run the packaged exe; from
        source: ``python -m pdfapp`` with ``src`` on ``PYTHONPATH`` so the child
        can import the package even when it was only on the parent's sys.path.
        """
        env = {**os.environ, "PDF_EDITOR_NO_SINGLE_INSTANCE": "1"}
        if getattr(sys, "frozen", False):
            return [sys.executable], env
        src = str(Path(__file__).resolve().parents[1])  # .../src
        env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
        return [sys.executable, "-m", "pdfapp"], env

    def new_window(self) -> None:
        """Open a fresh, independent window (a new process). File → New Window."""
        args, env = self._new_window_command()
        try:
            subprocess.Popen(args, env=env)  # noqa: S603 - fixed argv, our own exe
        except OSError as exc:
            self.statusBar().showMessage(f"Couldn't open a new window: {exc}", 8000)

    def open_path(self, path: Path) -> None:
        # Focus an already-open tab for the same file rather than duplicating it.
        existing = self._find_tab(path)
        if existing is not None:
            self._tabs.setCurrentWidget(existing)
            self._recent_files.add(path)  # bump it to the front of Open Recent
            # Re-announce a signed document's status: the original flag is
            # transient, and re-opening a tampered file from Explorer an hour
            # later must not read as a clean bill of health.
            self._announce_signatures(existing)
            return

        try:
            doc = PdfDocument.open(path)
        except Exception as exc:  # noqa: BLE001 - surface any open error to the user
            diagnostics.log_event(f"open failed: {path.name}: {exc}")
            QMessageBox.critical(self, "Open failed", f"Could not open:\n{path}\n\n{exc}")
            return

        password: str | None = None
        if doc.needs_pass:
            password = self._prompt_password(doc, path)
            if password is None:
                doc.close()
                return

        view = DocumentView(doc)
        view.open_password = password  # memory-only; signature checks need it
        self._add_view(view)
        # Only successfully-opened files land in Open Recent (a failed open /
        # cancelled password prompt returned above).
        self._recent_files.add(path)
        # A breadcrumb so a later hang/crash log shows what was open (no-op until
        # diagnostics.install has run, i.e. never in tests).
        diagnostics.log_event(f"opened {path.name} ({doc.page_count} pages)")
        # Signed documents get Acrobat-style status on arrival — a TAMPERED
        # file must be flagged, not silently rendered.
        self._announce_signatures(view)

    # --- single-instance external open ----------------------------------
    def handle_external_open(self, paths: list[str]) -> None:
        """Open files forwarded by a second launch as tabs, then surface us.

        Wired to `single_instance.SingleInstanceServer`: when the app is already
        running, a new `pdf-editor.exe "<file>"` (double-click / "Open with")
        routes here instead of starting a second window. Empty `paths` = a bare
        re-launch (Start-menu / no file): just raise the window.
        """
        for p in paths:
            self.open_path(Path(p))
        self.bring_to_front()

    def bring_to_front(self) -> None:
        """Restore + raise + activate the window (used by single-instance).

        On Windows, ``activateWindow()`` from a background process only
        FLASHES the taskbar icon — the OS refuses the focus change unless the
        foreground process granted it. The forwarding secondary grants that
        right (``AllowSetForegroundWindow`` in ``single_instance``) and the
        explicit ``SetForegroundWindow`` here claims it.
        """
        self.setWindowState(
            (self.windowState() & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive
        )
        self.show()
        self.raise_()
        self.activateWindow()
        if sys.platform == "win32":
            try:
                import ctypes

                ctypes.windll.user32.SetForegroundWindow(int(self.winId()))
            except Exception:
                pass  # cosmetic — never let chrome break the open itself

    # --- drag-and-drop open ---------------------------------------------
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        # Accept only when the drag carries at least one local PDF, so the
        # cursor shows a valid drop target and dropEvent then fires.
        if _dropped_pdf_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = _dropped_pdf_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        # Same entry point as File > Open: focus an already-open tab, prompt
        # for a password if needed, add a tab per dropped document.
        for path in paths:
            self.open_path(path)

    def _add_view(self, view: DocumentView) -> None:
        view.set_thumbnails_visible(self._thumbs_visible)
        # Seed the persisted app-level defaults into the fresh view BEFORE
        # wiring stateChanged (these are per-document flags; the last user
        # choice becomes the default every new document opens with).
        view.set_show_editable_areas(self._settings.get("show_editable_areas", True))
        view.set_dblclick_paragraph(self._settings.get("dblclick_paragraph", True))
        view.set_highlight_color(self._highlight_color_rgb())  # current picker colour
        view.stateChanged.connect(lambda v=view: self._on_view_state_changed(v))
        view.editWarning.connect(lambda msg: self.statusBar().showMessage(msg, 8000))
        view.signatureRectSelected.connect(
            lambda n, rect, v=view: self._run_sign_flow(v, page_index=n, rect=rect)
        )
        view.signatureDetailsRequested.connect(self.show_signature_status)
        view.hoverHintChanged.connect(self._show_hover_hint)
        view.styleContextChanged.connect(self._populate_style_from)
        view.selectionFormatChanged.connect(self._on_selection_format_changed)
        view.editorClosed.connect(self._on_editor_closed)
        view.style_provider = self.current_text_style
        view.align_provider = self.current_text_align
        self._undo_group.addStack(view.undo_stack)
        index = self._tabs.addTab(view, view.title)
        if view.path is not None:
            self._tabs.setTabToolTip(index, str(view.path))
        self._tabs.setCurrentIndex(index)

    def _find_tab(self, path: Path) -> DocumentView | None:
        target = path.resolve()
        for view in self._views():
            if view.path is not None and view.path.resolve() == target:
                return view
        return None

    def _show_hover_hint(self, hint: str) -> None:
        """Persistent hover hint (U2b). Clearing only removes OUR hint —
        never an 8s warning that landed after it (moving the cursor off an
        element must not eat "font can't be matched exactly")."""
        bar = self.statusBar()
        if hint:
            self._last_hover_hint = hint
            bar.showMessage(hint)
        elif bar.currentMessage() == self._last_hover_hint:
            bar.clearMessage()

    def _on_view_state_changed(self, view: DocumentView) -> None:
        index = self._tabs.indexOf(view)
        if index >= 0:
            self._tabs.setTabText(index, view.title + (" *" if view.dirty else ""))
        if view is self.active_view:
            self._sync_chrome()

    # --- close handling -------------------------------------------------
    def _close_tab(self, index: int) -> None:
        view = self._tabs.widget(index)
        if not isinstance(view, DocumentView):
            return
        if not self._confirm_close(view):
            return
        self._undo_group.removeStack(view.undo_stack)
        self._tabs.removeTab(index)
        view.close_document()
        view.deleteLater()
        self._sync_chrome()  # refresh chrome + Window menu; empty state if last

    def _confirm_close(self, view: DocumentView) -> bool:
        """Ask about unsaved changes. Returns False only if the user cancels."""
        if not view.dirty:
            return True
        answer = QMessageBox.question(
            self,
            "Unsaved changes",
            f"Save changes to {view.title}?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.Save:
            return view.save()
        return True  # Discard

    def _prompt_password(self, doc: PdfDocument, path: Path) -> str | None:
        """Prompt until the password authenticates (returning it) or the user
        cancels (None). The password is kept in memory on the view only —
        signature checks on an encrypted+signed file need it to decrypt."""
        while True:
            pw, ok = QInputDialog.getText(
                self,
                "Password required",
                f"Enter password for:\n{path.name}",
                QLineEdit.EchoMode.Password,
            )
            if not ok:
                return None
            if doc.authenticate(pw):
                return pw
            QMessageBox.warning(self, "Wrong password", "That password did not work. Try again.")

    # --- delegations to the active view ---------------------------------
    def go_to_page(self, index: int) -> None:
        if (v := self.active_view) is not None:
            v.go_to_page(index)

    def next_page(self) -> None:
        if (v := self.active_view) is not None:
            v.next_page()

    def prev_page(self) -> None:
        if (v := self.active_view) is not None:
            v.prev_page()

    def first_page(self) -> None:
        if (v := self.active_view) is not None:
            v.first_page()

    def last_page(self) -> None:
        if (v := self.active_view) is not None:
            v.last_page()

    def _on_spin_changed(self, value: int) -> None:
        if (v := self.active_view) is not None:
            v.go_to_page(value - 1)

    def zoom_in(self) -> None:
        if (v := self.active_view) is not None:
            v.zoom_in()

    def zoom_out(self) -> None:
        if (v := self.active_view) is not None:
            v.zoom_out()

    def fit_page(self) -> None:
        if (v := self.active_view) is not None:
            v.fit_page()

    def fit_width(self) -> None:
        if (v := self.active_view) is not None:
            v.fit_width()

    def rotate_clockwise(self) -> None:
        if (v := self.active_view) is not None:
            v.rotate_clockwise()

    def rotate_counterclockwise(self) -> None:
        if (v := self.active_view) is not None:
            v.rotate_counterclockwise()

    def move_page_up(self) -> None:
        if (v := self.active_view) is not None:
            v.move_page_up()

    def move_page_down(self) -> None:
        if (v := self.active_view) is not None:
            v.move_page_down()

    def delete_current_page(self) -> None:
        if (v := self.active_view) is not None:
            v.delete_current_page()

    def insert_text(self) -> None:
        if (v := self.active_view) is not None:
            if v.armed_action == "text":
                v.cancel_armed_mode()  # clicking the checked action cancels
            else:
                v.begin_insert_text()
            self._sync_chrome()  # a cancelled dialog must not leave a stale check

    def insert_image(self) -> None:
        if (v := self.active_view) is not None:
            if v.armed_action == "image":
                v.cancel_armed_mode()
            else:
                v.begin_insert_image()
            self._sync_chrome()

    def hyperlink(self) -> None:
        """The ONE hyperlink command (Ctrl+K): arm the tool, or cancel it."""
        if (v := self.active_view) is not None:
            if v.armed_action in ("hyperlink", "link"):
                v.cancel_armed_mode()  # clicking the checked action cancels
            else:
                v.begin_hyperlink()
            self._sync_chrome()

    # --- digital signing (Sign menu) -------------------------------------
    def place_signature(self) -> None:
        """Arm the draw-a-rectangle gesture for a VISIBLE signature."""
        if (v := self.active_view) is not None:
            if v.armed_action == "sign":
                v.cancel_armed_mode()  # clicking the checked action cancels
            else:
                v.begin_place_signature()
            self._sync_chrome()

    def sign_invisible(self) -> None:
        """Sign without a visible stamp — straight to the sign dialog."""
        if (v := self.active_view) is not None:
            self._run_sign_flow(v, page_index=None, rect=None)

    def place_initials(self) -> None:
        """Arm click-to-place for a stored profile's initials image.

        Initials are decorative content stamps (undoable) applied BEFORE
        signing so the one cryptographic signature covers them.
        """
        view = self.active_view
        if view is None:
            return
        if view.armed_action == "initials":
            view.cancel_armed_mode()
            self._sync_chrome()
            return
        candidates = [p for p in self._signatures.profiles() if p.initials_image is not None]
        if not candidates:
            self.statusBar().showMessage(
                "No stored signature has an initials image — add one via Sign → Manage signatures…",
                8000,
            )
            # A checkable action toggles BEFORE triggered fires — un-toggle
            # (skipping this left a stale checkmark; adversarial-review find).
            self._sync_chrome()
            return
        profile = candidates[0]
        if len(candidates) > 1 and self.isVisible():
            names = [p.name for p in candidates]
            last = self._settings.get("last_sign_profile")
            start = names.index(last) if last in names else 0
            name, ok = QInputDialog.getItem(
                self, "Whose initials?", "Place initials for:", names, start, False
            )
            if not ok:
                self._sync_chrome()  # a cancelled picker must not leave a check
                return
            profile = candidates[names.index(name)]
        view.begin_place_initials(profile.initials_image)
        self._sync_chrome()

    def manage_signatures(self):
        """Open the signature-library manager; returns the dialog (exec'd only
        when actually on screen — offscreen tests drive its core methods)."""
        dialog = SignatureManagerDialog(self, self._signatures, self._settings)
        if self.isVisible():
            dialog.exec()
        return dialog

    def _announce_signatures(self, view: DocumentView, *, dialog: bool = True) -> None:
        """Surface a signed document's status: the BANNER + status message.

        A tampered signed file must be FLAGGED the moment it opens (user
        report: Acrobat flags it, we showed nothing) — and STAY flagged (the
        banner; a status-bar message fades in seconds). Verification runs on
        the FILE bytes as read from disk — never a flatten, which would
        break the very signatures being measured. ``dialog=False`` skips the
        one-time warning modal (the after-save refresh — the user just
        answered the save warning; only the banner should update).
        """
        path = view.path
        if path is None:
            return
        if not view.document.has_signatures():
            # No signatures (any more) — a stale banner must not keep
            # vouching for (or warning about) signatures that are gone,
            # e.g. right after a consented strip-on-save.
            view.set_signature_banner("none")
            return
        try:
            checks = signing.verify_pdf_signatures(path.read_bytes(), password=view.open_password)
        except (ValueError, OSError) as exc:
            self.statusBar().showMessage(f"Could not check this document's signatures: {exc}", 8000)
            return
        broken = [c for c in checks if not (c.intact and c.valid)]
        if checks and not broken:
            names = ", ".join(sorted({c.signer_name or "an unknown signer" for c in checks}))
            message = f"Digitally signed by {names} — signature intact (identity not verified)."
            # The BANNER is the real flag (a status-bar blip fades in seconds
            # — user report); intact is dismissable, a problem is permanent.
            view.set_signature_banner("intact", message)
            self.statusBar().showMessage(message, 8000)
            return
        # Widgets say signed but nothing verifiable (checks == []), or a
        # broken check. Only claim MODIFICATION when it was positively
        # determined — an unverifiable signature gets honest wording, never
        # a tamper accusation the engine did not make.
        if checks and any(c.tampered for c in broken):
            banner_msg = (
                "⚠ SIGNATURE PROBLEM: this document was modified after it was "
                "signed — the content may not be what the signer approved."
            )
            dialog_msg = (
                "This document's digital signature is BROKEN — the file was "
                "modified after it was signed.\n\n"
                "The content shown may not be what the signer approved. "
                "Sign → Signature status… has the details."
            )
        else:
            banner_msg = (
                "⚠ SIGNATURE PROBLEM: this document displays a signature that "
                "cannot be verified. Treat it as unsigned."
            )
            dialog_msg = (
                "This document displays a digital signature that CANNOT be "
                "verified.\n\n"
                "Treat it as unsigned — the content is not vouched for. "
                "Sign → Signature status… has the details."
            )
        view.set_signature_banner("problem", banner_msg)
        self.statusBar().showMessage(banner_msg, 8000)
        if dialog and self.isVisible():
            QMessageBox.warning(self, "Signature problem", dialog_msg)

    def signature_status_text(self, view: DocumentView) -> str:
        """Plain-words status of every signature in the view's ON-DISK file."""
        path = view.path
        if path is None:
            return "This document has no digital signatures."
        try:
            checks = signing.verify_pdf_signatures(path.read_bytes(), password=view.open_password)
        except (ValueError, OSError) as exc:
            return f"Could not check this document's signatures:\n{exc}"
        if not checks:
            if view.document.has_signatures():
                # Widgets DISPLAY a signature no validator can see — the same
                # state the sign flow refuses as broken; don't call it clean.
                return (
                    "This document DISPLAYS a digital signature, but it cannot "
                    "be verified — treat the document as unsigned."
                )
            return "This document has no digital signatures."
        lines = []
        for check in checks:
            who = check.signer_name or "an unknown signer"
            if check.intact and check.valid:
                state = "INTACT — the document has not been changed since it was signed"
                if not check.trusted:
                    state += "\n   (signer identity not verified — the certificate is not trusted)"
            else:
                state = f"BROKEN — {check.problem}"
            lines.append(f"{check.field_name}: signed by {who}\n   {state}")
        if view.dirty:
            lines.append(
                "Note: this tab has UNSAVED changes — they are not part of the signed file on disk."
            )
        return "\n\n".join(lines)

    def show_signature_status(self) -> None:
        """Sign → Signature status…: the per-signature detail view."""
        view = self.active_view
        if view is None:
            return
        text = self.signature_status_text(view)
        if self.isVisible():
            QMessageBox.information(self, "Signature status", text)
        else:
            self.statusBar().showMessage(text.splitlines()[0], 8000)

    def _run_sign_flow(self, view: DocumentView, *, page_index: int | None, rect) -> None:
        """Interactive signing: sign dialog → save-as picker → _execute_signing.

        ``rect`` None = invisible signature. Offscreen tests drive
        ``_execute_signing`` directly — the modal dialogs would block them.
        """
        if not self.isVisible():
            return
        if view.document.is_protected:
            # Decision (2026-07-25): signing flattens to DECRYPTED bytes, and
            # re-encrypting afterwards would break the signature — the signed
            # copy is honestly unprotected. Full encrypt-then-sign compose is
            # a later milestone.
            answer = QMessageBox.question(
                self,
                "Signed copy will not be protected",
                "This document is password-protected, but the SIGNED COPY "
                "will NOT be: signing works on the decrypted content, and "
                "protection cannot be re-applied without breaking the "
                "signature.\n\nContinue?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        default_value = self._settings.get(DEFAULT_P12_KEY)
        default_p12 = (
            Path(default_value) if isinstance(default_value, str) and default_value else None
        )
        dlg = SignDialog(
            self,
            self._signatures.profiles(),
            default_p12=default_p12,
            visible_signature=rect is not None,
            last_profile=self._settings.get("last_sign_profile"),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.signer is None:
            return
        spec = dlg.spec()
        if spec["save_default"] and spec["cert_path"] is not None:
            self._settings.set(DEFAULT_P12_KEY, str(spec["cert_path"]))
        if spec["profile_name"]:
            self._settings.set("last_sign_profile", spec["profile_name"])
        source = view.path
        suggested = (
            str(source.with_name(source.stem + "-signed.pdf"))
            if source is not None
            else "signed.pdf"
        )
        out_str, _ = QFileDialog.getSaveFileName(
            self, "Save signed copy as", suggested, "PDF files (*.pdf)"
        )
        if not out_str:
            return
        self._execute_signing(
            view,
            Path(out_str),
            dlg.signer,
            page_index=page_index if page_index is not None else 0,
            rect=rect,
            image_path=spec["image_path"],
            reason=spec["reason"],
            location=spec["location"],
        )

    def _execute_signing(
        self,
        view: DocumentView,
        out_path: Path,
        signer,
        *,
        page_index: int = 0,
        rect: tuple[float, float, float, float] | None = None,
        image_path: Path | None = None,
        reason: str | None = None,
        location: str | None = None,
    ):
        """Sign the view's CURRENT state into ``out_path``; open the signed copy.

        Returns the engine ``SignResult`` or None on failure. TERMINAL: the
        open document stays unsigned — the signed artifact is the new file,
        opened in its own tab. An ALREADY-SIGNED clean document is signed by
        appending to its own file bytes (save_signed's flatten is a PyMuPDF
        rewrite, which would destroy the existing signatures); a signed
        document with UNSAVED EDITS is refused honestly — the edits
        themselves would break the signatures.
        """
        # "Already signed" means a field actually HOLDS a signature — an empty
        # placeholder field (unsigned contract template) is NOT a signature
        # and signs via the normal flatten path (adversarial-review finding);
        # field NAMES (empty ones included) only feed the auto-naming.
        already_signed = view.document.has_signatures()
        field_name = signing.next_field_name(view.document.signature_field_names())
        try:
            target_tab = self._find_tab(out_path)
            if target_tab is view:
                raise ValueError("choose a new file name for the signed copy")
            if target_tab is not None:
                # Overwriting a file another tab holds open would leave that
                # STALE tab focused as if it were the fresh copy.
                raise ValueError(
                    f"“{out_path.name}” is already open in another tab — close "
                    "it first, or pick a different name"
                )
            if already_signed and view.path is not None and not view.dirty:
                data = view.path.read_bytes()
                # REAL cryptographic verification, not the layout heuristic
                # (the user's hand-test slipped a broken file past the cheap
                # check): a signed file that was edited and re-saved has a
                # clean stack but already-broken signatures, and appending
                # would produce output readers flag as invalid while we
                # claim the opposite. Zero verifiable signatures on a file
                # whose widgets SAY signed is the same broken state.
                checks = signing.verify_pdf_signatures(data, password=view.open_password)
                if not checks or any(not (c.intact and c.valid) for c in checks):
                    raise ValueError(
                        "this file's existing signatures are already broken — "
                        "the file was modified after signing. Sign a fresh "
                        "copy of the original document instead"
                    )
                # Add a signature WITHOUT re-serialising: incremental updates
                # compose, so every prior signature stays valid.
                result = signing.sign_pdf_bytes(
                    data,
                    signer,
                    field_name=field_name,
                    reason=reason,
                    location=location,
                    page_index=page_index,
                    rect=rect,
                    image_path=image_path,
                )
                out_path.write_bytes(result.pdf_bytes)
            elif already_signed:
                raise ValueError(
                    "this document already holds digital signatures, and signing "
                    "edited content would invalidate them — save your edits as a "
                    "new file and sign that, or sign the unedited original"
                )
            else:
                result = view.document.save_signed(
                    out_path,
                    signer,
                    field_name=field_name,
                    reason=reason,
                    location=location,
                    page_index=page_index,
                    rect=rect,
                    image_path=image_path,
                )
        except (ValueError, OSError, signing.SigningError) as exc:
            diagnostics.log_event(f"signing failed: {exc}")
            if self.isVisible():
                QMessageBox.critical(self, "Signing failed", f"Could not sign:\n\n{exc}")
            self.statusBar().showMessage(f"Signing failed: {exc}", 8000)
            return None
        diagnostics.log_event(f"signed {out_path.name} as {result.signer_name}")
        self.open_path(out_path)
        self.statusBar().showMessage(
            f"Signed: {out_path.name} (opened in a new tab; the original stays unsigned).", 8000
        )
        if result.self_signed and self.isVisible():
            QMessageBox.information(
                self,
                "Signed with a self-signed certificate",
                f"{out_path.name} is signed and tamper-evident.\n\n"
                "The certificate is SELF-SIGNED: PDF readers will show the "
                "signature as unknown/untrusted until the recipient chooses to "
                "trust the certificate. It proves the document hasn't changed "
                "— not who signed it.",
            )
        return result

    def insert_comment(self) -> None:
        if (v := self.active_view) is not None:
            if v.armed_action == "comment":
                v.cancel_armed_mode()
            else:
                v.begin_insert_comment()
            self._sync_chrome()

    def insert_callout(self) -> None:
        if (v := self.active_view) is not None:
            if v.armed_action in ("callout_target", "callout_box"):
                v.cancel_armed_mode()
            else:
                v.begin_insert_callout()
            self._sync_chrome()

    def highlight_text(self) -> None:
        if (v := self.active_view) is not None:
            if v.has_text_selection():
                v.highlight_selection()  # highlight the marquee selection now
            elif v.armed_action == "highlight":
                v.cancel_armed_mode()
            else:
                v.begin_highlight()  # no selection: arm the area-marquee drag
            self._sync_chrome()

    def insert_pages_from_file(self) -> None:
        view = self.active_view
        if view is None:
            return
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Insert pages from PDF", "", "PDF files (*.pdf);;All files (*)"
        )
        if path_str:
            view.insert_from_path(Path(path_str), view.current_page + 1)

    def _on_edit_mode_toggled(self, checked: bool) -> None:
        if (v := self.active_view) is None or v.edit_mode == checked:
            return
        # Entering EDIT mode on a digitally signed document: every edit path
        # (text redaction, image move, page ops …) invalidates the signatures
        # — warn ONCE at the door, like Acrobat, instead of relying on each
        # downstream path to notice (user report: an edit slipped through
        # without any warning).
        if checked and v.document.has_signatures() and self.isVisible():
            answer = QMessageBox.question(
                self,
                "Document is digitally signed",
                "This document is digitally signed. Edits cannot preserve the "
                "signatures — SAVING will REMOVE them (you'll be asked again "
                "then, and Save As… keeps the signed original).\n\n"
                "Switch to Edit mode anyway?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._sync_chrome()  # un-toggle the action
                return
        # Restricted document (permission flags deny modification AND page
        # assembly): entering Edit mode needs the permissions password —
        # honoring the flags is what makes us a compliant reader.
        if checked and v.document.is_protected:
            perms = v.document.permissions
            if not (perms.can_modify or perms.can_assemble):
                if not self.isVisible() or not self._prompt_owner_password(
                    v, "This document's permissions restrict editing."
                ):
                    self._sync_chrome()  # un-toggle the action
                    return
        v.set_edit_mode(checked)

    def _on_show_areas_toggled(self, checked: bool) -> None:
        # Persist as the app-level default for newly opened documents (last
        # choice wins); existing open tabs keep their own value.
        self._settings.set("show_editable_areas", checked)
        if (v := self.active_view) is not None and v.show_editable_areas != checked:
            v.set_show_editable_areas(checked)

    def _on_dblclick_para_toggled(self, checked: bool) -> None:
        self._settings.set("dblclick_paragraph", checked)
        if (v := self.active_view) is not None and v.dblclick_paragraph != checked:
            v.set_dblclick_paragraph(checked)

    def show_gesture_help(self):
        """Open the gestures cheat sheet; returns the dialog (exec'd only
        when actually on screen — offscreen tests inspect the return)."""
        from pdfapp.help_dialog import GestureHelpDialog

        dialog = GestureHelpDialog(self)
        if self.isVisible():
            dialog.exec()
        return dialog

    def show_about(self):
        """Open the About dialog; returns it (exec'd only when on screen —
        offscreen tests inspect the return)."""
        from pdfapp.about_dialog import AboutDialog

        dialog = AboutDialog(self)
        if self.isVisible():
            dialog.exec()
        return dialog

    def show_diagnostics_log(self) -> bool:
        """Help → reveal the diagnostics log so the user can send it back. If no
        log exists yet, point them at where it will be. Returns True if a log was
        revealed (offscreen tests inspect the return; the info box only exec's
        when actually on screen)."""
        if diagnostics.reveal_log():
            return True
        if self.isVisible():
            QMessageBox.information(
                self,
                "Diagnostics log",
                "No diagnostics log has been written yet.\n\n"
                f"It will appear here once the app records something:\n{diagnostics.log_path()}",
            )
        return False

    def find_in_document(self) -> None:
        """Edit → Find (Ctrl+F): open the ACTIVE tab's search bar (SR2)."""
        view = self.active_view
        if view is not None:
            view.open_search()

    def detect_links(self):
        """Tools → Detect & link URLs: find web/email addresses and style+link
        them across the document (edit mode). Delegated to the active view."""
        if (v := self.active_view) is not None:
            v.detect_and_link_urls()

    def extract_text(self):
        """Tools → Extract text (X2: whole document by default, per-page
        native/OCR routing, cancellable progress). Returns the dialog;
        exec'd only when on screen (offscreen tests inspect it)."""
        from pdfapp.extract_support import build_extract_dialog

        view = self.active_view
        if view is None:
            return None
        title = f"Extracted text — {self._tabs.tabText(self._tabs.currentIndex())}"
        dialog = build_extract_dialog(self, title, lambda scope: self._run_extraction(view, scope))
        if self.isVisible():
            dialog.exec()
        return dialog

    def _run_extraction(self, view: DocumentView, scope: str) -> str:
        """One extraction pass for the dialog's runner (X2).

        Flow: cheap text-layer scan → tesseract-missing warning path /
        bulk-OCR confirm gate (declining skips OCR, never aborts) →
        cancellable bulk OCR through the view's shared cache →
        section collection with honest empty-page labels.
        """
        from pdfapp import extract_support
        from pdfcore import ocr
        from pdfcore.textsource import format_extracted_text

        doc = view.document
        if scope == extract_support.SCOPE_CURRENT:
            pages = [view.current_page]
        else:
            pages = list(range(doc.page_count))
        no_layer = [n for n in pages if not doc.has_text_layer(n)]

        words_by_page: dict = {}
        note = None
        if no_layer:
            if not ocr.tesseract_available():
                self.statusBar().showMessage(
                    f"Tesseract is not installed — {len(no_layer)} scanned page(s) "
                    "are listed as empty (OCR not available).",
                    8000,
                )
            else:
                wants_ocr = True
                if len(no_layer) >= extract_support.BULK_OCR_WARN_AT:
                    wants_ocr = extract_support.confirm_bulk_ocr(self, len(no_layer))
                if wants_ocr:
                    # Re-entrancy guard: the progress dialog pumps events, so
                    # a second Extract click must find the action disabled.
                    self._extract_text_action.setEnabled(False)
                    try:
                        result = extract_support.run_bulk_ocr(
                            self, doc, no_layer, view.ocr_word_cache, label="Extracting text (OCR)…"
                        )
                    finally:
                        self._extract_text_action.setEnabled(True)
                    words_by_page = result.words_by_page
                    if result.tesseract_missing:
                        self.statusBar().showMessage(
                            "OCR stopped: the Tesseract runtime is unavailable.", 8000
                        )
                    if result.cancelled:
                        skipped = len(no_layer) - len(words_by_page)
                        note = (
                            f"[Extraction cancelled — {skipped} scanned page(s) "
                            "were not OCR'd and are listed as not attempted.]"
                        )

        sections, _ = extract_support.collect_sections(doc, pages, ocr_words_for=words_by_page.get)
        text = format_extracted_text(sections)
        return f"{text}\n\n{note}" if note else text

    def _toggle_thumbnails(self, checked: bool) -> None:
        self._thumbs_visible = checked
        self._settings.set("thumbnails_visible", checked)
        for view in self._views():
            view.set_thumbnails_visible(checked)

    # --- save -----------------------------------------------------------
    # --- password protection ---------------------------------------------
    def _update_protection_label(self, view: DocumentView | None) -> None:
        """The permanent status-bar indicator beside the mode label."""
        if view is None:
            self._protection_label.setText("")
            self._protection_label.setToolTip("")
            return
        state = view.protection_state
        if state == "none":
            self._protection_label.setText("")
            self._protection_label.setToolTip("")
        elif state == "pending":
            self._protection_label.setText("Protection pending save")
            self._protection_label.setToolTip("The protection change is applied when you save.")
        elif state == "protected":
            self._protection_label.setText("Protected")
            self._protection_label.setToolTip(
                "This document is encrypted. No restrictions at your access level."
            )
        else:  # restricted
            perms = view.document.permissions
            denied = [
                label
                for label, allowed in (
                    ("editing", perms.can_modify),
                    ("page changes", perms.can_assemble),
                    ("commenting", perms.can_annotate),
                    ("form filling", perms.can_fill_forms),
                    ("copying", perms.can_copy),
                    ("printing", perms.can_print),
                )
                if not allowed
            ]
            self._protection_label.setText("Restricted")
            self._protection_label.setToolTip(
                "This document's permissions deny: "
                + ", ".join(denied)
                + ". Entering Edit mode asks for the permissions password."
            )

    def _prompt_owner_password(self, view: DocumentView, why: str) -> bool:
        """Prompt-until-unlocked (or cancel) for the PERMISSIONS password.

        The open password is politely rejected with an explanation —
        ``unlock`` only accepts owner-level authentication.
        """
        while True:
            pw, ok = QInputDialog.getText(
                self,
                "Permissions password required",
                f"{why}\nEnter the permissions password:",
                QLineEdit.EchoMode.Password,
            )
            if not ok:
                return False
            if view.document.unlock(pw):
                self._sync_chrome()  # restrictions just lifted live
                return True
            QMessageBox.warning(
                self,
                "Wrong password",
                "That password did not unlock the document — note the OPEN "
                "password is not the permissions password.",
            )

    def protect_document(self):
        """File → Protect document…: set/change/remove protection (owner-gated).

        Returns the dialog (exec'd only when actually on screen — offscreen
        tests drive ``set_pending_protection`` and the core methods directly).
        """
        view = self.active_view
        if view is None:
            return None
        doc = view.document
        if doc.is_protected and not doc.is_owner:
            if not self.isVisible():
                return None  # offscreen: tests unlock explicitly
            if not self._prompt_owner_password(
                view, "Changing this document's protection needs the permissions password."
            ):
                return None
        dialog = ProtectDialog(self, currently_protected=doc.is_protected)
        if self.isVisible():
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return dialog
            view.set_pending_protection(dialog.spec())
            self.statusBar().showMessage("Protection will be applied when you save.", 8000)
            self._sync_chrome()
        return dialog

    def _signed_save_choice(self, view: DocumentView) -> str:
        """``"save"`` / ``"save_as"`` / ``"cancel"`` for saving a SIGNED doc.

        A save rewrites the file, which breaks its signatures anyway — with
        consent they are REMOVED (stripped) so the result is an honest
        unsigned derivative, never a file carrying broken signatures that
        read as tampering (Word's model). Save As… is offered first so the
        signed ORIGINAL survives untouched. Offscreen (tests) consents to a
        plain save; the dialog is hand-verified.
        """
        if not view.document.has_signatures() or not self.isVisible():
            return "save"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Document is digitally signed")
        box.setText(
            "Saving re-writes the file, and digital signatures cannot survive "
            "that. They will be REMOVED (visible stamps included) so the saved "
            "file isn't left with broken signatures.\n\n"
            "Save As… keeps the signed original untouched."
        )
        save_as_button = box.addButton("Save As…", QMessageBox.ButtonRole.ActionRole)
        save_button = box.addButton("Save anyway", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(save_as_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_as_button:
            return "save_as"
        if clicked is save_button:
            return "save"
        return "cancel"

    def _strip_signatures_step(self, view: DocumentView) -> None:
        """The consented removal, as ONE undoable command (no-op unsigned)."""
        if view.document.has_signatures():
            view.remove_signatures()

    def save(self) -> None:
        view = self.active_view
        if view is None:
            return
        choice = self._signed_save_choice(view)
        if choice == "cancel":
            return
        if choice == "save_as":
            self.save_as(confirmed=True)
            return
        self._strip_signatures_step(view)
        if view.save():
            self.statusBar().showMessage(f"Saved {view.path}")
            # The banner must stop vouching for signatures the save removed
            # (banner only: the user just answered the save dialog).
            self._announce_signatures(view, dialog=False)

    def save_as(self, confirmed: bool = False) -> None:
        view = self.active_view
        if view is None:
            return
        if not confirmed and view.document.has_signatures() and self.isVisible():
            answer = QMessageBox.question(
                self,
                "Document is digitally signed",
                "The new file cannot keep this document's digital signatures — "
                "they will be REMOVED in the copy (visible stamps included). "
                "The original file stays untouched.\n\nContinue?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        out_str, _ = QFileDialog.getSaveFileName(self, "Save PDF as", "", "PDF files (*.pdf)")
        if not out_str:
            return
        self._strip_signatures_step(view)
        if view.save_as_path(Path(out_str)):
            self.statusBar().showMessage(f"Saved {out_str}")
            self._announce_signatures(view, dialog=False)

    def print_current(self) -> None:
        view = self.active_view
        if view is None:
            return
        dialog = PrintDialog(self._print_options, self)
        dialog.previewRequested.connect(lambda: show_preview(self, view.document, dialog.options()))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._print_options = dialog.options()  # remember the choices
            print_document(self, view.document, self._print_options)

    # --- file-level operations (produce new files) ----------------------
    def merge_documents(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select PDFs to merge (in order)", "", "PDF files (*.pdf);;All files (*)"
        )
        if not paths:
            return
        out_str, _ = QFileDialog.getSaveFileName(
            self, "Save merged PDF as", "", "PDF files (*.pdf)"
        )
        if out_str:
            self._merge_files([Path(p) for p in paths], Path(out_str))

    def _merge_files(self, paths: list[Path], out: Path) -> None:
        try:
            pages.merge(paths, out)
        except Exception as exc:  # noqa: BLE001 - surface any merge error
            QMessageBox.critical(self, "Merge failed", f"Could not merge PDFs:\n\n{exc}")
            return
        self.open_path(out)

    def split_document(self) -> None:
        src_str, _ = QFileDialog.getOpenFileName(
            self, "Select a PDF to split", "", "PDF files (*.pdf);;All files (*)"
        )
        if not src_str:
            return
        text, ok = QInputDialog.getText(self, "Split ranges", "Page ranges (e.g. 1-3, 4, 5-8):")
        if not ok or not text.strip():
            return
        try:
            ranges = _parse_page_ranges(text)
        except ValueError:
            QMessageBox.warning(self, "Split", "Could not parse those page ranges.")
            return
        out_dir = QFileDialog.getExistingDirectory(self, "Choose output folder")
        if out_dir:
            self._split_file(Path(src_str), ranges, Path(out_dir))

    def _split_file(self, src: Path, ranges: list[tuple[int, int]], out_dir: Path) -> None:
        try:
            outputs = pages.split(src, ranges, out_dir)
        except Exception as exc:  # noqa: BLE001 - surface any split error
            QMessageBox.critical(self, "Split failed", f"Could not split:\n\n{exc}")
            return
        self.statusBar().showMessage(f"Split into {len(outputs)} file(s) in {out_dir}")

    # --- chrome sync ----------------------------------------------------
    def _sync_chrome(self) -> None:
        view = self.active_view
        has = view is not None
        edit_on = view.edit_mode if view is not None else False
        self._undo_group.setActiveStack(view.undo_stack if view is not None else None)
        count = view.page_count if view is not None else 0
        current = view.current_page if view is not None else 0
        at_start = current <= 0
        at_end = current >= count - 1

        self._prev_action.setEnabled(has and not at_start)
        self._first_action.setEnabled(has and not at_start)
        self._next_action.setEnabled(has and not at_end)
        self._last_action.setEnabled(has and not at_end)

        self._page_spin.setEnabled(has)
        # Block signals so syncing the spinbox doesn't re-trigger navigation.
        self._page_spin.blockSignals(True)
        self._page_spin.setMaximum(max(1, count))
        self._page_spin.setValue(current + 1)
        self._page_spin.blockSignals(False)
        self._page_total_label.setText(f"/ {count}")

        for action in self._zoom_actions:
            action.setEnabled(has)
        # Permission flags of the OPEN document (honor-the-standard: we're a
        # compliant reader now; all-allowed for unprotected docs, lifted live
        # by an owner unlock).
        perms = view.document.permissions if has else None
        can_modify = perms.can_modify if perms is not None else False
        can_pages = (perms.can_assemble or perms.can_modify) if perms is not None else False
        # CONTENT-edit actions require edit mode (U0) + the modify permission.
        for action in self._page_edit_actions:
            action.setEnabled(has and edit_on and can_modify)
        # Page ops honor the ASSEMBLE bit (Acrobat's page-layout level) — an
        # assemble-only document gets page ops in edit mode, nothing else.
        self._rotate_cw_action.setEnabled(has and edit_on and can_pages)
        self._rotate_ccw_action.setEnabled(has and edit_on and can_pages)
        self._insert_action.setEnabled(has and edit_on and can_pages)
        # A PDF must keep at least one page.
        self._delete_action.setEnabled(has and edit_on and count > 1 and can_pages)
        self._move_up_action.setEnabled(has and edit_on and not at_start and can_pages)
        self._move_down_action.setEnabled(has and edit_on and not at_end and can_pages)
        # Annotation actions are available in BOTH modes (highlight/comment/
        # callout are markup, not content) — enabled on `has` alone, but they
        # DO honor the annotation permission.
        can_annotate = perms.can_annotate if perms is not None else False
        for action in self._annotate_actions:
            action.setEnabled(has and can_annotate)
        # Undo/redo follow the stack in EITHER mode: annotations now mutate in
        # Markup mode, so a restore must be reachable there too (undo is
        # document-wide — it may also reverse an earlier content edit).
        self._undo_action.setEnabled(view is not None and view.undo_stack.canUndo())
        self._redo_action.setEnabled(view is not None and view.undo_stack.canRedo())
        self._edit_mode_action.setEnabled(has)
        self._edit_mode_action.blockSignals(True)
        self._edit_mode_action.setChecked(edit_on)
        self._edit_mode_action.blockSignals(False)
        # Armed one-shot modes show as checked on their launching action (U4).
        armed = view.armed_action if view is not None else None
        self._insert_text_action.setChecked(armed == "text")
        self._insert_image_action.setChecked(armed == "image")
        self._place_signature_action.setChecked(armed == "sign")
        self._place_initials_action.setChecked(armed == "initials")
        self._hyperlink_action.setChecked(armed in ("hyperlink", "link"))
        # Works on PAGE text, so it can't act on a selection inside an open
        # in-place editor — disabled rather than inert (user report).
        if has and view.editor_open:
            self._hyperlink_action.setEnabled(False)
        self._highlight_action.setChecked(armed == "highlight")
        self._insert_comment_action.setChecked(armed == "comment")
        self._insert_callout_action.setChecked(armed in ("callout_target", "callout_box"))
        # Reveal-all (U5): per-document, meaningful only in edit mode.
        self._show_areas_action.setEnabled(has and edit_on)
        self._show_areas_action.blockSignals(True)
        self._show_areas_action.setChecked(bool(has and view.show_editable_areas))
        self._show_areas_action.blockSignals(False)
        # Double-click sub-mode (U8): the checked state IS the indicator.
        self._dblclick_para_action.setEnabled(has and edit_on)
        self._dblclick_para_action.blockSignals(True)
        self._dblclick_para_action.setChecked(bool(has and view.dblclick_paragraph))
        self._dblclick_para_action.blockSignals(False)
        self._mode_label.setText(("Editing" if edit_on else "Markup") if has else "")
        self._update_protection_label(view if has else None)
        self._save_action.setEnabled(has)
        self._save_as_action.setEnabled(has)
        self._protect_action.setEnabled(has)
        self._print_action.setEnabled(has and (perms.can_print if perms is not None else False))
        # Read features (X1/SR2): available whenever a document is open, even
        # read-only — deliberately NOT in _page_edit_actions. Extract honors
        # the COPY permission (extraction IS the copy bit; accessibility
        # extraction is the reader's business, not our extract tool's).
        self._extract_text_action.setEnabled(
            has and (perms.can_copy if perms is not None else False)
        )
        self._find_action.setEnabled(has)
        self._detect_links_action.setEnabled(has and edit_on and can_modify)  # a content op
        # Signing writes a signed COPY (terminal op, never mutates the open
        # document) — but the copy is a MODIFIED derivative, so signing
        # honors the modify permission on restricted files. Initials ride
        # _page_edit_actions (a content stamp); Manage is app-level.
        self._place_signature_action.setEnabled(has and can_modify)
        self._sign_invisible_action.setEnabled(has and can_modify)
        self._signature_status_action.setEnabled(has)

        self._thumbs_action.setEnabled(has)
        self._thumbs_action.blockSignals(True)
        self._thumbs_action.setChecked(view.thumbnails_visible if has else self._thumbs_visible)
        self._thumbs_action.blockSignals(False)

        if not has:
            self.statusBar().showMessage("Open a PDF to begin.")
        self._rebuild_window_menu()
        self._update_title()

    def _rebuild_window_menu(self) -> None:
        """List the open documents; selecting one activates its tab."""
        self._window_menu.clear()
        active = self.active_view
        views = self._views()
        if not views:
            placeholder = self._window_menu.addAction("No documents open")
            placeholder.setEnabled(False)
            return
        for index, view in enumerate(views):
            action = self._window_menu.addAction(view.title)
            action.setCheckable(True)
            action.setChecked(view is active)
            action.triggered.connect(
                lambda _checked=False, idx=index: self._tabs.setCurrentIndex(idx)
            )

    def _rebuild_recent_menu(self) -> None:
        """Populate the Open Recent fly-out from the persisted list."""
        self._recent_menu.clear()
        entries = self._recent_files.entries()
        if not entries:
            placeholder = self._recent_menu.addAction("No recent files")
            placeholder.setEnabled(False)
            return
        for index, path in enumerate(entries, start=1):
            # &1..&9, then &0 for the tenth — a keyboard accelerator per entry.
            # Escape any '&' in the file name so it isn't read as a mnemonic.
            accel = index % 10
            label = path.name.replace("&", "&&")
            action = self._recent_menu.addAction(f"&{accel}  {label}")
            action.setToolTip(str(path))
            action.triggered.connect(lambda _checked=False, p=path: self._open_recent(p))
        self._recent_menu.addSeparator()
        clear_action = self._recent_menu.addAction("Clear Recent Files")
        clear_action.triggered.connect(self._clear_recent)

    def _open_recent(self, path: Path) -> None:
        """Open a file chosen from Open Recent, pruning it if it has gone."""
        if not path.exists():
            self._recent_files.remove(path)
            QMessageBox.warning(
                self,
                "File not found",
                f"This file is no longer available and was removed from Open Recent:\n\n{path}",
            )
            return
        self.open_path(path)

    def _clear_recent(self) -> None:
        self._recent_files.clear()

    def _update_title(self) -> None:
        view = self.active_view
        if view is None:
            self.setWindowTitle("PDF Editor")
            return
        star = " *" if view.dirty else ""
        self.setWindowTitle(f"PDF Editor — {view.title}{star}")

    # --- window layout persistence --------------------------------------
    def _restore_window_layout(self) -> None:
        """Restore saved geometry + toolbar state (base64 in settings).

        restoreGeometry BEFORE restoreState (Qt requirement); both no-op safely
        on an empty/None value or a version mismatch."""
        geometry = self._settings.get("window_geometry")
        if geometry:
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))
        state = self._settings.get("window_state")
        if state:
            self.restoreState(QByteArray.fromBase64(state.encode("ascii")), _STATE_VERSION)

    def _save_window_layout(self) -> None:
        """Persist geometry + toolbar state as base64 (called at close time)."""
        self._settings.set("window_geometry", bytes(self.saveGeometry().toBase64()).decode("ascii"))
        self._settings.set(
            "window_state", bytes(self.saveState(_STATE_VERSION).toBase64()).decode("ascii")
        )

    # --- lifecycle ------------------------------------------------------
    def closeEvent(self, event) -> None:
        # Prompt for each dirty document before the window closes. Only when
        # actually shown to a user — offscreen tests never call show(), so this
        # stays non-blocking there.
        if self.isVisible():
            for view in self._views():
                self._tabs.setCurrentWidget(view)  # show which doc we're asking about
                if not self._confirm_close(view):
                    event.ignore()
                    return
            # A real, shown window is really closing: snapshot the layout. The
            # isVisible() guard is load-bearing — a never-shown offscreen test
            # window's saveGeometry() is degenerate and would clobber the real
            # saved layout in the shared data dir.
            self._save_window_layout()
        event.accept()
