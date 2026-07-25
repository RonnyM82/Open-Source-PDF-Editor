"""pdfapp — the thin PySide6 UI layer.

Imports pdfcore only. Wires widgets to engine calls and renders; contains no PDF
logic of its own. The single Qt<->engine seam is pdfapp/qt_image.py.
"""

# Derived from pyproject.toml [project].version — the single source (see
# pdfcore/version.py; pdfapp may import pdfcore, never the reverse). Never
# hardcode a version string here again.
from pdfcore import __version__ as __version__
