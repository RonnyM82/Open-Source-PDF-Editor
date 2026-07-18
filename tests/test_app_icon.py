"""Guards for the shipping app-icon assets and the frozen-safe resource resolver.

Dependency-free: PNG/ICO headers are parsed with ``struct`` so the test needs
neither Pillow nor Qt. The generator (``scripts/make_icons.py``) uses those; the
committed ``assets/`` files are what actually ship, so those are what we assert.
"""

from __future__ import annotations

import struct
import sys

from pdfapp.resources import resource_path

EXPECTED_ICO_SIZES = {16, 32, 48, 256}


def _png_dimensions(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    assert data[12:16] == b"IHDR", "IHDR is not the first chunk"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _ico_sizes(data: bytes) -> set[int]:
    reserved, image_type, count = struct.unpack_from("<HHH", data, 0)
    assert reserved == 0 and image_type == 1, "not an .ico"
    sizes: set[int] = set()
    for i in range(count):
        # A width/height byte of 0 encodes 256 in the ICO directory entry.
        width, height = struct.unpack_from("<BB", data, 6 + i * 16)
        sizes.add(width or 256)
        assert (height or 256) == (width or 256), "non-square frame"
    return sizes


def test_icon_png_is_512_square():
    png = resource_path("assets/icon.png")
    assert png.exists(), f"missing {png}"
    assert _png_dimensions(png.read_bytes()) == (512, 512)


def test_icon_ico_is_multi_size():
    ico = resource_path("assets/icon.ico")
    assert ico.exists(), f"missing {ico}"
    sizes = _ico_sizes(ico.read_bytes())
    # A single-size .ico (which Windows scales badly) would fail this guard.
    assert len(sizes) >= 2
    assert sizes == EXPECTED_ICO_SIZES


def test_resource_path_dev_points_at_project_root():
    # In dev (no sys._MEIPASS) the path is <project-root>/<relative> and exists.
    got = resource_path("assets/icon.png")
    assert got.is_absolute()
    assert got.parent.name == "assets"
    assert got.exists()


def test_resource_path_frozen_uses_meipass(tmp_path, monkeypatch):
    # Frozen: files listed in the spec's `datas` unpack under sys._MEIPASS.
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert resource_path("assets/icon.png") == tmp_path / "assets" / "icon.png"
