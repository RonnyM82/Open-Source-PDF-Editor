"""A single open document: its PdfDocument, view state, and widgets.

Each DocumentView owns one PdfDocument plus everything scoped to it — current
page, dirty state, a per-document render cache, the page canvas, and its own
thumbnail sidebar (in a QSplitter). All per-document operations live here; the
window is thin chrome that delegates to the active view.

Boundary: this is pdfapp (Qt) and calls the engine; the engine stays headless.
The render cache is UI-side and cleared on every mutation (CLAUDE.md rule 8).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QTextCharFormat, QUndoStack
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMessageBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from pdfapp import icons, page_coords, theme
from pdfapp.font_files import font_choice
from pdfapp.gestures import hover_hint
from pdfapp.ocr_cache import OcrWordCache
from pdfapp.page_canvas import PageCanvas
from pdfapp.page_geometry import (
    GeometryCache,
    PageGeometry,
    corner_hit,
    hover_target,
    nearest_span_in_paragraph,
)
from pdfapp.qt_image import rendered_page_to_qpixmap
from pdfapp.render_cache import RenderCache
from pdfapp.search_bar import SearchBar
from pdfapp.text_editor_overlay import PT_PROPERTY, ParagraphEditorOverlay, TextEditorOverlay
from pdfapp.thumbnail_panel import ThumbnailPanel
from pdfapp.undo import SnapshotCommand, undo_limit_for
from pdfcore import textselect
from pdfcore.document import PdfDocument
from pdfcore.textedit import (
    FLAG_BOLD,
    FLAG_ITALIC,
    SCRIPT_NORMAL,
    SCRIPT_SUB,
    SCRIPT_SUPER,
    Paragraph,
    StyledRun,
    TextSpan,
    TextStyle,
    merge_paragraphs,
)

# Thumbnails render at a fixed low dpi for speed; the main page renders at
# exactly the on-screen resolution (see PageCanvas).
_THUMB_DPI = 16
# Cap on the engine render zoom (8.0 == 576 dpi) — beyond it the view upscales.
_MAX_RENDER_ZOOM = 8.0
# Byte budget for cached page renders, plus a generous item cap so thumbnail
# entries for long documents don't evict the large main renders.
_CACHE_ITEMS = 512
_CACHE_BYTES = 256 * 1024 * 1024
# Default size for newly inserted text (helv, black). 9pt on every launch —
# the quotes' body size (user decision; the toolbar spin starts there too).
_INSERT_TEXT_SIZE = 9.0


def _save_failure_text(path, exc: Exception) -> str:
    """Human wording for a failed save.

    A WinError 5/32 from the atomic replace almost always means the target
    file is open in another program — the user hit exactly this with the PDF
    open in Acrobat, and the raw ``[WinError 5] Access is denied`` text
    helped nobody. Anything else keeps the honest raw error.
    """
    name = path.name if path is not None else "this file"
    if isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in (5, 32, 33):
        return (
            f"“{name}” is open in another application — most likely a PDF "
            "viewer showing the same file.\n\n"
            "Close it there, then save again. Your changes are still here."
        )
    return f"Could not save:\n\n{exc}"


class DocumentView(QWidget):
    # Emitted when page / dirty / count changes so the window can refresh chrome.
    stateChanged = Signal()
    # Non-modal edit notices ("font can't be matched exactly", overflow) — the
    # window shows them in the status bar.
    editWarning = Signal(str)
    # A span/paragraph editor opened — carries the clicked TextSpan/Paragraph
    # so the window can reflect its style in the style toolbar.
    styleContextChanged = Signal(object)
    # The open editor's selection/caret format for the toolbar: a dict with
    # "size" (float | None), "bold"/"italic"/"underline"/"strike" (bool | None),
    # "script" (SCRIPT_* | None) and "color" (sRGB int | None) of the editor
    # selection. None = MIXED — blanks/unchecks/neutralises the control.
    selectionFormatChanged = Signal(object)
    # An in-place editor session ended (commit or cancel) — the toolbar
    # restores its GLOBAL insert-default states (E11.3: selection reflection
    # must not bleed into the defaults used for the next insert).
    editorClosed = Signal()
    # Persistent point-of-need hint for the hovered element (U2b); "" clears.
    hoverHintChanged = Signal(str)

    def __init__(self, doc: PdfDocument, parent=None) -> None:
        super().__init__(parent)
        self._doc: PdfDocument = doc
        self._current_page = 0
        self._cache = RenderCache(capacity=_CACHE_ITEMS, max_cost=_CACHE_BYTES)
        # Editable geometry per page (U1) — same invalidation funnel as the
        # render cache (after_command), same rule-8 rationale.
        self._geometry = GeometryCache()
        # OCR words per page (X0) — third cache on the SAME funnel. Slow to
        # fill (~1–2 s/page), shared by search and Extract Text so either
        # pre-warms the other.
        self._ocr_words = OcrWordCache()

        # All mutations flow through this stack (SnapshotCommand); dirty state
        # derives from it. The depth cap MUST be set while the stack is empty
        # (Qt ignores setUndoLimit later) and is budgeted from the file size.
        self._undo_stack = QUndoStack(self)
        source = doc.source
        file_size = source.stat().st_size if source is not None and source.exists() else 1
        self._undo_stack.setUndoLimit(undo_limit_for(file_size))
        # Signal-to-signal: cleanChanged(bool) -> stateChanged() (arg dropped).
        self._undo_stack.cleanChanged.connect(self.stateChanged)

        # Read-only vs edit mode (U0): documents open READ-ONLY. Edits are
        # destructive (redact-and-reinsert) and saving drops encryption, so
        # editing is a deliberate per-document opt-in. The mode gates the
        # editorial entry points; navigation/zoom/print/save never gate.
        self._edit_mode = False
        # "Show editable areas" (U5): outline every paragraph/image. Default
        # ON (user decision after the hands-on pass, 2026-07-03). Displayed
        # only in edit mode; the flag itself survives mode flips.
        self._show_areas = True
        # Double-click sub-mode (U8): what a PLAIN double-click edits.
        # True = the whole paragraph (user decision after the hands-on
        # pass); Ctrl is always a momentary override to the other target
        # (XOR), so Ctrl+double-click edits one line by default.
        self._dblclick_paragraph = True
        # Current highlighter colour (r, g, b) 0-1, or None for the engine's
        # default yellow. Set by MainWindow from the Annotate toolbar swatch
        # (A4); all highlight paths (marquee, span, selection) read it.
        self._highlight_color: tuple[float, float, float] | None = None

        self._canvas = PageCanvas(self)
        self._canvas.renderNeeded.connect(self._on_render_needed)
        self._canvas.pointActivated.connect(self._on_point_activated)
        self._canvas.insertPointSelected.connect(self._on_insert_point)
        self._canvas.moveDragStarted.connect(self._on_move_drag_started)
        self._canvas.moveDragFinished.connect(self._on_move_drag_finished)
        self._canvas.backgroundPressed.connect(self._commit_open_editor)
        self._canvas.regionSelected.connect(self._on_region_selected)
        self._canvas.contextMenuRequested.connect(self._on_context_menu)
        self._canvas.hoverMoved.connect(self._on_hover_moved)
        self._canvas.hoverKindChanged.connect(
            lambda kind: self.hoverHintChanged.emit(hover_hint(kind, self._dblclick_paragraph))
        )
        self._canvas.selectDragStarted.connect(self._on_select_drag_started)
        self._canvas.textSelectMoved.connect(self._on_text_select_moved)
        self._canvas.textSelectFinished.connect(self._on_text_select_finished)
        self._canvas.copyRequested.connect(self.copy_selection)
        self._canvas.escapePressed.connect(self._on_escape)
        self._canvas.deleteSelectionRequested.connect(self._on_delete_selection)
        # Signal-to-signal: armed state feeds chrome sync (checked actions).
        self._canvas.armedChanged.connect(self.stateChanged)
        self._canvas.pageScrollRequested.connect(self._on_page_scroll)
        self._thumbnails = ThumbnailPanel(self)
        self._thumbnails.pageSelected.connect(self.go_to_page)

        self._editor = TextEditorOverlay(self._canvas.viewport())
        self._editor.committed.connect(self._on_edit_committed)
        self._editor.cancelled.connect(self._on_edit_cancelled)
        self._editor.selectionChanged.connect(self._on_editor_selection_changed)
        self._editor.cursorPositionChanged.connect(self._on_editor_selection_changed)
        self._pending_edit: tuple[int, TextSpan] | None = None
        self._para_editor = ParagraphEditorOverlay(self._canvas.viewport())
        self._para_editor.committed.connect(self._on_paragraph_committed)
        self._para_editor.cancelled.connect(self._on_edit_cancelled)
        self._para_editor.selectionChanged.connect(self._on_editor_selection_changed)
        self._para_editor.cursorPositionChanged.connect(self._on_editor_selection_changed)
        # Connected AFTER the main handlers (Qt delivers in connection order):
        # the toolbar restores its global defaults once the session is DONE.
        for overlay in (self._editor, self._para_editor):
            overlay.committed.connect(lambda _text: self.editorClosed.emit())
            overlay.cancelled.connect(self.editorClosed)
        self._pending_paragraph: tuple[int, Paragraph] | None = None
        self._pending_insert: tuple[int, tuple[float, float]] | None = None
        # Review comments (E11): pending create (page, rect, callout target),
        # pending text edit (page, xref), and an in-flight Ctrl+drag move.
        self._pending_comment: tuple[int, tuple, tuple | None] | None = None
        self._pending_comment_edit: tuple[int, int, str] | None = None
        self._move_comment: tuple[int, object] | None = None
        self._move_paragraph: tuple[int, Paragraph] | None = None
        # Ctrl/Shift multi-selection of text boxes (E10.7): grouped moves and
        # merge-into-one. Cleared with the single selection (one funnel).
        self._multi_paragraphs: list[tuple[int, Paragraph]] = []
        self._move_group: tuple[int, list[Paragraph]] | None = None
        self._move_image_target: tuple[int, object] | None = None
        self._resize_image: tuple[int, object, tuple[float, float]] | None = None
        # Click-to-select state (U6): ("text"|"image", page, Paragraph|ImageInfo).
        # The canvas only displays the chrome; this is the source of truth.
        self._selection: tuple[str, int, object] | None = None
        # Read-only word-snapped flow text selection (X4). The selection lives
        # in PAGE space as (line, word) positions into the current page's
        # reading-order lines (cached in _text_lines); the canvas only draws the
        # rects. Cleared on page change / entering edit mode / mutation / close.
        # A drag draws a rectangle; the selection is the per-line runs of words
        # inside it (textselect.Region, PAGE space). _text_sel_anchor_pt is the
        # drag's press point (page space). _text_lines is the whole-page grouped
        # words (basis for BOTH the window select and the hover I-beam), cached
        # per page. Cleared on page change / entering edit mode / mutation.
        self._text_selection: list | None = None
        self._text_sel_anchor_pt: tuple[float, float] | None = None
        self._text_lines: list | None = None
        self._text_lines_page = -1
        # Armed one-shot click action: ("text", None) | ("image", Path).
        # The canvas' insertPointSelected feeds it. (Highlighting uses the
        # canvas' region-select mode instead — a dragged window.)
        self._click_action: tuple[str, object] | None = None
        # Set by MainWindow: () -> (TextStyle, QFont preview). None = match
        # the original style (headless/tests without a style toolbar).
        self.style_provider = None
        # State captured when an editor opens: the toolbar style (fallback
        # no-op detection), the rich-content signature (rich no-op detection)
        # and the zoom (pixel-size -> pt conversion at commit).
        self._edit_open_style = None
        self._edit_open_sig: tuple = ()
        self._edit_open_zoom: float = 1.0
        # (family, bold, italic) -> (code, fontfile, resolved); shared default.
        self.format_resolver = font_choice

        # Search (SR2): per-tab bar + state. ALWAYS case-insensitive (by
        # design — no toggle exists). The debounce keeps whole-document
        # re-searches off the typing hot path; Enter forces an immediate run.
        self._search_bar = SearchBar(self)
        self._search_bar.queryChanged.connect(self._on_search_query_changed)
        self._search_bar.nextRequested.connect(self.next_match)
        self._search_bar.prevRequested.connect(self.prev_match)
        self._search_bar.closeRequested.connect(self.close_search)
        self._search_bar.ocrRequested.connect(self.search_with_ocr)
        self._search_hits: list = []
        self._search_index = -1
        # SR4: per-view, per-session opt-in to OCR-backed search. Reset when
        # a bulk run is cancelled — a partial index must never claim
        # completeness (the offer reappears instead).
        self._search_ocr_opted_in = False
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self.run_search)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self._thumbnails)
        splitter.addWidget(self._canvas)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([180, 820])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        # Explicit stretch: a HORIZONTAL splitter's default size policy is
        # Expanding only along its orientation — vertically it is Preferred,
        # the same as the bar, so without this the layout split the height
        # 50/50 whenever the search bar was visible (giant bar, half-size
        # page — latent since SR2, reported 2026-07-18).
        layout.addWidget(self._search_bar, 0)
        layout.addWidget(splitter, 1)

        self._populate_thumbnails()
        self._show_page(0)

    # --- read-only state ------------------------------------------------
    @property
    def ocr_word_cache(self) -> OcrWordCache:
        """The per-page OCR cache shared by search and Extract Text."""
        return self._ocr_words

    @property
    def document(self) -> PdfDocument:
        return self._doc

    @property
    def page_count(self) -> int:
        return self._doc.page_count

    @property
    def current_page(self) -> int:
        return self._current_page

    @property
    def dirty(self) -> bool:
        return not self._undo_stack.isClean()

    @property
    def undo_stack(self) -> QUndoStack:
        return self._undo_stack

    @property
    def path(self) -> Path | None:
        return self._doc.source

    @property
    def title(self) -> str:
        source = self._doc.source
        return source.name if source is not None else "Untitled"

    @property
    def thumbnails_visible(self) -> bool:
        return self._thumbnails.isVisible()

    def set_thumbnails_visible(self, visible: bool) -> None:
        self._thumbnails.setVisible(visible)

    def close_document(self) -> None:
        self._doc.close()

    def page_geometry(self, n: int) -> PageGeometry:
        """Cached editable geometry of page ``n`` (spans/paragraphs/images).

        Insert-isolation boundaries come from the box REGISTRY stored in the
        document itself (E10) — undo/save/reopen carry it automatically, so
        isolation never silently lapses the way the old session tracking did.
        """
        boundaries = tuple(box.rect for box in self._doc.boxes(n))
        return self._geometry.page(self._doc, n, boundaries)

    @staticmethod
    def _box_for(doc: PdfDocument, n: int, bbox: tuple[float, float, float, float]):
        """The registered box a paragraph belongs to (its centre inside the
        box rect), or None for pre-existing text."""
        cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        for box in doc.boxes(n):
            x0, y0, x1, y1 = box.rect
            if x0 - 2 <= cx <= x1 + 2 and y0 - 2 <= cy <= y1 + 2:
                return box
        return None

    # --- read-only / edit mode (U0) ---------------------------------------
    @property
    def edit_mode(self) -> bool:
        return self._edit_mode

    def set_edit_mode(self, on: bool) -> None:
        """Flip between Markup mode (annotate) and Edit mode (content editing).

        A mode switch is a clean slate for transient interaction state in BOTH
        directions: commit any open in-place editor (comment editors can be open
        in Markup mode too), disarm one-shot modes, drop element + text
        selection and hover chrome. The undo stack is untouched — the mode gates
        which NEW interactions are offered, not document state.
        """
        on = bool(on)
        if on == self._edit_mode:
            return
        self._edit_mode = on
        self._commit_open_editor()  # apply any in-progress edit (either mode)
        self.cancel_armed_mode()
        self._clear_selection()
        self._clear_text_selection()  # the marquee selection is Markup-mode only (X4)
        self._canvas.clear_hover()  # reset the I-beam / hover outline
        self._push_reveal_chrome()  # reveal-all displays only in edit mode
        self.stateChanged.emit()

    # --- "show editable areas" (U5) ----------------------------------------
    @property
    def show_editable_areas(self) -> bool:
        return self._show_areas

    def set_show_editable_areas(self, on: bool) -> None:
        on = bool(on)
        if on == self._show_areas:
            return
        self._show_areas = on
        self._push_reveal_chrome()
        self.stateChanged.emit()

    # --- double-click sub-mode (U8) ------------------------------------------
    @property
    def dblclick_paragraph(self) -> bool:
        return self._dblclick_paragraph

    def set_dblclick_paragraph(self, on: bool) -> None:
        on = bool(on)
        if on == self._dblclick_paragraph:
            return
        self._dblclick_paragraph = on
        # The mode must stay visible: refresh a live text hint immediately
        # rather than waiting for the next hover change.
        if self._canvas.hover_kind == "text":
            self.hoverHintChanged.emit(hover_hint("text", on))
        self.stateChanged.emit()

    def _push_reveal_chrome(self) -> None:
        """(Re-)display the reveal-all outlines — every paragraph and image
        of the current page, from the geometry cache. Recomputed after page
        switches, re-renders and mutations (the callers of this method)."""
        if not (self._edit_mode and self._show_areas and self._canvas.has_page):
            self._canvas.set_reveal_rects([])
            return
        n = self._current_page
        geometry = self.page_geometry(n)
        zoom = self._canvas.render_zoom
        rot = self._doc.page_rotation(n)
        size = self._doc.page_size(n)
        rects = [
            page_coords.page_rect_to_scene(bbox, render_zoom=zoom, rotation=rot, page_size_pts=size)
            for bbox in ([p.bbox for p in geometry.paragraphs] + [i.bbox for i in geometry.images])
        ]
        self._canvas.set_reveal_rects(rects)

    # --- navigation -----------------------------------------------------
    def go_to_page(self, index: int) -> None:
        index = max(0, min(index, self._doc.page_count - 1))
        if index != self._current_page:
            self._clear_selection()  # a selection never outlives its page view
            self._clear_text_selection()  # nor does the read-only text selection
        self._current_page = index
        self._show_page(index)
        self.stateChanged.emit()

    def next_page(self) -> None:
        self.go_to_page(self._current_page + 1)

    def prev_page(self) -> None:
        self.go_to_page(self._current_page - 1)

    def first_page(self) -> None:
        self.go_to_page(0)

    def last_page(self) -> None:
        self.go_to_page(self._doc.page_count - 1)

    def _on_page_scroll(self, direction: int) -> None:
        """A plain scroll crossed a page edge: flip one page and land at the
        matching edge — the next page's top / the previous page's bottom. At
        the first page's top / last page's bottom, do nothing (no wrap).
        A read feature like the nav buttons — never edit-gated."""
        target = self._current_page + direction
        if not 0 <= target < self._doc.page_count:
            return
        self.go_to_page(target)
        self._canvas.scroll_to_vertical_edge(top=direction > 0)

    # --- search (SR2) ------------------------------------------------------
    def open_search(self) -> None:
        """Show + focus the search bar (Ctrl+F / Edit → Find)."""
        self._search_bar.open_bar()
        self._sync_search_offer()

    def close_search(self) -> None:
        """Hide the bar, drop hits + chrome, hand focus back to the canvas."""
        self.clear_search_results()
        self._search_bar.hide()
        self._canvas.setFocus()

    def clear_search_results(self) -> None:
        """Drop hits/index/chrome; the bar's visibility and query survive.

        Called on EVERY mutation via after_command — hit rects describe
        content that may no longer exist, so the count would lie.
        """
        self._search_hits = []
        self._search_index = -1
        self._search_bar.set_status("")
        self._canvas.clear_search_hits()

    def _on_search_query_changed(self, _text: str) -> None:
        self._search_timer.start()

    def run_search(self) -> None:
        """Search the whole document now (debounce flushed).

        Native pages via the engine; scanned pages via cached OCR words when
        this view has opted in (SR4) — the SAME matcher either way. A page is
        exactly one source, so a stable page-index sort keeps reading order.
        """
        self._search_timer.stop()
        query = self._search_bar.query()
        if not query.strip():
            self.clear_search_results()
            self._sync_search_offer()
            return
        hits = list(self._doc.search(query, include_comments=self._search_bar.include_comments()))
        ocr_found_nothing = None  # None = OCR did not run this query
        if self._search_ocr_opted_in:
            ocr_hits, ocr_found_nothing = self._ocr_search_hits(query)
            hits.extend(ocr_hits)
            hits.sort(key=lambda h: h.page_index)
        self._search_hits = hits
        self._search_index = 0 if hits else -1
        if hits:
            self._go_to_hit(0)
        else:
            self._search_bar.set_status(self._no_match_message(ocr_found_nothing))
            self._push_search_chrome()
        self._sync_search_offer()

    def search_with_ocr(self) -> None:
        """The offer button (SR4): user-initiated opt-in to OCR-backed search.

        Applies the same bulk-OCR confirm gate as Extract Text, then re-runs
        the current query with scanned pages included.
        """
        from pdfapp import extract_support
        from pdfcore import ocr

        scanned = self._scanned_pages()
        if not scanned or not ocr.tesseract_available():
            return
        if len(scanned) >= extract_support.BULK_OCR_WARN_AT and not (
            extract_support.confirm_bulk_ocr(self, len(scanned))
        ):
            return
        self._search_ocr_opted_in = True
        self.run_search()

    def _scanned_pages(self) -> list[int]:
        return [n for n in range(self._doc.page_count) if not self._doc.has_text_layer(n)]

    def _ocr_search_hits(self, query: str) -> tuple[list, bool | None]:
        """Hits from scanned pages' OCR words; ``(hits, found_nothing)``.

        The bulk runner is X2's — same progress, same cancel, same shared
        cache (cache hits are instant, so re-queries cost nothing; a page
        evicted by after_command re-OCRs here). A cancelled run drops the
        opt-in so the offer reappears — partial coverage must not pose as
        complete. ``found_nothing`` is True only when OCR covered EVERY
        scanned page and produced zero words (the unsearchable signal).
        """
        from pdfapp import extract_support
        from pdfcore import textsource

        scanned = self._scanned_pages()
        if not scanned:
            return [], None
        result = extract_support.run_bulk_ocr(
            self, self._doc, scanned, self._ocr_words, label="Recognising scanned pages…"
        )
        if result.cancelled or result.tesseract_missing:
            self._search_ocr_opted_in = False
        hits = []
        for n in scanned:
            words = result.words_by_page.get(n)
            if not words:
                continue
            hits.extend(
                textsource.SearchHit(page_index=n, rects=rects)
                for rects in textsource.search_words(words, query)
            )
        covered_all = len(result.words_by_page) == len(scanned)
        total_words = sum(len(w) for w in result.words_by_page.values())
        return hits, (covered_all and total_words == 0)

    def _no_match_message(self, ocr_found_nothing: bool | None) -> str:
        from pdfcore import ocr

        scanned = self._scanned_pages()
        whole_doc_scanned = bool(scanned) and len(scanned) == self._doc.page_count
        if whole_doc_scanned and ocr_found_nothing:
            return "This document isn't searchable."
        if whole_doc_scanned and not ocr.tesseract_available():
            return "No text layer — OCR is unavailable (Tesseract not installed)."
        return "No matches"

    def _sync_search_offer(self) -> None:
        from pdfcore import ocr

        show = (
            not self._search_ocr_opted_in
            and bool(self._scanned_pages())
            and ocr.tesseract_available()
        )
        self._search_bar.show_ocr_offer(show)

    def next_match(self) -> None:
        self._step_match(1)

    def prev_match(self) -> None:
        self._step_match(-1)

    def _step_match(self, delta: int) -> None:
        # Enter with a pending debounce (or before any run) searches NOW —
        # landing on the first hit IS the step the user asked for.
        if self._search_timer.isActive() or (
            not self._search_hits and self._search_bar.query().strip()
        ):
            self.run_search()
            return
        if not self._search_hits:
            return
        self._go_to_hit((self._search_index + delta) % len(self._search_hits))

    def _go_to_hit(self, index: int) -> None:
        hit = self._search_hits[index]
        self._search_index = index
        if hit.page_index != self._current_page:
            self.go_to_page(hit.page_index)  # _show_page re-pushes the chrome
        else:
            self._push_search_chrome()
        self._search_bar.set_status(f"{index + 1} of {len(self._search_hits)}")
        scene_rects = self._hit_scene_rects(hit)
        if scene_rects:
            x0, y0, x1, y1 = scene_rects[0]
            self._canvas.ensureVisible(QRectF(x0, y0, x1 - x0, y1 - y0), 50, 50)

    def _hit_scene_rects(self, hit) -> list[tuple[float, float, float, float]]:
        n = hit.page_index
        return [
            page_coords.page_rect_to_scene(
                bbox,
                render_zoom=self._canvas.render_zoom,
                rotation=self._doc.page_rotation(n),
                page_size_pts=self._doc.page_size(n),
            )
            for bbox in hit.rects
        ]

    def _push_search_chrome(self) -> None:
        """The CURRENT page's hit highlights → canvas (scene px).

        Re-pushed wherever selection/reveal chrome is (page show, re-render)
        so highlights survive zoom changes and page switches.
        """
        if not self._search_hits:
            self._canvas.clear_search_hits()
            return
        page = self._current_page
        other_rects: list[tuple[float, float, float, float]] = []
        current_rects: list[tuple[float, float, float, float]] = []
        for i, hit in enumerate(self._search_hits):
            if hit.page_index != page:
                continue
            scene = self._hit_scene_rects(hit)
            if i == self._search_index:
                current_rects.extend(scene)
            else:
                other_rects.extend(scene)
        self._canvas.set_search_hits(other_rects, current_rects)

    # --- theme -----------------------------------------------------------
    def refresh_theme(self) -> None:
        """Re-pull themed chrome (canvas backdrop, armed chip) after a switch."""
        self._canvas.setBackgroundBrush(theme.canvas_brush())
        self._canvas.refresh_chip_theme()
        self._search_bar.refresh_theme()

    # --- zoom (delegate to the canvas) ----------------------------------
    def zoom_in(self) -> None:
        self._canvas.zoom_in()

    def zoom_out(self) -> None:
        self._canvas.zoom_out()

    def fit_page(self) -> None:
        self._canvas.fit_page()

    def fit_width(self) -> None:
        self._canvas.fit_width()

    # --- page edits (all mutations flow through the undo stack) ---------
    def rotate_clockwise(self) -> None:
        self._rotate_current(90)

    def rotate_counterclockwise(self) -> None:
        self._rotate_current(-90)

    def _rotate_current(self, deg: int) -> None:
        n = self._current_page
        self._push_command("Rotate page", lambda d: d.rotate([n], deg), ("page", n))

    def delete_current_page(self) -> None:
        # Guards stay OUTSIDE the command so no-op pushes never happen.
        if self._doc.page_count <= 1:
            QMessageBox.information(self, "Delete page", "A PDF must keep at least one page.")
            return

        def op(doc: PdfDocument) -> None:
            doc.delete([self._current_page])
            self._current_page = min(self._current_page, doc.page_count - 1)

        self._push_command("Delete page", op, ("all", -1))

    def move_page_up(self) -> None:
        self._move_current(-1)

    def move_page_down(self) -> None:
        self._move_current(1)

    def _move_current(self, delta: int) -> None:
        i = self._current_page
        j = i + delta
        if not (0 <= j < self._doc.page_count):
            return
        order = list(range(self._doc.page_count))
        order[i], order[j] = order[j], order[i]

        def op(doc: PdfDocument) -> None:
            doc.reorder(order)
            self._current_page = j  # follow the moved page

        self._push_command("Move page", op, ("all", -1))

    def insert_from_path(self, src_path: Path, at: int) -> None:
        def op(doc: PdfDocument) -> None:
            doc.insert_from(src_path, at=at)
            self._current_page = min(at, doc.page_count - 1)

        self._push_command("Insert pages", op, ("all", -1))

    # --- click-to-edit text (Phase 2) ------------------------------------
    def focus_open_editor(self) -> None:
        """Hand keyboard focus back to whichever in-place editor is open.

        Called after typing a size into the toolbar spin (which needs focus
        to accept typed digits) so the user lands straight back in the text.
        """
        if self._editor.is_editing:
            self._editor.setFocus()
        elif self._para_editor.is_editing:
            self._para_editor.setFocus()

    def _commit_open_editor(self) -> None:
        """Commit whichever in-place editor is open (click-away / new action).

        Focus-out no longer dismisses the editors (so the style toolbar can be
        used mid-edit), so this is how an open edit is applied when the user
        clicks the page or starts another edit.
        """
        if self._editor.is_editing:
            self._editor.commit()
        elif self._para_editor.is_editing:
            self._para_editor.commit()

    def _on_point_activated(self, sx: float, sy: float, block: bool = False) -> None:
        """Double-click on the canvas: hit-test, open the matching editor.

        The canvas reports the RAW gesture (``block`` = Ctrl held); the U8
        sub-mode sets the default target and Ctrl is a momentary override:
        ``target = sub_mode XOR ctrl``. In read-only mode it selects the word
        under the cursor (X4) instead of opening an editor.
        """
        self._commit_open_editor()  # apply any in-progress edit first
        n = self._current_page
        px, py = page_coords.scene_to_page(
            sx,
            sy,
            render_zoom=self._canvas.render_zoom,
            rotation=self._doc.page_rotation(n),
            page_size_pts=self._doc.page_size(n),
        )
        # Hit-test the DISPLAYED geometry (hover/reveal outlines): the whole
        # box is the affordance, so a double-click on blank space inside an
        # outlined paragraph starts the edit too — not just its glyphs.
        geometry = self.page_geometry(n)
        target = hover_target(geometry, px, py)
        # A comment is markup — double-click edits it in EITHER mode (comments
        # float on top). Checked BEFORE the Markup-mode word-selection branch.
        if target is not None and target.kind == "comment":
            self._begin_comment_edit(n, target.payload)
            return
        if not self._edit_mode:
            self._select_word_at(sx, sy)  # Markup mode: word selection (X4)
            return
        if target is not None and target.kind == "text":
            para = target.payload
            if any(s.rotation != 0 for s in para.spans):
                # Rotated text is a single-line entity: BOTH sub-modes edit
                # the span (the paragraph layout engine is horizontal-only).
                span = page_coords.span_at(geometry.spans, px, py) or nearest_span_in_paragraph(
                    para, px, py
                )
                if span is not None:
                    self._begin_text_edit(n, span)
                return
            if self._dblclick_paragraph != block:  # XOR — see U8
                self._begin_paragraph_edit(n, para)
            else:
                span = page_coords.span_at(geometry.spans, px, py) or nearest_span_in_paragraph(
                    para, px, py
                )
                if span is not None:
                    self._begin_text_edit(n, span)
            return
        # No text target under the point — in EITHER sub-mode a double-click
        # on an image swaps it.
        image = self._doc.image_at(n, px, py)
        if image is not None:
            self._replace_image_at(n, image)

    def _begin_text_edit(self, page_index: int, span: TextSpan) -> None:
        if span.rotation is None:
            self.editWarning.emit(
                "This text is rotated at an unsupported angle and can't be edited."
            )
            return
        self._clear_selection()  # one chrome at a time — the editor takes over
        scene_rect = page_coords.page_rect_to_scene(
            span.bbox,
            render_zoom=self._canvas.render_zoom,
            rotation=self._doc.page_rotation(page_index),
            page_size_pts=self._doc.page_size(page_index),
        )
        top_left = self._canvas.mapFromScene(QPointF(scene_rect[0], scene_rect[1]))
        bottom_right = self._canvas.mapFromScene(QPointF(scene_rect[2], scene_rect[3]))
        rect = QRect(top_left, bottom_right).normalized().adjusted(-2, -2, 2, 2)
        if span.rotation:  # rotated: edit in a HORIZONTAL box at the same spot
            length = max(rect.width(), rect.height()) + 24
            height = max(18, round(span.size * self._canvas.zoom) + 10)
            rect = QRect(rect.left(), rect.top(), max(60, length), height)
            self.editWarning.emit(
                "Rotated text — shown horizontally while editing; it stays rotated on the page."
            )
        self._pending_edit = (page_index, span)
        self.styleContextChanged.emit(span)
        self._edit_open_style = self._current_style()
        self._edit_open_zoom = self._canvas.zoom
        base_font = self._editor_font_for(span)
        self._editor.open_pieces(rect, self._pieces_from_span(span), base_font, select_all=True)
        self._edit_open_sig = self._pieces_signature(self._editor)

    def _char_format_for(self, span: TextSpan | Paragraph) -> QTextCharFormat:
        """A char format visually AND semantically matching an extracted span.

        The font's pixel size is display-only (pt x zoom); the TRUE point size
        rides along as PT_PROPERTY so commits round-trip exactly at any zoom.
        """
        fmt = QTextCharFormat()
        fmt.setFont(self._editor_font_for(span))
        fmt.setProperty(PT_PROPERTY, float(span.size))
        color = span.color
        fmt.setForeground(QColor((color >> 16) & 255, (color >> 8) & 255, color & 255))
        # Underline/strike are drawn rules, detected geometrically at
        # extraction (TextSpan.underline/strike) — carry the COARSE state here
        # so re-editing ruled text shows the rule and the toggle tracks it. The
        # per-run split (partly-ruled lines) is applied by _pieces_from_span.
        # Paragraph (the empty-lines fallback) has no such attrs; getattr keeps
        # that path safe.
        fmt.setFontUnderline(bool(getattr(span, "underline", False)))
        fmt.setFontStrikeOut(bool(getattr(span, "strike", False)))
        return fmt

    def _pieces_from_span(self, span: TextSpan) -> list[tuple[str, QTextCharFormat]]:
        """Editor (text, format) pieces for a span, SPLIT by its per-run
        ``rule_segments`` so a line ruled on only some words re-opens with
        exactly those words ruled — not the whole line (user report: editing a
        partly-struck sentence re-applied the strike across all of it)."""
        base = self._char_format_for(span)
        segments = getattr(span, "rule_segments", None)
        if not segments:
            return [(span.text, base)]
        pieces: list[tuple[str, QTextCharFormat]] = []
        for text, underline, strike in segments:
            fmt = QTextCharFormat(base)
            fmt.setFontUnderline(underline)
            fmt.setFontStrikeOut(strike)
            pieces.append((text, fmt))
        return pieces

    def _pieces_signature(self, editor) -> tuple:
        """A comparable snapshot of an editor's rich content (no-op detection)."""
        pieces = editor._pieces()
        runs, _resolved = self._runs_from_pieces(pieces)
        return tuple((run.text, run.style) for run in runs)

    def _editor_font_for(self, span: TextSpan | Paragraph) -> QFont:
        """A font that visually matches the span/paragraph at the current zoom.

        Family follows the base-14 mapping (what the replacement will render
        as under Windows substitution); pixel size = fontsize (pt) x logical
        zoom (px/pt) so the editor text is the same on-screen size as the page
        text under it.
        """
        code = span.base14 or ""
        if code.startswith("he"):
            family = "Arial"
        elif code.startswith("ti"):
            family = "Times New Roman"
        elif code.startswith("co"):
            family = "Courier New"
        else:
            family = span.font  # best effort for embedded/unmapped fonts
        font = QFont(family)
        font.setPixelSize(max(8, round(span.size * self._canvas.zoom)))
        font.setBold(bool(span.flags & FLAG_BOLD))
        font.setItalic(bool(span.flags & FLAG_ITALIC))
        return font

    def _runs_from_pieces(self, pieces) -> tuple[list, bool]:
        """Editor (text, QTextCharFormat) pieces -> engine StyledRuns.

        Pixel sizes convert back to points via the zoom captured when the
        editor opened. Returns (runs, all_fonts_resolved).
        """
        resolver = self.format_resolver or font_choice
        zoom = self._edit_open_zoom or self._canvas.zoom or 1.0
        runs: list[StyledRun] = []
        all_resolved = True
        for text, fmt in pieces:
            if text == "\n":
                runs.append(StyledRun("\n", runs[-1].style if runs else TextStyle()))
                continue
            font = fmt.font()
            families = fmt.fontFamilies()
            family = families[0] if families else font.family()
            bold = font.bold() or fmt.fontWeight() >= 600
            italic = font.italic()
            code, fontfile, resolved = resolver(family, bold, italic)
            all_resolved = all_resolved and resolved
            pt_property = fmt.property(PT_PROPERTY)
            pixel = font.pixelSize()
            if isinstance(pt_property, float) and pt_property > 0:
                size = round(pt_property, 1)  # exact, zoom-independent
            elif pixel > 0:
                size = round(pixel / zoom, 1)
            else:
                size = round(font.pointSizeF(), 1) if font.pointSizeF() > 0 else 11.0
            brush = fmt.foreground()
            qcolor = brush.color() if brush.style() != Qt.BrushStyle.NoBrush else QColor(0, 0, 0)
            color = (qcolor.red() << 16) | (qcolor.green() << 8) | qcolor.blue()
            alignment = fmt.verticalAlignment()
            if alignment == QTextCharFormat.VerticalAlignment.AlignSuperScript:
                script = SCRIPT_SUPER
            elif alignment == QTextCharFormat.VerticalAlignment.AlignSubScript:
                script = SCRIPT_SUB
            else:
                script = SCRIPT_NORMAL
            runs.append(
                StyledRun(
                    text,
                    TextStyle(
                        code=code,
                        fontfile=fontfile,
                        size=max(1.0, size),
                        color=color,
                        underline=fmt.fontUnderline() or font.underline(),
                        strike=fmt.fontStrikeOut() or font.strikeOut(),
                        script=script,
                    ),
                )
            )
        return runs, all_resolved

    def _fmt_size_pt(self, fmt: QTextCharFormat) -> float:
        """True point size of a char format — PT_PROPERTY first (pixel sizes
        cannot round-trip points at small zooms; same rule as commits)."""
        pt_property = fmt.property(PT_PROPERTY)
        if isinstance(pt_property, float) and pt_property > 0:
            return round(pt_property, 1)
        font = fmt.font()
        zoom = self._edit_open_zoom or self._canvas.zoom or 1.0
        if font.pixelSize() > 0:
            return round(font.pixelSize() / zoom, 1)
        return round(font.pointSizeF(), 1) if font.pointSizeF() > 0 else 11.0

    @staticmethod
    def _fmt_traits(fmt: QTextCharFormat) -> tuple[bool, bool, bool, bool]:
        """(bold, italic, underline, strike) of a char format — the SAME
        predicates the commit conversion uses (_runs_from_pieces)."""
        font = fmt.font()
        return (
            font.bold() or fmt.fontWeight() >= 600,
            font.italic(),
            fmt.fontUnderline() or font.underline(),
            fmt.fontStrikeOut() or font.strikeOut(),
        )

    @staticmethod
    def _fmt_script(fmt: QTextCharFormat) -> int:
        """SCRIPT_* of a char format — same mapping as the commit conversion."""
        alignment = fmt.verticalAlignment()
        if alignment == QTextCharFormat.VerticalAlignment.AlignSuperScript:
            return SCRIPT_SUPER
        if alignment == QTextCharFormat.VerticalAlignment.AlignSubScript:
            return SCRIPT_SUB
        return SCRIPT_NORMAL

    @staticmethod
    def _fmt_color(fmt: QTextCharFormat) -> int:
        """sRGB int of a char format's foreground — same rule as the commit
        conversion (an unset brush reads as black)."""
        brush = fmt.foreground()
        qcolor = brush.color() if brush.style() != Qt.BrushStyle.NoBrush else QColor(0, 0, 0)
        return (qcolor.red() << 16) | (qcolor.green() << 8) | qcolor.blue()

    @property
    def has_open_editor(self) -> bool:
        return self._editor.is_editing or self._para_editor.is_editing

    def _on_editor_selection_changed(self) -> None:
        """Report the open editor's selection format to the toolbar: uniform
        values show as the actual size / checked state; MIXED values report
        None (blank size field, unchecked toggle)."""
        if self._editor.is_editing:
            editor = self._editor
        elif self._para_editor.is_editing:
            editor = self._para_editor
        else:
            return
        cursor = editor.textCursor()
        if not cursor.hasSelection():
            fmt = cursor.charFormat()
            bold, italic, underline, strike = self._fmt_traits(fmt)
            self.selectionFormatChanged.emit(
                {
                    "size": self._fmt_size_pt(fmt),
                    "bold": bold,
                    "italic": italic,
                    "underline": underline,
                    "strike": strike,
                    "script": self._fmt_script(fmt),
                    "color": self._fmt_color(fmt),
                }
            )
            return
        probe = editor.textCursor()
        sizes: set[float] = set()
        bolds: set[bool] = set()
        italics: set[bool] = set()
        underlines: set[bool] = set()
        strikes: set[bool] = set()
        scripts: set[int] = set()
        colors: set[int] = set()
        for pos in range(cursor.selectionStart() + 1, cursor.selectionEnd() + 1):
            probe.setPosition(pos)  # charFormat() = format of the char BEFORE pos
            fmt = probe.charFormat()
            sizes.add(self._fmt_size_pt(fmt))
            bold, italic, underline, strike = self._fmt_traits(fmt)
            bolds.add(bold)
            italics.add(italic)
            underlines.add(underline)
            strikes.add(strike)
            scripts.add(self._fmt_script(fmt))
            colors.add(self._fmt_color(fmt))

        def uniform(values: set):
            return next(iter(values)) if len(values) == 1 else None

        self.selectionFormatChanged.emit(
            {
                "size": uniform(sizes),
                "bold": uniform(bolds),
                "italic": uniform(italics),
                "underline": uniform(underlines),
                "strike": uniform(strikes),
                "script": uniform(scripts),
                "color": uniform(colors),
            }
        )

    def _on_edit_committed(self, text: str) -> None:
        if self._pending_edit is None:
            return
        page_index, span = self._pending_edit
        self._pending_edit = None

        pieces = self._editor.committed_pieces_for(text)
        results: list = []
        resolved = True
        if pieces is not None:  # rich path: per-selection styles from the editor
            runs, resolved = self._runs_from_pieces(pieces)
            signature = tuple((run.text, run.style) for run in runs)
            if text == span.text and signature == self._edit_open_sig:
                return  # neither text nor styling changed — no command

            def op(doc: PdfDocument) -> None:
                results.append(doc.replace_text_runs(page_index, span, runs))
        else:  # direct-call fallback (tests / programmatic edits): one style
            style = self._current_style()
            if text == span.text and style == self._edit_open_style:
                return

            def op(doc: PdfDocument) -> None:
                results.append(doc.replace_text(page_index, span, text, style=style))

        if not self._push_command("Edit text", op, ("page", page_index)):
            return
        result = results[0]
        if not result.exact_font or not resolved:
            self.editWarning.emit("Font can't be matched exactly — closest standard font used.")
        if result.overflow:
            self.editWarning.emit("New text is wider than the original.")

    def _begin_paragraph_edit(self, page_index: int, para: Paragraph) -> None:
        if para.spans and any(s.rotation != 0 for s in para.spans):
            # Rotated singletons edit through the span path (engine refuses
            # the horizontal paragraph layout for them).
            self._begin_text_edit(page_index, para.spans[0])
            return
        self._clear_selection()  # one chrome at a time — the editor takes over
        scene_rect = page_coords.page_rect_to_scene(
            para.bbox,
            render_zoom=self._canvas.render_zoom,
            rotation=self._doc.page_rotation(page_index),
            page_size_pts=self._doc.page_size(page_index),
        )
        top_left = self._canvas.mapFromScene(QPointF(scene_rect[0], scene_rect[1]))
        bottom_right = self._canvas.mapFromScene(QPointF(scene_rect[2], scene_rect[3]))
        rect = QRect(top_left, bottom_right).normalized().adjusted(-2, -2, 2, 8)
        self._pending_paragraph = (page_index, para)
        self.styleContextChanged.emit(para)
        self._edit_open_style = self._current_style()
        self._edit_open_zoom = self._canvas.zoom
        # Rich prefill from the paragraph's own spans: existing bold words,
        # colours etc. survive an edit untouched instead of flattening.
        pieces: list = []
        for i, line in enumerate(para.lines):
            if i:
                pieces.append(("\n", QTextCharFormat()))
            for line_span in line:
                pieces.extend(self._pieces_from_span(line_span))
        if not pieces:
            pieces = [(para.text, self._char_format_for(para))]
        font = self._editor_font_for(para)
        # Editor line height = the paragraph's pitch at the font's effective
        # px-per-pt (keeps the max-8px legibility floor consistent): tight-set
        # blocks then look in the editor like they do on the page, instead of
        # at QTextEdit's looser natural spacing.
        line_height = para.pitch * font.pixelSize() / para.size if para.size > 0 else None
        alignment = {
            "right": Qt.AlignmentFlag.AlignRight,
            "center": Qt.AlignmentFlag.AlignHCenter,
        }.get(para.align)
        self._para_editor.open_pieces(
            rect,
            pieces,
            font,
            select_all=False,
            line_height_px=line_height,
            fit_content=True,
            alignment=alignment,
            base_size_pt=para.size if para.size > 0 else None,
        )
        self._edit_open_sig = self._pieces_signature(self._para_editor)
        self.editWarning.emit("Editing paragraph — Ctrl+Enter applies, Esc cancels.")

    # --- armed click actions: insert text/image, highlight (E5/E6/E7) ------
    def begin_insert_text(self) -> None:
        """Arm click-to-place: the next click opens an empty editor there."""
        if not self._edit_mode or not self._canvas.has_page:
            return
        self._click_action = ("text", None)
        self._canvas.arm_insert_point("Click where the new text should start · Esc cancels")

    def begin_insert_image(self) -> None:
        """Pick an image file, then click where it should be placed."""
        if not self._edit_mode or not self._canvas.has_page:
            return
        path = self._prompt_image_path()
        if path is None:
            return
        self._click_action = ("image", path)
        self._canvas.arm_insert_point("Click where the image should go · Esc cancels")

    def begin_insert_comment(self) -> None:
        """Arm click-to-place for a review comment (markup; never prints by
        default). An ANNOTATION — available in Markup mode, not gated on edit."""
        if not self._canvas.has_page:
            return
        self._click_action = ("comment", None)
        self._canvas.arm_insert_point("Click where the comment should go · Esc cancels")

    def begin_insert_callout(self) -> None:
        """Arm a TWO-click callout: first the arrow target, then the box.
        An ANNOTATION — available in Markup mode."""
        if not self._canvas.has_page:
            return
        self._click_action = ("callout_target", None)
        self._canvas.arm_insert_point("Click what the callout should point AT · Esc cancels")

    def begin_retarget_callout(self, n: int, comment) -> None:
        """Arm one click to re-point a callout's arrowhead (context menu).
        An ANNOTATION edit — available in Markup mode."""
        if not self._canvas.has_page:
            return
        self._click_action = ("retarget", (n, comment.xref))
        self._canvas.arm_insert_point("Click what the arrowhead should point AT · Esc cancels")

    def begin_highlight(self) -> None:
        """Arm a window selection: drag across the text to highlight it.
        An ANNOTATION — available in Markup mode."""
        if not self._canvas.has_page:
            return
        self._click_action = None
        self._canvas.arm_region_select(
            "Drag across text — or double-click a word — to highlight · Esc cancels"
        )

    @property
    def armed_action(self) -> str | None:
        """The armed one-shot mode: "text" | "image" | "highlight" | None."""
        if self._canvas.region_armed:
            return "highlight"
        if self._canvas.insert_armed and self._click_action is not None:
            return self._click_action[0]
        return None

    def cancel_armed_mode(self) -> None:
        """Drop any armed one-shot mode (Esc, mode exit, toolbar re-click)."""
        self._click_action = None
        self._canvas.disarm_insert_point()
        self._canvas.disarm_region_select()

    def _on_region_selected(self, sx0: float, sy0: float, sx1: float, sy1: float) -> None:
        """Highlight everything inside the dragged window (one undo step).

        Corners convert through the seam individually (rotation-safe), then
        normalize in page space. A tiny drag (a click, or the first press of a
        double-click) highlights just the WORD under the point — Qt suppresses
        the trailing double-click, so double-clicking a word lands on the word.
        """
        n = self._current_page
        ax, ay = self._scene_point_to_page(sx0, sy0, n)
        bx, by = self._scene_point_to_page(sx1, sy1, n)
        x0, x1 = sorted((ax, bx))
        y0, y1 = sorted((ay, by))
        spans = self._doc.text_spans(n)
        if (x1 - x0) < 2.0 and (y1 - y0) < 2.0:  # a click / double-click: the word
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            word = textselect.word_region_at(self._page_text_lines(), cx, cy)
            if word:  # a native word — highlight exactly it (its bbox IS the word,
                self._highlight_word_rects(n, textselect.region_rects(word))  # no re-clip
                return
            # no native word (outline / scanned text): fall back to the span
            span = page_coords.span_at(spans, cx, cy)
            if span is None:
                self.editWarning.emit("No text there to highlight.")
                return
            self._highlight_rect(n, span.bbox)
            return
        rect = (x0, y0, x1, y1)
        if not any(
            s.bbox[0] < x1 and s.bbox[2] > x0 and s.bbox[1] < y1 and s.bbox[3] > y0 for s in spans
        ):
            self.editWarning.emit("No text in the selection.")
            return
        self._highlight_rect(n, rect)

    def _highlight_word_rects(
        self, page_index: int, rects: list[tuple[float, float, float, float]]
    ) -> None:
        """Highlight the given word rects DIRECTLY (one undo step) — the native
        word bbox is exactly the word, so no character re-clipping (which can
        drop a tight bbox whose char centres fall on the border)."""
        color = self._highlight_color

        def op(doc: PdfDocument) -> None:
            doc.highlight_rects(page_index, rects, color)

        self._push_command("Highlight text", op, ("page", page_index))

    def _highlight_rect(self, page_index: int, rect: tuple[float, float, float, float]) -> None:
        """Highlight all text inside ``rect`` as one undo step (region drag
        and the context menu's span highlight both land here), in the current
        highlighter colour."""
        color = self._highlight_color
        results: list = []

        def op(doc: PdfDocument) -> None:
            results.append(doc.highlight_region(page_index, rect, color))

        if self._push_command("Highlight text", op, ("page", page_index)) and results[0] == 0:
            self.editWarning.emit("No text in the selection.")

    def set_highlight_color(self, rgb: tuple[float, float, float] | None) -> None:
        """Set the highlighter colour for subsequent highlights ((r,g,b) 0-1 or
        None for the engine's default yellow). Driven by the Annotate toolbar."""
        self._highlight_color = rgb

    def has_text_selection(self) -> bool:
        """True when a marquee text selection is active (X4) — the source for a
        'highlight the selection' action."""
        return bool(self._text_selection)

    def highlight_selection(self) -> None:
        """Highlight the current marquee text selection (X4 Region) in the
        current colour, as one undo step, then clear the selection (its
        positions go stale after the page-scoped mutation).

        The Region already names the selected words per line, so its per-line
        union rects map straight onto highlight annotations — no coordinate
        conversion (they are unrotated page space, engine is rotation-blind)
        and no character re-clipping (which could disagree with the selection).
        """
        if not self._text_selection:
            return
        n = self._current_page
        rects = textselect.region_rects(self._text_selection)
        if not rects:
            return
        color = self._highlight_color

        def op(doc: PdfDocument) -> None:
            doc.highlight_rects(n, rects, color)

        if self._push_command("Highlight selection", op, ("page", n)):
            self._clear_text_selection()

    _COMMENT_W = 220.0  # default comment box size (page pts)
    _COMMENT_H = 64.0

    def _comment_rect_at(self, n: int, px: float, py: float) -> tuple[float, float, float, float]:
        """The default comment box anchored at a click, clamped onto the page."""
        page_w, page_h = self._doc.page_size(n)
        if self._doc.page_rotation(n) % 180 == 90:
            page_w, page_h = page_h, page_w  # comments live in unrotated space
        x0 = min(max(px, 0.0), max(0.0, page_w - self._COMMENT_W))
        y0 = min(max(py, 0.0), max(0.0, page_h - self._COMMENT_H))
        return (x0, y0, x0 + self._COMMENT_W, y0 + self._COMMENT_H)

    def _comment_editor_font(self) -> QFont:
        font = QFont("Arial")
        font.setPixelSize(max(8, round(9.0 * self._canvas.zoom)))
        return font

    def _open_comment_editor(
        self, n: int, px: float, py: float, callout_target: tuple[float, float] | None
    ) -> None:
        rect = self._comment_rect_at(n, px, py)
        self._pending_comment = (n, rect, callout_target)
        scene_rect = page_coords.page_rect_to_scene(
            rect,
            render_zoom=self._canvas.render_zoom,
            rotation=self._doc.page_rotation(n),
            page_size_pts=self._doc.page_size(n),
        )
        top_left = self._canvas.mapFromScene(QPointF(scene_rect[0], scene_rect[1]))
        bottom_right = self._canvas.mapFromScene(QPointF(scene_rect[2], scene_rect[3]))
        widget_rect = QRect(top_left, bottom_right).normalized()
        self._para_editor.open_at(widget_rect, "", self._comment_editor_font())
        self._edit_open_zoom = self._canvas.zoom
        self.editWarning.emit("Type the comment — Ctrl+Enter adds it, Esc cancels.")

    def _begin_comment_edit(self, n: int, comment) -> None:
        """Double-click a comment: edit its text in place."""
        self._clear_selection()
        self._pending_comment_edit = (n, comment.xref, comment.text)
        scene_rect = page_coords.page_rect_to_scene(
            comment.rect,
            render_zoom=self._canvas.render_zoom,
            rotation=self._doc.page_rotation(n),
            page_size_pts=self._doc.page_size(n),
        )
        top_left = self._canvas.mapFromScene(QPointF(scene_rect[0], scene_rect[1]))
        bottom_right = self._canvas.mapFromScene(QPointF(scene_rect[2], scene_rect[3]))
        widget_rect = QRect(top_left, bottom_right).normalized()
        self._para_editor.open_at(widget_rect, comment.text, self._comment_editor_font())
        self._edit_open_zoom = self._canvas.zoom
        self.editWarning.emit("Editing comment — Ctrl+Enter applies, Esc cancels.")

    def _prompt_image_path(self) -> Path | None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Choose image", "", "Images (*.png *.jpg *.jpeg *.bmp)"
        )
        return Path(path_str) if path_str else None

    def _on_insert_point(self, sx: float, sy: float) -> None:
        action, payload = self._click_action or ("text", None)
        self._click_action = None
        n = self._current_page
        px, py = self._scene_point_to_page(sx, sy, n)
        if action == "image":
            self._place_image(n, px, py, payload)
            return
        if action == "callout_target":
            # First click of the two-click callout: remember the target and
            # re-arm for the box position.
            self._click_action = ("callout_box", (px, py))
            self._canvas.arm_insert_point(
                "Now click where the comment box should sit · Esc cancels"
            )
            return
        if action == "retarget":
            page_index, xref = payload
            if n != page_index:  # the leader is same-page geometry
                self.editWarning.emit("Click a point on the callout's own page.")
                return

            def retarget_op(doc: PdfDocument) -> None:
                doc.move_comment_target(page_index, xref, (px, py))

            self._push_command("Move arrowhead", retarget_op, ("page", page_index))
            return
        if action in ("comment", "callout_box"):
            self._open_comment_editor(n, px, py, payload)
            return
        self._pending_insert = (n, (px, py))
        if self.style_provider is not None:
            style, font = self.style_provider()
            size_pt = style.size
        else:
            size_pt = _INSERT_TEXT_SIZE
            font = QFont("Arial")
        pixel_size = max(8, round(size_pt * self._canvas.zoom))
        font.setPixelSize(pixel_size)
        anchor = self._canvas.mapFromScene(QPointF(sx, sy))
        width = min(320, max(120, self._canvas.viewport().width() - anchor.x() - 4))
        rect = QRect(anchor.x(), anchor.y() - pixel_size, width, 3 * pixel_size + 10)
        self._para_editor.open_at(rect, "", font)
        # Seed the typing format with the TRUE point size (and record the zoom
        # the pixel size was built with): without PT_PROPERTY the commit
        # derived pt from pixel ÷ _edit_open_zoom, and that zoom was STALE on
        # the insert path — a 9pt insert at zoom 2 committed as 18pt.
        self._edit_open_zoom = self._canvas.zoom
        fmt = QTextCharFormat()
        fmt.setFont(font)
        fmt.setProperty(PT_PROPERTY, float(size_pt))
        color = style.color if self.style_provider is not None else 0
        fmt.setForeground(QColor((color >> 16) & 255, (color >> 8) & 255, color & 255))
        self._para_editor.merge_selection_format(fmt)
        self.editWarning.emit("Type the new text — Ctrl+Enter inserts, Esc cancels.")

    def _on_paragraph_committed(self, text: str) -> None:
        if self._pending_comment is not None:
            page_index, rect, callout_target = self._pending_comment
            self._pending_comment = None
            if not text.strip():
                return
            import getpass

            author = getpass.getuser()

            def comment_op(doc: PdfDocument) -> None:
                doc.add_comment(
                    page_index, rect, text, author=author, callout_target=callout_target
                )

            self._push_command("Add comment", comment_op, ("page", page_index))
            return
        if self._pending_comment_edit is not None:
            page_index, xref, original = self._pending_comment_edit
            self._pending_comment_edit = None
            if text == original:
                return  # unchanged — don't recreate the comment or dirty the doc

            def comment_edit_op(doc: PdfDocument) -> None:
                if text.strip():
                    doc.update_comment_text(page_index, xref, text)
                else:  # emptied: a comment with no text is just clutter
                    doc.delete_comment(page_index, xref)

            self._push_command("Edit comment", comment_edit_op, ("page", page_index))
            return
        if self._pending_insert is not None:
            page_index, point = self._pending_insert
            self._pending_insert = None
            if not text.strip():
                return
            pieces = self._para_editor.committed_pieces_for(text)
            if pieces is not None:  # rich path: styles typed/applied in the editor
                runs, _resolved = self._runs_from_pieces(pieces)

                def do_insert(doc: PdfDocument) -> None:
                    doc.insert_runs(page_index, point, runs)
            else:  # direct-call fallback: uniform toolbar style
                style = self._current_style()

                def do_insert(doc: PdfDocument) -> None:
                    doc.insert_text(page_index, point, text, style=style)

            def insert_op(doc: PdfDocument) -> None:
                # Register the new box INSIDE the op: the command's snapshot
                # then carries content + registry together, so undo/redo can
                # never split them (E10).
                before = {(s.text, s.bbox) for s in doc.text_spans(page_index)}
                do_insert(doc)
                new = [s for s in doc.text_spans(page_index) if (s.text, s.bbox) not in before]
                if new:
                    doc.add_box(
                        page_index,
                        (
                            min(s.bbox[0] for s in new),
                            min(s.bbox[1] for s in new),
                            max(s.bbox[2] for s in new),
                            max(s.bbox[3] for s in new),
                        ),
                    )

            self._push_command("Insert text", insert_op, ("page", page_index))
            return
        if self._pending_paragraph is None:
            return
        page_index, para = self._pending_paragraph
        self._pending_paragraph = None
        width_pts = None
        if self._para_editor.user_sized_width is not None:
            # Editor px -> page pts (viewport px per pt == logical zoom).
            width_pts = max(30.0, (self._para_editor.user_sized_width - 8) / self._canvas.zoom)

        pieces = self._para_editor.committed_pieces_for(text)
        results: list = []
        resolved = True
        if pieces is not None:  # rich path — per-word styles preserved/applied
            runs, resolved = self._runs_from_pieces(pieces)
            signature = tuple((run.text, run.style) for run in runs)
            if text == para.text and width_pts is None and signature == self._edit_open_sig:
                return  # nothing changed — text, width and styling all identical

            def do_edit(doc: PdfDocument):
                return doc.replace_paragraph_runs(page_index, para, runs, width=width_pts)
        else:  # direct-call fallback: one uniform style for the whole block
            style = self._current_style()
            if text == para.text and width_pts is None and style == self._edit_open_style:
                return

            def do_edit(doc: PdfDocument):
                return doc.replace_paragraph(page_index, para, text, style=style, width=width_pts)

        def op(doc: PdfDocument) -> None:
            box = self._box_for(doc, page_index, para.bbox)
            result = do_edit(doc)
            results.append(result)
            if box is not None:  # keep the registry rect in step with the edit
                if result.new_bbox is not None:
                    doc.update_box_rect(box.id, result.new_bbox)
                else:  # emptied — the box's text is gone, drop its identity
                    doc.remove_box(box.id)

        if not self._push_command("Edit paragraph", op, ("page", page_index)):
            return
        result = results[0]
        if not result.exact_font or not resolved:
            self.editWarning.emit("Font can't be matched exactly — closest standard font used.")
        if result.resized:
            self.editWarning.emit("The text box grew to fit the new text.")

    def _place_image(self, page_index: int, px: float, py: float, path: Path) -> None:
        image = QImage(str(path))
        if image.isNull() or image.width() < 1:
            self.editWarning.emit(f"Could not read image: {path.name}")
            return
        page_w, page_h = self._doc.page_size(page_index)
        if self._doc.page_rotation(page_index) % 180 == 90:
            page_w, page_h = page_h, page_w  # engine rect is unrotated space
        width = min(200.0, max(12.0, page_w - px - 2))
        height = width * image.height() / image.width()
        if py + height > page_h - 1:
            height = max(12.0, page_h - py - 1)
            width = height * image.width() / image.height()
        rect = (px, py, px + width, py + height)

        def op(doc: PdfDocument) -> None:
            doc.insert_image(page_index, rect, path)

        self._push_command("Insert image", op, ("page", page_index))

    def _replace_image_at(self, page_index: int, info) -> None:
        path = self._prompt_image_path()
        if path is None:
            return

        def op(doc: PdfDocument) -> None:
            doc.replace_image(page_index, info, path)

        self._push_command("Replace image", op, ("page", page_index))

    # --- move paragraph (E5.1): Ctrl+drag ---------------------------------
    def _scene_point_to_page(self, sx: float, sy: float, n: int) -> tuple[float, float]:
        return page_coords.scene_to_page(
            sx,
            sy,
            render_zoom=self._canvas.render_zoom,
            rotation=self._doc.page_rotation(n),
            page_size_pts=self._doc.page_size(n),
        )

    def _on_move_drag_started(self, sx: float, sy: float) -> None:
        """Ctrl+drag: move/resize directly, no selection needed (fast path)."""
        if not self._edit_mode:
            return  # read-only: never accept the drag (canvas ignores the press)
        n = self._current_page
        px, py = self._scene_point_to_page(sx, sy, n)
        target = hover_target(self.page_geometry(n), px, py)
        if target is None:
            return  # nothing under the cursor — the canvas ignores the press
        self._accept_target_drag(n, px, py, target)

    def _accept_target_drag(self, n: int, px: float, py: float, target) -> None:
        """Accept a pending canvas drag for ``target``.

        Text paragraphs take priority over images (hover_target's rule, the
        same one the hover affordance shows); an image press near a corner
        RESIZES from the opposite corner, elsewhere it moves.
        """
        zoom = self._canvas.render_zoom
        rot = self._doc.page_rotation(n)
        size = self._doc.page_size(n)
        if target.kind == "comment":
            self._move_comment = (n, target.payload)
            self._move_paragraph = None
            self._move_image_target = None
            self._resize_image = None
            scene_rect = page_coords.page_rect_to_scene(
                target.bbox, render_zoom=zoom, rotation=rot, page_size_pts=size
            )
            self._canvas.begin_move_feedback(scene_rect)
            return
        if target.kind == "text":
            if any(s.rotation != 0 for s in target.payload.spans):
                return  # rotated text can't be drag-moved (engine refuses)
            self._move_paragraph = (n, target.payload)
            self._move_image_target = None
            self._resize_image = None
            feedback = target.bbox
            members = [pp for pn, pp in self._multi_paragraphs if pn == n]
            if len(members) >= 2 and any(
                pp.bbox == target.payload.bbox and pp.text == target.payload.text for pp in members
            ):
                # Dragging a member of the multi-selection moves the GROUP —
                # relative positions preserved (E10.7).
                self._move_group = (n, members)
                feedback = (
                    min(pp.bbox[0] for pp in members),
                    min(pp.bbox[1] for pp in members),
                    max(pp.bbox[2] for pp in members),
                    max(pp.bbox[3] for pp in members),
                )
            scene_rect = page_coords.page_rect_to_scene(
                feedback, render_zoom=zoom, rotation=rot, page_size_pts=size
            )
            self._canvas.begin_move_feedback(scene_rect)
            return
        anchor = self._corner_anchor(target.bbox, px, py)
        if target.kind == "image_corner" and anchor is not None:
            self._resize_image = (n, target.payload, anchor)
            self._move_image_target = None
            self._move_paragraph = None
            ascene = page_coords.page_to_scene(
                anchor[0], anchor[1], render_zoom=zoom, rotation=rot, page_size_pts=size
            )
            self._canvas.begin_resize_feedback(ascene)
        else:  # body -> move
            self._move_image_target = (n, target.payload)
            self._move_paragraph = None
            self._resize_image = None
            scene_rect = page_coords.page_rect_to_scene(
                target.bbox, render_zoom=zoom, rotation=rot, page_size_pts=size
            )
            self._canvas.begin_move_feedback(scene_rect)

    # --- click-to-select (U6) ----------------------------------------------
    def _on_select_drag_started(self, sx: float, sy: float) -> None:
        """Plain press: select what's under it. A press on the ALREADY
        selected element accepts a move/resize drag instead — click first,
        then drag (deliberate: a stray drag must never move text)."""
        n = self._current_page
        px, py = self._scene_point_to_page(sx, sy, n)
        target = hover_target(self.page_geometry(n), px, py)
        # A comment is markup — selectable + draggable in EITHER mode (comments
        # float on top), so this precedes both the Markup marquee and content
        # selection.
        if target is not None and target.kind == "comment":
            self._select_and_drag_comment(n, px, py, target)
            return
        if not self._edit_mode:
            # A press off any selected comment deselects it (parity with the
            # edit-mode click-away below) before starting a marquee — otherwise
            # the comment stays selected and a later Delete would remove it.
            self._clear_selection()
            self._begin_text_selection(sx, sy)  # Markup: marquee text selection (X4)
            return
        if target is None:
            self._clear_selection()
            return
        modifiers = QApplication.keyboardModifiers()  # read LIVE (U-series rule)
        if (
            modifiers & Qt.KeyboardModifier.ShiftModifier
            and target.kind == "text"
            and not any(s.rotation != 0 for s in target.payload.spans)
        ):
            self.toggle_multi_select(n, target.payload)  # Shift+click adds (E10.7)
            return
        kind = "image" if target.kind.startswith("image") else "text"
        if self._selection == (kind, n, target.payload):
            self._accept_target_drag(n, px, py, target)
            return
        self._selection = (kind, n, target.payload)
        self._push_selection_chrome()

    def _select_and_drag_comment(self, n: int, px: float, py: float, target) -> None:
        """Select a comment and accept its move drag on the FIRST press — works
        in either mode. Moving markup is a pure re-anchor and instantly
        undoable, so it is exempt from the U6 click-first rule (which protects
        CONTENT from stray drags); a press without a real drag still only
        selects (sub-1pt offsets are ignored on release)."""
        self._clear_text_selection()  # a comment selection replaces any marquee (X4)
        if self._selection != ("comment", n, target.payload):
            self._selection = ("comment", n, target.payload)
            self._push_selection_chrome()
        self._accept_target_drag(n, px, py, target)

    def _push_selection_chrome(self) -> None:
        """(Re-)display the selection on the canvas — also called after
        re-renders, where the scene scale changed under the stored rect."""
        members = [(pn, pp) for pn, pp in self._multi_paragraphs if pn == self._current_page]
        if members:  # multi-selection replaces the single chrome (E10.7)
            zoom = self._canvas.render_zoom
            rot = self._doc.page_rotation(self._current_page)
            size = self._doc.page_size(self._current_page)
            self._canvas.clear_selection()
            self._canvas.set_multi_selection(
                [
                    page_coords.page_rect_to_scene(
                        pp.bbox, render_zoom=zoom, rotation=rot, page_size_pts=size
                    )
                    for _pn, pp in members
                ]
            )
            return
        self._canvas.set_multi_selection([])
        if self._selection is None:
            self._canvas.clear_selection()
            return
        kind, n, payload = self._selection
        if n != self._current_page:
            self._canvas.clear_selection()
            return
        scene_rect = page_coords.page_rect_to_scene(
            payload.rect if kind == "comment" else payload.bbox,
            render_zoom=self._canvas.render_zoom,
            rotation=self._doc.page_rotation(n),
            page_size_pts=self._doc.page_size(n),
        )
        self._canvas.set_selection(kind, scene_rect)

    def toggle_multi_select(self, n: int, para: Paragraph) -> None:
        """Ctrl/Shift+click a text box: add it to (or remove it from) the
        multi-selection (E10.7 — grouped moves and merge)."""
        if not self._edit_mode:
            return
        for i, (page, member) in enumerate(self._multi_paragraphs):
            if page == n and member.bbox == para.bbox and member.text == para.text:
                self._multi_paragraphs.pop(i)
                break
        else:
            self._multi_paragraphs.append((n, para))
        self._selection = None  # multi replaces the single selection
        self._push_selection_chrome()

    def _clear_selection(self) -> None:
        if self._selection is None and not self._multi_paragraphs:
            return
        self._selection = None
        self._multi_paragraphs = []
        self._canvas.clear_selection()

    # --- read-only flow text selection + copy (X4) ------------------------
    def _page_text_lines(self) -> list:
        """The current page's reading-order lines for text selection (X4).

        Cached per page and self-invalidated on a page change; mutations clear
        it via after_command. Read-only never mutates, so the cache stays valid
        for the whole time a selection can live.
        """
        if self._text_lines is None or self._text_lines_page != self._current_page:
            self._text_lines = self._doc.text_lines(self._current_page)
            self._text_lines_page = self._current_page
        return self._text_lines

    def _begin_text_selection(self, sx: float, sy: float) -> None:
        """A plain read-only press starts a window/marquee selection: the drag
        draws a rectangle and everything inside it is selected. Accepts the
        drag only when the page has selectable native words — scanned/outline
        pages have nothing to select, so the drag stays inert there."""
        self._clear_text_selection()  # a new drag replaces any prior selection
        if not self._page_text_lines():
            return
        n = self._current_page
        px, py = self._scene_point_to_page(sx, sy, n)
        self._text_sel_anchor_pt = (px, py)
        self._canvas.begin_text_selection(sx, sy)

    def _on_text_select_moved(self, sx: float, sy: float) -> None:
        """Live-update the window selection as the rectangle is dragged."""
        if self._text_sel_anchor_pt is not None:
            ax, ay = self._text_sel_anchor_pt
            self._set_region(ax, ay, sx, sy)

    def _on_text_select_finished(self, sx: float, sy: float) -> None:
        """Finalize the window selection. A press with no real drag is a click,
        which deselects (matches Acrobat: click to place, drag to select)."""
        anchor_pt = self._text_sel_anchor_pt
        self._text_sel_anchor_pt = None
        if anchor_pt is None:
            return
        n = self._current_page
        px, py = self._scene_point_to_page(sx, sy, n)
        if abs(px - anchor_pt[0]) < 2.0 and abs(py - anchor_pt[1]) < 2.0:
            self._clear_text_selection()  # a click, not a drag — deselect
            return
        self._set_region(anchor_pt[0], anchor_pt[1], sx, sy)

    def _set_region(self, ax: float, ay: float, sx: float, sy: float) -> None:
        """Select every word whose centre is inside the rectangle from the
        drag's anchor (page space ``ax, ay``) to the current point (scene px)."""
        n = self._current_page
        px, py = self._scene_point_to_page(sx, sy, n)
        region = textselect.words_in_rect(self._page_text_lines(), (ax, ay, px, py))
        self._text_selection = region or None
        self._push_text_selection_chrome()

    def _select_word_at(self, sx: float, sy: float) -> None:
        """Read-only double-click: select the whole word under the cursor (X4)."""
        n = self._current_page
        px, py = self._scene_point_to_page(sx, sy, n)
        region = textselect.word_region_at(self._page_text_lines(), px, py)
        if not region:
            self._clear_text_selection()
            return
        self._text_selection = region
        self._push_text_selection_chrome()

    def copy_selection(self) -> None:
        """Copy the read-only text selection to the clipboard (X4: Ctrl+C or
        the read-only context menu). No-op in edit mode or with no selection;
        a pure read — the undo stack is never touched."""
        if self._edit_mode or not self._text_selection:
            return
        text = textselect.region_text(self._text_selection)
        if text:
            QApplication.clipboard().setText(text)

    def _push_text_selection_chrome(self) -> None:
        """(Re-)display the selection rects (scene px) — re-pushed after a page
        show and every re-render, exactly like the search chrome."""
        if not self._text_selection:
            self._canvas.clear_text_selection()
            return
        n = self._current_page
        rects = textselect.region_rects(self._text_selection)
        zoom = self._canvas.render_zoom
        rot = self._doc.page_rotation(n)
        size = self._doc.page_size(n)
        scene = [
            page_coords.page_rect_to_scene(r, render_zoom=zoom, rotation=rot, page_size_pts=size)
            for r in rects
        ]
        self._canvas.set_text_selection_rects(scene)

    def _clear_text_selection(self) -> None:
        self._text_sel_anchor_pt = None
        if self._text_selection is None:
            return
        self._text_selection = None
        self._canvas.clear_text_selection()

    def _annotate_context_menu(self, sx: float, sy: float) -> None:
        """Markup-mode right-click: annotation actions only — edit/delete a
        comment, retarget a callout, highlight the text under the cursor, copy a
        text selection, or add a comment here. Content-edit items stay in the
        edit-mode menu.

        Only built when on screen — offscreen tests call the dispatch methods
        (copy_selection, _highlight_rect, _open_comment_editor, …) directly.
        """
        if not self.isVisible():
            return
        n = self._current_page
        px, py = self._scene_point_to_page(sx, sy, n)
        geometry = self.page_geometry(n)
        span = page_coords.span_at(geometry.spans, px, py)
        comment = self._doc.comment_at(n, px, py)
        from PySide6.QtGui import QCursor
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        actions: dict[str, object] = {}
        if comment is not None:  # markup floats on top: its menu comes first
            actions["edit_comment"] = menu.addAction(
                icons.icon("insert_comment"), "Edit comment\tDouble-click"
            )
            if comment.kind == "callout":
                actions["retarget"] = menu.addAction(
                    icons.icon("insert_callout"), "Move arrowhead…"
                )
            actions["delete_comment"] = menu.addAction(
                icons.icon("delete_image"), "Delete comment\tDel"
            )
            menu.addSeparator()
        if self._text_selection is not None:
            actions["copy"] = menu.addAction(icons.icon("copy"), "Copy")
            actions["highlight_selection"] = menu.addAction(
                icons.icon("highlight"), "Highlight selection"
            )
        elif span is not None:
            actions["highlight"] = menu.addAction(icons.icon("highlight"), "Highlight this text")
        if comment is None:  # adding a comment ON a comment would stack them
            if not menu.isEmpty():
                menu.addSeparator()
            actions["add_comment"] = menu.addAction(
                icons.icon("insert_comment"), "Add comment here"
            )
        if menu.isEmpty():
            return
        chosen = menu.exec(QCursor.pos())
        if chosen is None:
            return
        if chosen is actions.get("edit_comment"):
            self._begin_comment_edit(n, comment)
        elif chosen is actions.get("retarget"):
            self.begin_retarget_callout(n, comment)
        elif chosen is actions.get("delete_comment"):
            self._delete_comment_at(n, comment.xref)
        elif chosen is actions.get("copy"):
            self.copy_selection()
        elif chosen is actions.get("highlight_selection"):
            self.highlight_selection()
        elif chosen is actions.get("highlight"):
            self._highlight_rect(n, span.bbox)
        elif chosen is actions.get("add_comment"):
            self._open_comment_editor(n, px, py, None)

    def _merge_selected_paragraphs(self) -> None:
        """Merge the multi-selected text boxes into ONE paragraph (E10.7).

        Physically rebuilds them: the union paragraph is re-committed in
        place, so its lines become one contiguous run — extraction (and
        every later edit) sees a single text box again. Undoable as one step.
        """
        members = [(pn, pp) for pn, pp in self._multi_paragraphs if pn == self._current_page]
        if len(members) < 2:
            self.editWarning.emit("Select two or more text boxes to merge.")
            return
        n = members[0][0]
        paras = [pp for _pn, pp in members]
        try:
            union = merge_paragraphs(paras)
        except ValueError as exc:
            self.editWarning.emit(str(exc))
            return
        runs = self._runs_from_paragraph(union)

        def op(doc: PdfDocument) -> None:
            boxes = [
                box for box in (self._box_for(doc, n, pp.bbox) for pp in paras) if box is not None
            ]
            result = doc.replace_paragraph_runs(n, union, runs)
            seen: set[str] = set()
            for box in boxes:
                if box.id not in seen:
                    seen.add(box.id)
                    doc.remove_box(box.id)
            if boxes and len(seen) == len(paras) and result.new_bbox is not None:
                # EVERY member was an inserted box: the union stays one.
                doc.add_box(n, result.new_bbox)

        self._push_command("Merge text boxes", op, ("page", n))
        # after_command cleared the (now stale) selection; nothing else to do.

    def _on_escape(self) -> None:
        if self._canvas.is_armed:  # armed mode first, selection second
            self.cancel_armed_mode()
            return
        if self._selection is not None or self._multi_paragraphs:
            self._clear_selection()  # clears a single OR a pure multi-selection
            return
        if self._text_selection is not None:  # read-only text selection (X4)
            self._clear_text_selection()
            return
        if not self._search_bar.isHidden():  # search close is LAST (SR2)
            self.close_search()

    def _on_delete_selection(self) -> None:
        """Delete/Backspace removes a selected COMMENT (either mode — markup) or
        IMAGE (edit mode only — content). Paragraph text is deleted through its
        editor instead. In Markup mode a selection can only ever be a comment."""
        if self._selection is None:
            return
        kind, n, payload = self._selection
        if n != self._current_page:
            return
        if kind == "comment":
            self._delete_comment_at(n, payload.xref)
        elif kind == "image" and self._edit_mode:
            self._delete_image_at(n, payload)

    def _delete_comment_at(self, page_index: int, xref: int) -> None:
        def op(doc: PdfDocument) -> None:
            doc.delete_comment(page_index, xref)

        self._push_command("Delete comment", op, ("page", page_index))

    def _corner_anchor(self, bbox, px, py) -> tuple[float, float] | None:
        """If (px, py) is within the corner grab zone of ``bbox``, return the
        OPPOSITE (fixed) corner in page points; else None. The zone rule is
        shared with the hover affordances (page_geometry.corner_hit)."""
        hit = corner_hit(bbox, px, py)
        return hit[1] if hit is not None else None

    # --- hover affordances (U2a) ------------------------------------------
    def _on_hover_moved(self, sx: float, sy: float) -> None:
        """Synchronous hover hit-test against the cached page geometry."""
        n = self._current_page
        px, py = self._scene_point_to_page(sx, sy, n)
        target = hover_target(self.page_geometry(n), px, py)
        over_comment = target is not None and target.kind == "comment"
        # Element hover outline: comments in EITHER mode (markup floats on top),
        # every element in edit mode.
        if self._edit_mode or over_comment:
            self._canvas.set_text_hover(False)  # not an I-beam target
            if target is None:
                self._canvas.clear_hover()
            else:
                self._show_hover_target(n, target)
            return
        # Markup mode, not over a comment: an I-beam over selectable words (X4),
        # else clean. Clear a lingering comment outline first.
        if self._canvas.hover_kind is not None:
            self._canvas.clear_hover()
        self._canvas.set_text_hover(textselect.word_at(self._page_text_lines(), px, py) is not None)

    def _show_hover_target(self, n: int, target) -> None:
        """Draw the hover outline (+ corner ticks) for ``target``."""
        zoom = self._canvas.render_zoom
        rot = self._doc.page_rotation(n)
        size = self._doc.page_size(n)
        scene_rect = page_coords.page_rect_to_scene(
            target.bbox, render_zoom=zoom, rotation=rot, page_size_pts=size
        )
        corner = None
        if target.corner is not None:
            corner = page_coords.page_to_scene(
                target.corner[0],
                target.corner[1],
                render_zoom=zoom,
                rotation=rot,
                page_size_pts=size,
            )
        self._canvas.set_hover(target.kind, scene_rect, corner, target.corner_zone * zoom)

    def _drag_offset(self, sx0, sy0, sx1, sy1, page_index) -> tuple[float, float] | None:
        # Deltas transform like points: convert both ends, then subtract
        # (rotation-safe — a scene-space drag maps to a page-space offset).
        x0, y0 = self._scene_point_to_page(sx0, sy0, page_index)
        x1, y1 = self._scene_point_to_page(sx1, sy1, page_index)
        dx, dy = x1 - x0, y1 - y0
        if abs(dx) < 1.0 and abs(dy) < 1.0:
            return None  # a click, not a move (also lets Ctrl+double-click through)
        return dx, dy

    def _on_move_drag_finished(self, sx0: float, sy0: float, sx1: float, sy1: float) -> None:
        if self._resize_image is not None:
            page_index, image, anchor = self._resize_image
            self._resize_image = None
            self._finish_image_resize(page_index, image, anchor, sx1, sy1)
            return
        if self._move_comment is not None:
            page_index, comment = self._move_comment
            self._move_comment = None
            offset = self._drag_offset(sx0, sy0, sx1, sy1, page_index)
            if offset is None:
                return
            x0, y0, x1, y1 = comment.rect
            new_rect = (x0 + offset[0], y0 + offset[1], x1 + offset[0], y1 + offset[1])

            def comment_move_op(doc: PdfDocument) -> None:
                doc.move_comment(page_index, comment.xref, new_rect)

            self._push_command("Move comment", comment_move_op, ("page", page_index))
            return
        if self._move_image_target is not None:
            page_index, image = self._move_image_target
            self._move_image_target = None
            offset = self._drag_offset(sx0, sy0, sx1, sy1, page_index)
            if offset is None:
                return

            def op(doc: PdfDocument) -> None:
                doc.move_image(page_index, image, offset)

            self._push_command("Move image", op, ("page", page_index))
            return
        if self._move_paragraph is None:
            return
        page_index, para = self._move_paragraph
        self._move_paragraph = None
        group = self._move_group
        self._move_group = None
        offset = self._drag_offset(sx0, sy0, sx1, sy1, page_index)
        if offset is None:
            # A Ctrl+CLICK (no drag): toggle the box in the multi-selection
            # (E10.7). Ctrl+double-click toggles twice — membership net
            # unchanged — then opens the paragraph editor as before.
            self.toggle_multi_select(page_index, para)
            return

        if group is not None:
            group_page, members = group
            moves = [(member, self._runs_from_paragraph(member)) for member in members]

            def group_op(doc: PdfDocument) -> None:
                for member, member_runs in moves:
                    box = self._box_for(doc, group_page, member.bbox)
                    result = doc.replace_paragraph_runs(
                        group_page, member, member_runs, offset=offset
                    )
                    if box is not None and result.new_bbox is not None:
                        doc.update_box_rect(box.id, result.new_bbox)

            self._push_command("Move text boxes", group_op, ("page", group_page))
            return  # selection cleared by after_command (stale payloads)

        # Rebuild the paragraph as rich runs from its own spans — a move now
        # PRESERVES mixed styles instead of flattening to the dominant one.
        runs = self._runs_from_paragraph(para)
        results: list = []

        def op(doc: PdfDocument) -> None:
            box = self._box_for(doc, page_index, para.bbox)  # match BEFORE the move
            result = doc.replace_paragraph_runs(page_index, para, runs, offset=offset)
            results.append(result)
            if box is not None and result.new_bbox is not None:
                doc.update_box_rect(box.id, result.new_bbox)  # registry follows

        if not self._push_command("Move text", op, ("page", page_index)):
            return
        if results[0].resized:
            self.editWarning.emit("The text box grew to fit at the new position.")

    def _finish_image_resize(self, page_index, image, anchor, sx1, sy1) -> None:
        """Resize ``image`` so the dragged corner reaches the release point,
        anchored at the opposite corner, aspect preserved (contain)."""
        rx, ry = self._scene_point_to_page(sx1, sy1, page_index)
        ax, ay = anchor
        x0, y0, x1, y1 = image.bbox
        w, h = x1 - x0, y1 - y0
        box_w, box_h = abs(rx - ax), abs(ry - ay)
        if box_w < 3.0 and box_h < 3.0:
            return  # negligible drag — leave the image as it was
        scale = min(box_w / w, box_h / h) if w > 0 and h > 0 else 1.0
        if scale <= 0:
            return
        new_w, new_h = w * scale, h * scale
        dir_x = 1.0 if rx >= ax else -1.0
        dir_y = 1.0 if ry >= ay else -1.0
        new_rect = (ax, ay, ax + dir_x * new_w, ay + dir_y * new_h)

        def op(doc: PdfDocument) -> None:
            doc.resize_image(page_index, image, new_rect)

        if not self._push_command("Resize image", op, ("page", page_index)):
            return

    # --- context menu: the visible twin of every gesture (U3) --------------
    def _on_context_menu(self, sx: float, sy: float) -> None:
        """Right-click menu for text, images and the page background.

        Menu item text doubles as gesture documentation (the "\\t" part
        renders right-aligned like a shortcut). Background items act at the
        CLICK POINT directly — no arm-then-click detour.
        """
        if not self._edit_mode:
            self._annotate_context_menu(sx, sy)  # Markup: comment / highlight / copy
            return
        n = self._current_page
        px, py = self._scene_point_to_page(sx, sy, n)
        geometry = self.page_geometry(n)
        target = hover_target(geometry, px, py)
        para = target.payload if target is not None and target.kind == "text" else None
        span = page_coords.span_at(geometry.spans, px, py)
        image = self._doc.image_at(n, px, py)
        if not self.isVisible():
            return  # offscreen tests call the dispatch methods directly
        from PySide6.QtGui import QCursor
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        actions: dict[str, object] = {}
        comment = self._doc.comment_at(n, px, py)
        if comment is not None:  # markup floats on top: its menu comes first
            actions["edit_comment"] = menu.addAction(
                icons.icon("insert_comment"), "Edit comment\tDouble-click"
            )
            if comment.kind == "callout":
                actions["retarget"] = menu.addAction(
                    icons.icon("insert_callout"), "Move arrowhead…"
                )
            actions["delete_comment"] = menu.addAction(
                icons.icon("delete_image"), "Delete comment\tDel"
            )
            menu.addSeparator()
        multi_count = sum(1 for pn, _pp in self._multi_paragraphs if pn == n)
        if multi_count >= 2:
            actions["merge"] = menu.addAction(
                icons.icon("edit_paragraph"), f"Merge {multi_count} text boxes into one"
            )
            menu.addSeparator()
        if para is not None or span is not None:
            actions["edit_text"] = menu.addAction(
                icons.icon("edit_text"), "Edit text\tDouble-click"
            )
            actions["edit_text"].setEnabled(span is not None)
            actions["edit_para"] = menu.addAction(
                icons.icon("edit_paragraph"), "Edit paragraph\tCtrl+Double-click"
            )
            actions["edit_para"].setEnabled(para is not None)
            actions["highlight"] = menu.addAction(icons.icon("highlight"), "Highlight this text")
            actions["highlight"].setEnabled(span is not None)
            actions["delete_text_box"] = menu.addAction(
                icons.icon("delete_image"), "Delete text box"
            )
            actions["delete_text_box"].setEnabled(para is not None)
        if image is not None:
            if not menu.isEmpty():
                menu.addSeparator()
            actions["replace"] = menu.addAction(
                icons.icon("replace_image"), "Replace image…\tDouble-click"
            )
            actions["rotate_cw"] = menu.addAction(
                icons.icon("rotate_image_cw"), "Rotate 90° clockwise"
            )
            actions["rotate_ccw"] = menu.addAction(
                icons.icon("rotate_image_ccw"), "Rotate 90° counter-clockwise"
            )
            actions["delete"] = menu.addAction(icons.icon("delete_image"), "Delete image\tDel")
        if menu.isEmpty():  # page background
            actions["insert_text"] = menu.addAction(icons.icon("insert_text"), "Insert text here")
            actions["insert_image"] = menu.addAction(
                icons.icon("insert_image"), "Insert image here…"
            )
        if comment is None:  # adding a comment ON a comment would stack them
            actions["add_comment"] = menu.addAction(
                icons.icon("insert_comment"), "Add comment here"
            )
        chosen = menu.exec(QCursor.pos())
        if chosen is None:
            return
        if chosen is actions.get("edit_comment"):
            self._begin_comment_edit(n, comment)
        elif chosen is actions.get("retarget"):
            self.begin_retarget_callout(n, comment)
        elif chosen is actions.get("delete_comment"):
            self._delete_comment_at(n, comment.xref)
        elif chosen is actions.get("add_comment"):
            self._open_comment_editor(n, px, py, None)
        elif chosen is actions.get("merge"):
            self._merge_selected_paragraphs()
        elif chosen is actions.get("edit_text"):
            self._begin_text_edit(n, span)
        elif chosen is actions.get("edit_para"):
            self._begin_paragraph_edit(n, para)
        elif chosen is actions.get("highlight"):
            self._highlight_rect(n, span.bbox)
        elif chosen is actions.get("delete_text_box"):
            self._delete_paragraph_at(n, para)
        elif chosen is actions.get("replace"):
            self._replace_image_at(n, image)
        elif chosen is actions.get("rotate_cw"):
            self._rotate_image_at(n, image, -90)  # engine's +90 is CCW on screen
        elif chosen is actions.get("rotate_ccw"):
            self._rotate_image_at(n, image, 90)
        elif chosen is actions.get("delete"):
            self._delete_image_at(n, image)
        elif chosen is actions.get("insert_text"):
            self._insert_text_at_point(sx, sy)
        elif chosen is actions.get("insert_image"):
            self._insert_image_at_point(n, px, py)

    def _insert_text_at_point(self, sx: float, sy: float) -> None:
        """Context menu: open the new-text editor AT the clicked point."""
        if not self._edit_mode:
            return
        self._click_action = ("text", None)
        self._on_insert_point(sx, sy)

    def _insert_image_at_point(self, page_index: int, px: float, py: float) -> None:
        """Context menu: pick an image file, place it AT the clicked point."""
        if not self._edit_mode:
            return
        path = self._prompt_image_path()
        if path is not None:
            self._place_image(page_index, px, py, path)

    def _delete_paragraph_at(self, page_index: int, para) -> None:
        """Context menu: delete a whole text block in one step — the same
        engine op as committing an emptied editor (redact every member,
        reinsert nothing; a registry box dissolves with its content)."""
        if not self._edit_mode:
            return
        self._clear_selection()

        def op(doc: PdfDocument) -> None:
            box = self._box_for(doc, page_index, para.bbox)
            doc.replace_paragraph(page_index, para, "")
            if box is not None:
                doc.remove_box(box.id)

        self._push_command("Delete text box", op, ("page", page_index))

    def _delete_image_at(self, page_index: int, image) -> None:
        def op(doc: PdfDocument) -> None:
            doc.delete_image(page_index, image)

        self._push_command("Delete image", op, ("page", page_index))

    def _rotate_image_at(self, page_index: int, image, deg: int) -> None:
        """Context menu: rotate an image ±90° about its centre (undoable)."""

        def op(doc: PdfDocument) -> None:
            doc.rotate_image(page_index, image, deg)

        self._push_command("Rotate image", op, ("page", page_index))

    def _runs_from_paragraph(self, para: Paragraph) -> list[StyledRun]:
        """The paragraph's own content as engine runs (per-span styles AND
        per-run underline/strike kept). Splitting each span by its
        ``rule_segments`` is what lets a MOVE, group-move or merge preserve
        the rules — without it a move re-laid the text with no rules (user
        report: moving a text box cleared its underline/strikethrough)."""
        runs: list[StyledRun] = []
        for i, line in enumerate(para.lines):
            if i:
                runs.append(StyledRun("\n", runs[-1].style if runs else TextStyle()))
            for line_span in line:
                segments = getattr(line_span, "rule_segments", None) or (
                    (line_span.text, line_span.underline, line_span.strike),
                )
                for text, underline, strike in segments:
                    runs.append(
                        StyledRun(
                            text,
                            TextStyle(
                                code=line_span.base14 or "helv",
                                size=line_span.size,
                                color=line_span.color,
                                underline=underline,
                                strike=strike,
                            ),
                        )
                    )
        if not runs:
            runs = [
                StyledRun(
                    para.text,
                    TextStyle(code=para.base14 or "helv", size=para.size, color=para.color),
                )
            ]
        return runs

    # --- selection formatting API (driven by the style toolbar) -----------
    def open_rich_editor(self):
        """The currently open in-place editor, or None."""
        if self._editor.is_editing:
            return self._editor
        if self._para_editor.is_editing:
            return self._para_editor
        return None

    def apply_format_to_editor(self, fmt: QTextCharFormat) -> bool:
        """Merge a char format into the open editor's selection. False = no
        editor open (the toolbar change only affects future inserts)."""
        editor = self.open_rich_editor()
        if editor is None:
            return False
        editor.merge_selection_format(fmt)
        return True

    def apply_size_pt_to_editor(self, size_pt: float) -> bool:
        """Apply a text size (points) to the open editor's selection, scaled
        to the editor's on-screen pixel size."""
        editor = self.open_rich_editor()
        if editor is None:
            return False
        font = QFont()
        font.setPixelSize(max(2, round(size_pt * self._edit_open_zoom)))
        fmt = QTextCharFormat()
        fmt.setFont(
            font, QTextCharFormat.FontPropertiesInheritanceBehavior.FontPropertiesSpecifiedOnly
        )
        fmt.setProperty(PT_PROPERTY, float(size_pt))  # the semantic size
        editor.merge_selection_format(fmt)
        return True

    def _current_style(self):
        """The style toolbar's TextStyle, or None (match the original)."""
        if self.style_provider is None:
            return None
        style, _preview = self.style_provider()
        return style

    def _on_edit_cancelled(self) -> None:
        self._pending_edit = None
        self._pending_paragraph = None
        self._pending_insert = None
        self._pending_comment = None
        self._pending_comment_edit = None

    # --- save -----------------------------------------------------------
    def save(self) -> bool:
        """Save changes back to the open file (atomic in-place). True on success."""
        try:
            self._doc.save_in_place()
        except Exception as exc:  # noqa: BLE001 - surface any save error
            QMessageBox.critical(self, "Save failed", _save_failure_text(self._doc.source, exc))
            return False
        self._undo_stack.setClean()
        self.stateChanged.emit()
        return True

    def save_as_path(self, out: Path) -> bool:
        source = self._doc.source
        if source is not None and out.resolve() == source.resolve():
            return self.save()  # same file -> atomic in-place
        try:
            self._doc.save(out)
        except Exception as exc:  # noqa: BLE001 - surface any save error
            QMessageBox.critical(self, "Save failed", _save_failure_text(out, exc))
            return False
        # Continue editing the newly-saved file within this view. The undo
        # stack is kept: snapshots are full states, so undoing past this point
        # restores pre-edit content (a later save writes it to the NEW path).
        self._doc.close()
        self._doc = PdfDocument.open(out)
        self._current_page = min(self._current_page, self._doc.page_count - 1)
        self._undo_stack.setClean()
        self._invalidate_render_cache()
        self._populate_thumbnails()
        self._show_page(self._current_page)
        self.stateChanged.emit()
        return True

    # --- mutation bookkeeping -------------------------------------------
    def _push_command(self, text: str, op, scope: tuple[str, int]) -> bool:
        """Route a mutation through the undo stack; report failure. True on success.

        push() executes the op via the command's first redo(). A failed op has
        already restored the pre-op state and marked itself obsolete (dropped
        from the stack) — only the error report is left to do here.
        """
        command = SnapshotCommand(text, self, op, scope)
        self._undo_stack.push(command)
        if command.error is not None:
            # Modal only when actually on screen — offscreen tests would hang
            # on exec() (same pattern as MainWindow.closeEvent).
            if self.isVisible():
                QMessageBox.critical(self, text, f"{text} failed:\n\n{command.error}")
            else:
                self.editWarning.emit(f"{text} failed: {command.error}")
            return False
        return True

    def after_command(self, scope: tuple[str, int]) -> None:
        """Single refresh funnel after a command executes or restores.

        ``("page", n)``: page-scoped mutation (rotate, text edit) — page
        count/order unchanged, so only that page's cache entries and thumbnail
        are stale.
        ``("all", -1)``: structural ops and EVERY undo/redo restore — a
        snapshot swap can change anything, so clear everything and rebuild
        rather than hand-tracking what changed.
        """
        kind, page = scope
        self._clear_selection()  # the selected object may no longer exist
        self._clear_text_selection()  # its (line, word) positions may be stale
        self._text_lines = None  # content changed — rebuild the line cache
        self.clear_search_results()  # hit rects describe pre-mutation content
        self._current_page = min(self._current_page, self._doc.page_count - 1)
        if kind == "page":
            self._cache.evict_page(page)
            self._geometry.evict_page(page)
            self._ocr_words.evict_page(page)
            self._thumbnails.update_thumbnail(page, self._thumb_pixmap(page))
            if page == self._current_page:
                self._show_page(page)
        else:
            self._invalidate_render_cache()
            self._populate_thumbnails()
            self._show_page(self._current_page)
        self.stateChanged.emit()

    # --- render cache + thumbnails --------------------------------------
    def _invalidate_render_cache(self) -> None:
        """Clear the render, geometry AND OCR-word caches. Called on every
        structural mutation, every undo/redo restore, and the save-as reopen
        (which can renumber xrefs — stale ImageInfo would target the wrong
        object; stale OCR words would describe replaced content)."""
        self._cache.clear()
        self._geometry.clear()
        self._ocr_words.clear()
        self._text_lines = None  # content may have changed — rebuild on next use (X4)

    def _page_pixmap(self, index: int, render_zoom: float):
        key = (index, "main", render_zoom)
        pixmap = self._cache.get(key)
        if pixmap is None:
            pixmap = rendered_page_to_qpixmap(self._doc.render_page(index, zoom=render_zoom))
            self._cache.put(key, pixmap, cost=pixmap.width() * pixmap.height() * 4)
        return pixmap

    def _thumb_pixmap(self, index: int):
        key = (index, "thumb", _THUMB_DPI)
        pixmap = self._cache.get(key)
        if pixmap is None:
            pixmap = rendered_page_to_qpixmap(self._doc.render_page(index, dpi=_THUMB_DPI))
            self._cache.put(key, pixmap, cost=pixmap.width() * pixmap.height() * 4)
        return pixmap

    def _render_zoom_for(self, zoom: float) -> float:
        """Engine zoom matching the true on-screen resolution: logical zoom times
        the monitor's device-pixel ratio (capped). Rendering at exactly this zoom
        maps render pixels 1:1 to device pixels at rest — no resampling."""
        return min(zoom * self._canvas.devicePixelRatioF(), _MAX_RENDER_ZOOM)

    def _on_render_needed(self, zoom: float) -> None:
        render_zoom = self._render_zoom_for(zoom)
        if abs(render_zoom - self._canvas.render_zoom) > 1e-9:
            self._canvas.update_pixmap(
                self._page_pixmap(self._current_page, render_zoom), render_zoom
            )
            self._push_selection_chrome()  # rescale to the new render zoom
            self._push_reveal_chrome()
            self._push_search_chrome()
            self._push_text_selection_chrome()

    def _populate_thumbnails(self) -> None:
        pixmaps = [self._thumb_pixmap(i) for i in range(self._doc.page_count)]
        self._thumbnails.set_thumbnails(pixmaps)
        self._thumbnails.set_current(self._current_page)

    # --- display --------------------------------------------------------
    def _show_page(self, index: int) -> None:
        size_pts = self._doc.page_size(index)
        zoom = self._canvas.zoom_for_page(size_pts)
        render_zoom = self._render_zoom_for(zoom)
        self._canvas.set_page(self._page_pixmap(index, render_zoom), render_zoom, size_pts)
        self._push_selection_chrome()
        self._push_reveal_chrome()
        self._push_search_chrome()
        self._push_text_selection_chrome()
        self._thumbnails.set_current(self._current_page)
