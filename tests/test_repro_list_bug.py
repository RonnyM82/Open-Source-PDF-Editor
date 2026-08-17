"""TEMPORARY repro for the reported 'Format as list has zero effect' bug.

Drives the REAL app path: paragraphs resolved through page_geometry (with
registry boundaries), dispatch via _apply_list_style — exactly what the
context menu does — on (a) a native paragraph and (b) a registered inserted
text box.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from pdfapp.main_window import MainWindow  # noqa: E402


def _page_text(view) -> str:
    return view.document._doc[0].get_text()


def _blank_view(window, tmp_path):
    import pymupdf

    path = tmp_path / "repro.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(str(path))
    doc.close()
    window.open_path(path)
    view = window.active_view
    view.set_edit_mode(True)
    view._canvas.resize(800, 600)
    return view


def _native_view(window, tmp_path):
    import pymupdf

    path = tmp_path / "native.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 100), "A native paragraph of text", fontname="helv", fontsize=11)
    doc.save(str(path))
    doc.close()
    window.open_path(path)
    view = window.active_view
    view.set_edit_mode(True)
    view._canvas.resize(800, 600)
    return view


def _geom_para(view, needle):
    return next(p for p in view.page_geometry(0).paragraphs if needle in p.text)


def test_format_native_paragraph_via_geometry(qapp, tmp_path):
    window = MainWindow()
    try:
        view = _native_view(window, tmp_path)
        para = _geom_para(view, "native paragraph")
        before = _page_text(view)
        view._apply_list_style("bullet", 0, [para])
        after = _page_text(view)
        print("NATIVE BEFORE:", repr(before))
        print("NATIVE AFTER :", repr(after))
        cmd_errors = [
            view._undo_stack.command(i).error
            for i in range(view._undo_stack.count())
            if hasattr(view._undo_stack.command(i), "error")
        ]
        print("command errors:", cmd_errors)
        assert after != before, "zero effect on a NATIVE paragraph"
    finally:
        window.close()


def test_format_registered_inserted_box_via_geometry(qapp, tmp_path):
    window = MainWindow()
    try:
        view = _blank_view(window, tmp_path)
        view._pending_insert = (0, (100.0, 200.0))
        view._on_paragraph_committed("typed box text")
        para = _geom_para(view, "typed box")
        before = _page_text(view)
        view._apply_list_style("bullet", 0, [para])
        after = _page_text(view)
        print("BOX BEFORE:", repr(before))
        print("BOX AFTER :", repr(after))
        cmd_errors = [
            getattr(view._undo_stack.command(i), "error", None)
            for i in range(view._undo_stack.count())
        ]
        print("command errors:", cmd_errors)
        assert after != before, "zero effect on a REGISTERED inserted box"
    finally:
        window.close()


def test_format_multiline_native_block_via_geometry(qapp, tmp_path):
    """A wrapped multi-line native paragraph — closer to real prose."""
    import pymupdf

    window = MainWindow()
    try:
        path = tmp_path / "multi.pdf"
        doc = pymupdf.open()
        page = doc.new_page()
        for i, line in enumerate(("First line of the block that runs on", "and wraps here")):
            page.insert_text((100, 100 + i * 13.2), line, fontname="helv", fontsize=11)
        doc.save(str(path))
        doc.close()
        window.open_path(path)
        view = window.active_view
        view.set_edit_mode(True)
        view._canvas.resize(800, 600)
        para = _geom_para(view, "First line")
        before = _page_text(view)
        view._apply_list_style("number", 0, [para])
        after = _page_text(view)
        print("MULTI BEFORE:", repr(before))
        print("MULTI AFTER :", repr(after))
        assert after != before, "zero effect on a multi-line native block"
    finally:
        window.close()
