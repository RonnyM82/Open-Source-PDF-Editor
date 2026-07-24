"""Create/edit a hyperlink: choose a web/email address or an internal page.

A small modal used by DocumentView for both creating a link (after the user
drags out its rectangle) and editing an existing one. Pure Qt chrome; it holds
no PDF state — it returns a plain spec the view turns into an engine call.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
)

from pdfcore.links import GOTO, LinkInfo, normalize_uri


class LinkDialog(QDialog):
    """Pick a link target: a URI (web/email) or a go-to-page destination.

    After ``exec()`` returns ``QDialog.Accepted``: ``removed`` is True when the
    user chose *Remove link* (edit mode only); otherwise :meth:`spec` gives the
    engine kwargs (``{"uri": ...}`` or ``{"dest_page": ...}``).
    """

    def __init__(
        self,
        parent,
        page_count: int,
        *,
        initial: LinkInfo | None = None,
        current_page: int = 0,
        text_link: bool = False,
    ) -> None:
        super().__init__(parent)
        editing = initial is not None
        self.setWindowTitle("Edit link" if editing else "Add link")
        self.setModal(True)
        self.removed = False
        self._page_count = max(1, page_count)

        layout = QVBoxLayout(self)
        self._uri_radio = QRadioButton("Web or email address")
        self._page_radio = QRadioButton("Go to a page in this document")
        group = QButtonGroup(self)
        group.addButton(self._uri_radio)
        group.addButton(self._page_radio)
        layout.addWidget(self._uri_radio)

        form = QFormLayout()
        self._uri_edit = QLineEdit()
        self._uri_edit.setPlaceholderText("https://example.com  or  name@example.com")
        self._uri_edit.setMinimumWidth(340)
        form.addRow(QLabel("Address:"), self._uri_edit)
        layout.addLayout(form)
        # Live feedback: shows what will actually be stored (a scheme is added
        # for you), or why the address can't be used. Without this the dialog
        # happily wrote scheme-less text, which PDF stores as a broken
        # file-LAUNCH action whose target is lost.
        self._uri_hint = QLabel("")
        self._uri_hint.setWordWrap(True)
        layout.addWidget(self._uri_hint)

        layout.addWidget(self._page_radio)
        page_form = QFormLayout()
        self._page_spin = QSpinBox()
        self._page_spin.setRange(1, self._page_count)
        self._page_spin.setValue(min(self._page_count, current_page + 1))
        page_form.addRow(QLabel("Page:"), self._page_spin)
        layout.addLayout(page_form)

        # For a link OVER TEXT (not a drawn box), offer the Word-style look.
        self._style_check: QCheckBox | None = None
        if text_link and not editing:
            self._style_check = QCheckBox("Style text as a hyperlink (blue + underline)")
            self._style_check.setChecked(True)
            layout.addWidget(self._style_check)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        if editing:
            remove = QPushButton("Remove link")
            self._buttons.addButton(remove, QDialogButtonBox.ButtonRole.DestructiveRole)
            remove.clicked.connect(self._on_remove)
        layout.addWidget(self._buttons)

        self._buttons.accepted.connect(self._on_accept)
        self._buttons.rejected.connect(self.reject)
        self._uri_radio.toggled.connect(self._sync_enabled)
        self._uri_edit.textChanged.connect(self._sync_enabled)

        # Seed from the initial link (or default to a URI link on create).
        if editing and initial.kind == GOTO:
            self._page_radio.setChecked(True)
            if initial.dest_page is not None:
                self._page_spin.setValue(min(self._page_count, initial.dest_page + 1))
        else:
            self._uri_radio.setChecked(True)
            if editing and initial.uri:
                self._uri_edit.setText(initial.uri)
        self._sync_enabled()

    # --- helpers ----------------------------------------------------------
    def _uri_mode(self) -> bool:
        return self._uri_radio.isChecked()

    def _sync_enabled(self) -> None:
        uri_mode = self._uri_mode()
        self._uri_edit.setEnabled(uri_mode)
        self._page_spin.setEnabled(not uri_mode)
        typed = self._uri_edit.text().strip()
        normalized = normalize_uri(typed) if uri_mode else None
        if not uri_mode or not typed:
            self._uri_hint.setText("")
        elif normalized is None:
            self._uri_hint.setText("That isn't a usable web or email address.")
        elif normalized != typed:
            self._uri_hint.setText(f"Will be saved as: {normalized}")
        else:
            self._uri_hint.setText("")
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setEnabled(not uri_mode or normalized is not None)

    def _on_remove(self) -> None:
        self.removed = True
        self.accept()

    def _on_accept(self) -> None:
        # OK is disabled for an unusable address, but guard the Enter key too.
        if self._uri_mode() and normalize_uri(self._uri_edit.text()) is None:
            return
        self.accept()

    def spec(self) -> dict:
        """Engine kwargs for the chosen target (call only on a non-removed OK).

        The address is normalized (a missing scheme is added) so the engine
        never receives text that PDF would store as a broken launch action."""
        if self._uri_mode():
            return {"uri": normalize_uri(self._uri_edit.text()) or ""}
        return {"dest_page": self._page_spin.value() - 1}

    def style_as_hyperlink(self) -> bool:
        """Whether to give the linked TEXT the blue-underline look (text links)."""
        return self._style_check is not None and self._style_check.isChecked()
