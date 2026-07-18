"""Generate the PDF document / file-type icon from its source SVG.

Dev / build-time ONLY — a one-off generator, NOT shipped and NOT a runtime
dependency (same rules as ``make_icons.py``). Re-run it after editing
``assets/file icon.svg``:

    .venv\\Scripts\\python.exe scripts/make_file_icon.py

Output (committed to ``assets/``):
    pdf-document.ico — MULTI-SIZE (256, 48, 32, 16). This is the icon Windows
        Explorer shows on .pdf FILES once PDF Editor is the default handler
        (the installer's ProgID DefaultIcon). It is DISTINCT from the app icon
        (``assets/icon.ico``), which stays the Start-menu / exe icon. A
        single-size .ico scales badly at 16/32 px in Explorer — hence all four
        frames in one container.

Tooling (both dev-only, deliberately NOT in pyproject runtime deps / NOTICE):
    PySide6 QtSvg — rasterises the SVG (already a project dependency; reliable
                    on Windows, unlike cairosvg's native Cairo DLLs).
    Pillow        — packs the multi-frame .ico.

Mirrors ``make_icons.py`` intentionally: the source SVG wraps its artwork in a
rounded-rect <clipPath>, but QtSvg 6.11 does NOT honour <clipPath>, so the
full-bleed blue tile renders with SHARP corners — we re-apply the rounding
after rasterising so the asset matches the source design.
"""

from __future__ import annotations

import io
import os
import struct
from pathlib import Path

# Render headless — no display, must not pop a window.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SVG = ROOT / "assets" / "file icon.svg"
ICO = ROOT / "assets" / "pdf-document.ico"

ICO_SIZES = [256, 48, 32, 16]  # Windows scales single-size .ico badly.

# The source SVG's rounded-rect clip: rx/ry = 30 on a 140px tile. QtSvg 6.11
# ignores <clipPath>, so re-apply the rounding after rasterising (transparent
# corners, matching the design at every size).
SVG_TILE = 140.0
SVG_CORNER_RX = 30.0


def _render(svg: QSvgRenderer, size: int) -> QImage:
    """Rasterise the SVG to a square ``size`` px image, rounded to match the source."""
    raw = QImage(size, size, QImage.Format.Format_ARGB32)
    raw.fill(Qt.GlobalColor.transparent)
    painter = QPainter(raw)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    svg.render(painter)
    painter.end()

    # Composite the render through an anti-aliased rounded-rect mask (SourceIn):
    # keeps pixels only where the mask is opaque — smooth corners, transparent
    # outside.
    out = QImage(size, size, QImage.Format.Format_ARGB32)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(Qt.GlobalColor.white)
    radius = size * SVG_CORNER_RX / SVG_TILE
    painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.drawImage(0, 0, raw)
    painter.end()
    return out


def _png_bytes(img: QImage) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba.data())


def _summarise_ico(path: Path) -> str:
    """Read back the ICONDIR so the caller can confirm real multi-frame output."""
    data = path.read_bytes()
    _reserved, _type, count = struct.unpack_from("<HHH", data, 0)
    frames = []
    for i in range(count):
        w, h = struct.unpack_from("<BB", data, 6 + i * 16)
        frames.append(f"{w or 256}x{h or 256}")
    return f"{count} frames [{', '.join(frames)}], {len(data):,} bytes"


def main() -> None:
    if not SVG.exists():
        raise SystemExit(f"source SVG not found: {SVG}")

    QGuiApplication([])  # a paint device needs a GUI app instance
    svg = QSvgRenderer(str(SVG))
    if not svg.isValid():
        raise SystemExit(f"QtSvg could not parse {SVG}")

    from PIL import Image

    frames = [
        Image.open(io.BytesIO(_png_bytes(_render(svg, s)))).convert("RGBA") for s in ICO_SIZES
    ]
    largest, rest = frames[0], frames[1:]
    largest.save(
        str(ICO),
        format="ICO",
        sizes=[(s, s) for s in ICO_SIZES],
        append_images=rest,
    )
    print(f"wrote {ICO.name}: {_summarise_ico(ICO)}")


if __name__ == "__main__":
    main()
