"""Tests for PdfDocument: open, inspect, save round-trip, encryption."""

from __future__ import annotations

import pymupdf
import pytest

from pdfcore.document import PdfDocument


def test_open_and_page_count(text_pdf):
    with PdfDocument.open(text_pdf) as doc:
        assert doc.page_count == 3


def test_page_size_positive(text_pdf):
    with PdfDocument.open(text_pdf) as doc:
        w, h = doc.page_size(0)
    assert w > 0 and h > 0


def test_save_roundtrip(text_pdf, tmp_path):
    out = tmp_path / "roundtrip.pdf"
    with PdfDocument.open(text_pdf) as doc:
        doc.save(out)
    assert out.exists() and out.stat().st_size > 0
    with PdfDocument.open(out) as reopened:
        assert reopened.page_count == 3


def test_save_over_open_file_refused(text_pdf):
    with PdfDocument.open(text_pdf) as doc:
        with pytest.raises(ValueError):
            doc.save(text_pdf)


def test_image_pdf_has_embedded_image(image_pdf):
    doc = pymupdf.open(str(image_pdf))
    try:
        has_image = any(doc[n].get_images() for n in range(doc.page_count))
    finally:
        doc.close()
    assert has_image


# --- encryption ---------------------------------------------------------


def test_encrypted_reports_needs_pass(encrypted_pdf):
    with PdfDocument.open(encrypted_pdf.path) as doc:
        assert doc.needs_pass is True


def test_encrypted_render_without_password_raises(encrypted_pdf):
    with PdfDocument.open(encrypted_pdf.path) as doc:
        with pytest.raises(ValueError):
            doc.render_page(0)


def test_encrypted_open_with_password_renders(encrypted_pdf):
    with PdfDocument.open(encrypted_pdf.path, password=encrypted_pdf.user_pw) as doc:
        assert doc.page_count == 1
        rp = doc.render_page(0)
        assert rp.width > 0 and rp.height > 0


def test_encrypted_authenticate_then_render(encrypted_pdf):
    with PdfDocument.open(encrypted_pdf.path) as doc:
        assert doc.authenticate(encrypted_pdf.user_pw) is True
        rp = doc.render_page(0)
        assert rp.width > 0


def test_wrong_password_returns_false(encrypted_pdf):
    with PdfDocument.open(encrypted_pdf.path) as doc:
        assert doc.authenticate("definitely-wrong") is False


# --- M12: source + atomic in-place save ---------------------------------


def test_source_property(text_pdf):
    with PdfDocument.open(text_pdf) as doc:
        assert doc.source == text_pdf


# --- Phase 2 (E3): snapshot / restore ------------------------------------


def test_snapshot_restore_roundtrip(multipage_pdf):
    with PdfDocument.open(multipage_pdf) as doc:
        before = doc.snapshot()
        doc.delete([0])
        doc.rotate([0], 90)
        assert doc.page_count == 4
        doc.restore(before)
        assert doc.page_count == 5
        assert doc.page_rotation(0) == 0
        assert doc.source == multipage_pdf  # source path preserved across restore


def test_restore_then_save_in_place(multipage_pdf, page_marker):
    with PdfDocument.open(multipage_pdf) as doc:
        before = doc.snapshot()
        doc.delete([0, 1])
        doc.restore(before)
        # A restored (stream-backed) document holds no handle on the source
        # file, so the atomic temp-write + os.replace still works.
        doc.save_in_place()
        assert doc.page_count == 5
    reopened = pymupdf.open(str(multipage_pdf))
    try:
        assert reopened.page_count == 5
        assert reopened[0].get_text().strip() == page_marker(0)
    finally:
        reopened.close()


def test_snapshot_restore_encrypted_needs_no_reauth(encrypted_pdf):
    with PdfDocument.open(encrypted_pdf.path, password=encrypted_pdf.user_pw) as doc:
        doc.restore(doc.snapshot())
        # Snapshots PRESERVE encryption (so a later save can KEEP it — the
        # old drop-on-snapshot behaviour made post-undo saves write
        # plaintext); the restore re-authenticates internally, so it still
        # never re-prompts for the password.
        assert doc.needs_pass is True  # the bytes carry the encryption
        assert doc.is_protected is True
        assert doc.render_page(0).width > 0  # …and we're authenticated


def test_restore_undoes_rotation_dimensions(text_pdf):
    with PdfDocument.open(text_pdf) as doc:
        snap = doc.snapshot()
        doc.rotate([0], 90)
        w_rot, h_rot = doc.page_size(0)
        doc.restore(snap)
        assert doc.page_size(0) == (h_rot, w_rot)  # swap undone by restore


def test_save_in_place_persists_and_reopens(multipage_pdf, page_marker):
    with PdfDocument.open(multipage_pdf) as doc:
        assert doc.page_count == 5
        doc.delete([0])
        doc.save_in_place()
        # the document keeps working after the in-place save (reopened internally)
        assert doc.page_count == 4
        assert doc.render_page(0).width > 0
    # change is persisted to the original path
    reopened = pymupdf.open(str(multipage_pdf))
    try:
        assert reopened.page_count == 4
        assert reopened[0].get_text().strip() == page_marker(1)
    finally:
        reopened.close()


def test_save_in_place_failure_keeps_document_and_edits_alive(tmp_path):
    """User report (2026-07-18): saving while Acrobat held the file open
    failed (WinError 5 on the atomic replace) and then EVERY later save said
    "document closed" — the failure path had closed our handle and never
    reopened. The document must survive the failure WITH its unsaved edits
    (restored from the temp bytes, which hold them), the temp file must not
    be left behind, and a retry after the lock clears must succeed."""
    import os as os_module

    import pytest

    from pdfcore import document as document_module

    path = tmp_path / "locked.pdf"
    doc0 = pymupdf.open()
    doc0.new_page()
    doc0.save(str(path))
    doc0.close()

    doc = PdfDocument.open(path)
    try:
        doc.add_comment(0, (100.0, 100.0, 0.0, 0.0), "unsaved edit", author="S")

        real_replace = os_module.replace

        def locked(*args, **kwargs):
            raise PermissionError(13, "Access is denied")

        document_module.os.replace = locked
        try:
            with pytest.raises(PermissionError):
                doc.save_in_place()
        finally:
            document_module.os.replace = real_replace

        # Alive, edits intact, no temp litter, disk file untouched.
        assert doc.page_count == 1
        assert doc.comments(0)[0].text == "unsaved edit"
        assert not path.with_name(path.name + ".tmp").exists()
        with PdfDocument.open(path) as on_disk:
            assert on_disk.comments(0) == []

        doc.save_in_place()  # the lock is gone: the retry succeeds
        with PdfDocument.open(path) as on_disk:
            assert on_disk.comments(0)[0].text == "unsaved edit"
    finally:
        doc.close()
