"""Enforce the hard boundary: pdfcore must never import Qt/PySide6.

This is the guard that keeps the engine headless and fully testable without a
GUI. If it fails, PDF logic has leaked a UI dependency into pdfcore.
"""

import re
from pathlib import Path

PDFCORE = Path(__file__).resolve().parents[1] / "src" / "pdfcore"

# Matches `import PySide6...` / `from PySide6 import ...` and the PyQt/shiboken
# equivalents at the start of a line (ignoring leading whitespace).
_QT_IMPORT = re.compile(r"^\s*(?:import|from)\s+(?:PySide6|PyQt5|PyQt6|shiboken6)\b")


def test_pdfcore_has_no_qt_imports():
    offenders = []
    for py in sorted(PDFCORE.rglob("*.py")):
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if _QT_IMPORT.match(line):
                offenders.append(f"{py.relative_to(PDFCORE)}:{lineno}: {line.strip()}")
    assert not offenders, "pdfcore must not import Qt/PySide6:\n" + "\n".join(offenders)
