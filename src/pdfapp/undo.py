"""Snapshot-based undo (Phase 2).

ALL document mutations flow through the per-document QUndoStack as
SnapshotCommands — a snapshot restore replaces the WHOLE document, so any
mutation that bypassed the stack would be silently resurrected by a later
undo. Undo restores document STATE; it never reverses a PDF op (redaction is
irreversible at the PDF level — we restore pre-redaction bytes instead).

A command runs its engine op on first ``redo()`` (``QUndoStack.push`` triggers
it) and captures before/after snapshots; every later undo/redo is a pure byte
restore. Failures never propagate across the C++ signal boundary: a failed op
restores the before-bytes, marks the command obsolete (Qt drops it from the
stack), and records the exception in ``error`` for the caller to report.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtGui import QUndoCommand

from pdfcore.document import PdfDocument

# Per-document byte budget for held snapshots (mirrors the render-cache budget).
_UNDO_BYTES = 256 * 1024 * 1024
# Hard depth cap even for tiny files.
_MAX_DEPTH = 64


def undo_limit_for(file_size: int, budget: int = _UNDO_BYTES) -> int:
    """Undo depth that keeps before+after snapshot pairs within ``budget``.

    Pure (unit-tested without Qt). Computed ONCE at stack creation — Qt
    ignores ``setUndoLimit`` on a non-empty stack. Never below 1: even a
    document larger than the budget gets one real undo step (~2x file size
    resident — the documented worst case).
    """
    pair = 2 * max(1, file_size)
    return max(1, min(_MAX_DEPTH, budget // pair))


class SnapshotCommand(QUndoCommand):
    """Generic undoable mutation: run an engine op once, then restore bytes.

    ``scope`` tells the view how to refresh after the FIRST execution —
    ``("page", n)`` for content-scoped ops, ``("all", -1)`` for structural
    ones. Every restore refreshes as ``("all", -1)`` regardless: a snapshot
    swap can change anything.
    """

    def __init__(
        self,
        text: str,
        view,  # DocumentView; untyped to keep this module import-light
        op: Callable[[PdfDocument], object],
        scope: tuple[str, int] = ("all", -1),
    ) -> None:
        super().__init__(text)
        self._view = view
        self._op = op
        self._scope = scope
        self._executed = False
        self._before: bytes | None = None
        self._after: bytes | None = None
        self.error: Exception | None = None

    def redo(self) -> None:
        doc = self._view.document
        if not self._executed:
            self._before = doc.snapshot()
            try:
                self._op(doc)
            except Exception as exc:  # noqa: BLE001 - recorded for the caller
                doc.restore(self._before)
                self.error = exc
                self.setObsolete(True)
                return
            self._after = doc.snapshot()
            self._executed = True
            self._view.after_command(self._scope)
        else:
            doc.restore(self._after)
            self._view.after_command(("all", -1))

    def undo(self) -> None:
        self._view.document.restore(self._before)
        self._view.after_command(("all", -1))
