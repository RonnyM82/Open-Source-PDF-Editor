"""Tests for the custom themed print-preview dialog (restyle S5).

Offscreen: dialogs are built and driven directly, never exec'd. PDF-format
printers keep everything deterministic and spool-free.
"""

from __future__ import annotations


def _pdf_printer(path, dpi=150):
    from PySide6.QtPrintSupport import QPrinter

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(path))
    printer.setResolution(dpi)
    return printer


def _wired_dialog(doc, tmp_path):
    from pdfapp.print_support import PrintOptions, PrintPreviewDialog, render_onto

    dialog = PrintPreviewDialog(_pdf_printer(tmp_path / "preview.pdf"))
    dialog.preview_widget.paintRequested.connect(
        lambda p: render_onto(p, doc, PrintOptions(), None)
    )
    return dialog


def test_every_preview_toolbar_button_has_icon_and_tooltip(qapp, multipage_pdf, tmp_path):
    from pdfcore.document import PdfDocument

    with PdfDocument.open(multipage_pdf) as doc:
        dialog = _wired_dialog(doc, tmp_path)
        buttons = [a for a in dialog._toolbar.actions() if not a.isSeparator()]
        icon_actions = [a for a in buttons if not a.icon().isNull()]
        assert len(icon_actions) == 14  # fit x2, zoom x2, orient x2, nav x4, view x3, print
        for action in icon_actions:
            assert action.toolTip(), "icon button without a tooltip"


def test_controls_drive_the_preview_widget(qapp, multipage_pdf, tmp_path):
    from PySide6.QtPrintSupport import QPrintPreviewWidget

    from pdfcore.document import PdfDocument

    with PdfDocument.open(multipage_pdf) as doc:
        dialog = _wired_dialog(doc, tmp_path)
        preview = dialog.preview_widget
        preview.updatePreview()  # generate pages so pageCount is real

        dialog._view_facing_action.trigger()
        assert preview.viewMode() == QPrintPreviewWidget.ViewMode.FacingPagesView
        dialog._view_overview_action.trigger()
        assert preview.viewMode() == QPrintPreviewWidget.ViewMode.AllPagesView
        dialog._view_single_action.trigger()
        assert preview.viewMode() == QPrintPreviewWidget.ViewMode.SinglePageView

        dialog._fit_width_action.trigger()
        assert preview.zoomMode() == QPrintPreviewWidget.ZoomMode.FitToWidth
        assert dialog._fit_width_action.isChecked()

        dialog._zoom_combo.setCurrentText("150%")
        dialog._apply_zoom_text()
        assert abs(preview.zoomFactor() - 1.5) < 1e-6
        assert not dialog._fit_width_action.isChecked()  # custom zoom unchecks fits


def test_orientation_buttons_drive_the_widget(qapp, tmp_path):
    # Unwired on purpose: with a paint handler attached, render_onto's
    # per-page auto-orientation immediately re-asserts each page's own
    # orientation (exactly as it did under the stock QPrintPreviewDialog —
    # parity, not a regression); the buttons' own wiring is what's under
    # test here, so no paint handler is connected.
    from PySide6.QtGui import QPageLayout

    from pdfapp.print_support import PrintPreviewDialog

    dialog = PrintPreviewDialog(_pdf_printer(tmp_path / "bare.pdf"))
    preview = dialog.preview_widget
    dialog._landscape_action.trigger()
    assert preview.orientation() == QPageLayout.Orientation.Landscape
    assert dialog._landscape_action.isChecked()
    dialog._portrait_action.trigger()
    assert preview.orientation() == QPageLayout.Orientation.Portrait
    assert dialog._portrait_action.isChecked()


def test_page_navigation_updates_current_page_and_total(qapp, multipage_pdf, tmp_path):
    from pdfcore.document import PdfDocument

    with PdfDocument.open(multipage_pdf) as doc:  # 5 pages
        dialog = _wired_dialog(doc, tmp_path)
        preview = dialog.preview_widget
        preview.updatePreview()
        assert dialog._page_total.text() == " / 5"

        dialog._last_action.trigger()
        assert preview.currentPage() == 5
        dialog._prev_action.trigger()
        assert preview.currentPage() == 4
        dialog._first_action.trigger()
        assert preview.currentPage() == 1
        dialog._next_action.trigger()
        assert preview.currentPage() == 2

        dialog._page_edit.setText("999")  # clamped to the last page
        dialog._go_to_typed()
        assert preview.currentPage() == 5
        assert dialog._page_edit.text() == "5"


def test_print_button_prints_via_preview_printer_and_closes(qapp, multipage_pdf, tmp_path):
    from pdfcore.document import PdfDocument

    out = tmp_path / "preview.pdf"
    with PdfDocument.open(multipage_pdf) as doc:
        dialog = _wired_dialog(doc, tmp_path)
        dialog.preview_widget.updatePreview()
        dialog._print_action.trigger()
        assert dialog.result() == 1  # accepted (closed)
    assert out.exists()  # printed to the capped preview printer, as before


def test_build_preview_dialog_caps_dpi_and_serves_repaints_from_cache(
    qapp, multipage_pdf, monkeypatch
):
    from pdfapp.print_support import _PREVIEW_DPI, PrintOptions, _build_preview_dialog
    from pdfcore.document import PdfDocument

    with PdfDocument.open(multipage_pdf) as doc:
        calls = {"n": 0}
        original = doc.render_page_at_dpi

        def counting(index, dpi, gray=False):
            calls["n"] += 1
            return original(index, dpi, gray=gray)

        doc.render_page_at_dpi = counting
        dialog = _build_preview_dialog(None, doc, PrintOptions())
        assert dialog._printer.resolution() <= _PREVIEW_DPI
        dialog.preview_widget.updatePreview()
        first_pass = calls["n"]
        assert first_pass == 5  # every page rendered once
        dialog.preview_widget.updatePreview()  # a repaint: fully cache-served
        assert calls["n"] == first_pass


def test_preview_toolbar_icons_bake_the_mode_current_at_construction(
    theme_app, multipage_pdf, tmp_path
):
    from pdfcore.document import PdfDocument

    app, theme = theme_app
    with PdfDocument.open(multipage_pdf) as doc:
        theme.apply_theme(app, theme.LIGHT)
        light = _wired_dialog(doc, tmp_path)
        light_img = light._print_action.icon().pixmap(20, 20).toImage()

        theme.apply_theme(app, theme.DARK)
        dark = _wired_dialog(doc, tmp_path)
        dark_img = dark._print_action.icon().pixmap(20, 20).toImage()

    assert light_img != dark_img  # glyph colour followed the mode at build time
