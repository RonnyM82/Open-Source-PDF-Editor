"""Offscreen tests for overlap-robust box ownership (task 5 Level 1).

Two inserted text boxes moved so their registry rects overlap must keep their
own content — the content fingerprint disambiguates them, so grouping no
longer mangles when boxes overlap while editing.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

import pymupdf  # noqa: E402

from pdfapp.main_window import MainWindow  # noqa: E402


def _blank(tmp_path):
    path = tmp_path / "blank.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "anchor", fontsize=10)  # keeps the page non-empty
    doc.save(str(path))
    doc.close()
    return path


def _insert(view, x, y, text):
    """Insert a text box via the real commit path (registers a box)."""
    view._pending_insert = (0, (x, y))
    view._on_paragraph_committed(text)


def _para_with(view, needle):
    return next(p for p in view.page_geometry(0).paragraphs if needle in p.text)


def test_two_inserted_boxes_stay_separate_when_moved_to_overlap(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_blank(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)

        _insert(view, 100, 200, "ALPHA box text")
        _insert(view, 100, 400, "BETA box text")
        assert len(view.document.boxes(0)) == 2

        # Move BETA up so it lands right on top of ALPHA (rects overlap).
        beta = _para_with(view, "BETA")
        alpha = _para_with(view, "ALPHA")
        # Offset that puts beta's baseline ~5pt below alpha's (heavy overlap).
        dx = alpha.bbox[0] - beta.bbox[0]
        dy = (alpha.bbox[1] + 6) - beta.bbox[1]
        view._apply_box_offsets(0, [(beta, (dx, dy))], "Move text")

        # Both boxes still exist and each resolves to its OWN content.
        paras = view.page_geometry(0).paragraphs
        texts = {p.text for p in paras}
        assert "ALPHA box text" in texts
        assert "BETA box text" in texts
        # Neither absorbed the other (no merged 2-line paragraph).
        assert not any("ALPHA" in p.text and "BETA" in p.text for p in paras)
        assert len(view.document.boxes(0)) == 2
    finally:
        window.close()


def test_moved_box_over_existing_text_does_not_absorb_it(qapp, tmp_path):
    """An inserted box moved over PRE-EXISTING page text must not swallow it."""
    path = tmp_path / "existing.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 200), "preexisting one", fontsize=9)
    page.insert_text((100, 212), "preexisting two", fontsize=9)
    doc.save(str(path))
    doc.close()

    window = MainWindow()
    try:
        window.open_path(path)
        view = window.active_view
        view.set_edit_mode(True)

        _insert(view, 100, 400, "floating label")
        label = _para_with(view, "floating")
        # Move the label up onto the existing paragraph.
        dy = 205 - label.bbox[1]
        view._apply_box_offsets(0, [(label, (0.0, dy))], "Move text")

        paras = view.page_geometry(0).paragraphs
        # The existing 2-line paragraph is intact and separate from the label.
        existing = next(p for p in paras if "preexisting one" in p.text)
        assert "preexisting two" in existing.text
        assert "floating" not in existing.text
        assert any(p.text == "floating label" for p in paras)
    finally:
        window.close()


def test_editing_a_box_updates_its_fingerprint(qapp, tmp_path):
    """After an edit changes a box's content, its stored fingerprint follows —
    so a later overlap still disambiguates by the NEW text."""
    window = MainWindow()
    try:
        window.open_path(_blank(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        _insert(view, 100, 200, "original words")
        box_id = view.document.boxes(0)[0].id

        para = _para_with(view, "original")
        view._begin_paragraph_edit(0, para)
        view._para_editor.setPlainText("replacement words")
        view._para_editor.commit()

        boxes = view.document.boxes(0)
        assert len(boxes) == 1
        assert boxes[0].id == box_id  # identity stable
        assert "replacement" in boxes[0].text  # fingerprint updated
    finally:
        window.close()
