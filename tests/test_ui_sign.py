"""Signing UI: Sign menu gating, the place gestures, dialogs, and the flow.

Offscreen: the modal dialogs never exec (isVisible() is False), so tests
drive the dispatch methods directly — ``MainWindow._execute_signing``,
``SignDialog._on_accept``, the manager's core methods — exactly the
test_ui_links convention. Signing validity is asserted with pyHanko's own
validation API against the generated test certificate.
"""

import io

import pymupdf
import pytest

pytest.importorskip("PySide6")

from pyhanko.pdf_utils.reader import PdfFileReader  # noqa: E402
from pyhanko.sign.validation import validate_pdf_signature  # noqa: E402
from pyhanko_certvalidator import ValidationContext  # noqa: E402

from pdfapp.main_window import MainWindow  # noqa: E402
from pdfapp.sign_dialog import SignDialog  # noqa: E402
from pdfcore import signing  # noqa: E402


@pytest.fixture(scope="session")
def signer(signer_p12):
    return signing.load_pkcs12_signer(signer_p12.path, signer_p12.password)


def _validly_signed(data: bytes, signer) -> bool:
    vc = ValidationContext(trust_roots=[signer.signing_cert], allow_fetching=False)
    statuses = [
        validate_pdf_signature(sig, signer_validation_context=vc)
        for sig in PdfFileReader(io.BytesIO(data)).embedded_signatures
    ]
    return bool(statuses) and all(st.intact and st.valid for st in statuses)


def _scene(view, x: float, y: float) -> tuple[float, float]:
    z = view._canvas.render_zoom
    return x * z, y * z


# --- menu gating -------------------------------------------------------------


def test_sign_actions_gating(qapp, text_pdf):
    window = MainWindow()
    try:
        # No document: signing needs a page; the library manager never does.
        assert not window._place_signature_action.isEnabled()
        assert not window._sign_invisible_action.isEnabled()
        assert not window._place_initials_action.isEnabled()
        assert window._manage_signatures_action.isEnabled()

        window.open_path(text_pdf)
        # Markup mode: signing is available (terminal op, like save/print) —
        # initials are a CONTENT stamp, edit-mode only.
        assert window._place_signature_action.isEnabled()
        assert window._sign_invisible_action.isEnabled()
        assert not window._place_initials_action.isEnabled()
        window.active_view.set_edit_mode(True)
        window._sync_chrome()
        assert window._place_initials_action.isEnabled()
    finally:
        window.close()


def test_place_signature_arms_and_toggles(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        window.place_signature()
        assert view._canvas.sign_rect_armed is True
        assert view.armed_action == "sign"
        assert window._place_signature_action.isChecked()
        chip = view._canvas._armed_chip
        assert not chip.isHidden() and "Esc cancels" in chip.text()

        window.place_signature()  # clicking the checked action cancels
        assert view._canvas.sign_rect_armed is False
        assert not window._place_signature_action.isChecked()
    finally:
        window.close()


# --- the rectangle gesture ---------------------------------------------------


def test_sign_rect_too_small_warns(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        warnings, rects = [], []
        view.editWarning.connect(warnings.append)
        view.signatureRectSelected.connect(lambda n, rect: rects.append((n, rect)))
        s0 = _scene(view, 100, 100)
        s1 = _scene(view, 105, 105)
        view._on_sign_rect_selected(s0[0], s0[1], s1[0], s1[1])
        assert rects == []
        assert warnings and "rectangle" in warnings[-1].lower()
    finally:
        window.close()


def test_sign_rect_emits_page_rect(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        rects = []
        view.signatureRectSelected.connect(lambda n, rect: rects.append((n, rect)))
        s0 = _scene(view, 250, 300)  # drawn bottom-right -> top-left: normalized
        s1 = _scene(view, 100, 200)
        view._on_sign_rect_selected(s0[0], s0[1], s1[0], s1[1])
        assert len(rects) == 1
        n, rect = rects[0]
        assert n == 0
        assert rect == pytest.approx((100.0, 200.0, 250.0, 300.0), abs=0.5)
    finally:
        window.close()


# --- the signing flow --------------------------------------------------------


def test_execute_signing_writes_valid_copy_and_opens_tab(
    qapp, text_pdf, signer, sample_png, tmp_path
):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        out = tmp_path / "signed-copy.pdf"
        result = window._execute_signing(
            view,
            out,
            signer,
            page_index=0,
            rect=(100.0, 500.0, 300.0, 560.0),
            image_path=sample_png,
            reason="Approved",
        )
        assert result is not None and result.field_name == "Signature1"
        assert _validly_signed(out.read_bytes(), signer)
        # The signed copy opened in its own tab; the original stays unsigned
        # and untouched (no undo entry, not dirty).
        assert window._tabs.count() == 2
        assert window.active_view.path == out
        assert not view.dirty
        assert view.undo_stack.count() == 0
    finally:
        window.close()


def test_execute_signing_field_name_increments(qapp, text_pdf, signer, tmp_path):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        first_out = tmp_path / "signed-once.pdf"
        assert window._execute_signing(window.active_view, first_out, signer) is not None
        signed_view = window.active_view  # _execute_signing opened the copy
        assert signed_view.path == first_out
        second_out = tmp_path / "signed-twice.pdf"
        result = window._execute_signing(signed_view, second_out, signer)
        assert result is not None and result.field_name == "Signature2"
        assert _validly_signed(second_out.read_bytes(), signer)
    finally:
        window.close()


def test_execute_signing_refuses_edited_signed_document(
    qapp, text_pdf, signer, sample_png, tmp_path
):
    """Editing a signed document breaks its signatures — signing the edited
    state must refuse honestly, never silently invalidate."""
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        first_out = tmp_path / "signed.pdf"
        assert window._execute_signing(window.active_view, first_out, signer) is not None
        signed_view = window.active_view
        signed_view.set_edit_mode(True)
        signed_view._place_image(0, 100.0, 100.0, sample_png)  # now dirty
        assert signed_view.dirty

        result = window._execute_signing(signed_view, tmp_path / "resigned.pdf", signer)
        assert result is None
        assert not (tmp_path / "resigned.pdf").exists()
        assert "invalidate" in window.statusBar().currentMessage().lower()
    finally:
        window.close()


def test_placeholder_field_document_signs_normally(qapp, text_pdf, signer, sample_png, tmp_path):
    """An EMPTY signature field (unsigned contract template) is not 'already
    signed' — editing + signing such a form must take the normal flatten
    path, never the false 'already holds digital signatures' refusal."""
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign.fields import SigFieldSpec, append_signature_field

    writer = IncrementalPdfFileWriter(io.BytesIO(text_pdf.read_bytes()))
    append_signature_field(writer, SigFieldSpec("ClientSignature", box=(100, 100, 300, 160)))
    buf = io.BytesIO()
    writer.write(buf)
    template = tmp_path / "template.pdf"
    template.write_bytes(buf.getvalue())

    window = MainWindow()
    try:
        window.open_path(template)
        view = window.active_view
        view.set_edit_mode(True)
        view._place_image(0, 100.0, 400.0, sample_png)  # filled in -> dirty
        assert view.dirty

        out = tmp_path / "template-signed.pdf"
        result = window._execute_signing(view, out, signer)
        assert result is not None and result.field_name == "Signature1"
        assert _validly_signed(out.read_bytes(), signer)
    finally:
        window.close()


def test_execute_signing_refuses_out_path_open_in_other_tab(qapp, text_pdf, signer, tmp_path):
    """Overwriting a file another tab holds open would focus that STALE tab
    as if it were the fresh signed copy — refuse with plain words."""
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        original_view = window.active_view
        out = tmp_path / "signed.pdf"
        assert window._execute_signing(original_view, out, signer) is not None
        assert window._tabs.count() == 2  # the signed copy's tab

        result = window._execute_signing(original_view, out, signer)  # same name again
        assert result is None
        assert "another tab" in window.statusBar().currentMessage().lower()
    finally:
        window.close()


def test_resigning_laundered_file_refused(qapp, text_pdf, signer, sample_png, tmp_path):
    """Edit a signed copy, save it (the rewrite silently breaks its signature
    on disk, and the stack is now clean) — the append branch must detect the
    broken coverage and refuse, not produce output readers flag as invalid."""
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        out = tmp_path / "signed.pdf"
        assert window._execute_signing(window.active_view, out, signer) is not None
        signed_view = window.active_view
        signed_view.set_edit_mode(True)
        signed_view._place_image(0, 100.0, 100.0, sample_png)
        window.save()  # offscreen: the signed-doc save warning auto-proceeds
        assert not signed_view.dirty  # clean stack, laundered file

        result = window._execute_signing(signed_view, tmp_path / "resigned.pdf", signer)
        assert result is None
        assert "already broken" in window.statusBar().currentMessage().lower()
    finally:
        window.close()


def test_laundered_visible_signature_refused(qapp, text_pdf, signer, sample_png, tmp_path):
    """The user's exact hand-test: VISIBLE image signature → edit the signed
    copy → Ctrl+S → re-sign. The append gate now uses REAL verification, so
    the broken file is refused however the cheap heuristic fares."""
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        out = tmp_path / "visibly-signed.pdf"
        assert (
            window._execute_signing(
                window.active_view,
                out,
                signer,
                rect=(100.0, 500.0, 300.0, 560.0),
                image_path=sample_png,
            )
            is not None
        )
        signed_view = window.active_view
        signed_view.set_edit_mode(True)
        signed_view._place_image(0, 100.0, 100.0, sample_png)
        window.save()  # offscreen: the signed-doc save warning auto-proceeds
        assert not signed_view.dirty

        result = window._execute_signing(signed_view, tmp_path / "resigned.pdf", signer)
        assert result is None
        assert "already broken" in window.statusBar().currentMessage().lower()
    finally:
        window.close()


# --- signature status surface ------------------------------------------------


def test_open_signed_document_announces_intact(qapp, text_pdf, signer, tmp_path):
    signed = tmp_path / "announce.pdf"
    signed.write_bytes(signing.sign_pdf_bytes(text_pdf.read_bytes(), signer).pdf_bytes)
    window = MainWindow()
    try:
        window.open_path(signed)
        message = window.statusBar().currentMessage().lower()
        assert "signature intact" in message
        assert "test signer" in message  # the signer's name is surfaced
    finally:
        window.close()


def test_open_tampered_document_flags_problem(qapp, text_pdf, signer, tmp_path):
    """The user report: Acrobat flags a tampered signed file, we showed
    NOTHING. Opening one must now announce the problem."""
    signed = signing.sign_pdf_bytes(text_pdf.read_bytes(), signer).pdf_bytes
    tampered = bytearray(signed)
    tampered[signed.find(b"stream") + 16] ^= 0xFF
    bad = tmp_path / "tampered.pdf"
    bad.write_bytes(bytes(tampered))
    window = MainWindow()
    try:
        window.open_path(bad)
        assert "signature problem" in window.statusBar().currentMessage().lower()
    finally:
        window.close()


def test_open_real_tampered_sample_flags_problem(qapp, real_tampered_signed_pdf):
    window = MainWindow()
    try:
        window.open_path(real_tampered_signed_pdf)
        assert "signature problem" in window.statusBar().currentMessage().lower()
    finally:
        window.close()


def test_open_incrementally_tampered_document_flags_problem(qapp, text_pdf, signer, tmp_path):
    """The appended-revision attack must flag on open like the byte-flip."""
    from pyhanko.pdf_utils.generic import StreamObject
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter

    signed = signing.sign_pdf_bytes(text_pdf.read_bytes(), signer).pdf_bytes
    writer = IncrementalPdfFileWriter(io.BytesIO(signed))
    page_obj = writer.root["/Pages"]["/Kids"][0].get_object()
    page_obj["/Contents"] = writer.add_object(
        StreamObject(stream_data=b"BT /F0 24 Tf 72 720 Td (TAMPERED) Tj ET")
    )
    writer.update_container(page_obj)
    buf = io.BytesIO()
    writer.write(buf)
    bad = tmp_path / "inc-tampered.pdf"
    bad.write_bytes(buf.getvalue())

    window = MainWindow()
    try:
        window.open_path(bad)
        message = window.statusBar().currentMessage().lower()
        assert "signature problem" in message and "modified" in message
    finally:
        window.close()


def test_broken_dialog_fires_only_for_problem_files(qapp, text_pdf, signer, tmp_path, monkeypatch):
    """The Acrobat-style modal: fires for a tampered file, NOT for an intact
    one (visible window; the dialog is monkeypatched, the repo pattern)."""
    from PySide6.QtWidgets import QMessageBox

    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: warnings.append(args[2]))
    signed = signing.sign_pdf_bytes(text_pdf.read_bytes(), signer).pdf_bytes
    good = tmp_path / "good.pdf"
    good.write_bytes(signed)
    tampered = bytearray(signed)
    tampered[signed.find(b"stream") + 16] ^= 0xFF
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(bytes(tampered))

    window = MainWindow()
    try:
        window.show()  # offscreen show(): isVisible() True, no real window
        window.open_path(good)
        assert warnings == []  # intact never pops the problem dialog
        window.open_path(bad)
        assert len(warnings) == 1 and "modified" in warnings[0].lower()
    finally:
        window.close()


def test_refocus_reannounces_signature_problem(qapp, text_pdf, signer, tmp_path):
    """Re-opening an already-open tampered file (Explorer double-click an
    hour later) must re-flag, not silently focus the tab."""
    signed = signing.sign_pdf_bytes(text_pdf.read_bytes(), signer).pdf_bytes
    tampered = bytearray(signed)
    tampered[signed.find(b"stream") + 16] ^= 0xFF
    bad = tmp_path / "refocus.pdf"
    bad.write_bytes(bytes(tampered))

    window = MainWindow()
    try:
        window.open_path(bad)
        window.open_path(text_pdf)  # focus moves away, message changes
        window.open_path(bad)  # refocus path — no new tab
        assert window._tabs.count() == 2
        assert "signature problem" in window.statusBar().currentMessage().lower()
    finally:
        window.close()


def test_unverifiable_signature_gets_honest_wording(qapp, text_pdf, signer, tmp_path, monkeypatch):
    """A signature that merely COULD NOT be checked must not be announced as
    'modified after signing' — the engine made no such determination."""
    signed = tmp_path / "unverifiable.pdf"
    signed.write_bytes(signing.sign_pdf_bytes(text_pdf.read_bytes(), signer).pdf_bytes)
    fake = signing.SignatureVerification(
        field_name="Signature1",
        signer_name=None,
        intact=False,
        valid=False,
        trusted=False,
        tampered=False,
        problem="this signature could not be checked (exotic algorithm)",
    )
    monkeypatch.setattr(signing, "verify_pdf_signatures", lambda *a, **k: [fake])

    window = MainWindow()
    try:
        window.open_path(signed)
        message = window.statusBar().currentMessage().lower()
        assert "cannot be verified" in message
        assert "modified" not in message
    finally:
        window.close()


def test_widgets_signed_but_nothing_verifiable_flags(qapp, text_pdf, signer, tmp_path):
    """AcroForm /Fields emptied while the widget stays on the page: pymupdf
    says signed, pyHanko sees nothing — must announce a problem, and the
    status text must not claim 'no digital signatures'."""
    signed = signing.sign_pdf_bytes(text_pdf.read_bytes(), signer).pdf_bytes
    with pymupdf.open(stream=signed, filetype="pdf") as doc:
        catalog = doc.pdf_catalog()
        doc.xref_set_key(catalog, "AcroForm/Fields", "[]")
        hollow = doc.tobytes()
    path = tmp_path / "hollow.pdf"
    path.write_bytes(hollow)

    window = MainWindow()
    try:
        window.open_path(path)
        assert "cannot be verified" in window.statusBar().currentMessage().lower()
        text = window.signature_status_text(window.active_view)
        assert "cannot be verified" in text.lower()
        assert "no digital signatures" not in text.lower()
    finally:
        window.close()


def test_edit_mode_warning_on_signed_document(qapp, text_pdf, signer, tmp_path, monkeypatch):
    """Entering Edit mode on a signed doc asks first: No un-toggles cleanly,
    Yes proceeds (visible window + monkeypatched question, the repo pattern)."""
    from PySide6.QtWidgets import QMessageBox

    signed = tmp_path / "warn.pdf"
    signed.write_bytes(signing.sign_pdf_bytes(text_pdf.read_bytes(), signer).pdf_bytes)
    answers = []

    def fake_question(*args, **kwargs):
        answers.append(args[2])
        return answers_queue.pop(0)

    monkeypatch.setattr(QMessageBox, "question", fake_question)

    window = MainWindow()
    try:
        window.open_path(signed)
        window.show()
        view = window.active_view

        answers_queue = [QMessageBox.StandardButton.No]
        window._edit_mode_action.setChecked(True)
        assert view.edit_mode is False  # refused
        assert window._edit_mode_action.isChecked() is False  # un-toggled
        assert answers and "digitally signed" in answers[-1].lower()

        answers_queue = [QMessageBox.StandardButton.Yes]
        window._edit_mode_action.setChecked(True)
        assert view.edit_mode is True  # proceeded
    finally:
        window.close()


def test_banner_problem_is_permanent(qapp, text_pdf, signer, tmp_path):
    """A broken signature shows the RED banner with no dismiss control."""
    signed = signing.sign_pdf_bytes(text_pdf.read_bytes(), signer).pdf_bytes
    tampered = bytearray(signed)
    tampered[signed.find(b"stream") + 16] ^= 0xFF
    bad = tmp_path / "banner-bad.pdf"
    bad.write_bytes(bytes(tampered))

    window = MainWindow()
    try:
        window.open_path(bad)
        banner = window.active_view._sig_banner
        assert not banner.isHidden()
        assert banner.problem is True
        assert banner._dismiss.isHidden()  # non-dismissable
        assert "modified" in banner.message().lower()
    finally:
        window.close()


def test_banner_intact_is_dismissable_once(qapp, text_pdf, signer, tmp_path):
    """The intact banner carries Dismiss; a re-announce must not re-nag."""
    good = tmp_path / "banner-good.pdf"
    good.write_bytes(signing.sign_pdf_bytes(text_pdf.read_bytes(), signer).pdf_bytes)

    window = MainWindow()
    try:
        window.open_path(good)
        view = window.active_view
        banner = view._sig_banner
        assert not banner.isHidden()
        assert banner.problem is False
        assert not banner._dismiss.isHidden()
        assert "signature intact" in banner.message().lower()

        banner._dismiss.click()
        assert banner.isHidden()
        window._announce_signatures(view)  # refocus/re-open path
        assert banner.isHidden(), "a dismissed intact banner must stay dismissed"
    finally:
        window.close()


def test_banner_absent_on_unsigned_documents(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        assert window.active_view._sig_banner.isHidden()
    finally:
        window.close()


def test_banner_flips_to_problem_after_saving_edited_signed(
    qapp, text_pdf, signer, sample_png, tmp_path
):
    """Saving an edited signed doc rewrites the file — the banner must stop
    vouching for the now-dead signature (and a dismissed intact banner must
    not suppress the problem variant)."""
    out = tmp_path / "flip.pdf"
    out.write_bytes(signing.sign_pdf_bytes(text_pdf.read_bytes(), signer).pdf_bytes)

    window = MainWindow()
    try:
        window.open_path(out)
        view = window.active_view
        view._sig_banner._dismiss.click()  # user closed the intact banner
        view.set_edit_mode(True)
        view._place_image(0, 100.0, 100.0, sample_png)
        window.save()  # offscreen: warnings auto-proceed

        banner = view._sig_banner
        assert not banner.isHidden()
        assert banner.problem is True
        assert banner._dismiss.isHidden()
    finally:
        window.close()


def test_banner_details_opens_signature_status(qapp, text_pdf, signer, tmp_path):
    good = tmp_path / "details.pdf"
    good.write_bytes(signing.sign_pdf_bytes(text_pdf.read_bytes(), signer).pdf_bytes)

    window = MainWindow()
    try:
        window.open_path(good)
        window.active_view._sig_banner._details.click()
        # Offscreen show_signature_status routes the first line to the status
        # bar — enough to prove the wiring reaches the composer.
        assert "signed by" in window.statusBar().currentMessage().lower()
    finally:
        window.close()


def test_signature_status_text(qapp, text_pdf, signer, signer_p12, tmp_path):
    signed = tmp_path / "status.pdf"
    signed_bytes = signing.sign_pdf_bytes(text_pdf.read_bytes(), signer).pdf_bytes
    signed.write_bytes(signed_bytes)
    tampered = bytearray(signed_bytes)
    tampered[signed_bytes.find(b"stream") + 16] ^= 0xFF
    bad = tmp_path / "status-bad.pdf"
    bad.write_bytes(bytes(tampered))

    window = MainWindow()
    try:
        window.open_path(text_pdf)
        assert "no digital signatures" in window.signature_status_text(window.active_view)

        window.open_path(signed)
        text = window.signature_status_text(window.active_view)
        assert "INTACT" in text and signer_p12.common_name in text
        assert "identity not verified" in text

        window.open_path(bad)
        text = window.signature_status_text(window.active_view)
        assert "BROKEN" in text and "modified" in text
    finally:
        window.close()


def test_execute_signing_failure_reports_and_keeps_tabs(qapp, text_pdf, signer, tmp_path):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        result = window._execute_signing(
            view,
            tmp_path / "never-written.pdf",
            signer,
            rect=(0.0, 0.0, 99999.0, 99999.0),  # off-page -> engine refuses
        )
        assert result is None
        assert not (tmp_path / "never-written.pdf").exists()
        assert window._tabs.count() == 1
        assert "failed" in window.statusBar().currentMessage().lower()
    finally:
        window.close()


# --- initials ----------------------------------------------------------------


def test_place_initials_flow(qapp, text_pdf, sample_png):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        view.set_edit_mode(True)
        window._signatures.add("Scott", sample_png, initials_image=sample_png)
        window.place_initials()
        assert view.armed_action == "initials"
        assert window._place_initials_action.isChecked()

        before = len(view.document.images(0))
        sx, sy = _scene(view, 500, 40)
        view._canvas.insertPointSelected.emit(sx, sy)
        images = view.document.images(0)
        assert len(images) == before + 1
        newest = max(images, key=lambda i: i.bbox[0])  # ours sits at x≈500
        assert (newest.bbox[2] - newest.bbox[0]) <= 64.5  # stamps SMALL
        assert view.undo_stack.count() == 1  # decorative content stamp: undoable
    finally:
        window.close()


def test_place_initials_needs_a_profile(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        window.active_view.set_edit_mode(True)
        window.place_initials()
        assert window.active_view.armed_action is None
        assert "manage signatures" in window.statusBar().currentMessage().lower()
    finally:
        window.close()


def test_place_initials_trigger_leaves_no_stale_check(qapp, text_pdf):
    """A checkable QAction toggles BEFORE triggered fires — the no-profiles
    early return must re-sync or the menu shows a phantom checkmark."""
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        window.active_view.set_edit_mode(True)
        window._sync_chrome()
        window._place_initials_action.trigger()  # empty store -> early return
        assert window.active_view.armed_action is None
        assert not window._place_initials_action.isChecked()
    finally:
        window.close()


def test_place_initials_inert_in_markup_mode(qapp, text_pdf, sample_png):
    """The view-level guard: initials are CONTENT — inert outside edit mode
    (the documented Markup-mode inertness invariant), while place-signature
    is a terminal op and must work in Markup mode."""
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        assert view.edit_mode is False
        view.begin_place_initials(sample_png)
        assert view.armed_action is None
        assert not view._canvas.is_armed

        view.begin_place_signature()  # markup mode: signing is allowed
        assert view.armed_action == "sign"
    finally:
        window.close()


def test_begin_place_signature_clears_pending_redefine(qapp, text_pdf):
    """A stale 'Redefine clickable area' target must not survive the sign
    detour and hijack the next link-rect drag into resizing an old link."""
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        view._pending_redefine = (0, 99)
        view.begin_place_signature()
        assert view._pending_redefine is None
    finally:
        window.close()


def test_esc_cancel_mid_drag_hides_band(qapp, text_pdf):
    """Esc during a live sign-rect drag must hide the shared rubber band —
    the release branch (the usual hider) is skipped once press is None."""
    from PySide6.QtCore import QPointF, QRect
    from PySide6.QtWidgets import QRubberBand

    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        canvas = view._canvas
        window.place_signature()
        canvas._signrect_press = QPointF(10.0, 10.0)  # simulate a live drag
        if canvas._move_band is None:
            canvas._move_band = QRubberBand(QRubberBand.Shape.Rectangle, canvas.viewport())
        canvas._move_band.setGeometry(QRect(0, 0, 40, 40))
        canvas._move_band.show()

        view.cancel_armed_mode()  # the Esc path
        assert canvas._move_band.isHidden()
        assert not canvas.sign_rect_armed
    finally:
        window.close()


# --- SignDialog --------------------------------------------------------------


def test_sign_dialog_validates_password_inline(qapp, signer_p12):
    dlg = SignDialog(None, [], default_p12=signer_p12.path, visible_signature=False)
    try:
        assert dlg.cert_path() == signer_p12.path
        dlg.set_password("wrong-password")
        dlg._on_accept()
        assert dlg.result() != 1  # not accepted — stays open for another try
        assert dlg.signer is None
        assert dlg._error_label.text() != ""

        dlg.set_password(signer_p12.password)
        dlg._on_accept()
        assert dlg.result() == 1
        assert dlg.signer is not None
        assert dlg.spec()["cert_path"] == signer_p12.path
    finally:
        dlg.deleteLater()


def test_sign_dialog_resolves_profile_certificates(qapp, tmp_path, signer_p12, sample_png):
    from pdfapp.signature_store import SignatureStore

    store = SignatureStore(tmp_path / "sigs" / "signatures.json")
    store.add("Own Cert", sample_png, p12_path=signer_p12.path)
    store.add("Uses Default", sample_png)
    default = tmp_path / "default.p12"
    default.write_bytes(b"placeholder")  # resolution only — never loaded here

    dlg = SignDialog(None, store.profiles(), default_p12=default, visible_signature=True)
    try:
        combo = dlg._profile_combo
        combo.setCurrentIndex(1)  # "Own Cert"
        assert dlg.cert_path() == signer_p12.path
        assert dlg._image_check.isChecked()  # profile has a signature image
        assert dlg.spec()["image_path"] is not None
        combo.setCurrentIndex(2)  # "Uses Default" -> falls back to the default
        assert dlg.cert_path() == default
        combo.setCurrentIndex(0)  # no profile -> default cert, no image skin
        assert dlg.cert_path() == default
        assert not dlg._image_check.isEnabled()
        assert dlg.spec()["image_path"] is None
    finally:
        dlg.deleteLater()


# --- manager dialog ----------------------------------------------------------


def test_manager_core_flow(qapp, tmp_path, sample_png, signer_p12):
    window = MainWindow()
    try:
        manager = window.manage_signatures()  # offscreen: returned, not exec'd
        assert manager.add_profile("Scott", sample_png, initials_image=sample_png) is None
        assert manager.profile_names() == ["Scott"]
        error = manager.add_profile("scott", sample_png)  # duplicate, case-insensitive
        assert error is not None and "already exists" in error
        assert manager.add_profile("Ghost", tmp_path / "missing.png") is not None

        manager.set_default_certificate(signer_p12.path)
        assert manager.default_certificate() == signer_p12.path
        assert window._settings.get("default_signing_p12") == str(signer_p12.path)
        manager.set_default_certificate(None)
        assert manager.default_certificate() is None

        out = tmp_path / "generated.p12"
        assert manager.generate_certificate(out, "Generated Signer", "pw-123") is None
        assert out.is_file()
        assert manager.default_certificate() == out  # set_default defaults True
        loaded = signing.load_pkcs12_signer(out, "pw-123")
        assert loaded.subject_name == "Generated Signer"

        manager.remove_profile("Scott")
        assert manager.profile_names() == []
    finally:
        window.close()
