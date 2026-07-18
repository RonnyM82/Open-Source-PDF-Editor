"""Tests for printing: target-rect math (Qt-free) + print-to-PDF round-trip."""

from __future__ import annotations

import pymupdf

from pdfapp.print_support import compute_target_rect

# --- pure target-rect math (no Qt) --------------------------------------


def test_fit_scales_preserving_aspect_and_centres():
    # target 1000x2000, square 500x500 image -> scale 2 -> 1000x1000, centred.
    x, y, w, h = compute_target_rect((0.0, 0.0, 1000.0, 2000.0), 500, 500, 100, 100, "fit", 300)
    assert (w, h) == (1000.0, 1000.0)
    assert x == 0.0
    assert y == 500.0  # (2000 - 1000) / 2


def test_fit_offsets_by_target_origin():
    x, y, w, h = compute_target_rect((10.0, 20.0, 800.0, 800.0), 400, 200, 100, 100, "fit", 300)
    # 400x200 into 800x800 -> scale 2 -> 800x400, centred horizontally at x=10.
    assert (w, h) == (800.0, 400.0)
    assert x == 10.0
    assert y == 20.0 + (800.0 - 400.0) / 2.0


def test_actual_size_maps_points_to_device_pixels():
    # A4 (595x842 pt) at 300 dpi -> points/72*dpi, anchored at the target origin.
    x, y, w, h = compute_target_rect(
        (10.0, 20.0, 9999.0, 9999.0), 2480, 3508, 595, 842, "actual", 300
    )
    assert x == 10.0 and y == 20.0
    assert abs(w - 595 / 72 * 300) < 1e-6
    assert abs(h - 842 / 72 * 300) < 1e-6


# --- print-to-PDF integration (exercises the whole paint loop headlessly) ---


def test_render_onto_pdf_has_every_page(qapp, multipage_pdf, tmp_path):
    from PySide6.QtPrintSupport import QPrinter

    from pdfapp.print_support import PrintOptions, render_onto
    from pdfcore.document import PdfDocument

    out = tmp_path / "printed.pdf"
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(out))
    printer.setResolution(150)  # keep the test fast; we only assert page count

    with PdfDocument.open(multipage_pdf) as doc:
        render_onto(printer, doc, PrintOptions())

    assert out.exists()
    printed = pymupdf.open(str(out))
    try:
        assert printed.page_count == 5  # one printed page per source page
    finally:
        printed.close()


# --- B3: PrintDialog scaling control ------------------------------------


def test_print_dialog_defaults_to_incoming_fit(qapp):
    from pdfapp.print_support import PrintDialog, PrintOptions

    dialog = PrintDialog(PrintOptions(fit="actual"))
    assert dialog._actual_radio.isChecked()
    assert dialog.options().fit == "actual"


def test_print_dialog_reads_selected_scaling(qapp):
    from pdfapp.print_support import PrintDialog, PrintOptions

    dialog = PrintDialog(PrintOptions(fit="fit"))
    assert dialog.options().fit == "fit"
    dialog._actual_radio.setChecked(True)
    assert dialog.options().fit == "actual"


def test_actual_size_print_to_pdf_has_all_pages(qapp, multipage_pdf, tmp_path):
    from PySide6.QtPrintSupport import QPrinter

    from pdfapp.print_support import PrintOptions, render_onto
    from pdfcore.document import PdfDocument

    out = tmp_path / "actual.pdf"
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(out))
    printer.setResolution(150)  # keep the test fast; we only assert page count
    with PdfDocument.open(multipage_pdf) as doc:
        render_onto(printer, doc, PrintOptions(fit="actual"))
    assert out.exists()
    printed = pymupdf.open(str(out))
    try:
        assert printed.page_count == 5
    finally:
        printed.close()


# --- B4: paper size + orientation ---------------------------------------


def test_print_dialog_reads_paper_and_orientation(qapp):
    from PySide6.QtGui import QPageSize

    from pdfapp.print_support import PrintDialog, PrintOptions

    dialog = PrintDialog(PrintOptions())
    assert dialog.options().page_size_id is None  # printer default
    assert dialog.options().orientation == "auto"  # default: match each page

    index = dialog._size_combo.findData(QPageSize.PageSizeId.A4)
    dialog._size_combo.setCurrentIndex(index)
    dialog._orientation_combo.setCurrentIndex(dialog._orientation_combo.findData("landscape"))
    opts = dialog.options()
    assert opts.page_size_id == QPageSize.PageSizeId.A4
    assert opts.orientation == "landscape"


def test_paper_and_orientation_applied_to_output(qapp, multipage_pdf, tmp_path):
    from PySide6.QtGui import QPageSize
    from PySide6.QtPrintSupport import QPrinter

    from pdfapp.print_support import PrintOptions, apply_page_setup, render_onto
    from pdfcore.document import PdfDocument

    out = tmp_path / "a4_landscape.pdf"
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(out))
    printer.setResolution(150)
    options = PrintOptions(page_size_id=QPageSize.PageSizeId.A4, orientation="landscape")
    with PdfDocument.open(multipage_pdf) as doc:
        apply_page_setup(printer, options, doc.page_size(0))
        render_onto(printer, doc, options)

    printed = pymupdf.open(str(out))
    try:
        rect = printed[0].rect
        # A4 landscape ~ 842 x 595 pt: wider than tall, ~A4 long edge.
        assert rect.width > rect.height
        assert abs(rect.width - 842) < 3
    finally:
        printed.close()


# --- orientation auto-detection + mixed documents ------------------------


def test_orientation_for_page_resolves_auto():
    from pdfapp.print_support import orientation_for_page

    assert orientation_for_page("auto", 842, 595) == "landscape"
    assert orientation_for_page("auto", 595, 842) == "portrait"
    assert orientation_for_page("auto", 500, 500) == "portrait"  # square -> portrait
    # Explicit choices pass through regardless of the page shape.
    assert orientation_for_page("portrait", 842, 595) == "portrait"
    assert orientation_for_page("landscape", 595, 842) == "landscape"


def _make_mixed_pdf(path):
    """Page 0 portrait A4, page 1 landscape A4, page 2 portrait A4."""
    doc = pymupdf.open()
    for w, h in ((595, 842), (842, 595), (595, 842)):
        page = doc.new_page(width=w, height=h)
        page.insert_text((72, 72), f"{w}x{h}", fontsize=18)
    doc.save(str(path))
    doc.close()


def test_auto_orientation_matches_each_page_in_mixed_document(qapp, tmp_path):
    from PySide6.QtGui import QPageSize
    from PySide6.QtPrintSupport import QPrinter

    from pdfapp.print_support import PrintOptions, apply_page_setup, render_onto
    from pdfcore.document import PdfDocument

    src = tmp_path / "mixed.pdf"
    _make_mixed_pdf(src)
    out = tmp_path / "mixed_printed.pdf"
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(out))
    printer.setResolution(150)
    options = PrintOptions(page_size_id=QPageSize.PageSizeId.A4, orientation="auto")
    with PdfDocument.open(src) as doc:
        apply_page_setup(printer, options, doc.page_size(0))
        render_onto(printer, doc, options)

    printed = pymupdf.open(str(out))
    try:
        shapes = [printed[i].rect.width > printed[i].rect.height for i in range(3)]
        assert shapes == [False, True, False]  # portrait, LANDSCAPE, portrait
    finally:
        printed.close()


def test_auto_orientation_on_landscape_document_prints_landscape(qapp, tmp_path):
    from PySide6.QtGui import QPageSize
    from PySide6.QtPrintSupport import QPrinter

    from pdfapp.print_support import PrintOptions, apply_page_setup, render_onto
    from pdfcore.document import PdfDocument

    src = tmp_path / "landscape.pdf"
    doc = pymupdf.open()
    doc.new_page(width=842, height=595).insert_text((72, 72), "wide", fontsize=18)
    doc.save(str(src))
    doc.close()

    out = tmp_path / "landscape_printed.pdf"
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(out))
    printer.setResolution(150)
    options = PrintOptions(page_size_id=QPageSize.PageSizeId.A4, orientation="auto")
    with PdfDocument.open(src) as pdf_doc:
        apply_page_setup(printer, options, pdf_doc.page_size(0))
        render_onto(printer, pdf_doc, options)

    printed = pymupdf.open(str(out))
    try:
        assert printed[0].rect.width > printed[0].rect.height  # landscape paper
    finally:
        printed.close()


# --- B5: colour / black & white -----------------------------------------


def test_print_dialog_reads_colour_mode(qapp):
    from pdfapp.print_support import PrintDialog, PrintOptions

    dialog = PrintDialog(PrintOptions(gray=False))
    assert dialog.options().gray is False
    dialog._bw_radio.setChecked(True)
    assert dialog.options().gray is True


def test_grayscale_print_to_pdf_has_all_pages(qapp, multipage_pdf, tmp_path):
    from PySide6.QtPrintSupport import QPrinter

    from pdfapp.print_support import PrintOptions, render_onto
    from pdfcore.document import PdfDocument

    out = tmp_path / "gray.pdf"
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(out))
    printer.setResolution(150)
    with PdfDocument.open(multipage_pdf) as doc:
        render_onto(printer, doc, PrintOptions(gray=True))  # exercises Grayscale8 path
    assert out.exists()
    printed = pymupdf.open(str(out))
    try:
        assert printed.page_count == 5
    finally:
        printed.close()


# --- B6: preview cache + Preview button ---------------------------------


def _pdf_printer(path, dpi=150):
    from PySide6.QtPrintSupport import QPrinter

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(path))
    printer.setResolution(dpi)
    return printer


def test_preview_cache_avoids_re_rendering(qapp, multipage_pdf, tmp_path):
    from pdfapp.print_support import PrintOptions, render_onto
    from pdfapp.render_cache import RenderCache
    from pdfcore.document import PdfDocument

    calls = {"n": 0}
    with PdfDocument.open(multipage_pdf) as doc:
        original = doc.render_page_at_dpi

        def counting(index, dpi, gray=False):
            calls["n"] += 1
            return original(index, dpi, gray=gray)

        doc.render_page_at_dpi = counting  # count engine renders
        cache = RenderCache()

        render_onto(_pdf_printer(tmp_path / "a.pdf"), doc, PrintOptions(), cache)
        assert calls["n"] == 5  # first pass renders every page

        # A second pass (a preview repaint) at the same dpi is fully cache-served.
        render_onto(_pdf_printer(tmp_path / "b.pdf"), doc, PrintOptions(), cache)
        assert calls["n"] == 5


def test_print_dialog_preview_button_emits_signal(qapp):
    from pdfapp.print_support import PrintDialog, PrintOptions

    dialog = PrintDialog(PrintOptions())
    fired = []
    dialog.previewRequested.connect(lambda: fired.append(True))
    dialog._preview_button.click()
    assert fired == [True]
