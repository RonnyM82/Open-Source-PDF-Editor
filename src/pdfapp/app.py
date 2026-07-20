"""Application entry point: QApplication + MainWindow.

Usage:
    python -m pdfapp [file.pdf]

An optional PDF path opens that file on startup (handy once packaged for
double-click-to-open).

Headless self-test hook (used to smoke-test the packaged .exe at M2 and the
later packaging checkpoints):
    PDF_EDITOR_SMOKE=<pdf>       construct the app, open <pdf>, render page 0,
                                 then exit WITHOUT showing a window.
    PDF_EDITOR_SMOKE_OUT=<file>  also write "OK" / "FAIL: ..." to <file>.
    PDF_EDITOR_PRINT_OUT=<pdf>   additionally exercise the print pipeline
                                 (render_onto -> a PDF), proving QtPrintSupport is
                                 bundled in the frozen build.
    PDF_EDITOR_OCR_SMOKE=<pdf>   render page 0 and OCR it via the (bundled)
                                 tesseract; success requires REAL recognised
                                 words — tesseract can exit 0 having produced
                                 nothing, so exit codes alone prove nothing.
The exit code is 0 on success, 1 on failure. Set one smoke mode per run
(PDF_EDITOR_SMOKE wins if both are set).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from pdfapp import theme
from pdfapp.main_window import MainWindow
from pdfapp.qt_image import rendered_page_to_qimage
from pdfapp.resources import resource_path
from pdfcore.document import PdfDocument


def _run_smoke(smoke_path: str) -> int:
    """Prove that PyMuPDF rendering and Qt image conversion work in this build.

    If PDF_EDITOR_PRINT_OUT is set, also exercise the print pipeline (proves
    QtPrintSupport + the printsupport plugin are bundled in a frozen build).
    """
    result = "FAIL"
    try:
        with PdfDocument.open(Path(smoke_path)) as doc:
            img = rendered_page_to_qimage(doc.render_page(0))
            ok = img.width() > 0 and img.height() > 0
            print_out = os.environ.get("PDF_EDITOR_PRINT_OUT")
            if ok and print_out:
                ok = _print_smoke(doc, print_out)
        result = "OK" if ok else "FAIL: empty output"
    except Exception as exc:  # noqa: BLE001 - report any failure to the result file
        result = f"FAIL: {exc}"
    out = os.environ.get("PDF_EDITOR_SMOKE_OUT")
    if out:
        Path(out).write_text(result, encoding="utf-8")
    print(result, flush=True)
    return 0 if result == "OK" else 1


def _print_smoke(doc: PdfDocument, out_path: str) -> bool:
    """Render the document to a PDF via the print pipeline; return True if written."""
    from PySide6.QtPrintSupport import QPrinter

    from pdfapp.print_support import PrintOptions, render_onto

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(out_path)
    printer.setResolution(150)
    render_onto(printer, doc, PrintOptions())
    return Path(out_path).exists()


def _run_ocr_smoke(pdf_path: str) -> int:
    """Prove the bundled tesseract runtime works in this build (O2).

    The assertion is on the WORD LIST, never on exit codes: tesseract exits 0
    even when a missing config/tessdata file means it produced no output at
    all (spike-verified false green). In a frozen build the resolver uses the
    bundled copy ONLY, so this failing loudly is exactly the bundle check.
    """
    try:
        with PdfDocument.open(Path(pdf_path)) as doc:
            words = doc.ocr_words(0)
        if len(words) >= 5:
            result = f"OCR OK: {len(words)} words"
        else:
            result = f"OCR FAIL: only {len(words)} words recognised"
    except Exception as exc:  # noqa: BLE001 - report any failure to the result file
        result = f"OCR FAIL: {exc}"
    out = os.environ.get("PDF_EDITOR_SMOKE_OUT")
    if out:
        Path(out).write_text(result, encoding="utf-8")
    print(result, flush=True)
    return 0 if result.startswith("OCR OK") else 1


def _apply_theme_and_icon(app: QApplication) -> None:
    """Central theme + app icon. The frozen-build smoke fails loudly here if
    qt-material's data files are missing (apply_theme raises)."""
    theme.apply_theme(app)
    # Frozen-safe resource lookup so the bundled PNG is found in the packaged
    # .exe, not just from source (window title bar + Windows taskbar icon).
    app.setWindowIcon(QIcon(str(resource_path("assets/icon.png"))))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName("PDF Editor")

    smoke_path = os.environ.get("PDF_EDITOR_SMOKE")
    if smoke_path:
        _apply_theme_and_icon(app)
        MainWindow()  # exercise widget construction in the frozen build
        return _run_smoke(smoke_path)

    ocr_smoke_path = os.environ.get("PDF_EDITOR_OCR_SMOKE")
    if ocr_smoke_path:
        _apply_theme_and_icon(app)
        MainWindow()  # widget construction must also work in this mode
        return _run_ocr_smoke(ocr_smoke_path)

    # Files passed on the command line (ignore Qt/option-style args). Opening a
    # PDF from Explorer / "Open with" launches us as `pdf-editor.exe "<file>"`;
    # selecting several files launches ONE such process PER file, all at once.
    file_args = [a for a in argv[1:] if not a.startswith("-")]

    # Single instance FIRST, before the theme or any window: if another instance
    # owns (or is racing to own) the UI, hand it our files — they open as tabs in
    # the ONE window — and exit. Electing the primary up front is what makes a
    # multi-file "Open" land in a single window AND keeps duplicate launches from
    # concurrently rebuilding qt-material's shared cache (that crash is the
    # reported symptom). Opt out with PDF_EDITOR_NO_SINGLE_INSTANCE to run
    # independent windows.
    from pdfapp import single_instance

    use_single_instance = "PDF_EDITOR_NO_SINGLE_INSTANCE" not in os.environ
    instance_server = None
    if use_single_instance:
        instance_server = single_instance.acquire_or_forward(file_args)
        if instance_server is None:
            return 0  # secondary: forwarded to the primary, exit before theming

    # We own the UI (primary, or single-instance disabled): now it is safe to
    # theme and build the window — no other duplicate launch is doing the same.
    _apply_theme_and_icon(app)

    # Crash + hang self-logging (real UI only — the smokes above stay pure).
    # Captures the exact stack to a log file if the app wedges or faults in the
    # wild, since some environment-specific hangs don't reproduce from source.
    from pdfapp import diagnostics

    diagnostics.install(app)

    window = MainWindow()
    # Wire the (already-listening) server to the window now that it exists;
    # any forward that arrived during startup was buffered and replays here.
    if instance_server is not None:
        instance_server.set_handler(window.handle_external_open)
    window.show()

    for arg in file_args:
        window.open_path(Path(arg))

    exit_code = app.exec()
    if instance_server is not None:
        instance_server.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
