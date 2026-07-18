"""Single-instance forwarding: a second launch hands its files to the primary.

The full socket round-trip runs in an ISOLATED SUBPROCESS with its own fresh
QApplication (see `_roundtrip_main` + the ``__main__`` guard below). A nested
QEventLoop on the *shared session* QApplication that every other UI test uses is
both flaky (the server's socket notifier gets starved under load) and unsafe (it
can segfault on teardown), so we never drive real sockets inside the session
loop. The in-process tests here only touch the safe, deterministic decision
logic. (Subprocess tests have precedent in this repo — see the page_coords
import-purity guard.)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from pdfapp.single_instance import default_key, try_forward_to_running

_SRC = str(Path(__file__).resolve().parents[1] / "src")


def _run_roundtrip(paths: list[str]) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": _SRC, "QT_QPA_PLATFORM": "offscreen"}
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "roundtrip", *paths],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_forward_roundtrip_delivers_paths():
    # A primary is listening; a threaded "secondary" forwards two paths and the
    # primary's callback must receive exactly those, in order.
    proc = _run_roundtrip(["C:/a.pdf", "C:/b.pdf"])
    assert "OK" in proc.stdout, proc.stdout + proc.stderr


def test_bare_relaunch_roundtrip_delivers_empty():
    # A launch with no file (Start-menu shortcut while already running) still
    # reaches the primary — as an empty list — so it can raise the window.
    proc = _run_roundtrip([])
    assert "OK" in proc.stdout, proc.stdout + proc.stderr


def test_grant_foreground_never_raises():
    """The focus grant is cosmetic: it must never break a forward (it runs
    unguarded by the caller). Direct call exercises the real API on Windows."""
    from pdfapp.single_instance import _grant_foreground_to_primary

    _grant_foreground_to_primary()  # must simply not raise


def test_bring_to_front_restores_and_survives_offscreen(qapp):
    """bring_to_front un-minimizes and runs its Windows foreground claim
    without raising (offscreen winIds are fake — the ctypes call is guarded)."""
    from PySide6.QtCore import Qt

    from pdfapp.main_window import MainWindow

    window = MainWindow()
    try:
        window.setWindowState(window.windowState() | Qt.WindowState.WindowMinimized)
        window.bring_to_front()
        assert not window.isMinimized()
    finally:
        window.close()


def test_forward_returns_false_when_nobody_listening(qapp):
    # Nothing bound to this key -> the caller should become the primary itself.
    # (Single-instance rests on THIS: a launch forwards only when a primary is
    # already listening; otherwise it becomes the primary. Note Windows named
    # pipes allow multiple instances of one name, so the guarantee is the
    # forward-first flow, not listen() failing on a duplicate.) Client-only, so
    # it is safe to run in the shared session loop.
    assert try_forward_to_running(["x.pdf"], key="PDFEditor-Test-nobody-home") is False


def test_default_key_is_per_user():
    assert default_key().startswith("PDFEditor-SingleInstance-")


def _roundtrip_main(expected: list[str]) -> None:
    """Run a primary server + a threaded secondary forward in THIS process.

    Prints ``OK`` and exits 0 when the primary receives exactly ``expected``,
    else prints ``FAIL ...`` and exits 1. Uses os._exit to skip Qt's
    destructors — an interpreter/Qt teardown crash must not turn a real success
    into a spurious non-zero exit.
    """
    import threading

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from pdfapp.single_instance import SingleInstanceServer, try_forward_to_running

    _app = QApplication.instance() or QApplication([])  # noqa: F841 - must exist for the loop
    received: list[list[str]] = []
    key = "PDFEditor-RoundTrip-Test"
    loop = QEventLoop()

    def on_paths(p: list[str]) -> None:
        received.append(p)
        loop.quit()

    server = SingleInstanceServer(on_paths, key=key)
    if not server.is_listening:
        print("FAIL: server not listening", flush=True)
        os._exit(1)

    QTimer.singleShot(
        0,
        lambda: threading.Thread(
            target=lambda: try_forward_to_running(expected, key=key), daemon=True
        ).start(),
    )
    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(loop.quit)
    guard.start(5000)
    loop.exec()

    ok = received == [expected]
    print("OK" if ok else f"FAIL: {received!r}", flush=True)
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "roundtrip":
        _roundtrip_main(sys.argv[2:])
