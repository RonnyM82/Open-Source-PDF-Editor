"""Cryptographic PDF signing (pyHanko): terminal signatures over finalised bytes.

Signing is a TERMINAL operation: all PyMuPDF edits happen first, the document is
flattened to final bytes, and pyHanko appends the signature as an incremental
update (the original bytes are left intact and the update is appended after
them). Signing never routes through PyMuPDF's save path, and ANY PyMuPDF
rewrite of signed bytes (``save``/``tobytes``) re-serialises the whole file and
INVALIDATES the signature — hand bytes between the two libraries, never a
shared document object. ``rect`` is unrotated page points, top-left origin,
y down (the usual pdfcore space); the flip to PDF user space (y up) happens
here. See CLAUDE.md "Digital signing".

The signer is a swappable boundary: :func:`sign_pdf_bytes` accepts any pyHanko
``Signer``, so a PKCS#11/smartcard signer can slot in later without touching
the signing path. Validation is deliberately NOT imported here — the engine
only signs; tests (and any future status UI) use pyHanko's validation API
directly.
"""

from __future__ import annotations

import io
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pymupdf
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from pyhanko.pdf_utils.images import PdfImage
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.misc import PdfError
from pyhanko.sign.fields import SigFieldSpec

# Re-exported so the UI can catch a signer-level failure without importing
# pyhanko itself (pdfapp talks to pdfcore only).
from pyhanko.sign.general import SigningError  # noqa: F401
from pyhanko.sign.signers import PdfSignatureMetadata, PdfSigner, Signer, SimpleSigner
from pyhanko.stamp import StaticStampStyle

DEFAULT_FIELD_NAME = "Signature1"


class CertificateLoadError(RuntimeError):
    """The PKCS#12 file could not be read (wrong password or not a certificate)."""


@dataclass(frozen=True)
class SignResult:
    """Outcome of a signing run: the signed bytes plus honest signer facts.

    ``self_signed`` is True when the certificate's issuer equals its subject —
    readers will show such a signature as unknown/untrusted until the recipient
    explicitly trusts the certificate. The UI must surface that plainly and
    never imply a self-signed signature is trusted.
    """

    pdf_bytes: bytes
    field_name: str
    signer_name: str | None
    self_signed: bool


def load_pkcs12_signer(path: str | Path, password: str | None) -> SimpleSigner:
    """Load a user PKCS#12 (.p12/.pfx) bundle into a pyHanko ``SimpleSigner``.

    Raises ValueError when the file is missing and
    :class:`CertificateLoadError` when it cannot be decrypted/parsed —
    ``SimpleSigner.load_pkcs12`` returns None on ANY failure (wrong password,
    corrupt data) rather than raising, so the distinction is made here for the
    UI to re-prompt on a bad password.
    """
    p12 = Path(path)
    if not p12.is_file():
        raise ValueError(f"certificate file not found: {p12}")
    passphrase = password.encode("utf-8") if password else None
    signer = SimpleSigner.load_pkcs12(pfx_file=str(p12), passphrase=passphrase)
    if signer is None:
        raise CertificateLoadError(
            f"could not load {p12.name}: wrong password, or not a PKCS#12 certificate bundle"
        )
    return signer


def generate_self_signed_p12(
    out_path: str | Path,
    common_name: str,
    password: str,
    *,
    valid_days: int = 1095,
    key_size: int = 2048,
) -> Path:
    """Generate an RSA self-signed cert + key, write a password-protected .p12.

    For users who want tamper-evidence without a CA-issued certificate. The
    result loads through :func:`load_pkcs12_signer` (the one loading path) and
    signs normally, but readers show it as unknown/untrusted until the
    recipient trusts the certificate — :attr:`SignResult.self_signed` reports
    it honestly. ``content_commitment`` (nonRepudiation) is set because
    pyHanko's default validation requires that key usage on signing certs.
    """
    name = common_name.strip()
    if not name:
        raise ValueError("common name must not be empty")
    if not password:
        raise ValueError("password must not be empty")
    if valid_days < 1:
        raise ValueError("validity must be at least one day")
    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=valid_days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=True,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    data = pkcs12.serialize_key_and_certificates(
        name=name.encode("utf-8"),
        key=key,
        cert=cert,
        cas=None,
        encryption_algorithm=serialization.BestAvailableEncryption(password.encode("utf-8")),
    )
    dest = Path(out_path)
    dest.write_bytes(data)
    return dest


def signature_field_names(doc: pymupdf.Document) -> list[str]:
    """Names of all signature form fields in the document (filled or empty).

    Auto-naming input ONLY (:func:`next_field_name`) — empty placeholder
    fields (unsigned contract templates) are included, so a fresh field never
    collides with one. For "is this document actually signed" use
    :func:`has_signatures`, never this list's truthiness.
    """
    names: list[str] = []
    for page in doc:
        for widget in page.widgets():
            if widget.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE and widget.field_name:
                names.append(widget.field_name)
    return names


def has_signatures(doc: pymupdf.Document) -> bool:
    """True when any signature field actually HOLDS a signature.

    A mere signature FIELD is not a signature: unsigned forms commonly ship
    empty placeholders (``widget.is_signed`` False, no ``/V``) — treating
    those as "already signed" refused signing on plain templates
    (adversarial-review finding).
    """
    for page in doc:
        for widget in page.widgets():
            if widget.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE and widget.is_signed:
                return True
    return False


def signatures_cover_file(pdf_bytes: bytes) -> bool:
    """Layout-level plausibility of the file's signatures — NOT cryptographic
    validation.

    A signature's ``/ByteRange`` must reach the end of the exact bytes it
    signed; after ANY rewrite (e.g. PyMuPDF save) the offsets point into a
    layout that no longer exists, so the most-covering signature no longer
    ends at EOF. That catches the laundering case — a signed file edited and
    re-saved has a clean undo stack but already-broken signatures, and
    appending to it would produce output readers flag as invalid
    (adversarial-review finding). Earlier signatures of a legit multi-sign
    file cover only their own revision, hence the MAX. Unreadable structure
    counts as not covering (conservative). True when no signed field exists.
    """
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except (RuntimeError, ValueError):
        return False
    try:
        ends: list[int] = []
        for page in doc:
            for widget in page.widgets():
                if widget.field_type != pymupdf.PDF_WIDGET_TYPE_SIGNATURE or not widget.is_signed:
                    continue
                vtype, vval = doc.xref_get_key(widget.xref, "V")
                if vtype != "xref":
                    return False
                rtype, rval = doc.xref_get_key(int(vval.split()[0]), "ByteRange")
                if rtype != "array":
                    return False
                try:
                    parts = [int(tok) for tok in rval.strip("[]").split()]
                except ValueError:
                    return False
                if len(parts) < 4:
                    return False
                ends.append(parts[-2] + parts[-1])
        return not ends or max(ends) == len(pdf_bytes)
    finally:
        doc.close()


def next_field_name(existing: Iterable[str]) -> str:
    """The first ``SignatureN`` name not already present in ``existing``."""
    taken = set(existing)
    n = 1
    while f"Signature{n}" in taken:
        n += 1
    return f"Signature{n}"


def sign_pdf_bytes(
    pdf_bytes: bytes,
    signer: Signer,
    *,
    field_name: str = DEFAULT_FIELD_NAME,
    reason: str | None = None,
    location: str | None = None,
    page_index: int = 0,
    rect: tuple[float, float, float, float] | None = None,
    image_path: str | Path | None = None,
) -> SignResult:
    """Sign finalised PDF bytes; returns the signed bytes (input untouched).

    ``rect`` (unrotated page points, top-left origin) places a VISIBLE
    signature widget on ``page_index``; None signs invisibly. ``image_path``
    (requires ``rect``) renders the image as the widget's whole appearance —
    the visual skin over the real cryptographic signature; without it a
    visible signature gets pyHanko's default text stamp. ``signer`` is any
    pyHanko ``Signer`` (PKCS#11 slots in here later).

    Encrypted input is refused: callers hand over decrypted bytes (the
    ``save_signed`` facade always does). When write-encryption lands as a
    feature, encrypt FIRST and relax this with a password parameter —
    encrypting after signing rewrites the file and kills the signature.
    """
    if not field_name.strip():
        raise ValueError("signature field name must not be empty")
    if image_path is not None and rect is None:
        raise ValueError("a signature image needs a placement rectangle")
    if image_path is not None and not Path(image_path).is_file():
        raise ValueError(f"image file not found: {Path(image_path)}")

    box: tuple[float, float, float, float] | None = None
    try:
        probe = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except (RuntimeError, ValueError) as exc:
        raise ValueError("the data to sign is not a valid PDF") from exc
    try:
        # needs_pass alone misses owner-password-only (permissions-locked)
        # files — pymupdf auto-authenticates those with the empty user
        # password, but the trailer still carries /Encrypt and pyHanko would
        # fail mid-signing with a cryptic PdfReadError. Check the trailer key
        # too so EVERY encrypted flavour is refused up front.
        if probe.needs_pass or probe.xref_get_key(-1, "Encrypt")[0] != "null":
            raise ValueError("cannot sign an encrypted PDF; supply decrypted bytes")
        if not 0 <= page_index < probe.page_count:
            raise ValueError(f"page index {page_index} out of range")
        if rect is not None:
            page = probe[page_index]
            cb = page.cropbox
            x0, y0, x1, y1 = rect
            if not (x0 < x1 and y0 < y1):
                raise ValueError("signature rectangle is empty")
            if x0 < 0 or y0 < 0 or x1 > cb.width or y1 > cb.height:
                raise ValueError("signature rectangle is outside the page")
            # pdfcore speaks top-left-origin y-down page points anchored at the
            # CROPBOX corner (the visible page — the same space text extraction
            # uses), NOT the mediabox; PDF user space (what SigFieldSpec.box
            # takes) is lower-left-origin y-up. page.cropbox is rotation-stable
            # and carries the RAW PDF x but a y flipped about the MEDIABOX top,
            # so the cropbox's top edge in PDF space is mb.y1 - cb.y0
            # (probe-verified incl. non-zero mediabox origins and rotated
            # pages; a mediabox-based flip misplaced the widget on cropped
            # pages — adversarial-review finding).
            top = page.mediabox.y1 - cb.y0
            box = (cb.x0 + x0, top - y1, cb.x0 + x1, top - y0)
    finally:
        probe.close()

    meta = PdfSignatureMetadata(field_name=field_name, reason=reason, location=location)
    spec = None
    style = None
    if rect is not None:
        # PdfSigner refuses a new_field_spec whose name differs from the
        # metadata field_name — both derive from the one argument here.
        spec = SigFieldSpec(sig_field_name=field_name, on_page=page_index, box=box)
        if image_path is not None:
            style = StaticStampStyle(background=PdfImage(str(image_path)), border_width=0)

    try:
        writer = IncrementalPdfFileWriter(io.BytesIO(pdf_bytes))
        out = PdfSigner(meta, signer=signer, stamp_style=style, new_field_spec=spec).sign_pdf(
            writer
        )
    except SigningError:
        raise  # signer-level failure — propagated as-is for the UI to name
    except PdfError as exc:
        # pyHanko's strict parser rejects layout quirks MuPDF silently
        # repairs, so the pymupdf probe above can pass on bytes pyHanko
        # refuses — surface that as the input error it is (PdfError is NOT a
        # ValueError; unwrapped it escaped the UI's except clause).
        raise ValueError(f"pyHanko could not process this PDF: {exc}") from exc
    signed = out.getvalue()

    cert = getattr(signer, "signing_cert", None)
    return SignResult(
        pdf_bytes=signed,
        field_name=field_name,
        signer_name=signer.subject_name if cert is not None else None,
        self_signed=bool(cert.self_issued) if cert is not None else False,
    )
