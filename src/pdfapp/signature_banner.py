"""The per-document signature banner — Acrobat's bar, not a status-bar blip.

A status-bar message is transient and easy to miss (user report: "too subtle
… disappears after a few seconds"), so a signed document's verdict lives in a
banner strip across the top of its DocumentView. Two variants: "problem"
(broken / unverifiable signature) is PERMANENT — no dismiss control, it stays
for the life of the tab; "intact" is informational and dismissable (once per
document). Both carry a Details… button that opens Sign → Signature status….
Styled by ``theme.signature_banner_qss`` per mode; DocumentView.refresh_theme
re-applies it on a theme switch.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from pdfapp import theme


class SignatureBanner(QFrame):
    """The banner strip: message + Details…, plus Dismiss when allowed."""

    detailsRequested = Signal()
    dismissed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("signature_banner")
        self._problem = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)
        self._label = QLabel("")
        self._label.setWordWrap(True)
        layout.addWidget(self._label, 1)
        self._details = QPushButton("Details…")
        self._details.clicked.connect(self.detailsRequested)
        layout.addWidget(self._details, 0)
        self._dismiss = QPushButton("Dismiss")
        self._dismiss.clicked.connect(self._on_dismiss)
        layout.addWidget(self._dismiss, 0)
        self.hide()

    @property
    def problem(self) -> bool:
        """True when showing the permanent (non-dismissable) variant."""
        return self._problem

    def message(self) -> str:
        return self._label.text()

    def present(self, severity: str, message: str) -> None:
        """Show the banner: ``"problem"`` = permanent, ``"intact"`` = dismissable."""
        self._problem = severity == "problem"
        self._label.setText(message)
        self._dismiss.setVisible(not self._problem)
        self.refresh_theme()
        self.show()

    def refresh_theme(self) -> None:
        self.setStyleSheet(theme.signature_banner_qss(self._problem))

    def _on_dismiss(self) -> None:
        self.hide()
        self.dismissed.emit()
