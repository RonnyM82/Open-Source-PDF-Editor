"""The Sign dialog: pick who signs, prove the password, choose the look.

Collects everything the terminal signing operation needs — a profile (or a
bare certificate file), the PKCS#12 password, optional reason/location, and
whether the profile's signature image skins the visible stamp. The password
is validated IN the dialog (accept loads the signer via
``pdfcore.signing.load_pkcs12_signer``; a wrong password shows an inline
error and keeps the dialog open for another try) so the caller receives a
READY ``signer`` — the password itself never leaves this dialog and is never
stored. Pure Qt chrome (S5 convention: app-wide qt-material styling, nothing
per-dialog); offscreen tests set the fields and call ``_on_accept`` directly.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from pdfapp.signature_store import SignatureProfile
from pdfcore import signing

_NO_PROFILE = "(No profile — certificate file only)"


class SignDialog(QDialog):
    """Collect signer + options; ``.signer`` is ready once accepted."""

    def __init__(
        self,
        parent,
        profiles: list[SignatureProfile],
        *,
        default_p12: Path | None = None,
        visible_signature: bool = False,
        last_profile: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sign document")
        self._profiles = list(profiles)
        self._default_p12 = default_p12
        self._visible_signature = visible_signature
        self._cert_overridden = False  # a Browse pick outlives profile switches
        self.signer = None  # a loaded pyHanko signer once accepted

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self._profile_combo = QComboBox()
        self._profile_combo.addItem(_NO_PROFILE)
        for profile in self._profiles:
            self._profile_combo.addItem(profile.name)
        if last_profile:
            index = self._profile_combo.findText(last_profile)
            if index > 0:
                self._profile_combo.setCurrentIndex(index)
        form.addRow(QLabel("Sign as:"), self._profile_combo)

        cert_row = QHBoxLayout()
        self._cert_edit = QLineEdit()
        self._cert_edit.setReadOnly(True)
        self._cert_edit.setMinimumWidth(320)
        self._cert_edit.setPlaceholderText("Choose a certificate (.p12 / .pfx)")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._on_browse_cert)
        cert_row.addWidget(self._cert_edit, 1)
        cert_row.addWidget(browse)
        form.addRow(QLabel("Certificate:"), cert_row)

        self._password_edit = QLineEdit()
        self._password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow(QLabel("Password:"), self._password_edit)

        self._reason_edit = QLineEdit()
        self._reason_edit.setPlaceholderText("e.g. Approved for release (optional)")
        form.addRow(QLabel("Reason:"), self._reason_edit)
        self._location_edit = QLineEdit()
        self._location_edit.setPlaceholderText("(optional)")
        form.addRow(QLabel("Location:"), self._location_edit)
        layout.addLayout(form)

        self._image_check = QCheckBox("Use the profile's signature image as the stamp")
        self._image_check.setVisible(visible_signature)
        layout.addWidget(self._image_check)

        self._default_check = QCheckBox("Set as my default certificate")
        layout.addWidget(self._default_check)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        layout.addWidget(self._error_label)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setText("Sign")
        layout.addWidget(self._buttons)

        self._buttons.accepted.connect(self._on_accept)
        self._buttons.rejected.connect(self.reject)
        self._profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        self._cert_edit.textChanged.connect(self._sync_enabled)
        self._on_profile_changed()

    # --- state -----------------------------------------------------------
    def _current_profile(self) -> SignatureProfile | None:
        index = self._profile_combo.currentIndex() - 1  # 0 = no profile
        if 0 <= index < len(self._profiles):
            return self._profiles[index]
        return None

    def cert_path(self) -> Path | None:
        text = self._cert_edit.text().strip()
        return Path(text) if text else None

    def set_password(self, password: str) -> None:
        self._password_edit.setText(password)

    def _on_profile_changed(self) -> None:
        profile = self._current_profile()
        if not self._cert_overridden:
            resolved = (profile.p12_path if profile is not None else None) or self._default_p12
            self._cert_edit.setText(str(resolved) if resolved else "")
        has_image = profile is not None and profile.signature_image is not None
        self._image_check.setEnabled(self._visible_signature and has_image)
        self._image_check.setChecked(self._visible_signature and has_image)
        self._error_label.setText("")
        self._sync_enabled()

    def _on_browse_cert(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Choose certificate", "", "PKCS#12 certificates (*.p12 *.pfx)"
        )
        if path_str:
            self._cert_overridden = True
            self._cert_edit.setText(path_str)

    def _sync_enabled(self) -> None:
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setEnabled(self.cert_path() is not None)

    # --- accept: prove the password, produce the signer ------------------
    def _on_accept(self) -> None:
        cert = self.cert_path()
        if cert is None:
            return
        try:
            self.signer = signing.load_pkcs12_signer(cert, self._password_edit.text())
        except (ValueError, signing.CertificateLoadError) as exc:
            self.signer = None
            self._error_label.setText(str(exc))
            return
        self.accept()

    def spec(self) -> dict:
        """Everything the sign flow needs besides the loaded ``signer``."""
        profile = self._current_profile()
        use_image = (
            self._visible_signature and self._image_check.isChecked() and profile is not None
        )
        return {
            "profile_name": profile.name if profile is not None else None,
            "cert_path": self.cert_path(),
            "reason": self._reason_edit.text().strip() or None,
            "location": self._location_edit.text().strip() or None,
            "image_path": profile.signature_image if use_image else None,
            "save_default": self._default_check.isChecked(),
        }
