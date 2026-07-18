"""Offscreen tests for file-level UI operations: merge (M10), split (M11)."""

from __future__ import annotations

import pymupdf
import pytest

pytest.importorskip("PySide6")

from pdfapp.main_window import MainWindow, _parse_page_ranges  # noqa: E402


def _make_pdf(path, labels):
    doc = pymupdf.open()
    for label in labels:
        doc.new_page().insert_text((72, 72), label, fontsize=24)
    doc.save(str(path))
    doc.close()


def test_merge_files_produces_and_opens_result(qapp, tmp_path):
    a = tmp_path / "a.pdf"
    _make_pdf(a, ["A0", "A1"])
    b = tmp_path / "b.pdf"
    _make_pdf(b, ["B0"])
    out = tmp_path / "merged.pdf"

    window = MainWindow()
    try:
        window._merge_files([a, b], out)
        assert out.exists()
        # The merged result is opened in the window.
        assert window.active_view.page_count == 3
        assert window.active_view._thumbnails.count() == 3
    finally:
        window.close()


def test_split_file_creates_parts(qapp, tmp_path):
    src = tmp_path / "src.pdf"
    _make_pdf(src, ["P0", "P1", "P2", "P3"])
    out_dir = tmp_path / "parts"

    window = MainWindow()
    try:
        window._split_file(src, [(0, 1), (2, 3)], out_dir)
        created = sorted(out_dir.glob("*.pdf"))
        assert len(created) == 2
    finally:
        window.close()


def test_parse_page_ranges_basic():
    assert _parse_page_ranges("1-3, 4, 5-8") == [(0, 2), (3, 3), (4, 7)]


def test_parse_page_ranges_single_page():
    assert _parse_page_ranges("2") == [(1, 1)]


def test_parse_page_ranges_rejects_empty():
    with pytest.raises(ValueError):
        _parse_page_ranges("   ")


def test_parse_page_ranges_rejects_non_integer():
    with pytest.raises(ValueError):
        _parse_page_ranges("a-b")
