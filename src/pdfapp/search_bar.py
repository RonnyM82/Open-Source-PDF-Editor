"""The in-document search bar (SR2) — one per DocumentView, hidden until Ctrl+F.

Per-tab search state travels with its view for free (the multiple-documents
rule: MainWindow stays thin chrome and owns only the Find action). Search is
ALWAYS case-insensitive — by design (user decision 2026-07-04); there is no
match-case control here and none should be added.

Key routing: with the query field focused, Enter/Shift+Enter step matches
(``returnPressed`` + LIVE modifier read — the house rule: never track key
state) and Esc closes via this widget's own keyPressEvent (QLineEdit ignores
Esc, so it bubbles here — the canvas never sees it, no priority conflicts).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QToolButton,
    QWidget,
)

from pdfapp import icons


class SearchBar(QWidget):
    queryChanged = Signal(str)
    nextRequested = Signal()
    prevRequested = Signal()
    closeRequested = Signal()
    # The SR4 offer: "this document has scanned pages — OCR them?" The view
    # decides when the button shows; clicking it is the user-initiated opt-in.
    ocrRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._query = QLineEdit(self)
        self._query.setPlaceholderText("Find in document")
        self._query.textChanged.connect(self.queryChanged)
        self._query.returnPressed.connect(self._on_return)
        self._count = QLabel("", self)
        self._count.setObjectName("search_count")
        self._ocr_offer = QToolButton(self)
        self._ocr_offer.setText("Search scanned pages (OCR)")
        self._ocr_offer.setToolTip(
            "Some pages have no text layer — recognise their text so search can see it"
        )
        self._ocr_offer.clicked.connect(self.ocrRequested)
        self._ocr_offer.hide()
        # Review comments are searched only on OPT-IN (E11, user decision:
        # off by default). Toggling re-runs the current query.
        self._comments_check = QCheckBox("Comments", self)
        self._comments_check.setChecked(False)
        self._comments_check.setToolTip("Also search review comments")
        self._comments_check.toggled.connect(lambda _on: self.queryChanged.emit(self.query()))
        self._prev = QToolButton(self)
        self._prev.setToolTip("Previous match (Shift+Enter)")
        self._prev.clicked.connect(self.prevRequested)
        self._next = QToolButton(self)
        self._next.setToolTip("Next match (Enter)")
        self._next.clicked.connect(self.nextRequested)
        self._close_button = QToolButton(self)
        self._close_button.setToolTip("Close search (Esc)")
        self._close_button.clicked.connect(self.closeRequested)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.addWidget(self._query, 1)
        layout.addWidget(self._count)
        layout.addWidget(self._comments_check)
        layout.addWidget(self._ocr_offer)
        layout.addWidget(self._prev)
        layout.addWidget(self._next)
        layout.addWidget(self._close_button)
        self.refresh_theme()
        self.hide()

    def show_ocr_offer(self, visible: bool) -> None:
        self._ocr_offer.setVisible(visible)

    def ocr_offer_visible(self) -> bool:
        # visibleTo: honest even while the whole bar is hidden offscreen.
        return self._ocr_offer.isVisibleTo(self)

    def refresh_theme(self) -> None:
        """Re-bake button icons — this widget is long-lived, unlike dialogs."""
        self._prev.setIcon(icons.icon("search_prev"))
        self._next.setIcon(icons.icon("search_next"))
        self._close_button.setIcon(icons.icon("search_close"))

    def open_bar(self) -> None:
        """Show + focus the query with its text selected (retype to replace)."""
        self.show()
        self._query.setFocus()
        self._query.selectAll()

    def query(self) -> str:
        return self._query.text()

    def include_comments(self) -> bool:
        return self._comments_check.isChecked()

    def set_status(self, text: str) -> None:
        """The count / message label ("3 of 17", "No matches", …)."""
        self._count.setText(text)

    def status(self) -> str:
        return self._count.text()

    def _on_return(self) -> None:
        # QLineEdit accepts Return itself, so keyPressEvent never sees it —
        # returnPressed + a LIVE modifier read routes Shift+Enter to prev.
        if QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.prevRequested.emit()
        else:
            self.nextRequested.emit()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.closeRequested.emit()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.prevRequested.emit()
            else:
                self.nextRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)
