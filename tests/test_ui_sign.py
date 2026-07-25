"""Signing UI: Sign menu gating, the place gestures, dialogs, and the flow.

Offscreen: the modal dialogs never exec (isVisible() is False), so tests
drive the dispatch methods directly — ``MainWindow._execute_signing``,
``SignDialog._on_accept``, the manager's core methods — exactly the
test_ui_links convention. Signing validity is asserted with pyHanko's own
validation API against the generated test certificate.
"""

import io

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
