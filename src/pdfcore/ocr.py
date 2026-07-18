"""OCR word extraction for pages with no text layer (Tesseract, O1).

READ/EXTRACT path only: OCR never touches the edit pipeline, redaction or
font mapping (CLAUDE.md guardrail). The page is rendered as VIEWED (rotation
applied — that is the orientation where text is upright for Tesseract) and
the recognised word boxes are mapped back through ``page.derotation_matrix``,
so the public API speaks unrotated page points like every other pdfcore
module.

pytesseract is a thin subprocess wrapper: it runs an external
``tesseract.exe`` that is NOT a pip package — a bundled asset in frozen
builds (packaged at O2), a local install in development. ``eng.traineddata``
resolution: tesseract looks at ``TESSDATA_PREFIX`` FIRST, then exe-relative
``tessdata`` — so a user-global ``TESSDATA_PREFIX`` hijacks the bundled
runtime (verified against the frozen build). :func:`extract_words` therefore
pins ``TESSDATA_PREFIX`` to the resolved exe's own ``tessdata`` for the
duration of the call (restored after). ``--tessdata-dir`` cannot be used
instead: pytesseract splits config with ``shlex(posix=False)`` on Windows,
which mangles quoted paths containing spaces.
"""

from __future__ import annotations

import os
import shutil
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pymupdf
import pytesseract
from PIL import Image
from pytesseract import Output

from pdfcore.render import render_page_at_dpi

DEFAULT_DPI = 300  # spike-verified: clean digital renders OCR reliably at 300

_DEV_TESSERACT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")


class TesseractNotFound(RuntimeError):
    """The tesseract executable could not be found; OCR is unavailable."""


@dataclass(frozen=True)
class OcrWord:
    """One recognised word.

    - ``text``: the word as recognised (whitespace-stripped by Tesseract).
    - ``bbox``: ``(x0, y0, x1, y1)`` in UNROTATED page points, top-left
      origin, y down — the same space as :class:`~pdfcore.textedit.TextSpan`.
    - ``confidence``: Tesseract's word confidence, 0–100.
    """

    text: str
    bbox: tuple[float, float, float, float]
    confidence: float


def tesseract_command() -> str:
    """Path of the tesseract executable to run.

    Frozen builds use ONLY the bundled copy (``<_MEIPASS>/tesseract/``) — a
    broken bundle must fail loudly, never pass because the build machine
    happens to have Tesseract installed. Development prefers the standard
    install location, then PATH. ``tessdata`` sits next to the exe in every
    case and is found by tesseract's exe-relative lookup.
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return str(base / "tesseract" / "tesseract.exe")
    if _DEV_TESSERACT.exists():
        return str(_DEV_TESSERACT)
    return "tesseract"


def tesseract_available() -> bool:
    """True when the resolved tesseract executable exists / is on PATH."""
    return shutil.which(tesseract_command()) is not None


def _tessdata_dir(exe: str) -> str | None:
    """The ``tessdata`` directory sitting next to ``exe``, if there is one."""
    resolved = shutil.which(exe) or exe
    candidate = Path(resolved).parent / "tessdata"
    return str(candidate) if candidate.is_dir() else None


@contextmanager
def _pinned_tessdata(tessdata: str | None):
    """Pin TESSDATA_PREFIX to the exe's own tessdata around a tesseract call.

    Tesseract consults TESSDATA_PREFIX BEFORE its exe-relative lookup, so a
    user-global value silently redirects the bundled runtime to the wrong (or
    missing) models. The previous value is restored afterwards.
    """
    if tessdata is None:
        yield
        return
    prev = os.environ.get("TESSDATA_PREFIX")
    os.environ["TESSDATA_PREFIX"] = tessdata
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("TESSDATA_PREFIX", None)
        else:
            os.environ["TESSDATA_PREFIX"] = prev


def extract_words(
    doc: pymupdf.Document, page_index: int, *, dpi: int = DEFAULT_DPI
) -> list[OcrWord]:
    """OCR page ``page_index``; word boxes in unrotated page points.

    Renders the page as viewed at ``dpi`` (greyscale — Tesseract binarises
    internally anyway), runs word-level TSV recognition, keeps real words
    (non-empty text, confidence >= 0; Tesseract's structural page/block/line
    rows carry confidence -1) and scales pixel boxes back to page points.

    Raises :class:`TesseractNotFound` when the binary is unavailable.
    """
    exe = tesseract_command()
    if shutil.which(exe) is None:
        raise TesseractNotFound(
            f"tesseract executable not found at {exe!r}; OCR is unavailable. "
            "Dev setup: winget install -e --id UB-Mannheim.TesseractOCR"
        )
    pytesseract.pytesseract.tesseract_cmd = exe

    page = doc[page_index]
    rendered = render_page_at_dpi(page, dpi, gray=True)
    image = Image.frombytes(
        "L",
        (rendered.width, rendered.height),
        rendered.samples,
        "raw",
        "L",
        rendered.stride,
        1,
    )
    with _pinned_tessdata(_tessdata_dir(exe)):
        data = pytesseract.image_to_data(
            image, lang="eng", config=f"--dpi {dpi}", output_type=Output.DICT
        )

    scale = 72.0 / dpi
    derotate = page.derotation_matrix
    words: list[OcrWord] = []
    for i, text in enumerate(data["text"]):
        if not text.strip():
            continue
        conf = float(data["conf"][i])
        if conf < 0:
            continue
        x, y = data["left"][i], data["top"][i]
        w, h = data["width"][i], data["height"][i]
        rect = pymupdf.Rect(x * scale, y * scale, (x + w) * scale, (y + h) * scale) * derotate
        rect.normalize()
        words.append(OcrWord(text=text, bbox=(rect.x0, rect.y0, rect.x1, rect.y1), confidence=conf))
    return words
