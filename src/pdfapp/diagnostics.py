"""Always-on failure self-logging for the running app.

The windowed build has ``console=False``, so anything Python or Qt would print
to stderr goes nowhere — a slot that raises, a Qt "out of memory" warning, a
hang, a hard fault all vanish. This routes every one of them to a log file the
user can hand back, so problems in the field are diagnosable instead of "it just
did something weird". Four mechanisms, all stdlib / Qt (no new dependency):

- ``faulthandler`` writes the C + Python traceback on a hard fault (access
  violation / stack overflow) — the "crash".
- A watchdog thread, INDEPENDENT of the Qt event loop, dumps every thread's
  stack if the main thread's heartbeat goes stale. It runs in C and does not
  need the GIL, so it fires even while the main thread is wedged inside a C
  call — the "Not Responding" hang.
- ``sys.excepthook`` / ``threading.excepthook`` log an unhandled Python
  exception (a raising slot leaves the app running but broken, with no other
  record in the windowed build), then chain to the previous hook.
- A Qt message handler logs Qt's own Warning / Critical / Fatal messages.

The log is rotated (one ``.old`` generation) so it never grows without bound.
Everything is defensive: diagnostics must never be the reason the app fails, so
every entry point swallows its own errors. Set ``PDF_EDITOR_NO_DIAGNOSTICS`` to
disable. Install once, from ``app.main`` on the real-UI path only (the packaging
smokes stay pure).
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time
from pathlib import Path

# The main thread stamps this from a Qt timer; the watchdog reads it. A plain
# float assignment/read is atomic enough under the GIL for a liveness check.
_last_beat = 0.0
_installed = False
_log_file = None  # kept open for faulthandler's lifetime (it writes to the fd)
# Hooks we replace on install() and restore in _reset_for_tests().
_prev_excepthook = None
_prev_threading_hook = None
_prev_qt_handler = None

# Windows ghosts a window ("Not Responding") after ~5s without pumping messages;
# dump a hair before that so the stack is captured as the hang bites.
_HANG_SECONDS = 4.0
_HEARTBEAT_MS = 250
# Rotate the log past this so a crash-looping or chatty session can't grow it
# without bound; one .old generation preserves the previous session's evidence.
_MAX_LOG_BYTES = 1_000_000


def log_path() -> Path:
    """Where the diagnostics log lives — a per-user, always-writable spot that
    survives in a frozen build (no writing next to a Program Files exe)."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
    return Path(base) / "PDF Editor" / "diagnostics.log"


def _rotate_if_large(path: Path) -> None:
    """Keep the log bounded: past _MAX_LOG_BYTES, move it aside to one .old
    generation and start fresh. Runs before the file is opened for the session."""
    try:
        if path.exists() and path.stat().st_size > _MAX_LOG_BYTES:
            os.replace(path, path.parent / (path.name + ".old"))
    except Exception:  # noqa: BLE001 - rotation must never break startup
        pass


def _write(line: str) -> None:
    if _log_file is None:
        return
    try:
        _log_file.write(line + "\n")
        _log_file.flush()
    except Exception:  # noqa: BLE001 - logging must never raise into the app
        pass


def _session_banner(app) -> None:
    """One block per launch: time, build, and every screen's size + DPR — the
    display facts that make a size/scaling-specific bug diagnosable from the log
    alone."""
    try:
        import platform

        from PySide6 import __version__ as pyside_version

        _write("\n" + "=" * 70)
        _write(f"session start  {time.strftime('%Y-%m-%d %H:%M:%S')}")
        frozen = getattr(sys, "frozen", False)
        _write(f"python {platform.python_version()}  PySide6 {pyside_version}  frozen={frozen}")
        _write(f"os {platform.platform()}")
        try:
            for i, s in enumerate(app.screens()):
                g = s.geometry()
                _write(
                    f"screen {i}: {g.width()}x{g.height()} @({g.x()},{g.y()}) "
                    f"dpr={s.devicePixelRatio()} logicalDPI={s.logicalDotsPerInch():.0f}"
                )
            prim = app.primaryScreen()
            _write(f"primary screen: {prim.name() if prim else '?'}")
        except Exception as exc:  # noqa: BLE001
            _write(f"(screen enumeration failed: {exc})")
        _write("=" * 70)
    except Exception:  # noqa: BLE001
        pass


def _maybe_dump(stale: float, dumped: bool) -> bool:
    """One watchdog tick's decision, factored out so it is testable without
    threads or real time. ``stale`` is seconds since the last heartbeat;
    ``dumped`` tracks whether THIS hang episode was already reported (dump once,
    not every tick). Returns the new ``dumped`` state."""
    if stale > _HANG_SECONDS and not dumped:
        _write(
            f"\n[{time.strftime('%H:%M:%S')}] MAIN THREAD UNRESPONSIVE for "
            f"{stale:.1f}s -- dumping all thread stacks:"
        )
        if _log_file is not None:
            faulthandler.dump_traceback(file=_log_file, all_threads=True)
            _log_file.flush()
        return True
    if stale < 1.0:
        return False  # recovered — re-arm for the next hang
    return dumped


def _watchdog() -> None:
    dumped = False
    while True:
        time.sleep(0.5)
        try:
            dumped = _maybe_dump(time.monotonic() - _last_beat, dumped)
        except Exception:  # noqa: BLE001 - the watchdog must never die
            pass


def _format_exc(exc_type, exc, tb) -> str:
    import traceback

    return "".join(traceback.format_exception(exc_type, exc, tb)).rstrip()


def _excepthook(exc_type, exc, tb) -> None:
    """Log an unhandled exception, then chain to the previous hook. In the
    windowed build the app keeps running after a raising slot, so without this
    the error would leave no trace at all."""
    _write(f"\n[{time.strftime('%H:%M:%S')}] UNHANDLED EXCEPTION:")
    _write(_format_exc(exc_type, exc, tb))
    if callable(_prev_excepthook):
        try:
            _prev_excepthook(exc_type, exc, tb)
        except Exception:  # noqa: BLE001
            pass


def _threading_hook(args) -> None:
    name = args.thread.name if args.thread is not None else "?"
    _write(f"\n[{time.strftime('%H:%M:%S')}] UNHANDLED EXCEPTION in thread {name!r}:")
    _write(_format_exc(args.exc_type, args.exc_value, args.exc_traceback))
    if callable(_prev_threading_hook):
        try:
            _prev_threading_hook(args)
        except Exception:  # noqa: BLE001
            pass


def _qt_message_handler(mode, context, message) -> None:
    """Route Qt's own Warning/Critical/Fatal messages to the log — they too go
    to a dead stderr in the windowed build. Debug/Info are dropped as noise; a
    real stderr (dev console) still sees the message."""
    from PySide6.QtCore import QtMsgType

    label = {
        QtMsgType.QtWarningMsg: "Qt WARNING",
        QtMsgType.QtCriticalMsg: "Qt CRITICAL",
        QtMsgType.QtFatalMsg: "Qt FATAL",
    }.get(mode)
    if label is None:
        return
    _write(f"[{time.strftime('%H:%M:%S')}] {label}: {message}")
    try:
        if sys.__stderr__ is not None:
            sys.__stderr__.write(f"{label}: {message}\n")
    except Exception:  # noqa: BLE001
        pass


def log_event(message: str) -> None:
    """Public breadcrumb hook — callers can drop a timestamped line into the log
    (e.g. "opened <file>") so a later crash has context. A no-op until install()."""
    _write(f"[{time.strftime('%H:%M:%S')}] {message}")


def reveal_log() -> bool:
    """Open the diagnostics log in the OS file browser (selected on Windows) so a
    non-technical user can find it to send back. Returns False if there is no log
    yet (nothing to reveal)."""
    path = log_path()
    try:
        if not path.exists():
            return False
        if sys.platform == "win32":
            import subprocess

            # A single string arg so Windows doesn't re-quote and break the
            # `/select,<path>` form; the path is app-derived, not user input.
            subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')  # noqa: S603
        else:  # dev fallback (Windows-only app): reveal the containing folder
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
        return True
    except Exception:  # noqa: BLE001 - a diagnostics helper must never raise
        return False


def install(app, *, start_watchdog: bool = True) -> None:
    """Enable crash / hang / unhandled-exception / Qt-message capture. Safe to
    call once; a no-op if disabled or already installed. ``app`` is the
    QApplication (for the heartbeat timer and the screen banner).
    ``start_watchdog`` is False in tests to avoid spawning a background thread."""
    global _installed, _log_file, _last_beat
    global _prev_excepthook, _prev_threading_hook, _prev_qt_handler
    if _installed or os.environ.get("PDF_EDITOR_NO_DIAGNOSTICS"):
        return
    _installed = True
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_large(path)
        _log_file = open(path, "a", buffering=1, encoding="utf-8")  # noqa: SIM115
        faulthandler.enable(file=_log_file)
        _session_banner(app)

        # Route otherwise-invisible failures (windowed build has no stderr) to
        # the log, chaining to whatever was installed before.
        _prev_excepthook = sys.excepthook
        sys.excepthook = _excepthook
        _prev_threading_hook = threading.excepthook
        threading.excepthook = _threading_hook
        from PySide6.QtCore import QTimer, qInstallMessageHandler

        _prev_qt_handler = qInstallMessageHandler(_qt_message_handler)

        _last_beat = time.monotonic()

        def _beat() -> None:
            global _last_beat
            _last_beat = time.monotonic()

        timer = QTimer(app)  # parented to the app so it lives for the session
        timer.timeout.connect(_beat)
        timer.start(_HEARTBEAT_MS)

        if start_watchdog:
            threading.Thread(target=_watchdog, name="hang-watchdog", daemon=True).start()
    except Exception as exc:  # noqa: BLE001 - never let diagnostics break startup
        try:
            print(f"diagnostics disabled ({exc})", file=sys.stderr, flush=True)
        except Exception:  # noqa: BLE001
            pass


def _reset_for_tests() -> None:
    """Undo install() so each test starts clean (module state is global) — and,
    crucially, restore the process-wide hooks we replaced so one test can't
    leak its excepthook/Qt handler into the rest of the suite."""
    global _installed, _log_file, _last_beat
    global _prev_excepthook, _prev_threading_hook, _prev_qt_handler
    if _prev_excepthook is not None:
        sys.excepthook = _prev_excepthook
        _prev_excepthook = None
    if _prev_threading_hook is not None:
        threading.excepthook = _prev_threading_hook
        _prev_threading_hook = None
    try:
        from PySide6.QtCore import qInstallMessageHandler

        qInstallMessageHandler(_prev_qt_handler)  # None restores Qt's default
    except Exception:  # noqa: BLE001
        pass
    _prev_qt_handler = None
    if _log_file is not None:
        try:
            _log_file.close()
        except Exception:  # noqa: BLE001
            pass
    _installed = False
    _log_file = None
    _last_beat = 0.0
