"""The single Qt<->engine seam: RenderedPage -> QImage / QPixmap.

This is the ONLY place in the app that bridges pdfcore's raw pixel bytes to Qt.
Keeping the conversion isolated here is what preserves the engine/UI boundary
(pdfcore never imports Qt; everything else in pdfapp works with Qt types).
"""

from __future__ import annotations

from PySide6.QtGui import QImage, QPixmap

from pdfcore.render import RenderedPage

# Components per pixel -> QImage format. 1 = grayscale (printing in B&W),
# 3 = RGB (screen + colour print), 4 = RGBA.
_CHANNELS_TO_FORMAT = {
    1: QImage.Format.Format_Grayscale8,
    3: QImage.Format.Format_RGB888,
    4: QImage.Format.Format_RGBA8888,
}


def rendered_page_to_qimage(rp: RenderedPage) -> QImage:
    """Convert a RenderedPage to a QImage that owns its pixel data."""
    fmt = _CHANNELS_TO_FORMAT[rp.channels]
    img = QImage(rp.samples, rp.width, rp.height, rp.stride, fmt)
    # .copy() detaches from the Python bytes buffer, which QImage does NOT take
    # ownership of. Without it, the buffer can be garbage-collected out from
    # under Qt, leaving a dangling pointer (crash / corrupted image).
    return img.copy()


def rendered_page_to_qpixmap(rp: RenderedPage) -> QPixmap:
    """Convert a RenderedPage to a QPixmap for display in a QGraphicsScene."""
    return QPixmap.fromImage(rendered_page_to_qimage(rp))
