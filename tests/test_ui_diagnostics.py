"""The crash/hang self-logger (pdfapp.diagnostics). The watchdog decision is
factored into _maybe_dump so it is testable without threads or real waits."""

from __future__ import annotations

import faulthandler
import sys

import pytest

pytest.importorskip("PySide6")

from pdfapp import diagnostics  # noqa: E402


@pytest.fixture
def clean_diagnostics(tmp_path, monkeypatch):
    """Point the log at tmp_path and restore global diagnostics state after."""
    log = tmp_path / "diag.log"
    monkeypatch.setattr(diagnostics, "log_path", lambda: log)
    yield log
    diagnostics._reset_for_tests()
    faulthandler.enable()  # undo enable(file=closed) — back to the default target


def test_log_path_uses_localappdata(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)  # dev run: not portable
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    p = diagnostics.log_path()
    assert p.name == "diagnostics.log"
    assert "PDF Editor" in str(p)


def test_log_path_is_exe_relative_in_portable_mode(tmp_path, monkeypatch):
    # Portable build: log lives beside the exe (data\), never in the profile.
    from pdfapp import portable

    exe = tmp_path / "pdf-editor.exe"
    exe.write_bytes(b"")
    (tmp_path / portable.MARKER_NAME).write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert diagnostics.log_path() == tmp_path / "data" / "diagnostics.log"


def test_install_writes_session_banner_with_screens(qapp, clean_diagnostics):
    diagnostics.install(qapp, start_watchdog=False)
    text = clean_diagnostics.read_text(encoding="utf-8")
    assert "session start" in text
    assert "screen 0:" in text  # the display facts a size-specific bug needs


def test_install_is_idempotent(qapp, clean_diagnostics):
    diagnostics.install(qapp, start_watchdog=False)
    size_after_first = clean_diagnostics.stat().st_size
    diagnostics.install(qapp, start_watchdog=False)  # second call is a no-op
    assert clean_diagnostics.stat().st_size == size_after_first


def test_env_var_disables(qapp, clean_diagnostics, monkeypatch):
    monkeypatch.setenv("PDF_EDITOR_NO_DIAGNOSTICS", "1")
    diagnostics.install(qapp, start_watchdog=False)
    assert not clean_diagnostics.exists()  # nothing opened, nothing written


def test_watchdog_dumps_once_per_hang_then_rearms(clean_diagnostics):
    # Drive _maybe_dump directly with fake staleness — no threads, no sleeping.
    diagnostics._log_file = open(clean_diagnostics, "a", buffering=1, encoding="utf-8")
    try:
        dumped = diagnostics._maybe_dump(5.0, False)  # hang -> dump
        assert dumped is True
        dumped = diagnostics._maybe_dump(6.0, dumped)  # still hung -> no re-dump
        assert dumped is True
        dumped = diagnostics._maybe_dump(0.2, dumped)  # recovered -> re-arm
        assert dumped is False
        text = clean_diagnostics.read_text(encoding="utf-8")
        assert text.count("MAIN THREAD UNRESPONSIVE") == 1  # exactly one episode
    finally:
        diagnostics._log_file.close()


def test_no_dump_below_threshold(clean_diagnostics):
    diagnostics._log_file = open(clean_diagnostics, "a", buffering=1, encoding="utf-8")
    try:
        assert diagnostics._maybe_dump(2.0, False) is False  # under _HANG_SECONDS
        assert "UNRESPONSIVE" not in clean_diagnostics.read_text(encoding="utf-8")
    finally:
        diagnostics._log_file.close()


def test_large_log_is_rotated_on_install(qapp, clean_diagnostics, monkeypatch):
    monkeypatch.setattr(diagnostics, "_MAX_LOG_BYTES", 100)
    clean_diagnostics.write_text("x" * 500, encoding="utf-8")  # over the cap
    diagnostics.install(qapp, start_watchdog=False)
    old = clean_diagnostics.parent / (clean_diagnostics.name + ".old")
    assert old.exists()  # previous session preserved
    assert clean_diagnostics.stat().st_size < 500  # fresh log, just the banner


def test_excepthook_logs_and_chains(clean_diagnostics):
    diagnostics._log_file = open(clean_diagnostics, "a", buffering=1, encoding="utf-8")
    chained = []
    saved = diagnostics._prev_excepthook
    diagnostics._prev_excepthook = lambda t, e, tb: chained.append(e)
    try:
        diagnostics._excepthook(ValueError, ValueError("boom in a slot"), None)
        text = clean_diagnostics.read_text(encoding="utf-8")
        assert "UNHANDLED EXCEPTION" in text
        assert "boom in a slot" in text
        assert len(chained) == 1  # previous hook still runs (no behaviour lost)
    finally:
        diagnostics._prev_excepthook = saved
        diagnostics._log_file.close()


def test_qt_handler_logs_serious_only(clean_diagnostics):
    from PySide6.QtCore import QtMsgType

    diagnostics._log_file = open(clean_diagnostics, "a", buffering=1, encoding="utf-8")
    try:
        diagnostics._qt_message_handler(QtMsgType.QtDebugMsg, None, "chatty debug")
        diagnostics._qt_message_handler(QtMsgType.QtWarningMsg, None, "QImage: out of memory")
        text = clean_diagnostics.read_text(encoding="utf-8")
        assert "chatty debug" not in text  # Debug/Info dropped as noise
        assert "Qt WARNING: QImage: out of memory" in text
    finally:
        diagnostics._log_file.close()


def test_install_replaces_hooks_and_reset_restores(qapp, clean_diagnostics):
    before = sys.excepthook
    diagnostics.install(qapp, start_watchdog=False)
    assert sys.excepthook is diagnostics._excepthook  # ours is active
    diagnostics._reset_for_tests()
    assert sys.excepthook is before  # cleanly restored, no leak into the suite


def test_reveal_log_opens_when_present(clean_diagnostics, monkeypatch):
    import subprocess

    clean_diagnostics.write_text("log exists", encoding="utf-8")
    captured = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, *a, **k: captured.append(cmd))
    assert diagnostics.reveal_log() is True
    if sys.platform == "win32":
        assert captured and "explorer /select" in captured[0]


def test_reveal_log_false_when_no_log(clean_diagnostics):
    # The fixture points log_path at a file that doesn't exist yet.
    assert diagnostics.reveal_log() is False
