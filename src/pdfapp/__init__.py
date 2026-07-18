"""pdfapp — the thin PySide6 UI layer.

Imports pdfcore only. Wires widgets to engine calls and renders; contains no PDF
logic of its own. The single Qt<->engine seam is pdfapp/qt_image.py.
"""

__version__ = "0.1.3"
