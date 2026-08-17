"""Round-trip tests for page-manipulation ops (open -> operate -> save -> reopen).

One module for all pdfcore.pages operations; grows one milestone at a time.
"""

from __future__ import annotations

import pymupdf
import pytest

from pdfcore import pages
from pdfcore.document import PdfDocument


def _rotations(path):
    doc = pymupdf.open(str(path))
    try:
        return [doc[i].rotation for i in range(doc.page_count)]
    finally:
        doc.close()


def _page_texts(path):
    doc = pymupdf.open(str(path))
    try:
        return [doc[i].get_text().strip() for i in range(doc.page_count)]
    finally:
        doc.close()


# --- M6: rotate ---------------------------------------------------------


def test_rotate_roundtrip(multipage_pdf, tmp_path):
    out = tmp_path / "rotated.pdf"
    with PdfDocument.open(multipage_pdf) as doc:
        doc.rotate([1, 3], 90)
        doc.save(out)
    assert _rotations(out) == [0, 90, 0, 90, 0]


def test_rotate_is_relative_and_normalized(multipage_pdf, tmp_path):
    out = tmp_path / "rotated.pdf"
    with PdfDocument.open(multipage_pdf) as doc:
        doc.rotate([0], 270)
        doc.rotate([0], 180)  # 270 + 180 = 450 -> 90
        doc.save(out)
    assert _rotations(out)[0] == 90


def test_rotate_rejects_non_multiple_of_90(multipage_pdf):
    with PdfDocument.open(multipage_pdf) as doc:
        with pytest.raises(ValueError):
            doc.rotate([0], 45)


# --- M7: delete ---------------------------------------------------------


def test_delete_roundtrip(multipage_pdf, page_marker, tmp_path):
    out = tmp_path / "deleted.pdf"
    with PdfDocument.open(multipage_pdf) as doc:
        doc.delete([1, 3])
        doc.save(out)
    assert _page_texts(out) == [page_marker(0), page_marker(2), page_marker(4)]


def test_delete_all_pages_refused(multipage_pdf):
    with PdfDocument.open(multipage_pdf) as doc:
        with pytest.raises(ValueError):
            doc.delete([0, 1, 2, 3, 4])


# --- M8: reorder --------------------------------------------------------


def test_reorder_roundtrip(multipage_pdf, page_marker, tmp_path):
    out = tmp_path / "reordered.pdf"
    with PdfDocument.open(multipage_pdf) as doc:
        doc.reorder([4, 3, 2, 1, 0])
        doc.save(out)
    assert _page_texts(out) == [page_marker(i) for i in (4, 3, 2, 1, 0)]


def test_reorder_rejects_incomplete_permutation(multipage_pdf):
    with PdfDocument.open(multipage_pdf) as doc:
        with pytest.raises(ValueError):
            doc.reorder([0, 1, 2])  # missing pages


def test_reorder_rejects_duplicates(multipage_pdf):
    with PdfDocument.open(multipage_pdf) as doc:
        with pytest.raises(ValueError):
            doc.reorder([0, 0, 1, 2, 3])


# --- M9: insert_from ----------------------------------------------------


def _make_pdf(path, labels):
    doc = pymupdf.open()
    for label in labels:
        doc.new_page().insert_text((72, 72), label, fontsize=24)
    doc.save(str(path))
    doc.close()


def test_insert_from_roundtrip(multipage_pdf, page_marker, tmp_path):
    src = tmp_path / "src.pdf"
    _make_pdf(src, ["SRC-A", "SRC-B"])
    out = tmp_path / "inserted.pdf"
    with PdfDocument.open(multipage_pdf) as doc:
        doc.insert_from(src, at=2)
        doc.save(out)
    assert _page_texts(out) == [
        page_marker(0),
        page_marker(1),
        "SRC-A",
        "SRC-B",
        page_marker(2),
        page_marker(3),
        page_marker(4),
    ]


def test_insert_from_page_range(multipage_pdf, tmp_path):
    src = tmp_path / "src.pdf"
    _make_pdf(src, ["SRC-0", "SRC-1", "SRC-2"])
    out = tmp_path / "range.pdf"
    with PdfDocument.open(multipage_pdf) as doc:
        before = doc.page_count
        doc.insert_from(src, at=0, from_page=1, to_page=1)  # only SRC-1
        doc.save(out)
    texts = _page_texts(out)
    assert texts[0] == "SRC-1"
    assert len(texts) == before + 1


def test_insert_blank_roundtrip(multipage_pdf, page_marker, tmp_path):
    out = tmp_path / "blank.pdf"
    with PdfDocument.open(multipage_pdf) as doc:
        width, height = doc.page_size(1)
        doc.insert_blank(2, width, height)
        doc.save(out)
    assert _page_texts(out) == [
        page_marker(0),
        page_marker(1),
        "",
        page_marker(2),
        page_marker(3),
        page_marker(4),
    ]


def test_extract_uses_live_document_state(multipage_pdf, page_marker, tmp_path):
    out = tmp_path / "page.pdf"
    with PdfDocument.open(multipage_pdf) as doc:
        doc.rotate([2], 90)
        doc.extract_pages([2], out)
    assert _page_texts(out) == [page_marker(2)]
    assert _rotations(out) == [90]


# --- M10: merge ---------------------------------------------------------


def test_merge_roundtrip(tmp_path):
    a = tmp_path / "a.pdf"
    _make_pdf(a, ["A0", "A1"])
    b = tmp_path / "b.pdf"
    _make_pdf(b, ["B0"])
    c = tmp_path / "c.pdf"
    _make_pdf(c, ["C0", "C1"])
    out = tmp_path / "merged.pdf"
    pages.merge([a, b, c], out)
    assert _page_texts(out) == ["A0", "A1", "B0", "C0", "C1"]


def test_merge_empty_list_refused(tmp_path):
    with pytest.raises(ValueError):
        pages.merge([], tmp_path / "x.pdf")


# --- M11: split ---------------------------------------------------------


def test_split_roundtrip(multipage_pdf, page_marker, tmp_path):
    out_dir = tmp_path / "parts"
    outputs = pages.split(multipage_pdf, [(0, 1), (2, 4)], out_dir)
    assert len(outputs) == 2
    assert _page_texts(outputs[0]) == [page_marker(0), page_marker(1)]
    assert _page_texts(outputs[1]) == [page_marker(2), page_marker(3), page_marker(4)]


def test_split_rejects_out_of_range(multipage_pdf, tmp_path):
    with pytest.raises(ValueError):
        pages.split(multipage_pdf, [(0, 99)], tmp_path / "parts")


def test_split_empty_ranges_refused(multipage_pdf, tmp_path):
    with pytest.raises(ValueError):
        pages.split(multipage_pdf, [], tmp_path / "parts")
