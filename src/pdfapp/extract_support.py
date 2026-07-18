"""Extract Text support: section collection + the output dialog (X1/X2).

READ-ONLY feature — nothing here mutates the document. Section collection is
plain Python over the engine's routing seam (`PdfDocument.page_text`); OCR
words arrive through a caller-supplied lookup so the slow work and its cache
stay under the caller's control (DocumentView owns the OcrWordCache).

The dialog follows the house split-builder pattern (print_support): build and
wire without exec'ing, so offscreen tests can drive it without a modal event
loop.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
)

from pdfapp.ocr_cache import OcrWordCache
from pdfcore.document import PdfDocument
from pdfcore.ocr import OcrWord, TesseractNotFound
from pdfcore.textsource import PageText

# Warn before OCR'ing at least this many pages (~2 s each — beyond this the
# run is long enough that the user should opt in). Declining skips OCR; the
# extraction still runs with native text and honest empty-page labels.
BULK_OCR_WARN_AT = 10

SCOPE_WHOLE = "whole"
SCOPE_CURRENT = "current"


def collect_sections(
    doc: PdfDocument,
    page_indices: Sequence[int],
    ocr_words_for: Callable[[int], list[OcrWord] | None] | None = None,
    *,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[tuple[int, PageText]], bool]:
    """Route each page's text; returns ``(sections, cancelled)``.

    ``ocr_words_for`` is consulted ONLY for pages without a text layer (so a
    cache-backed callable never OCRs a native page). ``should_cancel`` is
    checked between pages; a cancelled run returns the sections collected so
    far with ``cancelled=True``.
    """
    sections: list[tuple[int, PageText]] = []
    total = len(page_indices)
    for done, n in enumerate(page_indices):
        if should_cancel is not None and should_cancel():
            return sections, True
        if on_progress is not None:
            on_progress(done, total)
        words = None
        if ocr_words_for is not None and not doc.has_text_layer(n):
            words = ocr_words_for(n)
        sections.append((n, doc.page_text(n, ocr_words=words)))
    return sections, False


@dataclass(frozen=True)
class BulkOcrResult:
    """What a bulk OCR run produced (possibly partial)."""

    words_by_page: dict[int, list[OcrWord]]
    cancelled: bool
    tesseract_missing: bool


def confirm_bulk_ocr(parent, page_count: int) -> bool:
    """Ask before a long OCR run. Monkeypatch seam for tests.

    Declining skips OCR — the extraction still proceeds with native text and
    honest "not attempted" labels; it never aborts outright.
    """
    answer = QMessageBox.question(
        parent,
        "Extract text",
        f"{page_count} page(s) have no text layer and need OCR "
        f"(roughly {2 * page_count} seconds). Run OCR now?",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.Yes,
    )
    return answer == QMessageBox.StandardButton.Yes


def run_bulk_ocr(
    parent, doc: PdfDocument, pages: Sequence[int], cache: OcrWordCache, *, label: str
) -> BulkOcrResult:
    """OCR ``pages`` through the shared cache with a cancellable progress bar.

    ``wasCanceled`` is checked between pages (page-granular cancel — no
    threading in this app). ``TesseractNotFound`` is caught ONCE: attempts
    stop and the partial result is returned with ``tesseract_missing=True``
    (the caller warns; already-cached pages still count).
    """
    progress = QProgressDialog(label, "Cancel", 0, len(pages), parent)
    progress.setWindowModality(Qt.WindowModality.WindowModal)
    progress.setMinimumDuration(400)
    words: dict[int, list[OcrWord]] = {}
    cancelled = False
    missing = False
    try:
        for done, n in enumerate(pages):
            progress.setValue(done)
            if progress.wasCanceled():
                cancelled = True
                break
            try:
                words[n] = cache.words(doc, n)
            except TesseractNotFound:
                missing = True
                break
        progress.setValue(len(pages))
    finally:
        progress.close()
    return BulkOcrResult(words_by_page=words, cancelled=cancelled, tesseract_missing=missing)


class ExtractTextDialog(QDialog):
    """The extracted-text viewer: scope picker + read-only text + Copy/Save.

    The dialog owns no extraction logic: ``runner(scope)`` (supplied by
    MainWindow) produces the text for a scope, so switching the combo re-runs
    the extraction with any OCR progress parented here. ``copy_all`` and
    ``save_to`` are plain methods so offscreen tests drive them without the
    clipboard button or the file dialog.
    """

    def __init__(
        self,
        parent,
        title: str,
        runner: Callable[[str], str],
        *,
        initial_scope: str = SCOPE_WHOLE,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self._runner = runner
        self.scope_combo = QComboBox(self)
        self.scope_combo.addItem("Whole document", SCOPE_WHOLE)
        self.scope_combo.addItem("Current page", SCOPE_CURRENT)
        self.scope_combo.setCurrentIndex(0 if initial_scope == SCOPE_WHOLE else 1)
        # Size to the longest option so neither the control nor its dropdown
        # truncates (the dropdown list matches the control's width).
        self.scope_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Scope:", self))
        scope_row.addWidget(self.scope_combo)
        scope_row.addStretch(1)
        self.text_edit = QPlainTextEdit(self)
        self.text_edit.setPlainText(runner(initial_scope))
        self.text_edit.setReadOnly(True)
        # Connected AFTER the initial run so construction fires it once only.
        self.scope_combo.currentIndexChanged.connect(self._rerun)
        self.copy_button = QPushButton("Copy all", self)
        self.copy_button.clicked.connect(self.copy_all)
        self.save_button = QPushButton("Save as .txt…", self)
        self.save_button.clicked.connect(self._save_via_dialog)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.addButton(self.copy_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.save_button, QDialogButtonBox.ButtonRole.ActionRole)
        layout = QVBoxLayout(self)
        layout.addLayout(scope_row)
        layout.addWidget(self.text_edit)
        layout.addWidget(buttons)
        self.resize(680, 560)

    def _rerun(self) -> None:
        self.text_edit.setPlainText(self._runner(self.scope_combo.currentData()))

    def copy_all(self) -> None:
        QApplication.clipboard().setText(self.text_edit.toPlainText())

    def save_to(self, path: Path | str) -> None:
        Path(path).write_text(self.text_edit.toPlainText(), encoding="utf-8")

    def _save_via_dialog(self) -> None:
        out, _ = QFileDialog.getSaveFileName(self, "Save extracted text", "", "Text files (*.txt)")
        if out:
            self.save_to(out)


def build_extract_dialog(
    parent, title: str, runner: Callable[[str], str], *, initial_scope: str = SCOPE_WHOLE
) -> ExtractTextDialog:
    """Wired-but-not-exec'd dialog (offscreen tests drive it directly)."""
    return ExtractTextDialog(parent, title, runner, initial_scope=initial_scope)
