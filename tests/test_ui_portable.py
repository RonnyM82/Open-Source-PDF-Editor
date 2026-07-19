"""Portable-mode detection (pdfapp.portable). Pure logic — no Qt at runtime.

The portable ZIP and the installer ship the same bundle; a marker file next to
the exe is the only difference, and it flips where the app writes its own state.
These tests pin that switch and the resulting data_dir in all three cases:
dev (never portable), frozen+marker (portable), frozen+no-marker (installed).
"""

from __future__ import annotations

import sys

from pdfapp import portable


def _fake_frozen_exe(tmp_path, monkeypatch, *, marker: bool):
    """Make pdfapp.portable see a frozen exe under tmp_path, optionally with the
    portable marker beside it. Returns the exe's directory."""
    exe = tmp_path / "pdf-editor.exe"
    exe.write_bytes(b"")  # existence is all is_portable checks around it
    if marker:
        (tmp_path / portable.MARKER_NAME).write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    return tmp_path


def test_not_portable_when_not_frozen(monkeypatch):
    # Running under pytest is a dev run: never frozen, so never portable.
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert portable.is_portable() is False


def test_data_dir_uses_localappdata_when_not_portable(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    d = portable.data_dir()
    assert d.name == "PDF Editor"
    assert "AppData" in str(d)


def test_portable_when_frozen_with_marker(tmp_path, monkeypatch):
    exe_dir = _fake_frozen_exe(tmp_path, monkeypatch, marker=True)
    assert portable.is_portable() is True
    assert portable.data_dir() == exe_dir / "data"


def test_not_portable_when_frozen_without_marker(tmp_path, monkeypatch):
    _fake_frozen_exe(tmp_path, monkeypatch, marker=False)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    assert portable.is_portable() is False
    # Falls back to the per-user profile spot, never next to the exe.
    assert portable.data_dir().name == "PDF Editor"
    assert "AppData" in str(portable.data_dir())
