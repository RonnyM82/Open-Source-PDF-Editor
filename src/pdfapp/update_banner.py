"""The app-level "an update is available" banner.

One strip across the top of the MainWindow, above the tab widget, because an
available update is a fact about the APP, not about any one open document (the
signature banner, which this mirrors, is per document for the opposite reason).

Four ways out, and the difference between them is the whole point:

- the primary button either upgrades in place or opens the download page,
  depending on whether this build can replace itself (only the installed one
  can — see ``updates.can_self_update``);
- "Remind me in 7 days" defers by TIME, so it stays quiet for the week no
  matter what ships in the meantime;
- "Skip this version" defers by VERSION, so it stays quiet until something
  newer than the offered release ships, and then speaks up on its own;
- the close cross writes nothing at all, so the banner simply returns at the
  next launch.

Styled by ``theme.update_banner_qss``; MainWindow re-applies it on a theme
switch.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from pdfapp import theme
from pdfapp.updates import UpdateInfo, available_message

UPDATE_NOW = "Update now"
DOWNLOAD_PAGE = "Open download page"


class UpdateBanner(QFrame):
    """The banner strip: message, primary action, the two deferrals, a close cross."""

    updateRequested = Signal()
    remindLaterRequested = Signal()
    skipRequested = Signal()
    dismissed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("update_banner")
        self._info: UpdateInfo | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)

        self._label = QLabel("")
        self._label.setWordWrap(True)
        layout.addWidget(self._label, 1)

        self._primary = QPushButton(UPDATE_NOW)
        self._primary.clicked.connect(self.updateRequested)
        layout.addWidget(self._primary, 0)

        self._remind = QPushButton("Remind me in 7 days")
        self._remind.clicked.connect(self._on_remind)
        layout.addWidget(self._remind, 0)

        self._skip = QPushButton("Skip this version")
        self._skip.clicked.connect(self._on_skip)
        layout.addWidget(self._skip, 0)

        self._close = QPushButton("✕")
        self._close.setToolTip("Hide until the next launch")
        self._close.setFixedWidth(28)
        self._close.clicked.connect(self._on_close)
        layout.addWidget(self._close, 0)

        self.hide()

    # --- state ----------------------------------------------------------
    @property
    def info(self) -> UpdateInfo | None:
        """The release currently being offered (None when hidden)."""
        return self._info

    def message(self) -> str:
        return self._label.text()

    def primary_text(self) -> str:
        """Which action the primary button offers — the in-place upgrade or the
        download page. Tests assert this instead of clicking through a browser."""
        return self._primary.text()

    # --- presentation ---------------------------------------------------
    def present(self, info: UpdateInfo, current: str, *, can_update: bool) -> None:
        """Offer ``info``. ``can_update`` False (portable / dev, or a release with
        no installer asset) swaps the primary button for the download page."""
        self._info = info
        self._label.setText(available_message(current, info.version))
        self._primary.setText(UPDATE_NOW if can_update else DOWNLOAD_PAGE)
        self.refresh_theme()
        self.show()

    def refresh_theme(self) -> None:
        self.setStyleSheet(theme.update_banner_qss())

    # --- the ways out ---------------------------------------------------
    def _on_remind(self) -> None:
        self.hide()
        self.remindLaterRequested.emit()

    def _on_skip(self) -> None:
        self.hide()
        self.skipRequested.emit()

    def _on_close(self) -> None:
        self.hide()
        self.dismissed.emit()
