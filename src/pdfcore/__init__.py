"""pdfcore — the headless PDF engine.

Pure Python + PyMuPDF. This package must NEVER import Qt/PySide6 (see CLAUDE.md
for the hard engine/UI boundary rule; a guard test enforces it). All viewing and
editing operations live here as functions/classes with plain inputs and outputs,
fully testable with pytest and no GUI.
"""

# Derived from pyproject.toml [project].version — the single source (see
# pdfcore/version.py). Never hardcode a version string here again.
from pdfcore.version import app_version

__version__ = app_version()
