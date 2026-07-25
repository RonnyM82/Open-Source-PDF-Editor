"""Sign → "Manage signatures…": the signature-library editor.

Add/remove stored signing identities (name + signature image + optional
initials image + optional per-person certificate), set the app-wide DEFAULT
certificate (the ``default_signing_p12`` settings key — used by any profile
without its own), and generate a self-signed certificate for tamper-evidence.
Passwords are never collected here — signing prompts for them at signing
time. Pure Qt chrome (S5 convention); the interactive pickers are thin
wrappers over core methods (``add_profile`` / ``remove_profile`` /
``set_default_certificate`` / ``generate_certificate``) that offscreen tests
drive directly.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from pdfapp.settings import Settings
from pdfapp.signature_store import SignatureStore
from pdfcore import signing

DEFAULT_P12_KEY = "default_signing_p12"

_IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.bmp)"
_P12_FILTER = "PKCS#12 certificates (*.p12 *.pfx)"


class SignatureManagerDialog(QDialog):
    """The profile list + default certificate + self-signed generation."""

    def __init__(self, parent, store: SignatureStore, settings: Settings) -> None:
        super().__init__(parent)
        self.setWindowTitle("Manage signatures")
        self._store = store
        self._settings = settings

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Stored signatures (people you are authorised to sign for):"))
        self._list = QListWidget()
        self._list.setIconSize(QSize(96, 36))
        layout.addWidget(self._list, 1)

        buttons_row = QHBoxLayout()
        add_button = QPushButton("Add…")
        add_button.clicked.connect(self._on_add_clicked)
        remove_button = QPushButton("Remove")
        remove_button.clicked.connect(self._on_remove_clicked)
        buttons_row.addWidget(add_button)
        buttons_row.addWidget(remove_button)
        buttons_row.addStretch(1)
        layout.addLayout(buttons_row)

        default_row = QHBoxLayout()
        default_row.addWidget(QLabel("Default certificate:"))
        self._default_edit = QLineEdit()
        self._default_edit.setReadOnly(True)
        self._default_edit.setPlaceholderText("(none — profiles need their own)")
        browse_default = QPushButton("Browse…")
        browse_default.clicked.connect(self._on_browse_default)
        clear_default = QPushButton("Clear")
        clear_default.clicked.connect(lambda: self.set_default_certificate(None))
        default_row.addWidget(self._default_edit, 1)
        default_row.addWidget(browse_default)
        default_row.addWidget(clear_default)
        layout.addLayout(default_row)

        generate_button = QPushButton("Generate self-signed certificate…")
        generate_button.clicked.connect(self._on_generate_clicked)
        layout.addWidget(generate_button)
        note = QLabel(
            "A self-signed certificate proves the document hasn't changed after "
            "signing, but readers show it as untrusted until the recipient "
            "chooses to trust it."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        close_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_buttons.rejected.connect(self.reject)
        layout.addWidget(close_buttons)
        self.resize(520, 460)
        self.refresh()

    # --- core (test-drivable, no dialogs) --------------------------------
    def refresh(self) -> None:
        """Rebuild the list from the store."""
        self._list.clear()
        for profile in self._store.profiles():
            label = profile.name
            if profile.p12_path is None:
                label += "   (uses the default certificate)"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, profile.name)
            pixmap = QPixmap(str(profile.signature_image))
            if not pixmap.isNull():
                item.setIcon(QIcon(pixmap))
            self._list.addItem(item)
        current = self.default_certificate()
        self._default_edit.setText(str(current) if current else "")

    def profile_names(self) -> list[str]:
        return [
            self._list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self._list.count())
        ]

    def add_profile(
        self,
        name: str,
        signature_image: str | Path,
        *,
        initials_image: str | Path | None = None,
        p12_path: str | Path | None = None,
    ) -> str | None:
        """Add to the store; returns an error message, or None on success."""
        try:
            self._store.add(name, signature_image, initials_image=initials_image, p12_path=p12_path)
        except ValueError as exc:
            if self.isVisible():
                QMessageBox.warning(self, "Couldn't add signature", str(exc))
            return str(exc)
        self.refresh()
        return None

    def remove_profile(self, name: str) -> None:
        self._store.remove(name)
        self.refresh()

    def default_certificate(self) -> Path | None:
        value = self._settings.get(DEFAULT_P12_KEY)
        return Path(value) if isinstance(value, str) and value else None

    def set_default_certificate(self, path: Path | None) -> None:
        self._settings.set(DEFAULT_P12_KEY, str(path) if path else None)
        self._default_edit.setText(str(path) if path else "")

    def generate_certificate(
        self,
        out_path: str | Path,
        common_name: str,
        password: str,
        *,
        set_default: bool = True,
    ) -> str | None:
        """Generate a self-signed .p12; returns an error message or None."""
        try:
            created = signing.generate_self_signed_p12(out_path, common_name, password)
        except (ValueError, OSError) as exc:
            if self.isVisible():
                QMessageBox.warning(self, "Couldn't generate certificate", str(exc))
            return str(exc)
        if set_default:
            self.set_default_certificate(created)
        return None

    # --- interactive wrappers (gated for offscreen tests) ----------------
    def _on_add_clicked(self) -> None:
        if not self.isVisible():
            return  # offscreen tests drive add_profile directly
        form = _ProfileForm(self)
        if form.exec() != QDialog.DialogCode.Accepted:
            return
        values = form.values()
        self.add_profile(
            values["name"],
            values["signature_image"],
            initials_image=values["initials_image"],
            p12_path=values["p12_path"],
        )

    def _on_remove_clicked(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        if self.isVisible():
            answer = QMessageBox.question(
                self,
                "Remove signature",
                f"Remove the stored signature for {name!r}?\n\n"
                "Its copied signature/initials images are deleted too.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.remove_profile(name)

    def _on_browse_default(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(self, "Choose certificate", "", _P12_FILTER)
        if path_str:
            self.set_default_certificate(Path(path_str))

    def _on_generate_clicked(self) -> None:
        if not self.isVisible():
            return  # offscreen tests drive generate_certificate directly
        form = _GenerateForm(self)
        if form.exec() != QDialog.DialogCode.Accepted:
            return
        values = form.values()
        error = self.generate_certificate(
            values["out_path"],
            values["common_name"],
            values["password"],
            set_default=values["set_default"],
        )
        if error is None:
            QMessageBox.information(
                self,
                "Certificate created",
                f"Self-signed certificate saved to:\n{values['out_path']}\n\n"
                "Keep the password safe — it is needed every time you sign, "
                "and it is never stored.",
            )


class _ProfileForm(QDialog):
    """Add-a-profile sub-form: name + images + optional certificate."""

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add signature")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._name_edit = QLineEdit()
        form.addRow(QLabel("Name:"), self._name_edit)
        self._signature_edit = self._path_row(form, "Signature image:", _IMAGE_FILTER)
        self._initials_edit = self._path_row(form, "Initials image (optional):", _IMAGE_FILTER)
        self._p12_edit = self._path_row(form, "Certificate (optional):", _P12_FILTER)
        layout.addLayout(form)
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)
        self._name_edit.textChanged.connect(self._sync_enabled)
        self._signature_edit.textChanged.connect(self._sync_enabled)
        self._sync_enabled()

    def _path_row(self, form: QFormLayout, label: str, name_filter: str) -> QLineEdit:
        row = QHBoxLayout()
        edit = QLineEdit()
        browse = QPushButton("Browse…")

        def pick() -> None:
            path_str, _ = QFileDialog.getOpenFileName(self, "Choose file", "", name_filter)
            if path_str:
                edit.setText(path_str)

        browse.clicked.connect(pick)
        row.addWidget(edit, 1)
        row.addWidget(browse)
        form.addRow(QLabel(label), row)
        return edit

    def _sync_enabled(self) -> None:
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setEnabled(
                bool(self._name_edit.text().strip()) and bool(self._signature_edit.text().strip())
            )

    def values(self) -> dict:
        return {
            "name": self._name_edit.text().strip(),
            "signature_image": self._signature_edit.text().strip(),
            "initials_image": self._initials_edit.text().strip() or None,
            "p12_path": self._p12_edit.text().strip() or None,
        }


class _GenerateForm(QDialog):
    """Generate-a-self-signed-certificate sub-form."""

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.setWindowTitle("Generate self-signed certificate")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("The signer's name shown in PDF readers")
        form.addRow(QLabel("Name:"), self._name_edit)
        self._password_edit = QLineEdit()
        self._password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow(QLabel("Password:"), self._password_edit)
        self._confirm_edit = QLineEdit()
        self._confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow(QLabel("Confirm password:"), self._confirm_edit)

        path_row = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("Where to save the .p12 file")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._on_browse)
        path_row.addWidget(self._path_edit, 1)
        path_row.addWidget(browse)
        form.addRow(QLabel("Save to:"), path_row)
        layout.addLayout(form)

        self._default_check = QCheckBox("Set as my default certificate")
        self._default_check.setChecked(True)
        layout.addWidget(self._default_check)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)
        for edit in (self._name_edit, self._password_edit, self._confirm_edit, self._path_edit):
            edit.textChanged.connect(self._sync_enabled)
        self._sync_enabled()

    def _on_browse(self) -> None:
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Save certificate", "signing-certificate.p12", _P12_FILTER
        )
        if path_str:
            self._path_edit.setText(path_str)

    def _sync_enabled(self) -> None:
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setEnabled(
                bool(self._name_edit.text().strip())
                and bool(self._password_edit.text())
                and self._password_edit.text() == self._confirm_edit.text()
                and bool(self._path_edit.text().strip())
            )

    def values(self) -> dict:
        return {
            "common_name": self._name_edit.text().strip(),
            "password": self._password_edit.text(),
            "out_path": Path(self._path_edit.text().strip()),
            "set_default": self._default_check.isChecked(),
        }
