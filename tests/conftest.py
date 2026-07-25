"""Shared pytest fixtures: programmatically generated sample PDFs.

Each fixture writes into pytest's ``tmp_path`` and returns the file path (or, for
the encrypted case, a small named tuple with the passwords). See CLAUDE.md
"Testing": these generated fixtures are the whole corpus for now.
"""

from __future__ import annotations

import io
import os
from collections import namedtuple
from pathlib import Path

import pymupdf
import pytest


@pytest.fixture(scope="session")
def qapp():
    """A single offscreen QApplication shared by all UI tests.

    PySide6 is imported lazily inside the fixture so engine-only test runs never
    load Qt. The offscreen platform ensures no window is ever shown.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _isolate_app_data(tmp_path, monkeypatch):
    """Redirect the app's own data dir (recent files, etc.) to tmp_path.

    ``portable.data_dir()`` resolves to ``%LOCALAPPDATA%\\PDF Editor`` in a dev /
    test run, so without this every MainWindow that opens a file would write into
    the developer's real profile. Pointing LOCALAPPDATA at tmp_path keeps state
    per-test and off the real machine. test_ui_portable sets its own LOCALAPPDATA
    and so is unaffected.
    """
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))


@pytest.fixture
def theme_app(qapp):
    """qapp + the theme module, restoring EVERYTHING qt-material mutates.

    The QApplication is session-scoped and shared, so teardown restores the
    stylesheet, app font, palette (qt-material sets a translucent-blue Text
    role app-wide) and QStyle (it swaps to Fusion), plus the theme module's
    own state. Roboto font-database registration and the ~/.qt_material
    cache are benign additive residue that cannot be undone.
    """
    from pdfapp import theme

    font_before = qapp.font()
    palette_before = qapp.palette()
    style_before = qapp.style().objectName()
    yield qapp, theme
    qapp.setStyleSheet("")
    qapp.setFont(font_before)
    qapp.setPalette(palette_before)
    if style_before and qapp.style().objectName() != style_before:
        qapp.setStyle(style_before)
    theme._reset_for_tests()


ENCRYPT_USER_PW = "user-secret"
ENCRYPT_OWNER_PW = "owner-secret"

EncryptedPDF = namedtuple("EncryptedPDF", ["path", "user_pw", "owner_pw"])


def _page_marker(i: int) -> str:
    """Unique, searchable text stamped on page ``i`` (for page-op round-trips)."""
    return f"PAGE-MARKER-{i:03d}"


def _add_text_page(doc: pymupdf.Document, heading: str, body_lines: int = 20) -> None:
    page = doc.new_page()
    page.insert_text((72, 72), heading, fontsize=14)
    y = 110
    for j in range(body_lines):
        page.insert_text((72, y), f"Lorem ipsum dolor sit amet, line {j}.", fontsize=11)
        y += 18


@pytest.fixture
def text_pdf(tmp_path) -> Path:
    """A 3-page, text-heavy PDF."""
    doc = pymupdf.open()
    for i in range(3):
        _add_text_page(doc, f"Heading for page {i}")
    path = tmp_path / "text.pdf"
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture
def image_pdf(tmp_path) -> Path:
    """A 2-page PDF; the second page embeds a raster image."""
    doc = pymupdf.open()
    _add_text_page(doc, "Cover page")
    page = doc.new_page()
    page.insert_text((72, 72), "Page with an image", fontsize=14)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 80))
    pix.clear_with(200)
    page.insert_image(pymupdf.Rect(72, 110, 192, 190), pixmap=pix)
    path = tmp_path / "image.pdf"
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture
def multipage_pdf(tmp_path) -> Path:
    """A 5-page PDF; each page carries a unique searchable marker.

    Used by page-manipulation round-trip tests to assert on page identity after
    reorder / delete / insert.
    """
    doc = pymupdf.open()
    for i in range(5):
        page = doc.new_page()
        page.insert_text((72, 72), _page_marker(i), fontsize=24)
    path = tmp_path / "multipage.pdf"
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture
def page_marker():
    """Expose the marker helper to tests that assert on page identity."""
    return _page_marker


# A 3-page PDF carrying pre-existing hyperlinks (a URI link and an internal
# go-to-page link), for the "recognise existing links" read path and edit
# round-trips. Constants ride the namedtuple (importlib keeps conftest
# un-importable from test modules). Links must be inserted into a REAL PDF page
# (insert_link raises on a fresh new_page doc), so the fixture saves a base,
# reopens it, inserts the links, then saves the final file.
LinksPDF = namedtuple(
    "LinksPDF",
    ["path", "uri", "uri_rect", "goto_page", "goto_rect", "page_count", "text_under_uri"],
)


@pytest.fixture
def links_pdf(tmp_path) -> LinksPDF:
    uri = "https://example.com/quote"
    uri_rect = (72.0, 100.0, 240.0, 118.0)
    goto_rect = (72.0, 140.0, 240.0, 158.0)
    goto_page = 2
    text_under_uri = "Visit our website"

    base = tmp_path / "links_base.pdf"
    doc = pymupdf.open()
    for i in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), f"Links page {i}", fontsize=14)
        page.insert_text((72, 113), text_under_uri, fontsize=11)  # sits under uri_rect
        page.insert_text((72, 153), "Jump to the last page", fontsize=11)
    doc.save(str(base))
    doc.close()

    doc = pymupdf.open(str(base))
    doc[0].insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(uri_rect), "uri": uri})
    doc[0].insert_link(
        {
            "kind": pymupdf.LINK_GOTO,
            "from": pymupdf.Rect(goto_rect),
            "page": goto_page,
            "to": pymupdf.Point(0, 0),
        }
    )
    path = tmp_path / "links.pdf"
    doc.save(str(path))
    doc.close()
    return LinksPDF(
        path=path,
        uri=uri,
        uri_rect=uri_rect,
        goto_page=goto_page,
        goto_rect=goto_rect,
        page_count=3,
        text_under_uri=text_under_uri,
    )


# The generated quote fixture: strings tests locate spans by, plus the table
# geometry (PDF points) for pixel probes. Threaded through a namedtuple because
# importlib import mode keeps conftest un-importable from test modules.
QuotePDF = namedtuple(
    "QuotePDF",
    [
        "path",
        "heading",  # hebo, 16pt
        "price",  # helv, 9pt, in a gridline cell
        "total",
        "date",
        "address",
        "terms_label",  # hebo — abuts terms_value on the same line
        "terms_value",  # helv
        "red_text",  # helv, colour (1, 0, 0)
        "table_x0",  # table origin; 3 rows x 4 cols of cell_w x cell_h cells
        "table_y0",
        "cols",
        "rows",
        "cell_w",
        "cell_h",
    ],
)

_QUOTE = QuotePDF(
    path=None,
    heading="QUOTE Q-1001",
    price="$1,234.50",
    total="$2,469.00",
    date="2026-07-02",
    address="115 Sample Street",
    terms_label="Terms:",
    terms_value="NET 30 days",
    red_text="OVERDUE",
    table_x0=72.0,
    table_y0=300.0,
    cols=4,
    rows=3,
    cell_w=100.0,
    cell_h=24.0,
)


@pytest.fixture
def quote_pdf(tmp_path) -> QuotePDF:
    """A generated quote-style PDF: logo image, gridline table, helv/hebo text.

    The deterministic stand-in for the real quote sample (samples/ is
    local-only): known strings, fonts and coordinates for span extraction and
    the redaction-survival tests (gridlines are line-art; the logo is an image).
    """
    q = _QUOTE
    doc = pymupdf.open()
    page = doc.new_page()

    # "Logo": a raster image top-left.
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 80))
    pix.clear_with(90)
    page.insert_image(pymupdf.Rect(40, 30, 160, 110), pixmap=pix)

    # Bold heading, plus two ABUTTING same-line spans (different fonts keep
    # them separate spans) and a coloured span.
    page.insert_text((200, 60), q.heading, fontname="hebo", fontsize=16)
    x, y = 200.0, 90.0
    page.insert_text((x, y), q.terms_label + " ", fontname="hebo", fontsize=10)
    x += pymupdf.get_text_length(q.terms_label + " ", fontname="hebo", fontsize=10)
    page.insert_text((x, y), q.terms_value, fontname="helv", fontsize=10)
    page.insert_text((200, 110), q.red_text, fontname="helv", fontsize=10, color=(1, 0, 0))

    # Gridline table (line-art the E2 redaction must not remove).
    x1 = q.table_x0 + q.cols * q.cell_w
    y1 = q.table_y0 + q.rows * q.cell_h
    for i in range(q.rows + 1):
        yy = q.table_y0 + i * q.cell_h
        page.draw_line(pymupdf.Point(q.table_x0, yy), pymupdf.Point(x1, yy))
    for j in range(q.cols + 1):
        xx = q.table_x0 + j * q.cell_w
        page.draw_line(pymupdf.Point(xx, q.table_y0), pymupdf.Point(xx, y1))

    cells = [
        "Item",
        "Qty",
        "Unit",
        "Total",
        "C-Thru Separator",
        "2",
        q.price,
        q.total,
        q.date,
        q.address,
        "GST",
        "0.00",
    ]
    for idx, value in enumerate(cells):
        row, col = divmod(idx, q.cols)
        page.insert_text(
            (q.table_x0 + col * q.cell_w + 4, q.table_y0 + row * q.cell_h + 16),
            value,
            fontname="helv",
            fontsize=9,
        )

    path = tmp_path / "quote.pdf"
    doc.save(str(path))
    doc.close()
    return q._replace(path=path)


_ARIAL_TTF = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf"


@pytest.fixture
def embedded_font_pdf(tmp_path) -> Path:
    """A PDF whose text uses an EMBEDDED TrueType font (Windows Arial).

    Skips when arial.ttf is unavailable — acceptable for a Windows-internal
    tool. Exercises the embedded/best-effort branch of font mapping.
    """
    if not _ARIAL_TTF.exists():
        pytest.skip("arial.ttf not available to build the embedded-font fixture")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "Embedded font text",
        fontname="ArialEmbedded",
        fontfile=str(_ARIAL_TTF),
        fontsize=12,
    )
    path = tmp_path / "embedded_font.pdf"
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture
def sample_png(tmp_path) -> Path:
    """A small dark-grey PNG for image insert/replace tests."""
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 30))
    pix.clear_with(30)
    path = tmp_path / "sample.png"
    pix.save(str(path))
    return path


# --- digital signing (pyHanko) ----------------------------------------------
# The test certificate is GENERATED at test time via the engine's own
# self-signed helper — no cert material is ever committed. Session-scoped to
# amortise the RSA keygen across the signing tests.

SignerP12 = namedtuple("SignerP12", ["path", "password", "common_name"])


@pytest.fixture(scope="session")
def signer_p12(tmp_path_factory) -> SignerP12:
    """A generated self-signed PKCS#12 bundle + its password and subject CN."""
    from pdfcore.signing import generate_self_signed_p12

    common_name = "PDF Editor Test Signer"
    password = "test-p12-pass"
    path = tmp_path_factory.mktemp("signing") / "test-signer.p12"
    generate_self_signed_p12(path, common_name, password)
    return SignerP12(path=path, password=password, common_name=common_name)


# The quote sample is a SANITISED public document (fabricated identifiers):
# files carry SANITISED sample data (see the public samples/ directory).
REAL_QUOTE_PDF = Path(__file__).resolve().parents[1] / "samples" / "sample_quote.pdf"


@pytest.fixture
def real_quote_pdf() -> Path:
    """The real quote sample; tests using it skip cleanly when it is absent."""
    if not REAL_QUOTE_PDF.exists():
        pytest.skip("real quote sample not present (samples/ is local-only)")
    return REAL_QUOTE_PDF


# --- OCR fixtures (O-series) -------------------------------------------------
# Generated stand-ins for documents with NO text layer (scans / text exported
# as vector outlines): the text is rasterized into a full-page image, so
# get_text() returns nothing and only OCR can read it. Constants are threaded
# through namedtuples (importlib import mode keeps conftest un-importable).

OcrPDF = namedtuple(
    "OcrPDF",
    ["path", "expected_words", "anchor_word", "anchor_origin", "fontsize"],
)

_OCR_LINES = (
    ((72.0, 120.0), "Invoice SAMPLE 10001"),
    ((72.0, 200.0), "Total $3,600.00"),
)


def _rasterized_copy(src: pymupdf.Document, dpi: int) -> pymupdf.Document:
    """An image-only copy of ``src``: same page sizes, no text layer."""
    out = pymupdf.open()
    for page in src:
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
        dst = out.new_page(width=page.rect.width, height=page.rect.height)
        dst.insert_image(dst.rect, pixmap=pix)
    return out


@pytest.fixture
def ocr_pdf(tmp_path) -> OcrPDF:
    """A no-text-layer PDF with known words at known coordinates.

    ``anchor_word`` starts at ``anchor_origin`` (an ``insert_text`` baseline
    point) at ``fontsize`` — position assertions derive their tolerances
    from it.
    """
    doc = pymupdf.open()
    page = doc.new_page()
    for origin, line in _OCR_LINES:
        page.insert_text(origin, line, fontname="helv", fontsize=20)
    img_only = _rasterized_copy(doc, dpi=300)
    doc.close()
    path = tmp_path / "ocr.pdf"
    img_only.save(str(path))
    img_only.close()
    expected = tuple(word for _, line in _OCR_LINES for word in line.split())
    return OcrPDF(
        path=path,
        expected_words=expected,
        anchor_word="Invoice",
        anchor_origin=(72.0, 120.0),
        fontsize=20.0,
    )


OcrRotatedPDF = namedtuple("OcrRotatedPDF", ["path", "word", "expected_bbox"])


@pytest.fixture
def ocr_rotated_pdf(tmp_path) -> OcrRotatedPDF:
    """A ``/Rotate 90`` no-text-layer page whose VIEWED content reads upright.

    Built by pre-rotating the raster 90° counter-clockwise so that the page
    rotation's clockwise viewing turns it upright again (like a scanner that
    stored the sheet sideways). ``expected_bbox`` is the word's box in
    UNROTATED page points: the INK bounding box measured from the raster's own
    pixels (Tesseract boxes hug ink, not font ascender/descender metrics),
    pushed through the CCW point mapping ``(x, y) -> (y, W - x)`` — an oracle
    independent of the engine's derotation.
    """
    from PIL import Image as PILImage
    from PIL import ImageOps

    src = pymupdf.open()
    src_w, src_h = 400.0, 300.0
    page = src.new_page(width=src_w, height=src_h)
    page.insert_text((60, 150), "ROTATED", fontname="helv", fontsize=28)

    pix = page.get_pixmap(dpi=200, colorspace=pymupdf.csRGB, alpha=False)
    img = PILImage.frombytes("RGB", (pix.width, pix.height), pix.samples)
    src.close()

    scale = 72.0 / 200
    ink = ImageOps.invert(img.convert("L")).getbbox()  # the page's only word
    ix0, iy0, ix1, iy1 = (v * scale for v in ink)
    expected = (iy0, src_w - ix1, iy1, src_w - ix0)
    rotated = img.transpose(PILImage.Transpose.ROTATE_90)  # 90° CCW

    out = pymupdf.open()
    dst = out.new_page(width=src_h, height=src_w)  # dimensions swap
    buf = io.BytesIO()
    rotated.save(buf, format="PNG")
    dst.insert_image(dst.rect, stream=buf.getvalue())
    dst.set_rotation(90)
    path = tmp_path / "ocr_rotated.pdf"
    out.save(str(path))
    out.close()
    return OcrRotatedPDF(path=path, word="ROTATED", expected_bbox=expected)


# Generated invoice-style page for O3 field extraction: the invoice-print layout
# family (ID header row, items table, right-hand totals column sharing visual
# lines with a left-hand payments row, spaced-ABN line), rasterized to a
# no-text-layer page. Coordinates mirror the real sample's probed anchors.
InvoiceOcrPDF = namedtuple(
    "InvoiceOcrPDF",
    [
        "path",
        "invoice_no",
        "ref",
        "customer_po",
        "date",
        "abn",
        "subtotal",
        "tax",
        "total",
        "item1_total",
        "item2_total",
    ],
)


@pytest.fixture
def invoice_ocr_pdf(tmp_path) -> InvoiceOcrPDF:
    doc = pymupdf.open()
    page = doc.new_page()  # 595 x 842

    def put(x: float, y: float, text: str, size: float = 9.0) -> None:
        page.insert_text((x, y), text, fontname="helv", fontsize=size)

    # ID header + values (values left-align under their header anchors).
    put(50, 100, "Invoice Date")
    put(180, 100, "Ref")
    put(310, 100, "Invoice No")
    put(445, 100, "Customer PO No")
    put(50, 115, "23 Jun 2026")
    put(180, 115, "KOGREF99")
    put(310, 115, "77001")
    put(445, 115, "PO4512")

    # Items table: header anchors from the real sample's probe.
    for x, text in (
        (99, "Code"),
        (152, "Item"),
        (306, "Options"),
        (394, "Qty"),
        (420, "Unit Price"),
        (477, "Discount"),
        (532, "Subtotal"),
    ):
        put(x, 240, text)
    # Item 1 (one row + one continuation row), item 2 (single row).
    put(99, 256, "AAA111")
    put(152, 256, "Widget Pro Max")
    put(306, 256, "Colour: Blue")
    put(404, 256, "2")
    put(425, 256, "$10.00")
    put(535, 256, "$20.00")
    put(152, 267, "Extra descriptive words")
    put(99, 278, "BBB222")
    put(152, 278, "Gadget")
    put(404, 278, "3")
    put(422, 278, "$1,200.00")
    put(528, 278, "$3,600.00")

    # Totals column, first line SHARING its visual line with a payments row
    # (the two-column hazard: the money left of the label must be ignored).
    put(50, 340, "15 Jun 2026 Bank Transfer $3,620.00")
    put(420, 340, "Sub Total:")
    put(524, 340, "$3,620.00")
    put(420, 355, "Tax (10%):")
    put(524, 355, "$329.09")  # GST-inclusive: 3620.00 / 11
    put(380, 370, "Tax Invoice Total (AUD):")
    put(524, 370, "$3,620.00")

    # ABN with spaced digit groups (exercises the join path).
    put(99, 500, "ABN Number: 51 824 753 556")

    img_only = _rasterized_copy(doc, dpi=300)
    doc.close()
    path = tmp_path / "invoice_ocr.pdf"
    img_only.save(str(path))
    img_only.close()
    return InvoiceOcrPDF(
        path=path,
        invoice_no="77001",
        ref="KOGREF99",
        customer_po="PO4512",
        date="23 Jun 2026",
        abn="51824753556",
        subtotal="3620.00",
        tax="329.09",
        total="3620.00",
        item1_total="20.00",
        item2_total="3600.00",
    )


@pytest.fixture
def unsearchable_pdf(tmp_path) -> Path:
    """One page holding a BLANK raster: no text layer AND OCR finds nothing
    (SR4's "This document isn't searchable." case)."""
    src = pymupdf.open()
    blank = src.new_page()
    pix = blank.get_pixmap(dpi=72, colorspace=pymupdf.csRGB, alpha=False)
    src.close()
    out = pymupdf.open()
    page = out.new_page()
    page.insert_image(page.rect, pixmap=pix)
    path = tmp_path / "unsearchable.pdf"
    out.save(str(path))
    out.close()
    return path


MixedPDF = namedtuple(
    "MixedPDF",
    ["path", "native_page", "scanned_page", "blank_page", "native_marker", "scanned_words"],
)


@pytest.fixture
def mixed_pdf(tmp_path) -> MixedPDF:
    """Page 0 native text, page 1 rasterized words, page 2 a BLANK raster.

    The X2/SR4 routing fixture: one page per text source — native layer,
    OCR-able image, and the "no layer AND empty OCR" case.
    """
    out = pymupdf.open()
    p0 = out.new_page()
    p0.insert_text((72, 100), "MIXED-NATIVE-MARKER", fontsize=16)
    p0.insert_text((72, 140), "Native body line.", fontsize=12)

    src = pymupdf.open()
    scan_src = src.new_page()
    scan_src.insert_text((72, 120), "SCANNED MARKER WORDS", fontsize=22)
    scan_pix = scan_src.get_pixmap(dpi=300, colorspace=pymupdf.csRGB, alpha=False)
    blank_src = src.new_page()
    blank_pix = blank_src.get_pixmap(dpi=72, colorspace=pymupdf.csRGB, alpha=False)
    src.close()

    p1 = out.new_page()
    p1.insert_image(p1.rect, pixmap=scan_pix)
    p2 = out.new_page()
    p2.insert_image(p2.rect, pixmap=blank_pix)

    path = tmp_path / "mixed.pdf"
    out.save(str(path))
    out.close()
    return MixedPDF(
        path=path,
        native_page=0,
        scanned_page=1,
        blank_page=2,
        native_marker="MIXED-NATIVE-MARKER",
        scanned_words=("SCANNED", "MARKER", "WORDS"),
    )


# The CAD sample (sanitised; vector drawing whose section-line end labels
# export as same-baseline dict lines). LOCAL-ONLY like the other samples.
REAL_CAD_PDF = Path(__file__).resolve().parents[1] / "samples" / "sample_cad_drawing.pdf"


@pytest.fixture
def real_cad_pdf() -> Path:
    """The real CAD sample; tests using it skip cleanly when it is absent."""
    if not REAL_CAD_PDF.exists():
        pytest.skip("real CAD sample not present (samples/ is local-only)")
    return REAL_CAD_PDF


# The invoice sample (no text layer — the OCR target). A SANITISED public file
# like the quote; tests skip cleanly when it is absent.
REAL_INVOICE_PDF = Path(__file__).resolve().parents[1] / "samples" / "Invoice OCR.pdf"


@pytest.fixture
def real_invoice_pdf() -> Path:
    """The real OCR invoice sample; tests skip cleanly when it is absent."""
    if not REAL_INVOICE_PDF.exists():
        pytest.skip("real invoice sample not present (samples/ is local-only)")
    return REAL_INVOICE_PDF


@pytest.fixture
def encrypted_pdf(tmp_path) -> EncryptedPDF:
    """An AES-256 encrypted PDF with known user/owner passwords."""
    doc = pymupdf.open()
    _add_text_page(doc, "Encrypted document", body_lines=3)
    path = tmp_path / "encrypted.pdf"
    doc.save(
        str(path),
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw=ENCRYPT_OWNER_PW,
        user_pw=ENCRYPT_USER_PW,
    )
    doc.close()
    return EncryptedPDF(path=path, user_pw=ENCRYPT_USER_PW, owner_pw=ENCRYPT_OWNER_PW)
