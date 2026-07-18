"""Offscreen tests for the span-vs-paragraph double-click sub-mode (U8).

The canvas keeps reporting the raw gesture (pos, ctrl); the view computes
``target = sub_mode XOR ctrl`` so Ctrl is always a momentary override of
whichever default is active. The mode must stay visible: toolbar checked
state + the hover-hint wording names the plain double-click target.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from pdfapp.gestures import hover_hint  # noqa: E402
from pdfapp.main_window import MainWindow  # noqa: E402


def _paragraph_pdf(tmp_path):
    import pymupdf

    path = tmp_path / "para.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 111), "body first line of the paragraph", fontsize=8)
    page.insert_text((72, 119), "body second line with more words", fontsize=8)
    page.insert_text((72, 127), "body third and final line", fontsize=8)
    doc.save(str(path))
    doc.close()
    return path


def _open(window, tmp_path):
    window.open_path(_paragraph_pdf(tmp_path))
    view = window.active_view
    view.set_edit_mode(True)
    z = view._canvas.render_zoom
    return view, (100 * z, 118 * z)  # scene point on the second body line


def test_default_paragraph_mode_and_ctrl_override(qapp, tmp_path):
    window = MainWindow()
    try:
        view, point = _open(window, tmp_path)
        assert view.dblclick_paragraph is True  # paragraph-first default (decided)
        view._on_point_activated(*point)  # plain: the PARAGRAPH editor
        assert view._para_editor.is_editing
        assert view._pending_paragraph is not None
        view._para_editor.cancel()

        view._on_point_activated(*point, True)  # Ctrl: one-line override
        assert view._editor.is_editing
        assert view._pending_paragraph is None
        view._editor.cancel()
    finally:
        window.close()


def test_line_mode_flips_both_targets(qapp, tmp_path):
    window = MainWindow()
    try:
        view, point = _open(window, tmp_path)
        view.set_dblclick_paragraph(False)

        view._on_point_activated(*point)  # plain now edits ONE LINE
        assert view._editor.is_editing
        assert view._pending_paragraph is None
        view._editor.cancel()

        view._on_point_activated(*point, True)  # Ctrl now edits the paragraph
        assert view._para_editor.is_editing
        assert view._pending_paragraph is not None
        view._para_editor.cancel()
    finally:
        window.close()


def test_dblclick_on_blank_space_inside_the_box_starts_the_edit(qapp, tmp_path):
    """The displayed outline is the paragraph's UNION bbox — a double-click
    on whitespace inside it (past a short line's end) must start the edit,
    not silently do nothing."""
    from pdfapp.page_coords import page_to_scene

    window = MainWindow()
    try:
        view, _point = _open(window, tmp_path)
        spans = view.document.text_spans(0)
        long_line = next(s for s in spans if "second line" in s.text)
        short_line = next(s for s in spans if "third and final" in s.text)
        # Inside the union box, but past the short line's right edge: no
        # span or text line under the point.
        px = long_line.bbox[2] - 2.0
        py = (short_line.bbox[1] + short_line.bbox[3]) / 2
        assert px > short_line.bbox[2]
        point = page_to_scene(
            px,
            py,
            render_zoom=view._canvas.render_zoom,
            rotation=0,
            page_size_pts=view.document.page_size(0),
        )

        view._on_point_activated(*point)  # default: paragraph editor opens
        assert view._para_editor.is_editing
        assert view._pending_paragraph is not None
        view._para_editor.cancel()

        view._on_point_activated(*point, True)  # Ctrl: nearest LINE opens
        assert view._editor.is_editing
        assert view._editor.text() == short_line.text
        view._editor.cancel()
    finally:
        window.close()


def test_rotated_text_opens_span_editor_in_both_modes(qapp, real_cad_pdf):
    """Rotated CAD dimensions are single-line entities: whichever sub-mode
    is active, double-click opens the SPAN editor in a horizontal box."""
    from pdfapp.page_coords import page_to_scene

    window = MainWindow()
    try:
        window.open_path(real_cad_pdf)
        view = window.active_view
        view.set_edit_mode(True)
        span = next(s for s in view.document.text_spans(0) if s.text == "30" and s.rotation == 90)
        cx = (span.bbox[0] + span.bbox[2]) / 2
        cy = (span.bbox[1] + span.bbox[3]) / 2
        point = page_to_scene(
            cx,
            cy,
            render_zoom=view._canvas.render_zoom,
            rotation=0,
            page_size_pts=view.document.page_size(0),
        )
        view._on_point_activated(*point)  # paragraph-first default — still the span
        assert view._editor.is_editing
        assert view._editor.text() == "30"
        assert view._editor.width() > view._editor.height()  # horizontal box
        view._editor.cancel()

        view._on_point_activated(*point, True)  # Ctrl override — same routing
        assert view._editor.is_editing
        assert view._editor.text() == "30"
        view._editor.cancel()
    finally:
        window.close()


def test_hint_wording_names_the_plain_target(qapp, tmp_path):
    window = MainWindow()
    try:
        view, point = _open(window, tmp_path)
        assert "double-click edits ·" in hover_hint("text", False)
        assert "double-click edits the paragraph" in hover_hint("text", True)
        assert "Ctrl+double-click edits one line" in hover_hint("text", True)
        assert hover_hint("text") == hover_hint("text", True)  # default arg = app default

        # A live text hint refreshes immediately when the sub-mode flips.
        view._on_hover_moved(*point)
        assert window.statusBar().currentMessage() == hover_hint("text", True)
        view.set_dblclick_paragraph(False)
        assert window.statusBar().currentMessage() == hover_hint("text", False)
    finally:
        window.close()


def test_action_indicates_and_gates_per_document(qapp, tmp_path, quote_pdf):
    window = MainWindow()
    try:
        view, _point = _open(window, tmp_path)
        assert window._dblclick_para_action.isEnabled()
        assert window._dblclick_para_action.isChecked()  # paragraph-first default
        window._dblclick_para_action.setChecked(False)
        assert view.dblclick_paragraph is False

        window.open_path(quote_pdf.path)  # second tab: read-only, its own default
        assert not window._dblclick_para_action.isEnabled()
        assert window._dblclick_para_action.isChecked()  # shows the tab's own state

        window._tabs.setCurrentWidget(view)
        assert not window._dblclick_para_action.isChecked()
    finally:
        window.close()
