"""Password protection: permission presets, ProtectionSpec, Permissions readout.

Pure PyMuPDF constants + plain data — no Qt, no document handles. The model is
the PDF STANDARD's two passwords (ISO 32000): a USER (open) password — real
AES-256, cryptographic — and an OWNER (permissions) password whose permission
flags bind COMPLIANT readers only (the honor system; never word it as a
security boundary). The preset surface mirrors Acrobat's "Password Security"
dialog exactly, except printing is a plain yes/no (the 150 dpi option was
deliberately dropped — user decision 2026-07-25; when allowed, BOTH print bits
are granted). Accessibility extraction is always granted (PDF 2.0 behaviour).
See CLAUDE.md "Password protection".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

import pymupdf

_ALWAYS_GRANTED: Final = pymupdf.PDF_PERM_ACCESSIBILITY
_PRINT_BITS: Final = pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_PRINT_HQ


class ChangesAllowed(Enum):
    """Acrobat's "Changes Allowed" dropdown, verbatim (there is deliberately no
    commenting-without-form-fill option — the PDF permission bits can't express
    it, so Acrobat doesn't offer it and neither do we)."""

    NONE = "none"
    PAGES = "pages"  # inserting, deleting and rotating pages
    FORM_FILL = "form_fill"  # filling in form fields and signing existing fields
    COMMENT_FORM_FILL = "comment_form_fill"  # commenting + the above
    ANY_EXCEPT_EXTRACT = "any_except_extract"


_CHANGES_BITS: Final = {
    ChangesAllowed.NONE: 0,
    ChangesAllowed.PAGES: pymupdf.PDF_PERM_ASSEMBLE,
    ChangesAllowed.FORM_FILL: pymupdf.PDF_PERM_FORM,
    ChangesAllowed.COMMENT_FORM_FILL: pymupdf.PDF_PERM_ANNOTATE | pymupdf.PDF_PERM_FORM,
    ChangesAllowed.ANY_EXCEPT_EXTRACT: (
        pymupdf.PDF_PERM_MODIFY
        | pymupdf.PDF_PERM_ANNOTATE
        | pymupdf.PDF_PERM_FORM
        | pymupdf.PDF_PERM_ASSEMBLE
    ),
}


def permissions_mask(*, changes: ChangesAllowed, allow_print: bool, allow_copy: bool) -> int:
    """The PDF permission int for a preset (accessibility always granted)."""
    mask = _ALWAYS_GRANTED | _CHANGES_BITS[changes]
    if allow_print:
        mask |= _PRINT_BITS
    if allow_copy:
        mask |= pymupdf.PDF_PERM_COPY
    return mask


# Everything a preset could grant — used to recognise "restricted" specs.
_ALL_GRANTABLE: Final = permissions_mask(
    changes=ChangesAllowed.ANY_EXCEPT_EXTRACT, allow_print=True, allow_copy=True
)


@dataclass(frozen=True)
class ProtectionSpec:
    """Encryption to apply at the next save — always AES-256.

    ``user_pw`` is the OPEN password (None = anyone can open); ``owner_pw`` is
    the PERMISSIONS password (None with a user_pw = MuPDF makes the one
    password authenticate at both levels). ``permissions`` is a
    :func:`permissions_mask` value.
    """

    user_pw: str | None
    owner_pw: str | None
    permissions: int

    def __post_init__(self) -> None:
        if not self.user_pw and not self.owner_pw:
            raise ValueError("protection needs at least one password")
        if (self.permissions & _ALL_GRANTABLE) != _ALL_GRANTABLE and not self.owner_pw:
            raise ValueError(
                "restricted permissions need a permissions (owner) password — "
                "without one there is nothing gating the restrictions"
            )


def encryption_kwargs(spec: ProtectionSpec) -> dict:
    """The ``doc.save(**kwargs)`` payload applying ``spec``."""
    return {
        "encryption": pymupdf.PDF_ENCRYPT_AES_256,
        "user_pw": spec.user_pw or "",
        "owner_pw": spec.owner_pw or "",
        "permissions": spec.permissions,
    }


@dataclass(frozen=True)
class Permissions:
    """Decoded ``doc.permissions`` — what the CURRENT auth level allows."""

    can_print: bool
    can_modify: bool
    can_copy: bool
    can_annotate: bool
    can_fill_forms: bool
    can_assemble: bool

    @classmethod
    def from_mask(cls, mask: int) -> Permissions:
        return cls(
            can_print=bool(mask & pymupdf.PDF_PERM_PRINT),
            can_modify=bool(mask & pymupdf.PDF_PERM_MODIFY),
            can_copy=bool(mask & pymupdf.PDF_PERM_COPY),
            can_annotate=bool(mask & pymupdf.PDF_PERM_ANNOTATE),
            can_fill_forms=bool(mask & pymupdf.PDF_PERM_FORM),
            can_assemble=bool(mask & pymupdf.PDF_PERM_ASSEMBLE),
        )

    @property
    def all_allowed(self) -> bool:
        return all(
            (
                self.can_print,
                self.can_modify,
                self.can_copy,
                self.can_annotate,
                self.can_fill_forms,
                self.can_assemble,
            )
        )


class _Keep:
    """Sentinel: preserve whatever encryption the file already has."""

    def __repr__(self) -> str:  # aids debugging/asserts
        return "protect.KEEP"


KEEP: Final = _Keep()
