"""Single-source version resolution (pdfcore/version.py).

The release version lives ONLY in pyproject.toml [project].version; both
packages' __version__ derive from it at import time, and the frozen build
reads a bundled copy via sys._MEIPASS (the spec's datas line). These tests
pin every resolution branch so a stale-constant regression can't reappear.
"""

from __future__ import annotations

import shutil
import sys
import tomllib
from pathlib import Path

import pdfapp
import pdfcore
from pdfcore.version import app_version

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _pyproject_version() -> str:
    with open(_PYPROJECT, "rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def test_both_packages_match_pyproject():
    # The whole point: ONE bump site. Both dunders read pyproject's version.
    expected = _pyproject_version()
    assert pdfcore.__version__ == expected
    assert pdfapp.__version__ == expected
    assert expected != "0.0.0"  # the loud fallback never leaks into a dev run


def test_frozen_lookup_reads_bundled_pyproject(tmp_path, monkeypatch):
    # The exact frozen path: _MEIPASS/pyproject.toml, as the spec bundles it.
    bundled = tmp_path / "pyproject.toml"
    shutil.copy(_PYPROJECT, bundled)
    bundled.write_text(
        bundled.read_text(encoding="utf-8").replace(
            f'version = "{_pyproject_version()}"', 'version = "9.9.9"', 1
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert app_version() == "9.9.9"


def test_fallback_is_loud_never_raising(tmp_path, monkeypatch):
    # No pyproject anywhere + no dist metadata -> "0.0.0", not an exception.
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)  # empty dir

    import importlib.metadata

    def _raise(_name):
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(importlib.metadata, "version", _raise)
    assert app_version() == "0.0.0"
