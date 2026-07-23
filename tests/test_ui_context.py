"""Offscreen tests for the U3 context-menu dispatch paths.

The menu itself execs only when the view is visible (existing pattern); the
dispatch methods it routes to are tested directly, like the E9.2 image menu.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from pdfapp.main_window import MainWindow  # noqa: E402
from pdfapp.page_coords import page_to_scene  # noqa: E402


def _scene_point(view, px, py):
    return page_to_scene(
        px,
        py,
        render_zoom=view._canvas.render_zoom,
        rotation=view.document.page_rotation(0),
        page_size_pts=view.document.page_size(0),
    )


def test_insert_text_at_point_opens_editor_there(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view._insert_text_at_point(*_scene_point(view, 300.0, 400.0))
        assert view._para_editor.is_editing
        page_index, point = view._pending_insert
        assert page_index == 0
        assert point[0] == pytest.approx(300.0, abs=0.5)
        assert point[1] == pytest.approx(400.0, abs=0.5)
        view._para_editor.cancel()
    finally:
        window.close()


def test_insert_image_at_point_places_it(qapp, quote_pdf, sample_png, monkeypatch):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        monkeypatch.setattr(view, "_prompt_image_path", lambda: sample_png)
        assert len(view.document.images(0)) == 1
        view._insert_image_at_point(0, 300.0, 400.0)
        images = view.document.images(0)
        assert len(images) == 2
        placed = min(images, key=lambda i: abs(i.bbox[0] - 300.0))
        assert placed.bbox[0] == pytest.approx(300.0, abs=1.0)
        view.undo_stack.undo()
        assert len(view.document.images(0)) == 1
    finally:
        window.close()


def test_highlight_rect_adds_annotation_one_undo_step(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        span = next(s for s in view.document.text_spans(0) if s.text.strip() == quote_pdf.price)
        assert len(list(view.document._doc[0].annots())) == 0
        view._highlight_rect(0, span.bbox)
        assert len(list(view.document._doc[0].annots())) == 1
        assert view.undo_stack.count() == 1
        view.undo_stack.undo()
        assert len(list(view.document._doc[0].annots())) == 0
    finally:
        window.close()


def test_delete_text_box_from_context_menu(qapp, quote_pdf):
    """User request (2026-07-18): delete a whole text block from the context
    menu instead of opening the editor and emptying it — one undoable step."""
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        span = next(s for s in view.document.text_spans(0) if s.text.strip() == quote_pdf.price)
        cx = (span.bbox[0] + span.bbox[2]) / 2
        cy = (span.bbox[1] + span.bbox[3]) / 2
        para = view.document.paragraph_at(0, cx, cy)
        assert para is not None

        view._delete_paragraph_at(0, para)
        assert view.document.paragraph_at(0, cx, cy) is None
        assert all(s.text.strip() != quote_pdf.price for s in view.document.text_spans(0))
        assert view.undo_stack.count() == 1

        view.undo_stack.undo()
        assert any(s.text.strip() == quote_pdf.price for s in view.document.text_spans(0))
    finally:
        window.close()


def test_delete_text_box_dissolves_registry_box(qapp, quote_pdf):
    """Deleting an INSERTED text box drops its registry record with it (the
    same one-command atomicity as an emptied editor commit)."""
    from pdfapp.page_geometry import hover_target

    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view._pending_insert = (0, (300.0, 500.0))
        view._on_paragraph_committed("BOXED words")
        assert len(view.document.boxes(0)) == 1

        target = hover_target(view.page_geometry(0), 305.0, 498.0)
        assert target is not None and target.kind == "text"
        view._delete_paragraph_at(0, target.payload)
        assert view.document.boxes(0) == []
        assert all("BOXED" not in s.text for s in view.document.text_spans(0))

        view.undo_stack.undo()  # the delete only — insert stays undone/redone apart
        assert len(view.document.boxes(0)) == 1
        assert any("BOXED" in s.text for s in view.document.text_spans(0))
    finally:
        window.close()


def _para_for(view, text):
    span = next(s for s in view.document.text_spans(0) if s.text.strip() == text)
    return view.document.paragraph_at(
        0, (span.bbox[0] + span.bbox[2]) / 2, (span.bbox[1] + span.bbox[3]) / 2
    )


def test_duplicate_text_box_copies_it_below_the_original(qapp, quote_pdf):
    """User request (2026-07-23): right-click → Duplicate text box. ADDITIVE —
    the original survives untouched and the copy lands below it."""
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        para = _para_for(view, quote_pdf.price)
        assert para is not None

        view._duplicate_paragraph_at(0, para)
        copies = [s for s in view.document.text_spans(0) if s.text.strip() == quote_pdf.price]
        assert len(copies) == 2  # original + copy
        original = min(copies, key=lambda s: s.origin[1])
        copy = max(copies, key=lambda s: s.origin[1])
        assert original.origin == pytest.approx(para.first_origin, abs=0.5)  # untouched
        assert copy.origin[1] > original.bbox[3]  # clear of the original
        assert copy.origin[0] == pytest.approx(para.bbox[0], abs=0.5)  # same left edge
        assert view.undo_stack.count() == 1

        view.undo_stack.undo()
        remaining = [s for s in view.document.text_spans(0) if s.text.strip() == quote_pdf.price]
        assert len(remaining) == 1
    finally:
        window.close()


def test_duplicate_text_box_registers_its_own_box(qapp, quote_pdf):
    """The copy sits one pitch from its source — exactly what MuPDF blocks
    into ONE paragraph. Its registry box is what keeps them two separate,
    independently editable text boxes."""
    from pdfapp.page_geometry import hover_target

    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        para = _para_for(view, quote_pdf.price)
        view._duplicate_paragraph_at(0, para)

        assert len(view.document.boxes(0)) == 1
        copy = max(
            (s for s in view.document.text_spans(0) if s.text.strip() == quote_pdf.price),
            key=lambda s: s.origin[1],
        )
        # Hit-tested the way the app does it (page_geometry feeds the registry
        # boundaries in): the copy resolves as its OWN paragraph.
        cx = (copy.bbox[0] + copy.bbox[2]) / 2
        target = hover_target(view.page_geometry(0), cx, copy.origin[1] - 2)
        assert target is not None and target.kind == "text"
        assert target.payload.text.strip() == quote_pdf.price  # the copy alone

        view.undo_stack.undo()  # content and registry undo together (E10)
        assert view.document.boxes(0) == []
    finally:
        window.close()


def test_duplicate_text_box_keeps_styling_and_pitch(qapp, tmp_path):
    """A copy must look like its original: per-word styles survive and the
    tight line pitch is reproduced (the 1.2-em default would space it out)."""
    import pymupdf

    path = tmp_path / "styled.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 300), "bold heading line", fontname="hebo", fontsize=9)
    page.insert_text((72, 309), "plain follower line", fontsize=9)
    doc.save(str(path))
    doc.close()

    window = MainWindow()
    try:
        window.open_path(path)
        view = window.active_view
        view.set_edit_mode(True)
        para = view.document.paragraph_at(0, 100, 307)
        assert para is not None and len(para.lines) == 2

        view._duplicate_paragraph_at(0, para)
        spans = view.document.text_spans(0)
        copies = sorted(
            (s for s in spans if "bold heading" in s.text or "plain follower" in s.text),
            key=lambda s: s.origin[1],
        )
        assert len(copies) == 4  # two originals + two copies
        copy_head, copy_body = copies[2], copies[3]
        assert copy_head.base14 == "hebo"  # bold survived
        assert copy_body.base14 == "helv"
        assert copy_body.origin[1] - copy_head.origin[1] == pytest.approx(para.pitch, abs=0.2)
    finally:
        window.close()


def test_duplicate_of_a_bottom_paragraph_goes_above_it(qapp, tmp_path):
    """No room below: the copy stacks ABOVE instead of running off the page."""
    import pymupdf

    path = tmp_path / "bottom.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    height = page.rect.height
    page.insert_text((72, height - 6), "the very last line", fontsize=9)
    doc.save(str(path))
    doc.close()

    window = MainWindow()
    try:
        window.open_path(path)
        view = window.active_view
        view.set_edit_mode(True)
        para = view.document.paragraph_at(0, 100, height - 8)
        assert para is not None

        view._duplicate_paragraph_at(0, para)
        copies = [s for s in view.document.text_spans(0) if "very last line" in s.text]
        assert len(copies) == 2
        assert min(s.origin[1] for s in copies) < para.first_origin[1]  # placed above
    finally:
        window.close()


def test_context_dispatch_methods_gate_on_read_only(qapp, quote_pdf, sample_png, monkeypatch):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        monkeypatch.setattr(view, "_prompt_image_path", lambda: sample_png)
        view._insert_text_at_point(*_scene_point(view, 300.0, 400.0))
        assert not view._para_editor.is_editing
        view._insert_image_at_point(0, 300.0, 400.0)
        assert len(view.document.images(0)) == 1  # unchanged
        span = next(s for s in view.document.text_spans(0) if s.text.strip() == quote_pdf.price)
        para = view.document.paragraph_at(
            0, (span.bbox[0] + span.bbox[2]) / 2, (span.bbox[1] + span.bbox[3]) / 2
        )
        view._delete_paragraph_at(0, para)  # read-only: inert
        view._duplicate_paragraph_at(0, para)  # ditto — a CONTENT op
        assert view.undo_stack.count() == 0
        priced = [s for s in view.document.text_spans(0) if s.text.strip() == quote_pdf.price]
        assert len(priced) == 1
    finally:
        window.close()
