"""Inserted-box registry (E10): durable identity stored IN the document.

The registry rides /PieceInfo/PDFEditor/Private on the catalog (ISO 32000
§14.5 — the spec's channel for private product data), so undo snapshots,
saves and reopens carry it automatically and it can never drift from the
content it describes.
"""

from __future__ import annotations

import pymupdf

from pdfcore.boxregistry import read_boxes
from pdfcore.document import PdfDocument


def _blank_pdf(tmp_path, pages=1, name="reg.pdf"):
    path = tmp_path / name
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"PAGE-{i}", fontsize=14)
    doc.save(str(path))
    doc.close()
    return path


def test_add_read_update_remove_roundtrip(tmp_path):
    with PdfDocument.open(_blank_pdf(tmp_path)) as doc:
        assert doc.boxes() == []  # absent registry reads as empty
        box = doc.add_box(0, (100.0, 190.0, 170.0, 205.0))
        assert box.id and box.page == 0
        assert doc.boxes(0) == [box]
        assert doc.boxes(1) == []

        doc.update_box_rect(box.id, (120.0, 290.0, 190.0, 305.0))
        assert doc.boxes()[0].rect == (120.0, 290.0, 190.0, 305.0)
        assert doc.boxes()[0].id == box.id  # identity stable across moves

        doc.remove_box(box.id)
        assert doc.boxes() == []


def test_registry_survives_save_and_reopen(tmp_path):
    """Round-trip per rule 10 — and garbage=4 must not strip PieceInfo."""
    src = _blank_pdf(tmp_path)
    out = tmp_path / "saved.pdf"
    with PdfDocument.open(src) as doc:
        box = doc.add_box(0, (10.0, 20.0, 30.0, 40.0))
        doc.save(out)  # garbage=4, deflate=True
    with PdfDocument.open(out) as doc:
        assert doc.boxes() == [box]


def test_registry_survives_undo_snapshot_restore(tmp_path):
    """Snapshots are whole-document bytes, so the registry travels with the
    content it describes — the E9.9 session-tracking fragility is gone."""
    with PdfDocument.open(_blank_pdf(tmp_path)) as doc:
        before = doc.snapshot()
        box = doc.add_box(0, (10.0, 20.0, 30.0, 40.0))
        after = doc.snapshot()

        doc.restore(before)
        assert doc.boxes() == []  # undo of the insert removes its identity too
        doc.restore(after)
        assert doc.boxes() == [box]


def test_malformed_private_data_reads_as_empty(tmp_path):
    with PdfDocument.open(_blank_pdf(tmp_path)) as doc:
        cat = doc._doc.pdf_catalog()
        doc._doc.xref_set_key(
            cat, "PieceInfo/PDFEditor/Private", pymupdf.get_pdf_str("{not json![")
        )
        assert doc.boxes() == []  # never raises on foreign/mangled data
        # ...and stays writable: a fresh add replaces the mangled payload.
        box = doc.add_box(0, (1.0, 2.0, 3.0, 4.0))
        assert doc.boxes() == [box]


def test_delete_pages_remaps_and_drops(tmp_path):
    src = _blank_pdf(tmp_path, pages=3)
    out = tmp_path / "deleted.pdf"
    with PdfDocument.open(src) as doc:
        kept = doc.add_box(2, (1.0, 1.0, 2.0, 2.0))  # on the last page
        doc.add_box(1, (5.0, 5.0, 6.0, 6.0))  # on the page being deleted
        doc.delete([1])
        doc.save(out)
    with PdfDocument.open(out) as doc:
        boxes = doc.boxes()
        assert len(boxes) == 1  # deleted page's box dropped
        assert boxes[0].id == kept.id
        assert boxes[0].page == 1  # old page 2 became page 1


def test_reorder_remaps(tmp_path):
    src = _blank_pdf(tmp_path, pages=3)
    with PdfDocument.open(src) as doc:
        box = doc.add_box(0, (1.0, 1.0, 2.0, 2.0), "hello\nworld")
        doc.reorder([2, 0, 1])  # old page 0 is now page 1
        assert doc.boxes()[0].page == 1
        assert doc.boxes()[0].id == box.id
        # reorder rebuilds the registry by hand (select() drops PieceInfo) —
        # the content fingerprint must survive that rebuild, not just id/rect
        # (it silently came back "" until 0.10.0).
        assert doc.boxes()[0].text == "hello\nworld"


def test_insert_pages_shifts(tmp_path):
    src = _blank_pdf(tmp_path, pages=2)
    other = _blank_pdf(tmp_path, pages=2, name="other.pdf")
    with PdfDocument.open(src) as doc:
        first = doc.add_box(0, (1.0, 1.0, 2.0, 2.0))
        last = doc.add_box(1, (3.0, 3.0, 4.0, 4.0))
        doc.insert_from(other, at=1)
        by_id = {b.id: b for b in doc.boxes()}
        assert by_id[first.id].page == 0  # before the insertion point: unmoved
        assert by_id[last.id].page == 3  # after it: shifted by the 2 new pages


def test_untouched_documents_gain_no_pieceinfo(tmp_path):
    """Page ops on a document with NO registry must not create PieceInfo."""
    src = _blank_pdf(tmp_path, pages=3)
    out = tmp_path / "plain.pdf"
    with PdfDocument.open(src) as doc:
        doc.delete([1])
        doc.save(out)
    reopened = pymupdf.open(str(out))
    try:
        kind, _ = reopened.xref_get_key(reopened.pdf_catalog(), "PieceInfo")
        assert kind == "null"  # untouched docs stay untouched
    finally:
        reopened.close()


def test_read_boxes_on_raw_document(tmp_path):
    """Engine-level read on a plain pymupdf.Document (geometry-cache path)."""
    src = _blank_pdf(tmp_path)
    with PdfDocument.open(src) as doc:
        doc.add_box(0, (9.0, 9.0, 11.0, 11.0))
        assert len(read_boxes(doc._doc)) == 1


def test_content_fingerprint_roundtrips_and_move_preserves_it(tmp_path):
    """Task 5: a box stores its content fingerprint; a MOVE (update_box_rect)
    keeps it, an EDIT (update_box) replaces it."""
    with PdfDocument.open(_blank_pdf(tmp_path)) as doc:
        box = doc.add_box(0, (10.0, 20.0, 30.0, 40.0), text="hello world")
        assert doc.boxes()[0].text == "hello world"

        doc.update_box_rect(box.id, (50.0, 60.0, 70.0, 80.0))  # a move
        moved = doc.boxes()[0]
        assert moved.rect == (50.0, 60.0, 70.0, 80.0)
        assert moved.text == "hello world"  # fingerprint preserved

        doc.update_box(box.id, (50.0, 60.0, 70.0, 80.0), "goodbye world")  # an edit
        assert doc.boxes()[0].text == "goodbye world"


def test_legacy_record_without_text_reads_as_empty_fingerprint(tmp_path):
    """A pre-fingerprint registry entry (no 'text' key) reads as text="" and
    falls back to pure geometry — never raises."""
    import json

    with PdfDocument.open(_blank_pdf(tmp_path)) as doc:
        cat = doc._doc.pdf_catalog()
        payload = json.dumps([{"id": "abc123", "page": 0, "rect": [1.0, 2.0, 3.0, 4.0]}])
        doc._doc.xref_set_key(cat, "PieceInfo/PDFEditor/Private", pymupdf.get_pdf_str(payload))
        boxes = doc.boxes()
        assert len(boxes) == 1
        assert boxes[0].text == ""  # absent -> empty, backward compatible
