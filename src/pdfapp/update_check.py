"""The Qt bridge that runs an update check off the main thread.

``pdfapp/updates.py`` holds every rule and is Qt-free; this module is the thin
piece that keeps the network off the UI thread. The fetch runs on a daemon
thread and the result comes back through a signal, which Qt delivers to the
main thread as a queued connection because the emitting thread is not the one
the QObject lives on.

Two lifetime rules, both learned from the shape of the problem rather than from
a crash we shipped:

- a late result must never touch a window that has gone, so the emit is wrapped
  (PySide6 raises RuntimeError when the C++ object behind a wrapper is already
  deleted) and :meth:`UpdateChecker.abandon` stops a pending result being
  delivered at all;
- only ONE check runs at a time, so a user hammering the About dialog's button
  cannot pile up threads.

Tests call :meth:`UpdateChecker.run_now` directly — the synchronous body of the
thread — so the whole flow is exercised with no thread and no network (the
fetch seam itself is monkeypatched).
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal

from pdfapp import diagnostics, updates


class UpdateChecker(QObject):
    """Runs :func:`updates.fetch_latest_release` off the main thread.

    Emits exactly one of ``finished`` (with an :class:`updates.UpdateInfo`) or
    ``failed`` (with a short reason) per check — never both, never neither.
    """

    finished = Signal(object)  # updates.UpdateInfo
    failed = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._running = False
        self._abandoned = False

    @property
    def running(self) -> bool:
        """True while a check is in flight (a second start() is ignored)."""
        return self._running

    def abandon(self) -> None:
        """Drop any in-flight result. Called when the window is closing: the
        thread still finishes, but nothing is delivered."""
        self._abandoned = True

    def start(self, url: str | None = None) -> bool:
        """Begin a check on a background thread. False when one already runs."""
        if self._running:
            return False
        self._running = True
        threading.Thread(target=self.run_now, args=(url,), daemon=True).start()
        return True

    def run_now(self, url: str | None = None) -> None:
        """The body of the check — synchronous, so tests drive it directly.

        Never raises: a check that cannot reach the feed is a non-event for the
        user (the automatic check says nothing at all; the manual one reports
        that it could not reach the server).
        """
        self._running = True
        try:
            data = updates.fetch_latest_release(url)
            info = updates.release_to_info(data)
            if info is None:
                self._emit_failed("the release feed could not be read")
            else:
                self._emit_finished(info)
        except Exception as exc:  # noqa: BLE001 - every failure is reported, not raised
            self._emit_failed(str(exc) or exc.__class__.__name__)
        finally:
            self._running = False

    # --- delivery -------------------------------------------------------
    def _emit_finished(self, info: updates.UpdateInfo) -> None:
        if self._abandoned:
            return
        try:
            self.finished.emit(info)
        except RuntimeError:  # the window went away mid-check
            pass

    def _emit_failed(self, reason: str) -> None:
        diagnostics.log_event(f"update check failed: {reason}")
        if self._abandoned:
            return
        try:
            self.failed.emit(reason)
        except RuntimeError:  # the window went away mid-check
            pass
