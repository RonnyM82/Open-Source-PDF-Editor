"""Single-instance guard: a second launch hands its files to the running app.

Without this, double-clicking a .pdf (or "Open with > PDF Editor") starts a
NEW process each time, so files that should be tabs end up in separate windows.
One PRIMARY process owns the UI; any later launch is a SECONDARY that forwards
its file paths to the primary (which opens them as tabs and raises itself) and
then exits.

The election is `acquire_or_forward`, called BEFORE the theme or any window is
built. It matters most for a multi-file "Open" from Explorer: Windows launches
one process PER selected file, all at once, so the primary must be chosen from
a genuine race. `QLocalServer.listen()` is NOT atomic on Windows (a duplicate
pipe name simply opens another instance, so every racer would win), so a
cross-process `QLockFile` is the actual election — the winner holds the lock
and starts the server; losers wait for its server and forward. Deciding this up
front (and exiting a secondary before it themes) is also what stops duplicate
launches from concurrently rebuilding qt-material's shared on-disk cache, which
was crashing the app.

Transport is a `QLocalServer`/`QLocalSocket` pair — a named pipe on Windows.
Pure UI concern (QtNetwork + QtCore ship with PySide6 and are already bundled),
so it lives in `pdfapp`, never `pdfcore`.

The server key (and lock-file name) is per-user: Windows named pipes share one
machine-wide namespace, so a bare name would collide across users on a shared /
RDP machine. `UserAccessOption` restricts the pipe's DACL to the current user.
"""

from __future__ import annotations

import ctypes
import getpass
import os
import sys
import tempfile
import time
from collections.abc import Callable

from PySide6.QtCore import QLockFile, QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket

# Short by design: connect fails fast when no pipe exists, so a primary launch
# is not delayed; a real forward delivers its bytes within a couple of ms.
_CONNECT_TIMEOUT_MS = 300
_WRITE_TIMEOUT_MS = 500
# The secondary waits this long for the primary to close the connection, which
# is the "message consumed" acknowledgement (see the framing note below).
_ACK_TIMEOUT_MS = 1000

# How long a losing election waits for the winner to spin up its server before
# forwarding fails (Explorer launches one process PER file, near-simultaneously;
# the winner must build QApplication + theme + window first), and the poll gap.
_PRIMARY_WAIT_S = 6.0
_PRIMARY_POLL_S = 0.05
# A stale lock (primary crashed) is reclaimed once its PID is gone; this bounds
# the case where liveness can't be determined.
_STALE_LOCK_MS = 20_000

_ENCODING = "utf-8"
_HEADER_LEN = 4  # big-endian uint32 payload length prefix


def default_key() -> str:
    """Per-user server name (the machine-wide pipe namespace needs disambiguation)."""
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - getuser reads env vars that can be absent
        user = "default"
    return f"PDFEditor-SingleInstance-{user}"


# AllowSetForegroundWindow "any process" sentinel (DWORD -1).
_ASFW_ANY = 0xFFFFFFFF


def _grant_foreground_to_primary() -> None:
    """Delegate this launch's foreground rights to the primary (Windows).

    Windows refuses ``SetForegroundWindow`` from a background process — the
    primary's ``activateWindow()`` alone just FLASHES the taskbar icon (the
    reported symptom). The SECONDARY is the process the user actually
    launched (double-click / "Open with"), so it briefly holds the right and
    may hand it onward with ``AllowSetForegroundWindow`` before exiting; the
    primary's ``bring_to_front`` then claims it. Cosmetic only: any failure
    here must never break the forward itself.
    """
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.AllowSetForegroundWindow(ctypes.c_uint(_ASFW_ANY))
    except Exception:
        pass


def try_forward_to_running(paths: list[str], key: str | None = None) -> bool:
    """If another instance is listening, send it ``paths`` and return True.

    Returns False when no instance owns the socket — the caller should then
    become the primary. Filenames cannot contain newlines on Windows, so the
    payload is simply newline-joined UTF-8 (empty = a bare re-launch that should
    just raise the running window).
    """
    key = key or default_key()
    socket = QLocalSocket()
    socket.connectToServer(key)
    if not socket.waitForConnected(_CONNECT_TIMEOUT_MS):
        return False
    _grant_foreground_to_primary()
    # Length-prefixed so the primary knows when the whole message has arrived
    # and can close the connection to acknowledge it. We then wait for THAT
    # close instead of tearing the socket down ourselves — a self-initiated
    # disconnect can race ahead of the primary reading the bytes (data loss
    # under load). We keep the pipe open until the primary has consumed it.
    payload = "\n".join(paths).encode(_ENCODING)
    socket.write(len(payload).to_bytes(_HEADER_LEN, "big") + payload)
    socket.flush()
    socket.waitForBytesWritten(_WRITE_TIMEOUT_MS)
    socket.waitForDisconnected(_ACK_TIMEOUT_MS)
    return True


def _lock_path(key: str) -> str:
    return os.path.join(tempfile.gettempdir(), f"{key}.lock")


def acquire_or_forward(paths: list[str], key: str | None = None) -> SingleInstanceServer | None:
    """Elect ONE primary across simultaneous launches, or forward and stand down.

    Returns a listening :class:`SingleInstanceServer` when THIS process should
    own the UI (the primary); returns ``None`` after handing ``paths`` to the
    primary — the caller must then exit(0) WITHOUT building a window or applying
    the theme. That last part matters twice over: it is what makes multiple
    files open as tabs in ONE window, and it keeps a duplicate launch from
    touching qt-material's shared on-disk cache (concurrent rebuilds of it were
    crashing the app when Explorer launched one process per selected file).

    ``QLocalServer.listen()`` is NOT atomic on Windows (a duplicate name just
    opens another pipe instance, so every racer would think itself primary), so
    a cross-process ``QLockFile`` is the actual election; the local socket is
    only the transport.
    """
    key = key or default_key()
    # Fast path: a primary is already up (the common "app already running" case).
    if try_forward_to_running(paths, key):
        return None
    # Cold start — elect atomically.
    lock = QLockFile(_lock_path(key))
    lock.setStaleLockTime(_STALE_LOCK_MS)
    if lock.tryLock(0):
        return _become_primary(key, lock)
    if lock.error() != QLockFile.LockError.LockFailedError:
        # The lock file itself is unusable (permissions, read-only temp) — don't
        # hang; run standalone. Single-instance simply doesn't apply here.
        return _become_primary(key, lock)
    # Another cold-starting process won the election and is building its UI.
    # Wait for its server, forwarding the moment it answers.
    deadline = time.monotonic() + _PRIMARY_WAIT_S
    while time.monotonic() < deadline:
        if try_forward_to_running(paths, key):
            return None
        time.sleep(_PRIMARY_POLL_S)
    # The winner holds the lock but never came up (crashed mid-start). Take over
    # so the files still open rather than being silently dropped.
    lock.tryLock(0)
    return _become_primary(key, lock)


def _become_primary(key: str, lock: QLockFile) -> SingleInstanceServer:
    server = SingleInstanceServer(key=key)
    server.hold_lock(lock)
    return server


class SingleInstanceServer(QObject):
    """The primary instance's listener.

    Calls ``on_paths(list[str])`` once per secondary launch — with the forwarded
    paths, or an empty list for a bare re-launch (which should just surface the
    window). Listen failures are non-fatal: the app runs, just without the
    single-instance behaviour.
    """

    def __init__(
        self,
        on_paths: Callable[[list[str]], None] | None = None,
        key: str | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        # on_paths may be attached LATER (set_handler): the primary starts
        # listening before its window exists, so forwards that arrive early are
        # buffered and replayed once the handler is wired up.
        self._on_paths = on_paths
        self._buffer: list[list[str]] = []
        self._lock: QLockFile | None = None
        self._key = key or default_key()
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        # A crashed prior primary can leave a stale pipe/socket that blocks
        # listen(); clearing it first is safe — only the elected primary (lock
        # holder) ever constructs a server, so this can't clobber a live peer.
        QLocalServer.removeServer(self._key)
        self.is_listening = self._server.listen(self._key)
        if self.is_listening:
            self._server.newConnection.connect(self._on_new_connection)

    def set_handler(self, on_paths: Callable[[list[str]], None]) -> None:
        """Attach the paths handler and replay anything received before now."""
        self._on_paths = on_paths
        pending, self._buffer = self._buffer, []
        for paths in pending:
            on_paths(paths)

    def hold_lock(self, lock: QLockFile) -> None:
        """Keep the election lock alive for this process's lifetime."""
        self._lock = lock

    def _deliver(self, paths: list[str]) -> None:
        if self._on_paths is None:
            self._buffer.append(paths)  # handler not wired yet — replay on set
        else:
            self._on_paths(paths)

    def _on_new_connection(self) -> None:
        socket = self._server.nextPendingConnection()
        if socket is None:
            return
        # Read ASYNCHRONOUSLY and by FRAMING (a length prefix), not by waiting
        # for disconnect: a synchronous waitForReadyRead here is timing-fragile
        # (the slot can fire nested inside the peer's waitForConnected, before
        # it has written), and end-on-disconnect races the peer's teardown. Once
        # the full length-prefixed payload is in hand we close the socket, which
        # is the delivery acknowledgement the secondary waits for.
        buffer = bytearray()
        handled = False

        def process() -> None:
            nonlocal handled
            if handled:
                return
            buffer.extend(bytes(socket.readAll()))
            if len(buffer) < _HEADER_LEN:
                return
            length = int.from_bytes(buffer[:_HEADER_LEN], "big")
            if len(buffer) < _HEADER_LEN + length:
                return
            handled = True
            text = bytes(buffer[_HEADER_LEN : _HEADER_LEN + length]).decode(
                _ENCODING, errors="replace"
            )
            socket.disconnectFromServer()  # ack: tells the secondary we're done
            socket.deleteLater()
            self._deliver([line for line in text.split("\n") if line])

        socket.readyRead.connect(process)
        socket.disconnected.connect(process)
        process()  # data may already be buffered when this slot runs

    def close(self) -> None:
        self._server.close()
        if self._lock is not None:
            self._lock.unlock()
            self._lock = None
