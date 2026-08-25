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

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPalette, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pdfapp import __version__ as APP_VERSION
from pdfapp import theme, updates
from pdfapp.resources import resource_path
from pdfapp.update_banner import UPDATE_NOW

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


NOT_CHECKED = "Updates haven't been checked yet."
CHECKING = "Checking for updates…"


def update_status_text(
    current: str = APP_VERSION,
    *,
    info: updates.UpdateInfo | None = None,
    error: str | None = None,
    checked: bool = False,
    checking: bool = False,
) -> str:
    """The Updates status line. Pure, so the wording is asserted without a window.

    The line never claims more than the check proved: "you're on the latest
    version" is said only after a check actually came back, an unreachable feed
    says so plainly rather than implying either answer, and a build that has not
    checked says that instead of guessing.
    """
    if checking:
        return CHECKING
    if error:
        return updates.unreachable_message()
    if info is not None and updates.is_newer(current, info.version):
        return updates.available_message(current, info.version)
    if info is not None or checked:
        return updates.current_message(current)
    return NOT_CHECKED


class AboutDialog(QDialog):
    """The About box, with a live Updates section.

    Deliberately dumb about updating: it renders a status string and emits when
    a button is pressed. MainWindow owns the checker and drives
    :meth:`set_update_status`, which keeps the dialog short-lived and rebuildable
    (its own long-standing convention) while the check outlives it.
    """

    checkRequested = Signal()
    updateRequested = Signal()

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
        layout.addWidget(self._build_updates_row())
        layout.addWidget(buttons)
        self.setMinimumWidth(420)

    # --- the Updates section --------------------------------------------
    def _build_updates_row(self) -> QWidget:
        """Status line, a Check button, and (when there is one) the action."""
        frame = QFrame(self)
        frame.setFrameShape(QFrame.Shape.HLine)  # a rule above the row
        row_host = QWidget(self)
        outer = QVBoxLayout(row_host)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        outer.addWidget(frame)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._status_label = QLabel(NOT_CHECKED, row_host)
        self._status_label.setWordWrap(True)
        row.addWidget(self._status_label, 1)

        self._update_button = QPushButton(UPDATE_NOW, row_host)
        self._update_button.clicked.connect(self.updateRequested)
        self._update_button.hide()  # shown only when an update is actually there
        row.addWidget(self._update_button, 0)

        self._check_button = QPushButton("Check for updates", row_host)
        self._check_button.clicked.connect(self.checkRequested)
        row.addWidget(self._check_button, 0)

        outer.addLayout(row)
        return row_host

    def update_status(self) -> str:
        """What the status line currently reads (the tests' handle on it)."""
        return self._status_label.text()

    def update_action(self) -> str | None:
        """The action button's text, or None while it is hidden."""
        return self._update_button.text() if self._update_button.isVisibleTo(self) else None

    def set_update_status(
        self, text: str, *, action: str | None = None, busy: bool = False
    ) -> None:
        """Render one state. ``action`` None hides the action button; ``busy``
        disables the Check button while a check is in flight."""
        self._status_label.setText(text)
        self._check_button.setEnabled(not busy)
        if action:
            self._update_button.setText(action)
            self._update_button.show()
        else:
            self._update_button.hide()
