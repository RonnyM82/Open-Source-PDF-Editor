"""Offscreen tests for drag-and-drop open.

Dropping a PDF onto the window opens it through the SAME entry point as
File > Open (`open_path`): local `.pdf` URLs only, dedup against open tabs.

Lifetime note: QDragEnterEvent/QDropEvent hold a BORROWED pointer to the
QMimeData, so every test keeps the `mime` in a local alive until it is done —
letting it be garbage-collected leaves the event with a dangling pointer and
crashes the interpreter (access violation), not a clean failure.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QDragEnterEvent, QDropEvent  # noqa: E402

from pdfapp.main_window import MainWindow, _dropped_pdf_paths  # noqa: E402
from pdfapp.page_canvas import PageCanvas  # noqa: E402


def _mime_for(*paths) -> QMimeData:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    return mime


def _drag_enter(mime: QMimeData) -> QDragEnterEvent:
    return QDragEnterEvent(
        QPoint(10, 10),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )


def _drop(mime: QMimeData) -> QDropEvent:
    return QDropEvent(
        QPointF(10, 10),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )


# --- the pure path filter ----------------------------------------------------


def test_dropped_pdf_paths_keeps_only_local_pdfs(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    txt = tmp_path / "note.txt"
    txt.write_text("nope")

    mime = QMimeData()
    mime.setUrls(
        [
            QUrl.fromLocalFile(str(pdf)),
            QUrl.fromLocalFile(str(txt)),  # wrong suffix
            QUrl("https://example.com/remote.pdf"),  # not a local file
        ]
    )
    assert _dropped_pdf_paths(mime) == [pdf]


def test_dropped_pdf_paths_suffix_is_case_insensitive(tmp_path):
    pdf = tmp_path / "A.PDF"
    pdf.write_bytes(b"%PDF-1.4")
    assert _dropped_pdf_paths(_mime_for(pdf)) == [pdf]


def test_dropped_pdf_paths_preserves_order(tmp_path):
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    for p in (first, second):
        p.write_bytes(b"%PDF-1.4")
    assert _dropped_pdf_paths(_mime_for(first, second)) == [first, second]


def test_dropped_pdf_paths_empty_without_urls():
    mime = QMimeData()
    mime.setText("just some text")
    assert _dropped_pdf_paths(mime) == []


# --- the window accepts drops and opens them ---------------------------------


def test_window_accepts_drops(qapp):
    window = MainWindow()
    try:
        assert window.acceptDrops() is True
    finally:
        window.close()


def test_drag_enter_accepts_pdf(qapp, text_pdf):
    window = MainWindow()
    try:
        mime = _mime_for(text_pdf)
        event = _drag_enter(mime)
        window.dragEnterEvent(event)
        assert event.isAccepted()
    finally:
        window.close()


def test_drag_enter_ignores_non_pdf(qapp, tmp_path):
    window = MainWindow()
    try:
        txt = tmp_path / "note.txt"
        txt.write_text("nope")
        mime = _mime_for(txt)
        event = _drag_enter(mime)
        window.dragEnterEvent(event)
        assert not event.isAccepted()
    finally:
        window.close()


def test_drop_opens_pdf(qapp, text_pdf):
    window = MainWindow()
    try:
        assert window.active_view is None
        mime = _mime_for(text_pdf)
        event = _drop(mime)
        window.dropEvent(event)
        assert event.isAccepted()
        assert window.active_view is not None
        assert window.active_view.path == text_pdf
    finally:
        window.close()


def test_drop_opens_multiple_pdfs(qapp, text_pdf, multipage_pdf):
    window = MainWindow()
    try:
        mime = _mime_for(text_pdf, multipage_pdf)
        window.dropEvent(_drop(mime))
        paths = {v.path for v in window._views()}
        assert paths == {text_pdf, multipage_pdf}
    finally:
        window.close()


def test_drop_same_file_focuses_existing_tab(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        mime = _mime_for(text_pdf)
        window.dropEvent(_drop(mime))
        # Dedup is inherited from open_path: the second drop reuses the tab.
        assert len(window._views()) == 1
    finally:
        window.close()


# --- the open page must not swallow drops ------------------------------------
# QGraphicsView enables acceptDrops by default; without opting out, the canvas
# intercepts a file drop over the open page and refuses it (the scene has no
# drop handler), so it never propagates up to MainWindow. This was the reported
# bug: with a PDF open, only the toolbar area accepted drops.


def test_page_canvas_does_not_accept_drops(qapp):
    canvas = PageCanvas()
    assert canvas.acceptDrops() is False
    # The viewport is the widget actually under the cursor; it must opt out too.
    assert canvas.viewport().acceptDrops() is False


def test_open_document_canvas_does_not_block_drops(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        canvas = window.active_view._canvas
        assert canvas.acceptDrops() is False
    finally:
        window.close()
