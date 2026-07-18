"""Thumbnail sidebar: a scrollable list of low-DPI page previews.

Emits ``pageSelected(index)`` when the selection changes (click or keyboard).
Selection is kept in sync with the main view by the window via ``set_current``.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QListWidget, QListWidgetItem

_ICON_SIZE = QSize(120, 160)


class ThumbnailPanel(QListWidget):
    pageSelected = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setViewMode(QListWidget.ViewMode.ListMode)
        self.setIconSize(_ICON_SIZE)
        self.setMovement(QListWidget.Movement.Static)
        self.setSpacing(4)
        self.setUniformItemSizes(False)
        self.currentRowChanged.connect(self._on_row_changed)

    def set_thumbnails(self, pixmaps: list[QPixmap]) -> None:
        """Replace all thumbnails. ``pixmaps[i]`` is the preview for page ``i``."""
        self.blockSignals(True)
        self.clear()
        for i, pixmap in enumerate(pixmaps):
            item = QListWidgetItem(QIcon(pixmap), str(i + 1))
            item.setData(Qt.ItemDataRole.UserRole, i)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            self.addItem(item)
        self.blockSignals(False)

    def set_current(self, index: int) -> None:
        """Highlight ``index`` without emitting pageSelected (avoids nav loops)."""
        self.blockSignals(True)
        self.setCurrentRow(index)
        self.blockSignals(False)

    def update_thumbnail(self, index: int, pixmap: QPixmap) -> None:
        """Replace the preview for a single page (used after a rotate)."""
        item = self.item(index)
        if item is not None:
            item.setIcon(QIcon(pixmap))

    def _on_row_changed(self, row: int) -> None:
        if row >= 0:
            self.pageSelected.emit(row)
