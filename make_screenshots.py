"""Capture real app UI screenshots offscreen for the README / landing page."""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, "src")
from pdfapp import theme  # noqa: E402
from pdfapp.main_window import MainWindow  # noqa: E402
from pdfapp.resources import resource_path  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "shots")
OUT.mkdir(parents=True, exist_ok=True)
SAMPLES = Path("samples")


def pump(app, seconds=2.0):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def grab(window, name):
    pump(window_app, 1.5)
    window.grab().save(str(OUT / name))
    print("saved", name, window.size())


app = QApplication.instance() or QApplication(sys.argv)
window_app = app
app.setApplicationName("PDF Editor")
theme.apply_theme(app)
app.setWindowIcon(QIcon(str(resource_path("assets/icon.png"))))

# --- Shot 1: viewer, dark theme (default), quote open ---
win = MainWindow()
win.resize(1440, 900)
win.show()
win.open_path(SAMPLES / "sample_quote.pdf")
grab(win, "screenshot-viewer.png")

# --- Shot 2: edit mode + reveal-all outlines on the same quote ---
win._edit_mode_action.setChecked(True)  # toggles the active view into edit mode
grab(win, "screenshot-editing.png")

# --- Shot 3: light theme, CAD drawing ---
theme.apply_theme(app, theme.LIGHT)
win2 = MainWindow()
win2.resize(1440, 900)
win2.show()
win2.open_path(SAMPLES / "sample_cad_drawing.pdf")
grab(win2, "screenshot-cad.png")

print("done")
