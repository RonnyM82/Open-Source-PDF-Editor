"""Engine digital signing (pyHanko): sign finalised bytes, validate, tamper.

Validation deliberately uses pyHanko's OWN validation API (the engine never
imports it) with the generated self-signed cert as the trust root. The two
tamper tests are the executable form of the terminal-operation constraint:
a byte flip inside the signed range breaks ``intact``, and a PyMuPDF rewrite
of signed bytes destroys the signature entirely.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

import pymupdf
import pytest
from asn1crypto import x509 as asn1_x509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.fields import SigFieldSpec, append_signature_field
from pyhanko.sign.general import SigningError
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko_certvalidator import ValidationContext

from pdfcore import signing
from pdfcore.document import PdfDocument


@pytest.fixture(scope="session")
def signer(signer_p12):
    """The generated self-signed cert loaded through the one loading path."""
    return signing.load_pkcs12_signer(signer_p12.path, signer_p12.password)


def _statuses(data: bytes, signer):
    """Validate every embedded signature against the signer's own cert."""
    vc = ValidationContext(trust_roots=[signer.signing_cert], allow_fetching=False)
    reader = PdfFileReader(io.BytesIO(data))
    return [
        validate_pdf_signature(sig, signer_validation_context=vc)
        for sig in reader.embedded_signatures
    ]


def _validly_signed(data: bytes, signer) -> bool:
    """True only when at least one signature exists and ALL are intact+valid."""
    try:
        statuses = _statuses(data, signer)
    except Exception:
        return False  # unparseable / destroyed signature structures count as invalid
    return bool(statuses) and all(st.intact and st.valid for st in statuses)


def _signature_widgets(data: bytes, page_index: int):
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return [
            (w.field_name, tuple(w.rect))
            for w in doc[page_index].widgets()
            if w.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE
        ]


# --- certificate helpers -----------------------------------------------------


def test_generate_and_load_pkcs12(signer_p12, signer):
    assert signer_p12.path.is_file()
    assert signer.subject_name == signer_p12.common_name


def test_load_pkcs12_wrong_password(signer_p12):
    with pytest.raises(signing.CertificateLoadError, match="wrong password"):
        signing.load_pkcs12_signer(signer_p12.path, "not-the-password")


def test_load_pkcs12_missing_file(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        signing.load_pkcs12_signer(tmp_path / "nowhere.p12", "pw")


@pytest.mark.parametrize(
    ("common_name", "password", "valid_days"),
    [("", "pw", 30), ("   ", "pw", 30), ("Someone", "", 30), ("Someone", "pw", 0)],
)
def test_generate_p12_rejects_bad_args(tmp_path, common_name, password, valid_days):
    with pytest.raises(ValueError):
        signing.generate_self_signed_p12(
            tmp_path / "bad.p12", common_name, password, valid_days=valid_days
        )


# --- signing + validation ----------------------------------------------------


def test_sign_invisible_and_validate(text_pdf, signer, signer_p12):
    src = text_pdf.read_bytes()
    result = signing.sign_pdf_bytes(src, signer)

    statuses = _statuses(result.pdf_bytes, signer)
    assert len(statuses) == 1
    assert statuses[0].intact and statuses[0].valid and statuses[0].trusted
    assert result.field_name == "Signature1"
    assert result.signer_name == signer_p12.common_name
    assert result.self_signed is True
    # TERMINAL shape: pyHanko appends an incremental update — the original
    # bytes survive verbatim at the front (PyMuPDF never touched them).
    assert result.pdf_bytes[: len(src)] == src


def test_sign_visible_image_widget_geometry(text_pdf, signer, sample_png):
    rect = (72.0, 72.0, 272.0, 172.0)
    result = signing.sign_pdf_bytes(
        text_pdf.read_bytes(), signer, page_index=1, rect=rect, image_path=sample_png
    )

    assert _validly_signed(result.pdf_bytes, signer)
    widgets = _signature_widgets(result.pdf_bytes, 1)
    assert len(widgets) == 1
    name, got = widgets[0]
    assert name == "Signature1"
    # Pins the y-flip convention: the widget lands where the pdfcore-space
    # (top-left origin, y down) rect asked, within a point.
    assert got == pytest.approx(rect, abs=1.0)
    assert _signature_widgets(result.pdf_bytes, 0) == []


def test_sign_visible_default_text_appearance(text_pdf, signer):
    rect = (100.0, 500.0, 300.0, 560.0)
    result = signing.sign_pdf_bytes(text_pdf.read_bytes(), signer, rect=rect)

    assert _validly_signed(result.pdf_bytes, signer)
    assert len(_signature_widgets(result.pdf_bytes, 0)) == 1


def _dark_fraction(data: bytes, page_index: int, rect) -> float:
    """Fraction of rendered pixels inside ``rect`` that are dark (< 100)."""
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        clip = pymupdf.Rect(rect)
        pix = doc[page_index].get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=clip)
        dark = sum(1 for v in pix.samples if v < 100)
        return dark / len(pix.samples)


def test_visible_appearance_actually_renders(text_pdf, signer, sample_png):
    """The stamp draws real pixels — a blank /AP would pass the widget checks.

    Guards the range-pinned pyHanko against a patch that changes stamp
    rendering, and the engine against losing stamp_style.
    """
    rect = (100.0, 500.0, 300.0, 560.0)
    src = text_pdf.read_bytes()
    # Image skin: the dark-grey sample must actually paint ink in the rect
    # (pyHanko letterboxes the image inside the box, so the fraction is well
    # under 1.0 — a blank /AP would be ~0.0, which is what this guards).
    with_image = signing.sign_pdf_bytes(src, signer, rect=rect, image_path=sample_png)
    assert _dark_fraction(with_image.pdf_bytes, 0, rect) > 0.05
    # Default text stamp: not blank either (text + border draw ink).
    default_stamp = signing.sign_pdf_bytes(src, signer, rect=rect)
    assert _dark_fraction(default_stamp.pdf_bytes, 0, rect) > 0.01
    # Control: the same area unsigned is blank page background.
    assert _dark_fraction(src, 0, rect) < 0.005


def test_visible_image_transparency_preserved(text_pdf, signer, tmp_path):
    """An RGBA signature PNG keeps its transparency in the stamp (SMask path).

    The decorative-image pipeline once turned transparent PNGs solid black
    (the signature-goes-black bug) — pin that pyHanko's PdfImage doesn't.
    """
    w, h = 80, 40
    buf = bytearray(w * h * 4)  # all zero == transparent black
    for x in range(w):
        for dy in range(-2, 3):
            y = (x % h) + dy
            if 0 <= y < h:
                i = (y * w + x) * 4
                buf[i : i + 4] = bytes((0, 0, 0, 255))  # opaque black stroke
    sig_png = tmp_path / "sig-rgba.png"
    pymupdf.Pixmap(pymupdf.csRGB, w, h, bytes(buf), 1).save(str(sig_png))

    rect = (100.0, 600.0, 260.0, 680.0)
    result = signing.sign_pdf_bytes(text_pdf.read_bytes(), signer, rect=rect, image_path=sig_png)
    frac = _dark_fraction(result.pdf_bytes, 0, rect)
    # The stroke draws (some ink), the transparent body shows the white page
    # (nowhere near the ~0.7+ an opaque-black failure would produce).
    assert 0.01 < frac < 0.5


def test_sign_visible_rect_is_cropbox_relative(signer):
    """pdfcore space is CROPBOX-relative — a mediabox flip misplaces the widget.

    Adversarial-review finding: on a page whose CropBox differs from its
    MediaBox (common in print production), the widget must land where the
    rect asked in VISIBLE-page coordinates, and bounds must be the visible
    page, not the mediabox.
    """
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    doc.xref_set_key(page.xref, "CropBox", "[50 40 562 742]")  # visible 512x702
    src = doc.tobytes()
    doc.close()

    rect = (10.0, 10.0, 110.0, 60.0)
    result = signing.sign_pdf_bytes(src, signer, rect=rect)
    widgets = _signature_widgets(result.pdf_bytes, 0)
    assert len(widgets) == 1
    assert widgets[0][1] == pytest.approx(rect, abs=1.0)

    # Inside the mediabox but off the 512x702 VISIBLE page -> refused.
    with pytest.raises(ValueError, match="outside the page"):
        signing.sign_pdf_bytes(src, signer, rect=(520.0, 710.0, 600.0, 780.0))


def test_sign_reason_and_location_land_in_pdf(text_pdf, signer):
    result = signing.sign_pdf_bytes(
        text_pdf.read_bytes(), signer, reason="Approved for release", location="Auckland"
    )
    reader = PdfFileReader(io.BytesIO(result.pdf_bytes))
    sig = reader.embedded_signatures[0]
    assert sig.sig_object["/Reason"] == "Approved for release"
    assert sig.sig_object["/Location"] == "Auckland"


# --- tamper evidence ---------------------------------------------------------


def test_tamper_byteflip_breaks_intact(text_pdf, signer):
    result = signing.sign_pdf_bytes(text_pdf.read_bytes(), signer)
    signed = result.pdf_bytes

    # Flip one byte INSIDE the signed range (within the first content stream —
    # the file stays parseable, isolating the digest check from parse errors).
    idx = signed.find(b"stream") + 16
    tampered = bytearray(signed)
    tampered[idx] ^= 0xFF
    statuses = _statuses(bytes(tampered), signer)
    assert len(statuses) == 1
    assert not statuses[0].intact
    assert statuses[0].summary() == "INVALID"


def test_pymupdf_rewrite_invalidates(text_pdf, signer):
    """The terminal-operation constraint, executable: NEVER re-save signed bytes.

    A PyMuPDF rewrite (save/tobytes) re-serialises the whole file, so the
    signature — if it survives at all — no longer matches its byte ranges.
    """
    result = signing.sign_pdf_bytes(text_pdf.read_bytes(), signer)
    assert _validly_signed(result.pdf_bytes, signer)

    with pymupdf.open(stream=result.pdf_bytes, filetype="pdf") as doc:
        rewritten = doc.tobytes(garbage=4, deflate=True)
    assert not _validly_signed(rewritten, signer)


def test_second_signature_preserves_first(text_pdf, signer):
    first = signing.sign_pdf_bytes(text_pdf.read_bytes(), signer)
    second = signing.sign_pdf_bytes(first.pdf_bytes, signer, field_name="Signature2")

    statuses = _statuses(second.pdf_bytes, signer)
    assert len(statuses) == 2
    # Signing only ever APPENDS, so the first signature stays intact under the
    # second one's incremental update.
    assert all(st.intact and st.valid for st in statuses)


def test_signature_field_names_and_next_name(text_pdf, signer, tmp_path):
    """The UI's auto-naming: enumerate signature fields, pick the next free."""
    assert signing.next_field_name([]) == "Signature1"
    assert signing.next_field_name(["Signature1", "Signature2"]) == "Signature3"
    assert signing.next_field_name(["Signature2"]) == "Signature1"

    first = signing.sign_pdf_bytes(text_pdf.read_bytes(), signer)
    signed_path = tmp_path / "signed.pdf"
    signed_path.write_bytes(first.pdf_bytes)
    with PdfDocument.open(signed_path) as doc:
        names = doc.signature_field_names()
        assert names == ["Signature1"]
        assert signing.next_field_name(names) == "Signature2"
    with PdfDocument.open(text_pdf) as doc:
        assert doc.signature_field_names() == []


def _with_placeholder_field(pdf_bytes: bytes, name: str = "Placeholder") -> bytes:
    """The input bytes plus one EMPTY signature form field (a template)."""
    writer = IncrementalPdfFileWriter(io.BytesIO(pdf_bytes))
    append_signature_field(writer, SigFieldSpec(name, box=(100, 100, 300, 160)))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_empty_signature_field_is_not_a_signature(text_pdf, signer, tmp_path):
    """A placeholder FIELD (unsigned contract template) is not a signature —
    conflating them refused signing on plain templates (review finding)."""
    templated = tmp_path / "template.pdf"
    templated.write_bytes(_with_placeholder_field(text_pdf.read_bytes()))
    with PdfDocument.open(templated) as doc:
        assert doc.signature_field_names() == ["Placeholder"]  # naming input
        assert doc.has_signatures() is False  # but NOT signed

    signed = signing.sign_pdf_bytes(templated.read_bytes(), signer)
    signed_path = tmp_path / "template-signed.pdf"
    signed_path.write_bytes(signed.pdf_bytes)
    with PdfDocument.open(signed_path) as doc:
        assert doc.has_signatures() is True
        assert sorted(doc.signature_field_names()) == ["Placeholder", "Signature1"]


def test_signatures_cover_file_detects_rewrite(text_pdf, signer):
    """The layout check that catches a signed-then-resaved (laundered) file."""
    src = text_pdf.read_bytes()
    assert signing.signatures_cover_file(src) is True  # nothing signed yet
    first = signing.sign_pdf_bytes(src, signer)
    assert signing.signatures_cover_file(first.pdf_bytes) is True
    second = signing.sign_pdf_bytes(first.pdf_bytes, signer, field_name="Signature2")
    assert signing.signatures_cover_file(second.pdf_bytes) is True  # multi-sig legit

    with pymupdf.open(stream=first.pdf_bytes, filetype="pdf") as doc:
        laundered = doc.tobytes(garbage=4, deflate=True)
    assert signing.signatures_cover_file(laundered) is False


def test_pyhanko_unparseable_input_raises_valueerror(text_pdf, signer):
    """Bytes MuPDF repairs silently but pyHanko's strict parser refuses must
    surface as the contractual ValueError, not a raw pyHanko PdfError."""
    mangled = text_pdf.read_bytes().replace(b"startxref", b"startxrEf")
    with pymupdf.open(stream=mangled, filetype="pdf") as probe:
        assert probe.page_count > 0  # precondition: the engine's probe passes
    with pytest.raises(ValueError, match="could not process"):
        signing.sign_pdf_bytes(mangled, signer)


def test_verify_pdf_signatures_intact_tampered_and_trust(text_pdf, signer, signer_p12):
    """The engine's READ side: what the app's status surface reports."""
    src = text_pdf.read_bytes()
    assert signing.verify_pdf_signatures(src) == []  # unsigned

    signed = signing.sign_pdf_bytes(src, signer).pdf_bytes
    checks = signing.verify_pdf_signatures(signed)
    assert len(checks) == 1
    check = checks[0]
    assert check.intact and check.valid and check.problem is None
    assert check.signer_name == signer_p12.common_name
    assert check.trusted is False  # no trust roots supplied
    with_root = signing.verify_pdf_signatures(signed, trust_roots=[signer.signing_cert])
    assert with_root[0].trusted is True

    tampered = bytearray(signed)
    tampered[signed.find(b"stream") + 16] ^= 0xFF
    broken = signing.verify_pdf_signatures(bytes(tampered))
    assert len(broken) == 1
    assert not broken[0].intact
    assert "modified" in broken[0].problem


def test_incremental_update_tamper_detected(text_pdf, signer):
    """The classic attack: APPEND a revision that swaps page content — the
    signed revision's digest stays intact, so a digest-only verdict calls the
    file clean (review finding). docmdp/coverage analysis must flag it, and
    must NOT flag the app's own legit second signature."""
    from pyhanko.pdf_utils.generic import StreamObject

    signed = signing.sign_pdf_bytes(text_pdf.read_bytes(), signer).pdf_bytes
    writer = IncrementalPdfFileWriter(io.BytesIO(signed))
    page_obj = writer.root["/Pages"]["/Kids"][0].get_object()
    tampered_stream = StreamObject(stream_data=b"BT /F0 24 Tf 72 720 Td (TAMPERED) Tj ET")
    page_obj["/Contents"] = writer.add_object(tampered_stream)
    writer.update_container(page_obj)
    out = io.BytesIO()
    writer.write(out)

    checks = signing.verify_pdf_signatures(out.getvalue())
    assert len(checks) == 1
    assert not checks[0].intact
    assert checks[0].tampered is True
    assert "modified" in checks[0].problem

    # A LEGIT second signature is also an appended revision — it must stay clean.
    double = signing.sign_pdf_bytes(signed, signer, field_name="Signature2").pdf_bytes
    double_checks = signing.verify_pdf_signatures(double)
    assert len(double_checks) == 2
    assert all(c.intact and c.valid and not c.tampered for c in double_checks)


def test_verify_contract_never_leaks_raw_exceptions(text_pdf, signer, monkeypatch):
    """Unreadable input raises ValueError (whatever pyHanko aired), and a
    signature whose validation machinery crashes REPORTS as broken-but-not-
    tampered rather than raising (review findings: an AttributeError once
    escaped through a Qt slot)."""
    signed = signing.sign_pdf_bytes(text_pdf.read_bytes(), signer).pdf_bytes

    # A mangled field /V: pyHanko's reader constructs, iteration explodes.
    with pymupdf.open(stream=signed, filetype="pdf") as doc:
        widget = next(doc[0].widgets())
        doc.xref_set_key(widget.xref, "V", "(not-a-dict)")
        mangled = doc.tobytes()
    with pytest.raises(ValueError):
        signing.verify_pdf_signatures(mangled)

    # Validation machinery raising mid-check: reported, never raised.
    def boom(*args, **kwargs):
        raise RuntimeError("exotic algorithm")

    monkeypatch.setattr(signing, "validate_pdf_signature", boom)
    checks = signing.verify_pdf_signatures(signed)
    assert len(checks) == 1
    assert not checks[0].intact and not checks[0].valid
    assert checks[0].tampered is False  # unverifiable, NOT determined-modified
    assert "could not be checked" in checks[0].problem


def test_verify_encrypted_signed_needs_password(text_pdf, signer):
    """Encrypted+signed (encrypt-then-sign, the standard order): without the
    password verification fails as ValueError; with it, checks come back."""
    from pyhanko.pdf_utils.writer import copy_into_new_writer

    writer = copy_into_new_writer(PdfFileReader(io.BytesIO(text_pdf.read_bytes())))
    writer.encrypt("open-pw")
    buf = io.BytesIO()
    writer.write(buf)
    inc = IncrementalPdfFileWriter(io.BytesIO(buf.getvalue()))
    inc.encrypt("open-pw")
    from pyhanko.sign.signers import PdfSignatureMetadata, PdfSigner

    out = PdfSigner(PdfSignatureMetadata(field_name="Signature1"), signer=signer).sign_pdf(inc)
    enc_signed = out.getvalue()

    with pytest.raises(ValueError):
        signing.verify_pdf_signatures(enc_signed)  # no password
    checks = signing.verify_pdf_signatures(enc_signed, password="open-pw")
    assert len(checks) == 1
    assert checks[0].intact and checks[0].valid


def test_verify_real_signed_samples(real_signed_pdf, real_tampered_signed_pdf):
    """The user's hand-made samples: signed verifies intact, tampered flags."""
    good = signing.verify_pdf_signatures(real_signed_pdf.read_bytes())
    assert good and all(c.intact and c.valid for c in good)
    bad = signing.verify_pdf_signatures(real_tampered_signed_pdf.read_bytes())
    assert bad and any(not c.intact for c in bad)


def test_resign_same_field_name_raises_signing_error(text_pdf, signer):
    """A filled signature field can't be signed again — pyHanko's SigningError
    propagates as-is (documented contract the UI's error path will rely on)."""
    first = signing.sign_pdf_bytes(text_pdf.read_bytes(), signer)
    with pytest.raises(SigningError):
        signing.sign_pdf_bytes(first.pdf_bytes, signer, field_name=first.field_name)


# --- input validation --------------------------------------------------------


def test_sign_rejects_bad_inputs(text_pdf, encrypted_pdf, signer, sample_png, tmp_path):
    src = text_pdf.read_bytes()
    with pytest.raises(ValueError, match="out of range"):
        signing.sign_pdf_bytes(src, signer, page_index=99)
    with pytest.raises(ValueError, match="outside the page"):
        signing.sign_pdf_bytes(src, signer, rect=(0.0, 0.0, 10_000.0, 100.0))
    with pytest.raises(ValueError, match="empty"):
        signing.sign_pdf_bytes(src, signer, rect=(100.0, 200.0, 100.0, 300.0))
    with pytest.raises(ValueError, match="placement rectangle"):
        signing.sign_pdf_bytes(src, signer, image_path=sample_png)
    with pytest.raises(ValueError, match="image file not found"):
        signing.sign_pdf_bytes(
            src, signer, rect=(72.0, 72.0, 172.0, 122.0), image_path=tmp_path / "gone.png"
        )
    with pytest.raises(ValueError, match="field name"):
        signing.sign_pdf_bytes(src, signer, field_name="   ")
    with pytest.raises(ValueError, match="not a valid PDF"):
        signing.sign_pdf_bytes(b"this is not a pdf", signer)
    with pytest.raises(ValueError, match="encrypted"):
        signing.sign_pdf_bytes(encrypted_pdf.path.read_bytes(), signer)


def test_owner_password_only_input_refused(tmp_path, signer):
    """Permissions-locked PDFs (owner password, EMPTY user password) must be
    refused too: pymupdf auto-authenticates them so needs_pass is False, but
    the file is still encrypted and pyHanko would fail mid-signing
    (adversarial-review finding — the trailer /Encrypt check catches it)."""
    doc = pymupdf.open()
    doc.new_page()
    path = tmp_path / "owner-locked.pdf"
    doc.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="own-only", user_pw="")
    doc.close()
    with pytest.raises(ValueError, match="encrypted"):
        signing.sign_pdf_bytes(path.read_bytes(), signer)


def test_ca_issued_cert_reports_not_self_signed(text_pdf, tmp_path):
    """self_signed must be False for a CA-issued leaf — the UI's untrusted
    warning must never fire for properly issued certificates."""
    now = datetime.now(UTC)

    def _cert(subject, issuer, subject_key, signing_key, *, ca):
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(subject_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(hours=1))
            .not_valid_after(now + timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=not ca,
                    content_commitment=not ca,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=ca,
                    crl_sign=ca,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
        )
        return builder.sign(signing_key, hashes.SHA256())

    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Root CA")])
    root_cert = _cert(root_name, root_name, root_key, root_key, ca=True)
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Leaf Person")])
    leaf_cert = _cert(leaf_name, root_name, leaf_key, root_key, ca=False)

    p12_path = tmp_path / "ca-issued.p12"
    p12_path.write_bytes(
        pkcs12.serialize_key_and_certificates(
            name=b"leaf",
            key=leaf_key,
            cert=leaf_cert,
            cas=[root_cert],
            encryption_algorithm=serialization.BestAvailableEncryption(b"pw"),
        )
    )
    ca_signer = signing.load_pkcs12_signer(p12_path, "pw")
    result = signing.sign_pdf_bytes(text_pdf.read_bytes(), ca_signer)

    assert result.self_signed is False
    assert result.signer_name == "Leaf Person"
    # And it chains to the ROOT as trust anchor.
    root_asn1 = asn1_x509.Certificate.load(root_cert.public_bytes(serialization.Encoding.DER))
    vc = ValidationContext(trust_roots=[root_asn1], allow_fetching=False)
    reader = PdfFileReader(io.BytesIO(result.pdf_bytes))
    status = validate_pdf_signature(reader.embedded_signatures[0], signer_validation_context=vc)
    assert status.intact and status.valid and status.trusted


# --- PdfDocument facade ------------------------------------------------------


def test_save_signed_facade(text_pdf, signer, tmp_path):
    out = tmp_path / "signed.pdf"
    with PdfDocument.open(text_pdf) as doc:
        result = doc.save_signed(out, signer)
        assert result.self_signed is True
        assert _validly_signed(out.read_bytes(), signer)

        # Refuses the currently-open file, like save().
        with pytest.raises(ValueError, match="currently-open"):
            doc.save_signed(text_pdf, signer)

        # The OPEN document stays UNSIGNED — a later plain save is unsigned.
        unsigned = tmp_path / "unsigned.pdf"
        doc.save(unsigned)
    reader = PdfFileReader(io.BytesIO(unsigned.read_bytes()))
    assert len(reader.embedded_signatures) == 0


def test_initials_stamped_then_signed_validates(text_pdf, signer, sample_png, tmp_path):
    """The signature-library flow: initials on every page, ONE crypto signature.

    Initials are decorative image placements applied BEFORE flatten+sign, so
    the single signature covers them — never N signatures per page.
    """
    out = tmp_path / "initialled.pdf"
    with PdfDocument.open(text_pdf) as doc:
        for n in range(doc.page_count):
            doc.insert_image(n, (500.0, 24.0, 560.0, 69.0), sample_png)
        doc.save_signed(
            out,
            signer,
            page_index=2,
            rect=(300.0, 700.0, 500.0, 780.0),
            image_path=sample_png,
        )

    data = out.read_bytes()
    assert _validly_signed(data, signer)
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for n in range(doc.page_count):
            assert doc[n].get_image_info(), f"page {n} lost its initials stamp"
    assert len(_signature_widgets(data, 2)) == 1
