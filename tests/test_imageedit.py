"""Engine image ops: list, hit-test, insert, replace-in-place (E6)."""

from __future__ import annotations

import pymupdf
import pytest

from pdfcore.document import PdfDocument


def _images(path, page_index=0):
    doc = pymupdf.open(str(path))
    try:
        return doc[page_index].get_image_info(xrefs=True)
    finally:
        doc.close()


def _pixel(path, x_pt, y_pt, zoom=2.0):
    doc = pymupdf.open(str(path))
    try:
        pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        return pix.pixel(round(x_pt * zoom), round(y_pt * zoom))
    finally:
        doc.close()


def test_images_on_page_and_hit_test(quote_pdf):
    with PdfDocument.open(quote_pdf.path) as doc:
        infos = doc.images(0)
        assert len(infos) == 1
        logo = infos[0]
        # The fixture logo sits at (40, 30)-(160, 110).
        assert logo.bbox == pytest.approx((40.0, 30.0, 160.0, 110.0), abs=1.0)

        assert doc.image_at(0, 100.0, 70.0) == logo  # centre hit
        assert doc.image_at(0, 300.0, 70.0) is None  # miss


def test_insert_image_roundtrip(quote_pdf, sample_png, tmp_path):
    out = tmp_path / "with_image.pdf"
    with PdfDocument.open(quote_pdf.path) as doc:
        doc.insert_image(0, (300.0, 400.0, 380.0, 460.0), sample_png)
        doc.save(out)

    infos = _images(out)
    assert len(infos) == 2
    new = min(infos, key=lambda i: abs(i["bbox"][0] - 300.0))
    bx = new["bbox"]
    # Aspect preserved within the requested rect.
    assert bx[0] >= 299.0 and bx[2] <= 381.0 and bx[1] >= 399.0 and bx[3] <= 461.0
    # The sample is near-black (30); the fixture logo is 90-grey.
    cx, cy = (bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2
    assert max(_pixel(out, cx, cy)) < 60


def test_insert_image_rejects_missing_file(quote_pdf, tmp_path):
    with PdfDocument.open(quote_pdf.path) as doc:
        with pytest.raises(ValueError, match="not found"):
            doc.insert_image(0, (300, 400, 380, 460), tmp_path / "nope.png")


def test_replace_image_swaps_in_place(quote_pdf, sample_png, tmp_path):
    """The logo is swapped inside its own rectangle; text and gridlines
    survive (LINE_ART_NONE / TEXT_NONE recipe)."""
    out = tmp_path / "replaced.pdf"
    with PdfDocument.open(quote_pdf.path) as doc:
        logo = doc.images(0)[0]
        doc.replace_image(0, logo, sample_png)
        doc.save(out)

    infos = _images(out)
    assert len(infos) == 1  # old removed, new placed
    bx = infos[0]["bbox"]
    assert bx[0] >= 39.0 and bx[2] <= 161.0 and bx[1] >= 29.0 and bx[3] <= 111.0
    # Content actually changed: fixture logo was 90-grey, sample is 30-grey.
    cx, cy = (bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2
    assert max(_pixel(out, cx, cy)) < 60

    doc = pymupdf.open(str(out))
    try:
        text = doc[0].get_text()
        assert "QUOTE Q-1001" in text  # text untouched
        drawings = len(doc[0].get_drawings())
    finally:
        doc.close()
    assert drawings >= 9  # the fixture's 9 gridlines survive


def test_replace_image_rejects_missing_file(quote_pdf, tmp_path):
    with PdfDocument.open(quote_pdf.path) as doc:
        logo = doc.images(0)[0]
        with pytest.raises(ValueError, match="not found"):
            doc.replace_image(0, logo, tmp_path / "nope.png")


def test_replace_image_refuses_when_another_image_overlaps(sample_png, tmp_path):
    """IMAGE_REMOVE deletes every overlapping image whole (review finding) —
    the replace must refuse instead of silently destroying the neighbour."""
    src = tmp_path / "overlap.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 30))
    pix.clear_with(90)
    page.insert_image(pymupdf.Rect(100, 100, 300, 300), pixmap=pix)
    page.insert_image(pymupdf.Rect(250, 250, 350, 350), pixmap=pix)  # overlaps
    doc.save(str(src))
    doc.close()

    with PdfDocument.open(src) as pdoc:
        images = pdoc.images(0)
        assert len(images) == 2
        target = next(i for i in images if i.bbox[0] > 200)
        with pytest.raises(ValueError, match="overlaps"):
            pdoc.replace_image(0, target, sample_png)
        assert len(pdoc.images(0)) == 2  # nothing was destroyed


# --- delete + move (E9.2) ----------------------------------------------------


def _photo_pdf(tmp_path):
    """A page with one distinctly-coloured JPEG image (extractable bytes)."""
    path = tmp_path / "photo.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 40))
    pix.clear_with(30)  # dark grey
    page.insert_text((72, 300), "caption below the photo", fontsize=10)
    page.insert_image(pymupdf.Rect(80, 100, 200, 180), pixmap=pix)
    doc.save(str(path))
    doc.close()
    return path


def test_delete_image_removes_it_and_spares_text(tmp_path):
    src = _photo_pdf(tmp_path)
    out = tmp_path / "deleted.pdf"
    with PdfDocument.open(src) as doc:
        target = doc.images(0)[0]
        doc.delete_image(0, target)
        doc.save(out)

    assert _images(out) == []  # image gone
    reopened = pymupdf.open(str(out))
    try:
        assert "caption below the photo" in reopened[0].get_text()  # text survived
    finally:
        reopened.close()


def test_move_image_relocates_preserving_size(tmp_path):
    src = _photo_pdf(tmp_path)
    out = tmp_path / "moved.pdf"
    with PdfDocument.open(src) as doc:
        target = doc.images(0)[0]
        w = target.bbox[2] - target.bbox[0]
        h = target.bbox[3] - target.bbox[1]
        doc.move_image(0, target, (150.0, 250.0))
        doc.save(out)

    infos = _images(out)
    assert len(infos) == 1  # still exactly one image
    bx = infos[0]["bbox"]
    assert bx[0] == pytest.approx(80 + 150, abs=2.0)
    assert bx[1] == pytest.approx(100 + 250, abs=2.0)
    assert (bx[2] - bx[0]) == pytest.approx(w, abs=2.0)  # size preserved
    assert (bx[3] - bx[1]) == pytest.approx(h, abs=2.0)
    # The moved image is where it landed and gone from where it was.
    assert max(_pixel(out, 80 + 150 + w / 2, 100 + 250 + h / 2)) < 70
    assert max(_pixel(out, 80 + w / 2, 100 + h / 2)) > 200  # old spot now blank


def test_move_image_clamped_to_page(tmp_path):
    src = _photo_pdf(tmp_path)
    out = tmp_path / "clamped.pdf"
    with PdfDocument.open(src) as doc:
        page_w, page_h = doc.page_size(0)
        target = doc.images(0)[0]
        doc.move_image(0, target, (9999.0, 9999.0))
        doc.save(out)
    bx = _images(out)[0]["bbox"]
    assert bx[2] <= page_w + 0.5 and bx[3] <= page_h + 0.5  # stayed on the page


def _transparent_sig_png(tmp_path):
    """A signature-like RGBA PNG: opaque black diagonal, transparent elsewhere.

    Local (5, 35) is transparent; (5, 5) is on the stroke.
    """
    w, h = 80, 40
    buf = bytearray(w * h * 4)  # all zero == transparent black
    for x in range(w):
        for dy in range(-2, 3):
            y = (x % h) + dy
            if 0 <= y < h:
                i = (y * w + x) * 4
                buf[i : i + 4] = bytes((0, 0, 0, 255))  # opaque black stroke
    path = tmp_path / "sig.png"
    pymupdf.Pixmap(pymupdf.csRGB, w, h, bytes(buf), 1).save(str(path))
    return path


def _rgb_at(path, rx, ry, zoom=3.0):
    doc = pymupdf.open(str(path))
    try:
        pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        return pix.pixel(round(rx * zoom), round(ry * zoom))
    finally:
        doc.close()


# A mid-grey backdrop drawn behind the image; the transparent background must
# render THIS colour (background shows through) — distinct from black (the bug,
# which paints the opaque base colour) and from white (an empty page, which the
# base-is-black white-inference test couldn't tell apart).
_GREY = 128


def _sig_over_grey_pdf(tmp_path, sig):
    path = tmp_path / "s.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    # Grey rectangle spanning both the original and moved image locations.
    page.draw_rect(pymupdf.Rect(80, 80, 420, 380), color=(0.5, 0.5, 0.5), fill=(0.5, 0.5, 0.5))
    page.insert_image(pymupdf.Rect(100, 100, 180, 140), filename=str(sig))
    doc.save(str(path))
    doc.close()
    return path


def _is_grey(rgb, tol=25):
    return all(abs(c - _GREY) <= tol for c in rgb)


def test_move_preserves_transparency(tmp_path):
    """The signature-goes-black bug: moving a transparent PNG must keep its
    transparent background. Proven by a grey backdrop showing THROUGH the
    transparent pixels (the buggy extract_image path painted them black)."""
    sig = _transparent_sig_png(tmp_path)
    src = _sig_over_grey_pdf(tmp_path, sig)

    out = tmp_path / "moved.pdf"
    with PdfDocument.open(src) as pdoc:
        target = pdoc.images(0)[0]
        pdoc.move_image(0, target, (150.0, 200.0))
        pdoc.save(out)

    bg = _rgb_at(out, 100 + 150 + 5, 100 + 200 + 35)  # transparent corner
    stroke = _rgb_at(out, 100 + 150 + 5, 100 + 200 + 5)  # opaque stroke
    assert _is_grey(bg), f"background not showing through (transparency lost): {bg}"
    assert max(stroke) < 40, f"stroke not opaque black: {stroke}"


def test_resize_image_scales_and_preserves_transparency(tmp_path):
    sig = _transparent_sig_png(tmp_path)
    src = _sig_over_grey_pdf(tmp_path, sig)

    out = tmp_path / "resized.pdf"
    with PdfDocument.open(src) as pdoc:
        target = pdoc.images(0)[0]
        pdoc.resize_image(0, target, (100.0, 100.0, 140.0, 120.0))  # half size
        pdoc.save(out)

    info = _images(out)[0]
    bx = info["bbox"]
    assert (bx[2] - bx[0]) == pytest.approx(40.0, abs=2.0)
    assert (bx[3] - bx[1]) == pytest.approx(20.0, abs=2.0)
    # Grey backdrop still shows through the transparent pixels after resize.
    assert _is_grey(_rgb_at(out, 100 + 2, 100 + 17)), "transparency lost on resize"


def test_resize_image_rejects_tiny(tmp_path):
    src = _photo_pdf(tmp_path)
    with PdfDocument.open(src) as doc:
        target = doc.images(0)[0]
        with pytest.raises(ValueError, match="too small"):
            doc.resize_image(0, target, (100.0, 100.0, 102.0, 102.0))


# --- rotate (E9.7) -----------------------------------------------------------


def _half_dark_pdf(tmp_path):
    """A landscape image whose TOP half is dark — orientation is observable."""
    path = tmp_path / "halfdark.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 40))
    pix.clear_with(230)
    for x in range(60):
        for y in range(20):
            pix.set_pixel(x, y, (10, 10, 10))
    page.insert_image(pymupdf.Rect(80, 100, 200, 180), pixmap=pix)
    doc.save(str(path))
    doc.close()
    return path


def _dark_side(path, bbox):
    """Which half of ``bbox`` (in the saved PDF) is dark: top/bottom/left/right.

    Samples at centre ± a quarter (well inside each half — edge-midpoint
    samples land on the dark/light boundary and tie), picking the axis with
    the stronger contrast.
    """
    x0, y0, x1, y1 = bbox
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    qx, qy = (x1 - x0) / 4, (y1 - y0) / 4
    top = max(_pixel(path, cx, cy - qy))
    bottom = max(_pixel(path, cx, cy + qy))
    left = max(_pixel(path, cx - qx, cy))
    right = max(_pixel(path, cx + qx, cy))
    if abs(top - bottom) >= abs(left - right):
        return "top" if top < bottom else "bottom"
    return "left" if left < right else "right"


def test_rotate_image_ccw_turns_and_swaps_rect(tmp_path):
    src = _half_dark_pdf(tmp_path)
    out = tmp_path / "ccw.pdf"
    with PdfDocument.open(src) as doc:
        target = doc.images(0)[0]
        doc.rotate_image(0, target, 90)  # engine +90 = counter-clockwise
        doc.save(out)

    bx = _images(out)[0]["bbox"]
    # Rect swapped about the centre (140, 140): 120x80 -> 80x120.
    assert bx[0] == pytest.approx(100.0, abs=1.5) and bx[2] == pytest.approx(180.0, abs=1.5)
    assert bx[1] == pytest.approx(80.0, abs=1.5) and bx[3] == pytest.approx(200.0, abs=1.5)
    assert _dark_side(out, bx) == "left"  # top went left: counter-clockwise


def test_rotate_image_cw(tmp_path):
    src = _half_dark_pdf(tmp_path)
    out = tmp_path / "cw.pdf"
    with PdfDocument.open(src) as doc:
        target = doc.images(0)[0]
        doc.rotate_image(0, target, -90)  # UI "clockwise"
        doc.save(out)
    bx = _images(out)[0]["bbox"]
    assert _dark_side(out, bx) == "right"  # top went right: clockwise


def test_rotate_twice_compounds_to_180(tmp_path):
    """The placement's current rotation is read back from its transform, so a
    second rotation continues from the first instead of starting over."""
    src = _half_dark_pdf(tmp_path)
    out = tmp_path / "twice.pdf"
    with PdfDocument.open(src) as doc:
        doc.rotate_image(0, doc.images(0)[0], 90)
        doc.rotate_image(0, doc.images(0)[0], 90)  # fresh info: new bbox
        doc.save(out)
    bx = _images(out)[0]["bbox"]
    # Two 90s: dimensions back to the original 120x80 about the same centre.
    assert (bx[2] - bx[0]) == pytest.approx(120.0, abs=2.0)
    assert (bx[3] - bx[1]) == pytest.approx(80.0, abs=2.0)
    assert _dark_side(out, bx) == "bottom"  # 180: top ended up at the bottom


def test_move_preserves_rotation(tmp_path):
    """Regression: move/resize re-place by xref and previously reset the
    rotation to the page default — a turned image snapped back upright."""
    src = _half_dark_pdf(tmp_path)
    out = tmp_path / "rotmove.pdf"
    with PdfDocument.open(src) as doc:
        doc.rotate_image(0, doc.images(0)[0], 90)
        doc.move_image(0, doc.images(0)[0], (150.0, 100.0))
        doc.save(out)
    bx = _images(out)[0]["bbox"]
    assert bx[0] == pytest.approx(250.0, abs=2.0)  # moved
    assert _dark_side(out, bx) == "left"  # ...and still rotated


def test_rotate_image_rejects_bad_degrees(tmp_path):
    src = _half_dark_pdf(tmp_path)
    with PdfDocument.open(src) as doc:
        with pytest.raises(ValueError, match="90"):
            doc.rotate_image(0, doc.images(0)[0], 45)


def test_move_image_refuses_on_overlap(sample_png, tmp_path):
    src = tmp_path / "overlap.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 30))
    pix.clear_with(90)
    page.insert_image(pymupdf.Rect(100, 100, 300, 300), pixmap=pix)
    page.insert_image(pymupdf.Rect(250, 250, 350, 350), pixmap=pix)  # overlaps
    doc.save(str(src))
    doc.close()

    with PdfDocument.open(src) as pdoc:
        target = next(i for i in pdoc.images(0) if i.bbox[0] > 200)
        with pytest.raises(ValueError, match="overlaps"):
            pdoc.move_image(0, target, (10.0, 10.0))
        assert len(pdoc.images(0)) == 2  # nothing destroyed


def test_insert_image_upright_on_rotated_page(tmp_path):
    """On a rotated page the bitmap must be counter-rotated so it appears
    upright to the viewer (review finding: it came out sideways)."""
    half_dark = tmp_path / "half.png"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40))
    pix.clear_with(240)  # light everywhere...
    for x in range(40):
        for y in range(20):
            pix.set_pixel(x, y, (10, 10, 10))  # ...dark TOP half
    pix.save(str(half_dark))

    src = tmp_path / "rot.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc[0].set_rotation(90)
    doc.save(str(src))
    doc.close()

    out = tmp_path / "rot_img.pdf"
    with PdfDocument.open(src) as pdoc:
        pdoc.insert_image(0, (100.0, 100.0, 180.0, 180.0), half_dark)
        pdoc.save(out)

    # Render the page AS VIEWED, map the image region into view space, and
    # assert the dark half sits at the VIEWED top (i.e. the image is upright).
    reopened = pymupdf.open(str(out))
    try:
        page = reopened[0]
        bx = page.get_image_info()[0]["bbox"]  # unrotated pts
        m = page.rotation_matrix  # unrotated -> viewed coordinates
        corners = [pymupdf.Point(bx[0], bx[1]) * m, pymupdf.Point(bx[2], bx[3]) * m]
        vx = sorted(c.x for c in corners)
        vy = sorted(c.y for c in corners)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))
        centre_x = (vx[0] + vx[1]) / 2
        v_top = pix.pixel(round(centre_x * 2), round((vy[0] + 10) * 2))
        v_bot = pix.pixel(round(centre_x * 2), round((vy[1] - 10) * 2))
    finally:
        reopened.close()
    assert max(v_top) < 90, f"viewed top not dark: {v_top} vs bottom {v_bot}"
    assert max(v_bot) > 180, f"viewed bottom not light: {v_bot}"
