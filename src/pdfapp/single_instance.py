"""Single-instance guard: a second launch hands its files to the running app.

Without this, double-clicking a .pdf (or "Open with > PDF Editor") starts a
NEW process each time, so files that should be tabs end up in separate windows.
Here the first process to bind a per-user local socket becomes the PRIMARY and
owns the UI; any later launch is a SECONDARY that forwards its file paths to the
primary (which opens them as tabs and raises itself) and then exits.

Transport is a `QLocalServer`/`QLocalSocket` pair — a named pipe on Windows.
Pure UI concern (QtNetwork ships with PySide6 and is already bundled), so it
lives in `pdfapp`, never `pdfcore`.

The server key is per-user: Windows named pipes share one machine-wide
namespace, so a bare name would collide across users on a shared / RDP machine.
`UserAccessOption` restricts the pipe's DACL to the current user as well.
"""

from __future__ import annotations

import ctypes
import getpass
import sys
from collections.abc import Callable

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket

# Short by design: connect fails fast when no pipe exists, so a primary launch
# is not delayed; a real forward delivers its bytes within a couple of ms.
_CONNECT_TIMEOUT_MS = 300
_WRITE_TIMEOUT_MS = 500
# The secondary waits this long for the primary to close the connection, which
# is the "message consumed" acknowledgement (see the framing note below).
_ACK_TIMEOUT_MS = 1000

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


class SingleInstanceServer(QObject):
    """The primary instance's listener.

    Calls ``on_paths(list[str])`` once per secondary launch — with the forwarded
    paths, or an empty list for a bare re-launch (which should just surface the
    window). Listen failures are non-fatal: the app runs, just without the
    single-instance behaviour.
    """

    def __init__(
        self,
        on_paths: Callable[[list[str]], None],
        key: str | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_paths = on_paths
        self._key = key or default_key()
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        # A crashed prior primary can leave a stale pipe/socket that blocks
        # listen(); clearing it first is safe when nothing is really bound.
        QLocalServer.removeServer(self._key)
        self.is_listening = self._server.listen(self._key)
        if self.is_listening:
            self._server.newConnection.connect(self._on_new_connection)

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
            self._on_paths([line for line in text.split("\n") if line])

        socket.readyRead.connect(process)
        socket.disconnected.connect(process)
        process()  # data may already be buffered when this slot runs

    def close(self) -> None:
        self._server.close()
