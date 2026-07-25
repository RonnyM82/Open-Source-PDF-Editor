"""Help → "About PDF Editor" dialog.

A small static dialog naming the app, its release version, the key bundled
components (with their runtime versions) and the licence. Follows the
GestureHelpDialog conventions: qt-material styles it app-wide, it is
short-lived so state is baked at construction, and the version data is
gathered by a pure helper (`component_versions` / `about_html`) so an
offscreen test can assert the content without showing a window.

Component versions come from the LIVE modules (``PySide6.__version__``,
``pymupdf.__version__``, ``platform.python_version()``), never from
``importlib.metadata`` — PyInstaller does not collect ``.dist-info`` by
default, so metadata lookups would read ``—`` in the frozen build while the
module attributes are always present. The APP version is single-sourced from
pyproject.toml via ``pdfcore/version.py`` (the spec bundles pyproject.toml so
the frozen build reads the same source).
"""

from __future__ import annotations

import platform

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from pdfapp import __version__ as APP_VERSION
from pdfapp import theme
from pdfapp.resources import resource_path

APP_NAME = "PDF Editor"
APP_TAGLINE = "Standalone PDF viewer + editor for Windows"
REPO_URL = "https://github.com/RonnyM82/Open-Source-PDF-Editor"

# Link colour per theme mode. qt-material's default anchor blue is too light
# on the white light-mode dialog (poor contrast — user report); a darker blue
# (~7:1 on white) reads clearly. Dark mode keeps a light blue that pops on the
# dark surface.
_LINK_COLOR = {theme.DARK: "#6ab0f3", theme.LIGHT: "#0b5394"}


def _link_qcolor(mode: str | None = None) -> QColor:
    """The source-link colour as a QColor (for the label palette)."""
    return QColor(_LINK_COLOR.get(mode or theme.current_mode(), _LINK_COLOR[theme.DARK]))


def component_versions() -> dict[str, str]:
    """Runtime versions of the app and its key bundled components.

    Each lookup degrades to ``"—"`` rather than raising, so a stripped-down
    build still produces a dialog.
    """
    versions: dict[str, str] = {
        APP_NAME: APP_VERSION,
        "Python": platform.python_version(),
    }
    try:
        from PySide6 import __version__ as pyside_version
        from PySide6.QtCore import qVersion

        versions["PySide6"] = pyside_version
        versions["Qt"] = qVersion()
    except Exception:  # pragma: no cover - PySide6 is always present in the app
        versions["PySide6"] = "—"
    try:
        import pymupdf

        versions["PyMuPDF"] = getattr(pymupdf, "__version__", "—")
    except Exception:  # pragma: no cover - pymupdf is a hard dependency
        versions["PyMuPDF"] = "—"
    return versions


def about_html(mode: str | None = None) -> str:
    """The rich-text body of the About dialog (heading, versions, licence).

    ``mode`` picks the source-link colour (defaults to the current theme);
    the ``<a>`` carries it inline because qt-material's default anchor blue is
    too light on the light-mode dialog.
    """
    link_color = _LINK_COLOR.get(mode or theme.current_mode(), _LINK_COLOR[theme.DARK])
    versions = component_versions()
    rows = "".join(
        f"<tr><td><b>{name}</b>&nbsp;&nbsp;</td><td>{ver}</td></tr>"
        for name, ver in versions.items()
    )
    return (
        f"<h2 style='margin-bottom:2px'>{APP_NAME}</h2>"
        f"<p style='margin-top:0'>Version {APP_VERSION}</p>"
        f"<p>{APP_TAGLINE}. An internal, open-source tool.</p>"
        f"<h3 style='margin-bottom:2px'>Components</h3>"
        f"<table cellspacing='0' cellpadding='2'>{rows}</table>"
        f"<h3 style='margin-bottom:2px'>Licence</h3>"
        "<p style='margin-top:0'>Released under the GNU Affero General Public "
        "License v3.0 or later (AGPL-3.0). The complete corresponding source "
        f"is available at<br>"
        f"<a href='{REPO_URL}' style='color:{link_color}'>{REPO_URL}</a>.</p>"
    )


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")

        # App icon beside the text — a light identity cue; degrades gracefully
        # when the bundled PNG can't be loaded (icon label just stays empty).
        icon_label = QLabel(self)
        pixmap = QPixmap(str(resource_path("assets/icon.png")))
        if not pixmap.isNull():
            icon_label.setPixmap(
                pixmap.scaled(
                    64,
                    64,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        icon_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        icon_label.setContentsMargins(4, 8, 12, 4)

        mode = theme.current_mode()
        text_label = QLabel(about_html(mode), self)
        text_label.setTextFormat(Qt.TextFormat.RichText)
        text_label.setWordWrap(True)
        text_label.setOpenExternalLinks(True)
        text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        # The palette Link role backs up the inline <a> colour — qt-material's
        # stylesheet can otherwise repaint anchors with its own (too-light) blue.
        pal = text_label.palette()
        pal.setColor(QPalette.ColorRole.Link, _link_qcolor(mode))
        text_label.setPalette(pal)

        body = QHBoxLayout()
        body.addWidget(icon_label)
        body.addWidget(text_label, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addLayout(body)
        layout.addWidget(buttons)
        self.setMinimumWidth(420)
