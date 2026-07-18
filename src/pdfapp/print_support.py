"""Printing: the QPrinter/dialog/painter pipeline and target-rect math (pdfapp).

Boundary: every Qt print class lives here; pdfcore never imports QtPrintSupport.
ONE paint routine (`render_onto`) drives both direct printing and preview — it
reads the resolution from the *printer* and re-renders each page in the engine at
that DPI (never scaling a screen bitmap), one page at a time to bound memory.
That printer-DPI render is what makes output crisp.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QRectF, QSize, Signal
from PySide6.QtGui import QAction, QActionGroup, QIntValidator, QPageLayout, QPageSize, QPainter
from PySide6.QtPrintSupport import QPrintDialog, QPrinter, QPrintPreviewWidget
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QRadioButton,
    QToolBar,
    QVBoxLayout,
)

from pdfapp import icons
from pdfapp.qt_image import rendered_page_to_qimage
from pdfapp.render_cache import RenderCache
from pdfcore.document import PdfDocument

# Preview renders are capped at this dpi so the first render per page is fast
# and memory stays bounded; the real print job (File > Print) uses full DPI.
_PREVIEW_DPI = 150

# Common paper sizes offered in the Print dialog (label, QPageSize id).
_PAGE_SIZES = [
    ("A4", QPageSize.PageSizeId.A4),
    ("A3", QPageSize.PageSizeId.A3),
    ("Letter", QPageSize.PageSizeId.Letter),
    ("Legal", QPageSize.PageSizeId.Legal),
]


@dataclass
class PrintOptions:
    gray: bool = False
    fit: str = "fit"  # "fit" (fit-to-page) | "actual" (true physical size)
    page_size_id: QPageSize.PageSizeId | None = None  # None -> printer default
    # "auto" matches the paper orientation to EACH page (mixed portrait/landscape
    # documents print each page on matching paper); "portrait"/"landscape" force
    # one orientation for the whole job.
    orientation: str = "auto"
    # Review comments are MARKUP: they never print unless the user opts in
    # (E11, user decision). Highlights and other annotations still print.
    print_comments: bool = False


def orientation_for_page(orientation: str, page_w_pt: float, page_h_pt: float) -> str:
    """Resolve the paper orientation for one page (pure; unit-testable)."""
    if orientation == "auto":
        return "landscape" if page_w_pt > page_h_pt else "portrait"
    return orientation


def compute_target_rect(
    target: tuple[float, float, float, float],
    img_w: int,
    img_h: int,
    page_w_pt: float,
    page_h_pt: float,
    fit: str,
    dpi: int,
) -> tuple[float, float, float, float]:
    """Where to draw the page image, in device pixels at ``dpi``.

    Pure math (no Qt) so it is unit-testable. ``target`` is the printable area
    ``(x, y, w, h)`` in device pixels.

    - ``"fit"``: scale the image to fit inside ``target`` preserving aspect,
      centred. The image was rendered at printer DPI, so any scaling is a
      high-quality downscale, not an upscale of a blurry bitmap.
    - ``"actual"``: map the PDF's point size to device pixels (points/72*dpi),
      i.e. true physical size, anchored at the printable-area origin.
    """
    tx, ty, tw, th = target
    if fit == "actual":
        w = page_w_pt / 72.0 * dpi
        h = page_h_pt / 72.0 * dpi
        return (tx, ty, w, h)
    scale = min(tw / img_w, th / img_h)
    w = img_w * scale
    h = img_h * scale
    x = tx + (tw - w) / 2.0
    y = ty + (th - h) / 2.0
    return (x, y, w, h)


def _qt_orientation(orientation: str) -> QPageLayout.Orientation:
    return (
        QPageLayout.Orientation.Landscape
        if orientation == "landscape"
        else QPageLayout.Orientation.Portrait
    )


def apply_page_setup(
    printer: QPrinter,
    options: PrintOptions,
    first_page_size_pts: tuple[float, float] | None = None,
) -> None:
    """Apply paper size + orientation to ``printer`` before it is used.

    ``begin()`` prepares page 1, so with "auto" the first page's orientation must
    be set here (subsequent pages are handled per page inside render_onto).
    """
    if options.page_size_id is not None:
        printer.setPageSize(QPageSize(options.page_size_id))
    first_w, first_h = first_page_size_pts if first_page_size_pts is not None else (0.0, 1.0)
    printer.setPageOrientation(
        _qt_orientation(orientation_for_page(options.orientation, first_w, first_h))
    )


def render_onto(
    printer: QPrinter,
    doc: PdfDocument,
    options: PrintOptions,
    cache: RenderCache | None = None,
) -> None:
    """Paint every page of ``doc`` onto ``printer``. Shared by print and preview.

    ``cache`` (UI-side, keyed ``(index, dpi, gray)``) lets preview reuse renders
    across the repeated paintRequested calls it makes on zoom/resize/navigate.
    The engine render itself stays stateless — passing ``None`` renders fresh.
    """
    painter = QPainter()
    if not painter.begin(printer):  # begin() prepares page 1
        return
    # Pixmap rendering draws VISIBLE annotations regardless of their PDF
    # Print flag, so "don't print comments" means hiding them for the render
    # (restored in the finally — the toggle must never stick). Cached renders
    # carry the option in their KEY, so both states coexist in the preview
    # cache without invalidation.
    hidden_for_print = 0
    if not options.print_comments:
        hidden_for_print = doc.set_comments_hidden(True)
    try:
        dpi = printer.resolution()
        for i in range(doc.page_count):
            page_w_pt, page_h_pt = doc.page_size(i)
            if i > 0:
                # With "auto", match the paper to this page. Qt applies a
                # setPageOrientation made immediately before newPage() to the
                # new page (verified against 6.11: the output PDF and the paint
                # rect both track per-page orientation).
                printer.setPageOrientation(
                    _qt_orientation(orientation_for_page(options.orientation, page_w_pt, page_h_pt))
                )
                printer.newPage()
            # Re-read per page: the printable area changes with orientation.
            paint_rect = printer.pageLayout().paintRectPixels(dpi)
            target = (
                float(paint_rect.x()),
                float(paint_rect.y()),
                float(paint_rect.width()),
                float(paint_rect.height()),
            )
            rp = _render_page(doc, i, dpi, options.gray, cache, options.print_comments)
            img = rendered_page_to_qimage(rp)
            x, y, w, h = compute_target_rect(
                target, img.width(), img.height(), page_w_pt, page_h_pt, options.fit, dpi
            )
            painter.drawImage(QRectF(x, y, w, h), img)
            # img goes out of scope each iteration -> peak memory ~ one page.
    finally:
        painter.end()
        if hidden_for_print:
            doc.set_comments_hidden(False)


def _render_page(
    doc: PdfDocument,
    index: int,
    dpi: int,
    gray: bool,
    cache: RenderCache | None,
    with_comments: bool = False,
):
    """Engine render for one page, served from ``cache`` when provided.

    ``with_comments`` joins the key: previews with and without comments
    coexist in the cache instead of invalidating each other.
    """
    if cache is None:
        return doc.render_page_at_dpi(index, dpi, gray=gray)
    key = (index, dpi, gray, with_comments)
    rendered = cache.get(key)
    if rendered is None:
        rendered = doc.render_page_at_dpi(index, dpi, gray=gray)
        cache.put(key, rendered, cost=len(rendered.samples))
    return rendered


def print_document(parent, doc: PdfDocument, options: PrintOptions) -> None:
    """Apply page setup, show the native print dialog, then print the document."""
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    apply_page_setup(printer, options, doc.page_size(0))
    dialog = QPrintDialog(printer, parent)
    if dialog.exec() == QPrintDialog.DialogCode.Accepted:
        render_onto(printer, doc, options)


def _build_preview_dialog(parent, doc: PdfDocument, options: PrintOptions) -> PrintPreviewDialog:
    """Capped printer + per-session cache + wired dialog, not yet exec'd.

    Split from show_preview so offscreen tests can build and drive the dialog
    without a modal event loop.
    """
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    apply_page_setup(printer, options, doc.page_size(0))
    if printer.resolution() > _PREVIEW_DPI:
        printer.setResolution(_PREVIEW_DPI)
    cache = RenderCache(capacity=64, max_cost=64 * 1024 * 1024)
    dialog = PrintPreviewDialog(printer, parent)
    dialog.preview_widget.paintRequested.connect(lambda p: render_onto(p, doc, options, cache))
    return dialog


def show_preview(parent, doc: PdfDocument, options: PrintOptions) -> None:
    """Preview the print output. Uses the same render_onto as real printing.

    A fresh per-session cache backs the dialog's repeated repaints; because it is
    created here and discarded when the modal preview closes, it can never serve
    a stale render after a mutation (clear-on-mutation, trivially).
    """
    _build_preview_dialog(parent, doc, options).exec()


class PrintPreviewDialog(QDialog):
    """Themed print preview: QPrintPreviewWidget + the app's own toolbar (S5).

    Replaces QPrintPreviewDialog, whose internal bare-Qt toolbar neither the
    app theme nor the icon set can reach. Same controls, same behaviour:
    print, zoom in/out + zoom %, fit width / fit page, portrait/landscape,
    page navigation, single/facing/overview view modes. Printing from here
    uses the preview's capped-resolution printer exactly like the stock
    dialog did — File > Print remains the full-resolution path.

    Toolbar icons are baked for theme.current_mode() at CONSTRUCTION: the
    dialog is modal and short-lived, so a theme toggle cannot happen while
    it is open and the next open picks up the current mode (its actions are
    deliberately not in MainWindow's re-icon map).
    """

    _ZOOM_PRESETS = ("12.5%", "25%", "50%", "100%", "125%", "150%", "200%", "400%", "800%")

    def __init__(self, printer: QPrinter, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Print preview")
        self.resize(1080, 760)
        self._printer = printer  # the preview widget borrows it; keep it alive
        self._preview = QPrintPreviewWidget(printer, self)
        self._preview.previewChanged.connect(self._sync_from_preview)
        self._build_toolbar()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._toolbar)
        layout.addWidget(self._preview)
        self._sync_from_preview()

    @property
    def preview_widget(self) -> QPrintPreviewWidget:
        return self._preview

    # --- construction -----------------------------------------------------
    def _act(self, key: str, tip: str, slot=None, checkable: bool = False) -> QAction:
        action = QAction(self)
        action.setIcon(icons.icon(key))
        action.setToolTip(tip)
        action.setCheckable(checkable)
        if slot is not None:
            action.triggered.connect(slot)
        return action

    def _build_toolbar(self) -> None:
        bar = QToolBar("Preview", self)
        bar.setMovable(False)
        bar.setIconSize(QSize(20, 20))
        preview = self._preview

        # Fit modes: exclusive-optional — a custom zoom legitimately leaves
        # neither checked. triggered (user-only) drives the widget; state is
        # synced back from previewChanged, so there is no signal loop.
        self._fit_width_action = self._act(
            "fit_width", "Fit page width", lambda: self._fit(preview.fitToWidth), checkable=True
        )
        self._fit_page_action = self._act(
            "fit_page", "Fit whole page", lambda: self._fit(preview.fitInView), checkable=True
        )
        fit_group = QActionGroup(self)
        fit_group.setExclusionPolicy(QActionGroup.ExclusionPolicy.ExclusiveOptional)
        fit_group.addAction(self._fit_width_action)
        fit_group.addAction(self._fit_page_action)

        self._zoom_out_action = self._act(
            "zoom_out", "Zoom out", lambda: self._zoom(preview.zoomOut)
        )
        self._zoom_in_action = self._act("zoom_in", "Zoom in", lambda: self._zoom(preview.zoomIn))
        self._zoom_combo = QComboBox(bar)
        self._zoom_combo.setEditable(True)
        self._zoom_combo.addItems(self._ZOOM_PRESETS)
        self._zoom_combo.setCurrentText("100%")
        self._zoom_combo.setFixedWidth(84)
        self._zoom_combo.setToolTip("Zoom level")
        self._zoom_combo.activated.connect(lambda _i: self._apply_zoom_text())
        self._zoom_combo.lineEdit().returnPressed.connect(self._apply_zoom_text)

        self._portrait_action = self._act(
            "portrait",
            "Portrait paper",
            lambda: self._orient(preview.setPortraitOrientation),
            checkable=True,
        )
        self._landscape_action = self._act(
            "landscape",
            "Landscape paper",
            lambda: self._orient(preview.setLandscapeOrientation),
            checkable=True,
        )
        orient_group = QActionGroup(self)
        orient_group.addAction(self._portrait_action)
        orient_group.addAction(self._landscape_action)

        self._first_action = self._act("first_page", "First page", lambda: self._go_to(1))
        self._prev_action = self._act(
            "prev_page", "Previous page", lambda: self._go_to(preview.currentPage() - 1)
        )
        self._page_edit = QLineEdit(bar)
        self._page_edit.setFixedWidth(56)
        self._page_edit.setValidator(QIntValidator(1, 999999, self._page_edit))
        self._page_edit.setToolTip("Current page")
        self._page_edit.returnPressed.connect(self._go_to_typed)
        self._page_total = QLabel(" / 0", bar)
        self._next_action = self._act(
            "next_page", "Next page", lambda: self._go_to(preview.currentPage() + 1)
        )
        self._last_action = self._act(
            "last_page", "Last page", lambda: self._go_to(preview.pageCount())
        )

        self._view_single_action = self._act(
            "view_single",
            "One page at a time",
            lambda: self._view(preview.setSinglePageViewMode),
            checkable=True,
        )
        self._view_facing_action = self._act(
            "view_facing",
            "Facing pages",
            lambda: self._view(preview.setFacingPagesViewMode),
            checkable=True,
        )
        self._view_overview_action = self._act(
            "view_overview",
            "All pages overview",
            lambda: self._view(preview.setAllPagesViewMode),
            checkable=True,
        )
        view_group = QActionGroup(self)
        for action in (
            self._view_single_action,
            self._view_facing_action,
            self._view_overview_action,
        ):
            view_group.addAction(action)

        self._print_action = self._act(
            "print",
            "Print from the preview (preview resolution — File > Print for full quality)",
            self._print,
        )

        bar.addAction(self._fit_width_action)
        bar.addAction(self._fit_page_action)
        bar.addSeparator()
        bar.addAction(self._zoom_out_action)
        bar.addWidget(self._zoom_combo)
        bar.addAction(self._zoom_in_action)
        bar.addSeparator()
        bar.addAction(self._portrait_action)
        bar.addAction(self._landscape_action)
        bar.addSeparator()
        bar.addAction(self._first_action)
        bar.addAction(self._prev_action)
        bar.addWidget(self._page_edit)
        bar.addWidget(self._page_total)
        bar.addAction(self._next_action)
        bar.addAction(self._last_action)
        bar.addSeparator()
        bar.addAction(self._view_single_action)
        bar.addAction(self._view_facing_action)
        bar.addAction(self._view_overview_action)
        bar.addSeparator()
        bar.addAction(self._print_action)
        self._toolbar = bar

    # --- control handlers (each ends by re-syncing displayed state) --------
    def _fit(self, widget_slot) -> None:
        widget_slot()
        self._sync_from_preview()

    def _zoom(self, widget_slot) -> None:
        widget_slot()
        self._sync_from_preview()

    def _orient(self, widget_slot) -> None:
        widget_slot()
        self._sync_from_preview()

    def _view(self, widget_slot) -> None:
        widget_slot()
        self._sync_from_preview()

    def _apply_zoom_text(self) -> None:
        text = self._zoom_combo.currentText().strip().rstrip("%")
        try:
            pct = float(text)
        except ValueError:
            self._sync_from_preview()
            return
        if pct > 0:
            self._preview.setZoomFactor(pct / 100.0)
        self._sync_from_preview()

    def _go_to(self, page: int) -> None:
        page = max(1, min(self._preview.pageCount(), page))
        self._preview.setCurrentPage(page)
        self._sync_from_preview()

    def _go_to_typed(self) -> None:
        try:
            page = int(self._page_edit.text())
        except ValueError:
            self._sync_from_preview()
            return
        self._go_to(page)

    def _print(self) -> None:
        # Prints via the preview's capped printer, then closes — exactly what
        # the stock QPrintPreviewDialog's print button did. Full-resolution
        # printing stays on File > Print.
        self._preview.print_()
        self.accept()

    def _sync_from_preview(self) -> None:
        preview = self._preview
        zoom_mode = preview.zoomMode()
        self._fit_width_action.setChecked(zoom_mode == QPrintPreviewWidget.ZoomMode.FitToWidth)
        self._fit_page_action.setChecked(zoom_mode == QPrintPreviewWidget.ZoomMode.FitInView)
        if not self._zoom_combo.lineEdit().hasFocus():
            self._zoom_combo.setCurrentText(f"{preview.zoomFactor() * 100:.0f}%")
        portrait = preview.orientation() == QPageLayout.Orientation.Portrait
        self._portrait_action.setChecked(portrait)
        self._landscape_action.setChecked(not portrait)
        view_mode = preview.viewMode()
        self._view_single_action.setChecked(
            view_mode == QPrintPreviewWidget.ViewMode.SinglePageView
        )
        self._view_facing_action.setChecked(
            view_mode == QPrintPreviewWidget.ViewMode.FacingPagesView
        )
        self._view_overview_action.setChecked(
            view_mode == QPrintPreviewWidget.ViewMode.AllPagesView
        )
        if not self._page_edit.hasFocus():
            self._page_edit.setText(str(preview.currentPage()))
        self._page_total.setText(f" / {preview.pageCount()}")


class PrintDialog(QDialog):
    """Collects PrintOptions before printing: scaling, paper/orientation, colour.

    [Preview] emits previewRequested without closing (so the caller can preview
    with the current options and the user can still adjust and Print); [Print]
    accepts; [Cancel] rejects.
    """

    previewRequested = Signal()

    def __init__(self, options: PrintOptions, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Print")

        scaling_box = QGroupBox("Scaling")
        self._fit_radio = QRadioButton("Fit to page", scaling_box)
        self._actual_radio = QRadioButton("Actual size", scaling_box)
        (self._actual_radio if options.fit == "actual" else self._fit_radio).setChecked(True)
        scaling_layout = QVBoxLayout(scaling_box)
        scaling_layout.addWidget(self._fit_radio)
        scaling_layout.addWidget(self._actual_radio)

        paper_box = QGroupBox("Paper")
        self._size_combo = QComboBox(paper_box)
        self._size_combo.addItem("Printer default", None)
        for name, size_id in _PAGE_SIZES:
            self._size_combo.addItem(name, size_id)
        self._select_page_size(options.page_size_id)
        self._orientation_combo = QComboBox(paper_box)
        self._orientation_combo.addItem("Auto (match each page)", "auto")
        self._orientation_combo.addItem("Portrait", "portrait")
        self._orientation_combo.addItem("Landscape", "landscape")
        index = self._orientation_combo.findData(options.orientation)
        self._orientation_combo.setCurrentIndex(index if index >= 0 else 0)
        paper_form = QFormLayout(paper_box)
        paper_form.addRow("Size:", self._size_combo)
        paper_form.addRow("Orientation:", self._orientation_combo)

        colour_box = QGroupBox("Colour")
        self._colour_radio = QRadioButton("Colour", colour_box)
        self._bw_radio = QRadioButton("Black && white", colour_box)
        (self._bw_radio if options.gray else self._colour_radio).setChecked(True)
        colour_layout = QVBoxLayout(colour_box)
        colour_layout.addWidget(self._colour_radio)
        colour_layout.addWidget(self._bw_radio)

        markup_box = QGroupBox("Markup")
        self._comments_check = QCheckBox("Print comments", markup_box)
        self._comments_check.setChecked(options.print_comments)  # default: off
        self._comments_check.setToolTip(
            "Review comments are markup and stay off paper unless ticked"
        )
        markup_layout = QVBoxLayout(markup_box)
        markup_layout.addWidget(self._comments_check)

        buttons = QDialogButtonBox(self)
        self._preview_button = buttons.addButton("Preview…", QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton("Print", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        # ActionRole buttons don't close the dialog — preview, then keep editing.
        self._preview_button.clicked.connect(lambda: self.previewRequested.emit())
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(scaling_box)
        layout.addWidget(paper_box)
        layout.addWidget(colour_box)
        layout.addWidget(markup_box)
        layout.addWidget(buttons)

    def _select_page_size(self, page_size_id) -> None:
        if page_size_id is None:
            self._size_combo.setCurrentIndex(0)  # "Printer default"
            return
        index = self._size_combo.findData(page_size_id)
        if index >= 0:
            self._size_combo.setCurrentIndex(index)

    def options(self) -> PrintOptions:
        return PrintOptions(
            gray=self._bw_radio.isChecked(),
            fit="actual" if self._actual_radio.isChecked() else "fit",
            page_size_id=self._size_combo.currentData(),
            orientation=self._orientation_combo.currentData(),
            print_comments=self._comments_check.isChecked(),
        )
