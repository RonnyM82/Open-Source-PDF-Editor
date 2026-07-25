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


def test_server_buffers_paths_until_handler_is_set(qapp):
    """The primary starts listening before its window exists; forwards that
    arrive early must be buffered and replayed when the handler is attached."""
    from pdfapp.single_instance import SingleInstanceServer

    server = SingleInstanceServer(key="PDFEditor-Buffer-Test")
    try:
        got: list[list[str]] = []
        server._deliver(["early.pdf"])  # arrives before a handler exists
        assert got == []  # buffered, not lost
        server.set_handler(got.append)
        assert got == [["early.pdf"]]  # replayed on wire-up
        server._deliver(["later.pdf"])  # now delivered straight through
        assert got == [["early.pdf"], ["later.pdf"]]
    finally:
        server.close()


def test_acquire_is_primary_when_alone_and_holds_the_lock(qapp, tmp_path):
    """With no primary running, acquire_or_forward elects THIS process: a
    listening server plus the held election lock (so a racer can't also win)."""
    import os

    from PySide6.QtCore import QLockFile

    from pdfapp import single_instance

    key = f"PDFEditor-Elect-Alone-{os.getpid()}"
    lock_path = single_instance._lock_path(key)
    if os.path.exists(lock_path):
        os.remove(lock_path)
    server = single_instance.acquire_or_forward(["a.pdf"], key=key)
    try:
        assert server is not None
        assert server.is_listening
        # The election lock is held: a second QLockFile on the same path fails.
        other = QLockFile(lock_path)
        assert not other.tryLock(0)
    finally:
        if server is not None:
            server.close()


def _run_election(n: int, key: str, outdir: Path) -> subprocess.CompletedProcess:
    """Spawn ``n`` racing launches; each writes its verdict into ``outdir``."""
    env = {**os.environ, "PYTHONPATH": _SRC, "QT_QPA_PLATFORM": "offscreen"}
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "election",
                key,
                f"C:/file{i}.pdf",
                str(outdir),
                str(n),
            ],
            env=env,
        )
        for i in range(n)
    ]
    for proc in procs:
        proc.wait(timeout=40)
    return procs  # type: ignore[return-value]


def test_simultaneous_launches_elect_a_single_primary(tmp_path):
    """The core fix: N near-simultaneous launches (what Explorer does for a
    multi-file "Open") converge on ONE primary, and every file reaches it —
    instead of each spawning its own window."""
    key = f"PDFEditor-Election-{os.getpid()}"
    outdir = tmp_path / "verdicts"
    outdir.mkdir()
    n = 5
    _run_election(n, key, outdir)

    verdicts = [p.read_text(encoding="utf-8") for p in outdir.glob("*.txt")]
    assert len(verdicts) == n, verdicts
    primaries = [v for v in verdicts if v.startswith("PRIMARY")]
    secondaries = [v for v in verdicts if v.startswith("SECONDARY")]
    assert len(primaries) == 1, f"expected exactly one primary, got {verdicts}"
    assert len(secondaries) == n - 1
    # The one primary collected EVERY file (its own + all forwards).
    collected = set(primaries[0].splitlines()[1:])
    assert collected == {f"C:/file{i}.pdf" for i in range(n)}, collected


def _election_worker(key: str, myfile: str, outdir: str, expected: int) -> None:
    """One racing launch: elect, then (if primary) collect all forwards.

    Writes ``PRIMARY\\n<file>\\n...`` or ``SECONDARY`` to ``<outdir>/<pid>.txt``.
    ``os._exit`` skips Qt teardown so a real success never turns into a spurious
    non-zero exit (same rationale as the roundtrip harness).
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from pdfapp import single_instance

    _app = QApplication.instance() or QApplication([])  # noqa: F841 - loop needs it
    server = single_instance.acquire_or_forward([myfile], key=key)
    out = os.path.join(outdir, f"{os.getpid()}.txt")
    if server is None:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write("SECONDARY")
        os._exit(0)

    collected = [myfile]
    loop = QEventLoop()

    def on_paths(paths: list[str]) -> None:
        collected.extend(paths)
        # Count UNIQUE files: a delivery whose ack was lost is retried, so the
        # same path can arrive twice (harmless — focus-existing-tab dedupes it).
        # Quitting on the raw count could stop early on a duplicate and miss a
        # real forward.
        if len(set(collected)) >= expected:
            loop.quit()

    server.set_handler(on_paths)
    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(loop.quit)
    guard.start(15000)
    loop.exec()
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("PRIMARY\n" + "\n".join(collected))
    os._exit(0)


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
    elif len(sys.argv) >= 6 and sys.argv[1] == "election":
        _election_worker(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]))
