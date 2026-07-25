"""Password protection: presets, spec validation, save round-trips, unlock.

The load-bearing regressions live here: KEEP preserving existing encryption
through BOTH save paths, the snapshot/restore landmine (snapshots used to
DROP encryption, so the first undo made every later save write plaintext),
and MuPDF's auth-level downgrade trap.
"""

from __future__ import annotations

import pymupdf
import pytest

from pdfcore import protect, signing
from pdfcore.document import PdfDocument
from pdfcore.protect import ChangesAllowed, Permissions, ProtectionSpec

ACC = pymupdf.PDF_PERM_ACCESSIBILITY
PRINT = pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_PRINT_HQ
COPY = pymupdf.PDF_PERM_COPY


# --- pure preset/table tests -------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "extra"),
    [
        (ChangesAllowed.NONE, 0),
        (ChangesAllowed.PAGES, pymupdf.PDF_PERM_ASSEMBLE),
        (ChangesAllowed.FORM_FILL, pymupdf.PDF_PERM_FORM),
        (
            ChangesAllowed.COMMENT_FORM_FILL,
            pymupdf.PDF_PERM_ANNOTATE | pymupdf.PDF_PERM_FORM,
        ),
        (
            ChangesAllowed.ANY_EXCEPT_EXTRACT,
            pymupdf.PDF_PERM_MODIFY
            | pymupdf.PDF_PERM_ANNOTATE
            | pymupdf.PDF_PERM_FORM
            | pymupdf.PDF_PERM_ASSEMBLE,
        ),
    ],
)
def test_permissions_mask_table(changes, extra):
    base = protect.permissions_mask(changes=changes, allow_print=False, allow_copy=False)
    assert base == ACC | extra  # accessibility ALWAYS granted
    assert (
        protect.permissions_mask(changes=changes, allow_print=True, allow_copy=False)
        == ACC | extra | PRINT
    )  # print is yes/no: BOTH bits together (150dpi dropped by decision)
    assert (
        protect.permissions_mask(changes=changes, allow_print=True, allow_copy=True)
        == ACC | extra | PRINT | COPY
    )


def test_permissions_from_mask():
    assert Permissions.from_mask(-1).all_allowed
    limited = Permissions.from_mask(ACC | PRINT)
    assert limited.can_print
    assert not (limited.can_modify or limited.can_copy or limited.can_annotate)
    assert not limited.all_allowed


def test_protection_spec_validation():
    with pytest.raises(ValueError, match="at least one password"):
        ProtectionSpec(user_pw=None, owner_pw=None, permissions=-1)
    with pytest.raises(ValueError, match="owner"):
        # Restricted permissions with nothing gating them is a lie.
        ProtectionSpec(user_pw="open", owner_pw=None, permissions=ACC | PRINT)
    # Full permissions with only an open password is fine.
    ProtectionSpec(
        user_pw="open",
        owner_pw=None,
        permissions=protect.permissions_mask(
            changes=ChangesAllowed.ANY_EXCEPT_EXTRACT, allow_print=True, allow_copy=True
        ),
    )


# --- protect round-trips -----------------------------------------------------


def _spec(user="open-pw", owner="perm-pw", changes=ChangesAllowed.NONE):
    return ProtectionSpec(
        user_pw=user,
        owner_pw=owner,
        permissions=protect.permissions_mask(changes=changes, allow_print=True, allow_copy=False),
    )


def test_protect_roundtrip_save(text_pdf, tmp_path):
    out = tmp_path / "protected.pdf"
    with PdfDocument.open(text_pdf) as doc:
        assert doc.is_protected is False
        doc.set_protection(_spec())
        doc.save(out)

    raw = pymupdf.open(str(out))
    assert raw.needs_pass  # open password demanded
    assert int(raw.authenticate("open-pw")) == 2  # user level
    perms = Permissions.from_mask(int(raw.permissions))
    assert perms.can_print and not perms.can_modify and not perms.can_copy
    assert int(raw.authenticate("perm-pw")) in (4, 6)  # owner level lifts
    assert Permissions.from_mask(int(raw.permissions)).all_allowed
    raw.close()

    with PdfDocument.open(out, password="open-pw") as doc:
        assert doc.is_protected is True
        assert doc.is_owner is False
        assert doc.unlock("perm-pw") is True
        assert doc.is_owner is True


def test_protect_roundtrip_save_in_place(text_pdf):
    with PdfDocument.open(text_pdf) as doc:
        pages_before = doc.page_count
        doc.set_protection(_spec())
        doc.save_in_place()
        # The internal reopen authenticated itself — the doc keeps working.
        assert doc.page_count == pages_before
        assert doc.render_page(0).width > 0
        assert doc.is_protected is True
        assert doc.is_owner is True  # we wrote both passwords
        # The choice PERSISTS (never consumed): undo past this save must not
        # be able to launder the protection away — every save re-applies.
        assert doc.pending_protection is not protect.KEEP

    reopened = pymupdf.open(str(text_pdf))
    assert reopened.needs_pass
    assert int(reopened.authenticate("open-pw")) == 2
    reopened.close()


def test_keep_preserves_encryption_on_save(encrypted_pdf, tmp_path):
    """The wart this milestone fixes: saves used to silently strip encryption."""
    out = tmp_path / "kept.pdf"
    with PdfDocument.open(encrypted_pdf.path, password=encrypted_pdf.user_pw) as doc:
        doc.rotate([0], 90)  # a real edit rides along
        doc.save(out)

    raw = pymupdf.open(str(out))
    assert raw.needs_pass
    assert int(raw.authenticate(encrypted_pdf.user_pw)) == 2
    raw.close()
    raw = pymupdf.open(str(out))
    assert int(raw.authenticate(encrypted_pdf.owner_pw)) in (4, 6)  # owner pw survives
    assert raw[0].rotation == 90  # the edit landed too
    raw.close()


def test_keep_preserves_encryption_on_save_in_place(encrypted_pdf):
    with PdfDocument.open(encrypted_pdf.path, password=encrypted_pdf.user_pw) as doc:
        doc.rotate([0], 90)
        doc.save_in_place()
        assert doc.page_count > 0  # internal re-auth worked
        assert doc.is_protected is True

    raw = pymupdf.open(str(encrypted_pdf.path))
    assert raw.needs_pass
    assert int(raw.authenticate(encrypted_pdf.owner_pw)) in (4, 6)
    raw.close()


def test_keep_survives_snapshot_restore_then_save(encrypted_pdf):
    """THE LANDMINE: snapshots used to drop encryption, so the first undo left
    KEEP with nothing to keep and every later save wrote plaintext."""
    with PdfDocument.open(encrypted_pdf.path, password=encrypted_pdf.user_pw) as doc:
        snap = doc.snapshot()
        doc.rotate([0], 90)
        doc.restore(snap)  # the undo path
        assert doc.is_protected is True  # encryption survived the round-trip
        assert doc.page_count > 0  # restore re-authenticated internally
        doc.save_in_place()

    raw = pymupdf.open(str(encrypted_pdf.path))
    assert raw.needs_pass, "post-undo save silently stripped the protection"
    assert int(raw.authenticate(encrypted_pdf.user_pw)) == 2
    raw.close()
    raw = pymupdf.open(str(encrypted_pdf.path))
    assert int(raw.authenticate(encrypted_pdf.owner_pw)) in (4, 6)
    raw.close()


def test_remove_protection(encrypted_pdf):
    with PdfDocument.open(encrypted_pdf.path, password=encrypted_pdf.user_pw) as doc:
        doc.set_protection(None)
        doc.save_in_place()
        assert doc.is_protected is False
        assert doc.auth_password is None

    raw = pymupdf.open(str(encrypted_pdf.path))
    assert not raw.needs_pass
    assert (raw.metadata or {}).get("encryption") is None
    raw.close()


# --- auth levels / unlock ----------------------------------------------------


def test_unlock_semantics(text_pdf, tmp_path):
    out = tmp_path / "levels.pdf"
    with PdfDocument.open(text_pdf) as doc:
        doc.set_protection(_spec())
        doc.save(out)

    with PdfDocument.open(out, password="open-pw") as doc:
        assert doc.permissions.can_print and not doc.permissions.can_modify
        assert doc.unlock("wrong") is False
        assert doc.permissions.can_print and not doc.permissions.can_modify
        # The USER password is not an unlock — and must not change the level.
        assert doc.unlock("open-pw") is False
        assert doc.is_owner is False
        assert doc.unlock("perm-pw") is True
        assert doc.permissions.all_allowed
        # The downgrade trap: a stray user-password unlock attempt AFTER an
        # owner unlock must not silently re-restrict the session.
        assert doc.unlock("open-pw") is False
        assert doc.permissions.all_allowed
        assert doc.is_owner is True


def test_restricted_owner_only_document(restricted_pdf):
    """Permissions-locked files auto-authenticate: no prompt, restricted."""
    with PdfDocument.open(restricted_pdf.path) as doc:
        assert doc.needs_pass is False  # the liar
        assert doc.is_protected is True  # the truth (metadata)
        assert doc.is_owner is False
        perms = doc.permissions
        assert perms.can_print and not perms.can_modify and not perms.can_annotate
        assert doc.unlock(restricted_pdf.owner_pw) is True
        assert doc.permissions.all_allowed


def test_restore_across_password_change(text_pdf, tmp_path):
    """Undo across a password-CHANGING save must not brick the document —
    the snapshot carries the OLD encryption, which the NEW password can't
    open (review finding: render raised 'document closed or encrypted')."""
    out = tmp_path / "repro.pdf"
    with PdfDocument.open(text_pdf) as doc:
        doc.set_protection(_spec(user="usrA", owner="ownA"))
        doc.save_in_place()
        snap = doc.snapshot()  # bytes carry the A encryption
        doc.set_protection(_spec(user="usrB", owner="ownB"))
        doc.save_in_place()  # password changes
        doc.restore(snap)  # the undo — must re-auth with the OLD password
        assert doc.page_count > 0
        assert doc.render_page(0).width > 0
        doc.save(out)  # and the doc stays fully usable

    # The persistent pending choice re-applied the CURRENT spec on save.
    raw = pymupdf.open(str(out))
    assert raw.needs_pass
    assert int(raw.authenticate("usrB")) == 2
    raw.close()


def test_restore_after_strip_does_not_brick(encrypted_pdf):
    """Undo across a password-STRIPPING save: the snapshot is encrypted, the
    current password is None — known session passwords must still open it."""
    with PdfDocument.open(encrypted_pdf.path, password=encrypted_pdf.user_pw) as doc:
        snap = doc.snapshot()  # encrypted bytes
        doc.set_protection(None)
        doc.save_in_place()  # file now plain; _password None
        doc.restore(snap)  # undo — must fall back to the session password
        assert doc.page_count > 0
        assert doc.render_page(0).width > 0
        assert doc.is_protected is True


def test_undo_past_protection_save_still_saves_protected(text_pdf):
    """The laundering hole: pre-protection snapshots are PLAIN — after undoing
    past the protection-applying save, the next save must RE-APPLY the choice
    (it persists), never silently write plaintext (review finding)."""
    with PdfDocument.open(text_pdf) as doc:
        snap = doc.snapshot()  # plain bytes, before protection
        doc.set_protection(_spec())
        doc.save_in_place()  # protection lands
        doc.restore(snap)  # undo past it — restored bytes are plain
        assert doc.is_protected is False  # in memory, honestly
        doc.save_in_place()  # but the CHOICE persists

    raw = pymupdf.open(str(text_pdf))
    assert raw.needs_pass, "undo past the protection save laundered it away"
    assert int(raw.authenticate("open-pw")) == 2
    raw.close()


def test_owner_only_spec_save_keeps_owner_level(text_pdf):
    """Applying a restrictions-only spec (no open password) must leave the
    OWNER at owner level after the save — needs_pass is False for owner-only
    files and the old gate skipped re-auth, locking the owner out of the
    document they protected seconds earlier (review finding)."""
    spec = ProtectionSpec(
        user_pw=None,
        owner_pw="perm-pw",
        permissions=protect.permissions_mask(
            changes=ChangesAllowed.NONE, allow_print=True, allow_copy=False
        ),
    )
    with PdfDocument.open(text_pdf) as doc:
        doc.set_protection(spec)
        doc.save_in_place()
        assert doc.is_protected is True
        assert doc.is_owner is True
        assert doc.permissions.all_allowed, "the owner got locked out of their own file"


def test_owner_unlock_survives_save_and_undo(restricted_pdf):
    """An owner unlock on an owner-only file must survive BOTH a KEEP save
    and a snapshot restore (needs_pass is False for these files — the old
    gates silently dropped the unlock on every save/undo)."""
    with PdfDocument.open(restricted_pdf.path) as doc:
        assert doc.unlock(restricted_pdf.owner_pw) is True
        snap = doc.snapshot()
        doc.rotate([0], 90)
        doc.save_in_place()
        assert doc.permissions.all_allowed, "the save dropped the owner unlock"
        assert doc.is_owner is True
        doc.restore(snap)
        assert doc.permissions.all_allowed, "the undo dropped the owner unlock"


def test_open_authenticates_owner_only_files(restricted_pdf):
    """PdfDocument.open must apply the supplied password even when needs_pass
    is False (owner-only files) — the save-as continuation depends on it."""
    with PdfDocument.open(restricted_pdf.path, password=restricted_pdf.owner_pw) as doc:
        assert doc.is_owner is True
        assert doc.permissions.all_allowed
        assert doc.auth_password == restricted_pdf.owner_pw


def test_save_in_place_oserror_recovery_encrypted(encrypted_pdf, monkeypatch):
    """A locked-target save failure on an ENCRYPTED doc must leave the doc
    usable (the temp bytes are encrypted too — the recovery reopen needs the
    same re-auth as the happy path) and the retry must succeed."""
    import os as os_module

    from pdfcore import document as document_module

    with PdfDocument.open(encrypted_pdf.path, password=encrypted_pdf.user_pw) as doc:
        doc.rotate([0], 90)
        real_replace = os_module.replace
        calls = {"n": 0}

        def failing_replace(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError("target is locked")
            return real_replace(src, dst)

        monkeypatch.setattr(document_module.os, "replace", failing_replace)
        with pytest.raises(PermissionError):
            doc.save_in_place()
        # Recovery: the doc is stream-backed from the encrypted temp bytes
        # and must be authenticated + fully usable.
        assert doc.page_count > 0
        assert doc.render_page(0).width > 0
        assert doc.is_protected is True
        doc.save_in_place()  # the retry succeeds

    raw = pymupdf.open(str(encrypted_pdf.path))
    assert raw.needs_pass
    assert int(raw.authenticate(encrypted_pdf.user_pw)) == 2
    assert raw[0].rotation == 90
    raw.close()


# --- interplay with signing (decision: signed copies are unprotected) --------


def test_save_signed_on_protected_is_unencrypted(encrypted_pdf, signer_p12, tmp_path):
    signer = signing.load_pkcs12_signer(signer_p12.path, signer_p12.password)
    out = tmp_path / "signed-copy.pdf"
    with PdfDocument.open(encrypted_pdf.path, password=encrypted_pdf.user_pw) as doc:
        doc.save_signed(out, signer)

    with PdfDocument.open(out) as doc:  # no password needed
        assert doc.is_protected is False
        assert doc.has_signatures() is True
    checks = signing.verify_pdf_signatures(out.read_bytes())
    assert checks and all(c.intact and c.valid for c in checks)
