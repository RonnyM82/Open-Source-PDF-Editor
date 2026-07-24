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

from pdfcore.links import GOTO, LinkInfo


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
        self._uri_edit.setPlaceholderText("https://example.com  or  mailto:name@example.com")
        self._uri_edit.setMinimumWidth(340)
        form.addRow(QLabel("Address:"), self._uri_edit)
        layout.addLayout(form)

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
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setEnabled(not uri_mode or bool(self._uri_edit.text().strip()))

    def _on_remove(self) -> None:
        self.removed = True
        self.accept()

    def _on_accept(self) -> None:
        if self._uri_mode() and not self._uri_edit.text().strip():
            return  # OK is disabled in this state, but guard the Enter key too
        self.accept()

    def spec(self) -> dict:
        """Engine kwargs for the chosen target (call only on a non-removed OK)."""
        if self._uri_mode():
            return {"uri": self._uri_edit.text().strip()}
        return {"dest_page": self._page_spin.value() - 1}

    def style_as_hyperlink(self) -> bool:
        """Whether to give the linked TEXT the blue-underline look (text links)."""
        return self._style_check is not None and self._style_check.isChecked()
