# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for PDF Editor — one-folder (onedir) Windows build.
# Build via:  .\scripts\package.ps1   (or: pyinstaller pdf_editor.spec --noconfirm)
#
# Notes:
# - PyMuPDF 1.28 bundles its native MuPDF binaries INSIDE the `pymupdf` wheel
#   (there is no separate `pymupdfb` package installed here). collect_dynamic_libs
#   ensures those binaries land in the bundle even if analysis misses them.
# - PySide6's Qt plugins (platforms/imageformats) are handled by PyInstaller's
#   built-in PySide6 hook; no manual plugin wiring needed.
# - Unused heavy Qt modules are excluded to shrink the bundle. This is a
#   QtWidgets-only app (no WebEngine/QML/Quick/3D/Multimedia/Charts).
# - The digital-signing stack (pyHanko + cryptography + transitive deps,
#   added 2026-07-25) needs NO spec entries: pyHanko/asn1crypto/certvalidator
#   are pure Python (they ride the PYZ), cryptography has standard PyInstaller
#   support, and tzdata's zone files are collected by hooks-contrib's tzdata
#   hook automatically (verified: 605 zone files in the bundle, all frozen
#   smokes green). Password protection is pure pymupdf — nothing to bundle.

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

binaries = collect_dynamic_libs("pymupdf")

# Tesseract OCR runtime (O2): staged into vendor/tesseract by
# scripts/stage_tesseract.ps1 (gitignored — binaries are never committed).
# Lands under <_MEIPASS>/tesseract/, where pdfcore.ocr.tesseract_command()
# resolves it in frozen builds (bundled copy ONLY — no system fallback).
# Shipped via `datas`; PyInstaller 6 still RECLASSIFIES the PE files inside as
# binaries and dependency-walks them, planting a second copy of every DLL in
# _internal's root (~110 MB duplication, found at the O2 checkpoint) — those
# root-destined duplicates are filtered out after Analysis below. The vendored
# set is a complete, isolation-verified closure; nothing outside tesseract/ is
# needed.
_tess = Path("vendor/tesseract")
if not (_tess / "tesseract.exe").exists():
    raise SystemExit(
        "vendor/tesseract is missing or incomplete - run scripts/stage_tesseract.ps1 "
        "first (see the CLAUDE.md OCR ingestion section)."
    )

# qt-material ships its theme XMLs + Roboto fonts as package data; without
# them the themed launch crashes in the frozen build (dev runs hide this).
# qtawesome likewise ships its icon fonts + charmaps as package data
# (hooks-contrib has a qtawesome hook, but collecting explicitly keeps the
# spec self-sufficient); missing fonts render every button as a box glyph.
datas = collect_data_files("qt_material") + collect_data_files("qtawesome")

# App icon: bundle the PNG the runtime QIcon loads (window/taskbar icon). Like
# the theme/icon fonts above, a bare relative path fails SILENTLY in the frozen
# build, so it must ride in datas and be resolved via sys._MEIPASS at runtime.
datas += [("assets/icon.png", "assets")]

# Version single source: pdfcore/version.py reads [project].version from
# <_MEIPASS>/pyproject.toml at runtime (About dialog, diagnostics banner) —
# the frozen build has no .dist-info, so without this it degrades to the
# loud "0.0.0" fallback.
datas += [("pyproject.toml", ".")]

# The whole staged runtime: exe + closure DLLs + tessdata (model + configs),
# tessdata next to the exe (tesseract resolves it exe-relative — no env vars,
# no --tessdata-dir; see pdfcore/ocr.py).
datas += [(str(_tess), "tesseract")]

a = Analysis(
    ["src/pdfapp/__main__.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=["pymupdf"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtQuick3D",
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
        "PySide6.Qt3DCore",
        "PySide6.Qt3DRender",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "tkinter",
    ],
    noarchive=False,
)

# Drop the root-destined duplicates of the vendored tesseract DLLs (see the
# reclassification note above): keep vendor-sourced files ONLY under the
# tesseract/ prefix. Everything not sourced from vendor/ is left untouched.
_tess_src = str(_tess.resolve()).lower()
a.binaries = [
    (dest, src, kind)
    for dest, src, kind in a.binaries
    if not (
        str(Path(src).resolve()).lower().startswith(_tess_src)
        and not dest.replace("/", "\\").lower().startswith("tesseract\\")
    )
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pdf-editor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon="assets/icon.ico",  # .exe file icon shown in Windows Explorer
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="pdf-editor",
)
