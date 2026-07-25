"""File → "Protect document…": the password/permissions dialog.

Acrobat's "Password Security" surface, two sections: a Document Open password
(real AES-256 cryptography) and a permissions password with the standard
restriction set — printing (plain yes/no; the 150 dpi middle option was
deliberately dropped, user decision 2026-07-25), the five "Changes allowed"
levels verbatim, and the copy checkbox. Accessibility extraction is always
enabled (PDF 2.0 behaviour; a static note says so). Validation happens
in-dialog (inline error, stays open — the SignDialog pattern): at least one
section on, confirm fields match, the two passwords must differ (one password
cannot honestly be both levels — Acrobat's rule too). ``spec()`` returns a
``ProtectionSpec`` or ``None`` (the Remove-protection button, shown only when
the document is currently protected). Pure Qt chrome (S5 convention);
offscreen tests set fields and call ``_on_accept`` directly.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from pdfcore import protect
from pdfcore.protect import ChangesAllowed, ProtectionSpec

# Plain-words hint per "Changes allowed" level (user request: the presets are
# Acrobat's five — a LADDER over four permission bits, not mix-and-match, and
# the page-ops level forks off on its own — so each selection explains itself).
_CHANGES_HINTS: dict[ChangesAllowed, str] = {
    ChangesAllowed.NONE: (
        "No changes at all — the document is read-only. Printing and copying "
        "follow the settings below."
    ),
    ChangesAllowed.PAGES: (
        "Only whole-page changes: insert, delete, rotate and reorder pages. "
        "Content on pages — existing or inserted — cannot be edited."
    ),
    ChangesAllowed.FORM_FILL: (
        "Only filling in form fields and signing existing signature fields — "
        "no other changes. (This app has no form editing, so here the "
        "document behaves as read-only.)"
    ),
    ChangesAllowed.COMMENT_FORM_FILL: (
        "Commenting (highlights, comments, callouts) plus form filling and "
        "signing. No content or page changes."
    ),
    ChangesAllowed.ANY_EXCEPT_EXTRACT: (
        "Full editing — everything except extracting pages into other documents."
    ),
}

# Combo order == Acrobat's dropdown order.
_CHANGES_OPTIONS: list[tuple[str, ChangesAllowed]] = [
    ("None", ChangesAllowed.NONE),
    ("Inserting, deleting and rotating pages", ChangesAllowed.PAGES),
    (
        "Filling in form fields and signing existing signature fields",
        ChangesAllowed.FORM_FILL,
    ),
    (
        "Commenting, filling in form fields, and signing existing signature fields",
        ChangesAllowed.COMMENT_FORM_FILL,
    ),
    ("Any except extracting pages", ChangesAllowed.ANY_EXCEPT_EXTRACT),
]


def _password_row(form: QFormLayout, label: str) -> tuple[QLineEdit, QLineEdit]:
    edit = QLineEdit()
    edit.setEchoMode(QLineEdit.EchoMode.Password)
    confirm = QLineEdit()
    confirm.setEchoMode(QLineEdit.EchoMode.Password)
    form.addRow(QLabel(label), edit)
    form.addRow(QLabel("Confirm:"), confirm)
    return edit, confirm


class ProtectDialog(QDialog):
    """Collect a ProtectionSpec (or removal); ``spec()`` after accept."""

    def __init__(self, parent, *, currently_protected: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("Protect document")
        self.removed = False
        layout = QVBoxLayout(self)

        self._open_group = QGroupBox("Require a password to open the document")
        self._open_group.setCheckable(True)
        self._open_group.setChecked(False)
        open_form = QFormLayout(self._open_group)
        self._open_pw, self._open_confirm = _password_row(open_form, "Open password:")
        layout.addWidget(self._open_group)

        self._restrict_group = QGroupBox("Restrict editing and printing")
        self._restrict_group.setCheckable(True)
        self._restrict_group.setChecked(False)
        restrict_form = QFormLayout(self._restrict_group)
        self._perm_pw, self._perm_confirm = _password_row(restrict_form, "Permissions password:")
        self._print_check = QCheckBox("Allow printing")
        self._print_check.setChecked(True)
        restrict_form.addRow(self._print_check)
        self._changes_combo = QComboBox()
        for label, _value in _CHANGES_OPTIONS:
            self._changes_combo.addItem(label)
        restrict_form.addRow(QLabel("Changes allowed:"), self._changes_combo)
        self._changes_hint = QLabel("")
        self._changes_hint.setWordWrap(True)
        restrict_form.addRow(self._changes_hint)
        self._changes_combo.currentIndexChanged.connect(self._update_changes_hint)
        self._update_changes_hint()
        self._copy_check = QCheckBox("Enable copying of text, images, and other content")
        self._copy_check.setChecked(True)
        restrict_form.addRow(self._copy_check)
        note = QLabel("Text access for screen readers is always enabled.")
        note.setWordWrap(True)
        restrict_form.addRow(note)
        layout.addWidget(self._restrict_group)

        caveat = QLabel(
            "The open password is real encryption. Editing/printing restrictions "
            "are honoured by compliant PDF readers (including this one) but are "
            "not a security boundary."
        )
        caveat.setWordWrap(True)
        layout.addWidget(caveat)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        layout.addWidget(self._error_label)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setText("Protect")
        if currently_protected:
            remove = QPushButton("Remove protection")
            self._buttons.addButton(remove, QDialogButtonBox.ButtonRole.DestructiveRole)
            remove.clicked.connect(self._on_remove)
        layout.addWidget(self._buttons)
        self._buttons.accepted.connect(self._on_accept)
        self._buttons.rejected.connect(self.reject)

    # --- programmatic setters (offscreen tests + seeding) -----------------
    def set_open_password(self, password: str | None) -> None:
        self._open_group.setChecked(password is not None)
        self._open_pw.setText(password or "")
        self._open_confirm.setText(password or "")

    def set_restrictions(
        self,
        password: str,
        *,
        changes: ChangesAllowed,
        allow_print: bool = True,
        allow_copy: bool = True,
    ) -> None:
        self._restrict_group.setChecked(True)
        self._perm_pw.setText(password)
        self._perm_confirm.setText(password)
        self._print_check.setChecked(allow_print)
        self._copy_check.setChecked(allow_copy)
        for index, (_label, value) in enumerate(_CHANGES_OPTIONS):
            if value is changes:
                self._changes_combo.setCurrentIndex(index)
                break

    def _update_changes_hint(self) -> None:
        level = _CHANGES_OPTIONS[self._changes_combo.currentIndex()][1]
        self._changes_hint.setText(_CHANGES_HINTS[level])

    # --- accept / result --------------------------------------------------
    def _fail(self, message: str) -> None:
        self._error_label.setText(message)

    def _on_accept(self) -> None:
        open_on = self._open_group.isChecked()
        restrict_on = self._restrict_group.isChecked()
        if not open_on and not restrict_on:
            return self._fail("Turn on at least one protection, or Cancel.")
        if open_on:
            if not self._open_pw.text():
                return self._fail("The open password must not be empty.")
            if self._open_pw.text() != self._open_confirm.text():
                return self._fail("The open passwords don't match.")
        if restrict_on:
            if not self._perm_pw.text():
                return self._fail("The permissions password must not be empty.")
            if self._perm_pw.text() != self._perm_confirm.text():
                return self._fail("The permissions passwords don't match.")
        if open_on and restrict_on and self._open_pw.text() == self._perm_pw.text():
            return self._fail(
                "The open and permissions passwords must be different — one "
                "password can't honestly be both levels."
            )
        self._error_label.setText("")
        self.accept()

    def _on_remove(self) -> None:
        self.removed = True
        self.accept()

    def spec(self) -> ProtectionSpec | None:
        """The accepted protection; None = remove protection."""
        if self.removed:
            return None
        open_on = self._open_group.isChecked()
        restrict_on = self._restrict_group.isChecked()
        if restrict_on:
            changes = _CHANGES_OPTIONS[self._changes_combo.currentIndex()][1]
            mask = protect.permissions_mask(
                changes=changes,
                allow_print=self._print_check.isChecked(),
                allow_copy=self._copy_check.isChecked(),
            )
            return ProtectionSpec(
                user_pw=self._open_pw.text() if open_on else None,
                owner_pw=self._perm_pw.text(),
                permissions=mask,
            )
        # Open password only: no restrictions — full permissions.
        return ProtectionSpec(
            user_pw=self._open_pw.text(),
            owner_pw=None,
            permissions=protect.permissions_mask(
                changes=ChangesAllowed.ANY_EXCEPT_EXTRACT,
                allow_print=True,
                allow_copy=True,
            ),
        )
