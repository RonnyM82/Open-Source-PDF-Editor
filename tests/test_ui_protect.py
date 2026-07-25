"""Password-protection UI: dialog, pending-on-save, honoring, unlock, indicator.

Offscreen conventions: modal dialogs never exec (isVisible False), so tests
drive ProtectDialog fields + set_pending_protection directly; password
prompts are exercised via monkeypatched QInputDialog on a shown window.
"""

import types

import pymupdf
import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox  # noqa: E402

from pdfapp.main_window import MainWindow  # noqa: E402
from pdfapp.protect_dialog import ProtectDialog  # noqa: E402
from pdfcore import protect  # noqa: E402
from pdfcore.protect import ChangesAllowed  # noqa: E402

# --- ProtectDialog -----------------------------------------------------------


def test_dialog_open_password_only(qapp):
    dlg = ProtectDialog(None)
    try:
        dlg.set_open_password("open-pw")
        dlg._on_accept()
        assert dlg.result() == 1
        spec = dlg.spec()
        assert spec.user_pw == "open-pw" and spec.owner_pw is None
        assert protect.Permissions.from_mask(spec.permissions).all_allowed
    finally:
        dlg.deleteLater()


def test_dialog_restrictions_spec_roundtrip(qapp):
    dlg = ProtectDialog(None)
    try:
        dlg.set_restrictions(
            "perm-pw", changes=ChangesAllowed.COMMENT_FORM_FILL, allow_print=False, allow_copy=True
        )
        dlg._on_accept()
        assert dlg.result() == 1
        spec = dlg.spec()
        assert spec.user_pw is None and spec.owner_pw == "perm-pw"
        perms = protect.Permissions.from_mask(spec.permissions)
        assert perms.can_annotate and perms.can_fill_forms and perms.can_copy
        assert not perms.can_print and not perms.can_modify and not perms.can_assemble
    finally:
        dlg.deleteLater()


def test_dialog_validation_rejections(qapp):
    dlg = ProtectDialog(None)
    try:
        dlg._on_accept()  # nothing enabled
        assert dlg.result() != 1 and "at least one" in dlg._error_label.text().lower()

        dlg._open_group.setChecked(True)
        dlg._open_pw.setText("a")
        dlg._open_confirm.setText("b")
        dlg._on_accept()
        assert dlg.result() != 1 and "match" in dlg._error_label.text().lower()

        # Same password for both levels is rejected (Acrobat's rule).
        dlg.set_open_password("same")
        dlg.set_restrictions("same", changes=ChangesAllowed.NONE)
        dlg._on_accept()
        assert dlg.result() != 1 and "different" in dlg._error_label.text().lower()
    finally:
        dlg.deleteLater()


def test_dialog_changes_hint_tracks_selection(qapp):
    """Each 'Changes allowed' level explains itself in plain words (user
    request — the Acrobat ladder is not self-evident)."""
    dlg = ProtectDialog(None)
    try:
        expectations = {
            ChangesAllowed.NONE: "read-only",
            ChangesAllowed.PAGES: "whole-page",
            ChangesAllowed.FORM_FILL: "form fields",
            ChangesAllowed.COMMENT_FORM_FILL: "commenting",
            ChangesAllowed.ANY_EXCEPT_EXTRACT: "full editing",
        }
        seen = set()
        for changes, keyword in expectations.items():
            dlg.set_restrictions("pw", changes=changes)
            hint = dlg._changes_hint.text()
            assert keyword in hint.lower()
            seen.add(hint)
        assert len(seen) == len(expectations)  # every level has its OWN hint
    finally:
        dlg.deleteLater()


def test_dialog_remove_protection(qapp):
    dlg = ProtectDialog(None, currently_protected=True)
    try:
        dlg._on_remove()
        assert dlg.result() == 1 and dlg.removed
        assert dlg.spec() is None
    finally:
        dlg.deleteLater()


# --- pending protection applied at save --------------------------------------


def _spec(user="open-pw", owner="perm-pw", changes=ChangesAllowed.NONE):
    return protect.ProtectionSpec(
        user_pw=user,
        owner_pw=owner,
        permissions=protect.permissions_mask(changes=changes, allow_print=True, allow_copy=False),
    )


def test_pending_protection_applied_on_save(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        assert not view.dirty
        view.set_pending_protection(_spec())
        assert view.dirty  # pending protection is unsaved work
        assert view.protection_state == "pending"
        assert "pending" in window._protection_label.text().lower()

        assert window.save() is None  # MainWindow.save returns None; check state
        assert not view.dirty
        assert view.document.is_protected is True
        assert view.open_password == "perm-pw"  # owner pw authenticates the file
        assert view.protection_state == "protected"
        assert window._protection_label.text() == "Protected"
        assert view.document.page_count > 0  # the view survived its own save
    finally:
        window.close()

    raw = pymupdf.open(str(text_pdf))
    assert raw.needs_pass
    assert int(raw.authenticate("open-pw")) == 2
    raw.close()


def test_pending_protection_applied_on_save_as(qapp, text_pdf, tmp_path, monkeypatch):
    out = tmp_path / "protected-copy.pdf"
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        view.set_pending_protection(_spec())
        assert view.save_as_path(out) is True
        assert view.path == out  # re-pointed at the protected copy
        assert view.document.is_protected is True
        assert view.document.page_count > 0
        assert not view.dirty
    finally:
        window.close()

    raw = pymupdf.open(str(out))
    assert raw.needs_pass
    raw.close()


def test_keep_preserves_protection_through_ui_save(qapp, encrypted_pdf, monkeypatch):
    """Open a protected file via the app, edit, Ctrl+S — protection survives
    (the old behaviour silently stripped it)."""
    monkeypatch.setattr(
        QInputDialog,
        "getText",
        staticmethod(lambda *a, **k: (encrypted_pdf.user_pw, True)),
    )
    window = MainWindow()
    try:
        window.open_path(encrypted_pdf.path)
        view = window.active_view
        assert view.document.is_protected is True
        view.set_edit_mode(True)  # encrypted fixture has NO restrictions
        view._doc.rotate([0], 90)  # cheap direct edit; save is what we test
        assert window.save() is None
        assert view.document.is_protected is True
    finally:
        window.close()

    raw = pymupdf.open(str(encrypted_pdf.path))
    assert raw.needs_pass, "the UI save stripped the file's protection"
    assert int(raw.authenticate(encrypted_pdf.owner_pw)) in (4, 6)
    raw.close()


def test_remove_protection_roundtrip(qapp, encrypted_pdf, monkeypatch):
    monkeypatch.setattr(
        QInputDialog,
        "getText",
        staticmethod(lambda *a, **k: (encrypted_pdf.user_pw, True)),
    )
    window = MainWindow()
    try:
        window.open_path(encrypted_pdf.path)
        view = window.active_view
        view.document.unlock(encrypted_pdf.owner_pw)  # owner-gated change
        view.set_pending_protection(None)
        assert view.protection_state == "pending"
        window.save()
        assert view.document.is_protected is False
        assert view.protection_state == "none"
        assert window._protection_label.text() == ""
    finally:
        window.close()

    raw = pymupdf.open(str(encrypted_pdf.path))
    assert not raw.needs_pass
    raw.close()


# --- honoring restrictions ---------------------------------------------------


def test_restricted_document_gating(qapp, restricted_pdf):
    window = MainWindow()
    try:
        window.open_path(restricted_pdf.path)  # opens without a prompt
        view = window.active_view
        assert view.protection_state == "restricted"
        assert window._protection_label.text() == "Restricted"
        assert "editing" in window._protection_label.toolTip()

        # Print allowed by the fixture's mask; everything else denied.
        assert window._print_action.isEnabled()
        assert not window._extract_text_action.isEnabled()  # copy bit off
        for action in window._annotate_actions:
            assert not action.isEnabled()
        assert not window._place_signature_action.isEnabled()

        # Offscreen edit-mode entry on a restricted doc is refused (the
        # unlock prompt needs a visible window).
        window._edit_mode_action.setChecked(True)
        assert view.edit_mode is False
        assert not window._edit_mode_action.isChecked()
    finally:
        window.close()


def test_restricted_copy_guard(qapp, restricted_pdf):
    window = MainWindow()
    try:
        window.open_path(restricted_pdf.path)
        view = window.active_view
        warnings = []
        view.editWarning.connect(warnings.append)
        word = types.SimpleNamespace(text="secret")
        view._text_selection = [[word]]
        QApplication.clipboard().setText("sentinel")
        view.copy_selection()
        assert warnings and "restricted" in warnings[-1].lower()
        assert QApplication.clipboard().text() == "sentinel"  # nothing copied
    finally:
        window.close()


def test_edit_mode_unlock_with_owner_password(qapp, restricted_pdf, monkeypatch):
    answers = [restricted_pdf.owner_pw]
    monkeypatch.setattr(
        QInputDialog,
        "getText",
        staticmethod(lambda *a, **k: (answers.pop(0), True)),
    )
    window = MainWindow()
    try:
        window.open_path(restricted_pdf.path)
        window.show()  # the unlock prompt needs a visible window
        view = window.active_view
        window._edit_mode_action.setChecked(True)
        assert view.edit_mode is True  # unlocked
        assert view.document.permissions.all_allowed
        assert window._protection_label.text() == "Protected"  # no longer limited
        assert window._extract_text_action.isEnabled()  # gates lifted live
    finally:
        window.close()


def test_edit_mode_unlock_wrong_password_stays_markup(qapp, restricted_pdf, monkeypatch):
    calls = {"n": 0}

    def fake_get_text(*a, **k):
        calls["n"] += 1
        return ("wrong", True) if calls["n"] == 1 else ("", False)  # then cancel

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(fake_get_text))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    window = MainWindow()
    try:
        window.open_path(restricted_pdf.path)
        window.show()
        view = window.active_view
        window._edit_mode_action.setChecked(True)
        assert view.edit_mode is False
        assert not window._edit_mode_action.isChecked()
        assert view.document.is_owner is False
    finally:
        window.close()


def test_protect_document_owner_gated_offscreen(qapp, restricted_pdf):
    """Offscreen, a restricted doc without owner unlock gets None (the prompt
    needs a screen); after an explicit unlock the dialog is constructed."""
    window = MainWindow()
    try:
        window.open_path(restricted_pdf.path)
        assert window.protect_document() is None
        window.active_view.document.unlock(restricted_pdf.owner_pw)
        dialog = window.protect_document()
        assert dialog is not None
        dialog.deleteLater()
    finally:
        window.close()


def _make_protected(text_pdf, tmp_path, changes, name="preset.pdf"):
    """A saved copy of text_pdf protected owner-only with the given preset."""
    spec = protect.ProtectionSpec(
        user_pw=None,
        owner_pw="perm-pw",
        permissions=protect.permissions_mask(changes=changes, allow_print=True, allow_copy=False),
    )
    out = tmp_path / name
    from pdfcore.document import PdfDocument

    with PdfDocument.open(text_pdf) as doc:
        doc.set_protection(spec)
        doc.save(out)
    return out


def test_assemble_only_gestures_and_menus_refused(qapp, text_pdf, tmp_path):
    """The review's bypass hole: an assemble-only doc legitimately enters
    Edit mode (page ops) with NO prompt — but drags, editors and deletes
    must all refuse (toolbar gating alone was defeated)."""
    path = _make_protected(text_pdf, tmp_path, protect.ChangesAllowed.PAGES)
    window = MainWindow()
    try:
        window.open_path(path)
        view = window.active_view
        window._edit_mode_action.setChecked(True)
        assert view.edit_mode is True  # allowed: page ops are permitted
        assert window._rotate_cw_action.isEnabled()  # the assemble bit
        assert not window._insert_text_action.isEnabled()  # content denied

        # Drag funnel refuses content targets.
        para = view.page_geometry(0).paragraphs[0]
        target = types.SimpleNamespace(kind="text", payload=para, bbox=para.bbox)
        view._accept_target_drag(0, 100.0, 100.0, target)
        assert view._move_paragraph is None and view._move_group is None

        # Editor heads refuse.
        warnings = []
        view.editWarning.connect(warnings.append)
        view._begin_paragraph_edit(0, para)
        assert warnings and "restricted" in warnings[-1].lower()
        assert not view.editor_open

        # Delete key on a (faked) selected image refuses.
        view._selection = ("image", 0, types.SimpleNamespace(xref=99, bbox=(0, 0, 1, 1)))
        before = view.undo_stack.count()
        view._on_delete_selection()
        assert view.undo_stack.count() == before

        # A PAGE op still works — the only thing this preset allows.
        view.rotate_clockwise()
        assert view.undo_stack.count() == before + 1
    finally:
        window.close()


def test_annotate_denied_entry_points_refused(qapp, restricted_pdf):
    """Annotation permission honored beyond the toolbar: the begin methods
    and the Delete key refuse on an annotate-denied document."""
    window = MainWindow()
    try:
        window.open_path(restricted_pdf.path)
        view = window.active_view
        view.begin_insert_comment()
        assert view.armed_action is None
        view.begin_highlight()
        assert view.armed_action is None

        # Deleting an (arbitrarily selected) comment refuses too.
        view._selection = ("comment", 0, types.SimpleNamespace(xref=42))
        before = view.undo_stack.count()
        view._on_delete_selection()
        assert view.undo_stack.count() == before
    finally:
        window.close()


def test_annotate_allowed_preset_can_comment(qapp, text_pdf, tmp_path):
    """The positive side: a commenting-allowed restricted doc CAN annotate
    while content stays denied."""
    path = _make_protected(
        text_pdf, tmp_path, protect.ChangesAllowed.COMMENT_FORM_FILL, "commentable.pdf"
    )
    window = MainWindow()
    try:
        window.open_path(path)
        view = window.active_view
        assert view.can_annotate is True and view.can_edit_content is False
        view.begin_insert_comment()
        assert view.armed_action == "comment"
        for action in window._annotate_actions:
            assert action.isEnabled()
    finally:
        window.close()


# --- signing interplay -------------------------------------------------------


def test_sign_flow_warns_signed_copy_unprotected(qapp, encrypted_pdf, monkeypatch):
    monkeypatch.setattr(
        QInputDialog,
        "getText",
        staticmethod(lambda *a, **k: (encrypted_pdf.user_pw, True)),
    )
    questions = []

    def fake_question(*args, **kwargs):
        questions.append(args[2])
        return QMessageBox.StandardButton.No  # decline: flow must stop

    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))
    window = MainWindow()
    try:
        window.open_path(encrypted_pdf.path)
        window.show()
        window._run_sign_flow(window.active_view, page_index=None, rect=None)
        assert questions and "not be" in questions[-1].lower()
        assert window._tabs.count() == 1  # nothing signed
    finally:
        window.close()


def test_snapshot_undo_then_save_keeps_protection_ui(qapp, encrypted_pdf, monkeypatch):
    """The landmine at UI level: edit → undo → save must keep protection."""
    monkeypatch.setattr(
        QInputDialog,
        "getText",
        staticmethod(lambda *a, **k: (encrypted_pdf.user_pw, True)),
    )
    window = MainWindow()
    try:
        window.open_path(encrypted_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view.rotate_clockwise()  # an undoable command (snapshot-based)
        view.undo_stack.undo()
        window.save()
        assert view.document.is_protected is True
    finally:
        window.close()

    raw = pymupdf.open(str(encrypted_pdf.path))
    assert raw.needs_pass, "post-undo save stripped the protection"
    raw.close()


def test_unprotected_document_gating_unchanged(qapp, text_pdf):
    """Plain documents keep the full feature surface (no regression)."""
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        assert window._print_action.isEnabled()
        assert window._extract_text_action.isEnabled()
        for action in window._annotate_actions:
            assert action.isEnabled()
        assert window._protection_label.text() == ""
        window.active_view.set_edit_mode(True)
        window._sync_chrome()
        assert window._insert_text_action.isEnabled()
    finally:
        window.close()
