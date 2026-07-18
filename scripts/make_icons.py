"""Generate the shipping app-icon assets from the source SVG.

Dev / build-time ONLY — this is a one-off generator, NOT shipped and NOT a
runtime dependency. Re-run it after editing ``assets/icon.svg``:

    .venv\\Scripts\\python.exe scripts/make_icons.py

Outputs (committed to ``assets/``):
    icon.png  — 512x512, loaded at runtime as the QIcon (app / taskbar /
                title-bar icon).
    icon.ico  — MULTI-SIZE (256, 48, 32, 16) for the PyInstaller EXE file icon.
                A single-size .ico scales badly in Explorer; Windows picks the
                closest frame from a multi-size one.

Tooling (both dev-only — deliberately NOT added to pyproject runtime deps or
NOTICE, per the "no new runtime dependency for a one-off build step" rule):
    PySide6 QtSvg  — rasterises the SVG. Already a project dependency, and
                     reliable on Windows (unlike cairosvg, which needs native
                     Cairo DLLs that aren't present here).
    Pillow         — assembles the multi-frame .ico. Install once into the venv:
                         .venv\\Scripts\\python.exe -m pip install pillow
"""

from __future__ import annotations

import io
import os
import struct
from pathlib import Path

# Render headless — no display needed, and it must not pop a window.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SVG = ROOT / "assets" / "icon.svg"
PNG = ROOT / "assets" / "icon.png"
ICO = ROOT / "assets" / "icon.ico"

PNG_SIZE = 512
ICO_SIZES = [256, 48, 32, 16]  # Windows scales single-size .ico badly.

# The tile's rounded-rect clip in the source SVG: rx/ry = 30 within a 141px
# viewBox. QtSvg 6.11 does NOT honour the SVG's <clipPath>, so it renders the
# tile full-bleed with SHARP corners — we re-apply the rounding after
# rasterising so the asset matches the source design (rounded tile,
# transparent corners), scaled proportionally at every size.
SVG_VIEWBOX = 141.0
SVG_CORNER_RX = 30.0


def _render(svg: QSvgRenderer, size: int) -> QImage:
    """Rasterise the SVG to a square ``size`` px image, rounded to match the source.

    Rendered directly from the vector at each size (crisper than downscaling),
    then masked to the source's rounded-rect so the corners are transparent.
    """
    raw = QImage(size, size, QImage.Format.Format_ARGB32)
    raw.fill(Qt.GlobalColor.transparent)
    painter = QPainter(raw)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    svg.render(painter)
    painter.end()

    # Re-apply the rounded corners: paint an anti-aliased rounded rect, then
    # composite the render through it with SourceIn (keeps the render only where
    # the rounded mask is opaque — smooth corners, transparent outside).
    out = QImage(size, size, QImage.Format.Format_ARGB32)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(Qt.GlobalColor.white)
    radius = size * SVG_CORNER_RX / SVG_VIEWBOX
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

    # 1) 512x512 PNG for the runtime QIcon.
    _render(svg, PNG_SIZE).save(str(PNG), "PNG")
    print(f"wrote {PNG.name}: {PNG_SIZE}x{PNG_SIZE}, {PNG.stat().st_size:,} bytes")

    # 2) Multi-size .ico for the EXE file icon. Render each frame straight from
    #    the vector, then let Pillow pack them into one .ico container.
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
