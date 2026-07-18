"""Reconciliation guard for extracted invoices (O4).

Arithmetic and integrity cross-checks over an :class:`InvoiceExtract`. This
is the gate between OCR extraction and any downstream push (O5): data leaves
here either fully verified or carrying flags for human confirmation — a
failed check, a check that COULD NOT be evaluated (missing inputs), a
low-confidence word, or an extraction warning all become flags, and
``Reconciliation.ok`` is True only when there are NO flags. Nothing is ever
silently accepted.

Tax is verified under both Australian conventions and the matching one is
reported: EXCLUSIVE (tax = subtotal × rate, subtotal + tax = total) and
GST-INCLUSIVE (tax = total × rate/(1+rate), subtotal = total — the real
sample's $327.27 is exactly $3,600.00 ÷ 11). All money maths is Decimal,
quantized to cents half-up, with a 1-cent tolerance for the source's own
rounding.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from pdfcore.invoice import ExtractedField, InvoiceExtract

_CENT = Decimal("0.01")
_ABN_WEIGHTS = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)

DEFAULT_LOW_CONFIDENCE = 80.0


def abn_checksum_valid(abn: str) -> bool:
    """Australian ABN mod-89 checksum (11 digits; a misread digit fails it)."""
    if len(abn) != 11 or not abn.isdigit():
        return False
    digits = [int(c) for c in abn]
    digits[0] -= 1
    return sum(d * w for d, w in zip(digits, _ABN_WEIGHTS, strict=True)) % 89 == 0


def _cents(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _close(a: Decimal, b: Decimal) -> bool:
    """Equal to within one cent (the source's own rounding latitude)."""
    return abs(_cents(a) - _cents(b)) <= _CENT


@dataclass(frozen=True)
class Check:
    """One named verification: ok True/False, or None when not evaluable."""

    name: str
    ok: bool | None
    detail: str


@dataclass(frozen=True)
class Reconciliation:
    """The verdict: checks (with outcomes) and flags for a human.

    ``ok`` is deliberately strict: True only with ZERO flags — every failed
    check, unevaluable check, low-confidence field and extraction warning is
    a flag. ``tax_basis`` reports which tax convention matched
    ("gst_inclusive" / "exclusive" / "indeterminate") or None.
    """

    checks: tuple[Check, ...]
    flags: tuple[str, ...]
    tax_basis: str | None

    @property
    def ok(self) -> bool:
        return not self.flags


def reconcile(
    extract: InvoiceExtract, *, low_confidence: float = DEFAULT_LOW_CONFIDENCE
) -> Reconciliation:
    """Cross-check an extracted invoice; every problem becomes a flag."""
    checks: list[Check] = []
    flags: list[str] = []

    def add(check: Check) -> None:
        checks.append(check)
        if check.ok is False:
            flags.append(f"check failed: {check.name} — {check.detail}")
        elif check.ok is None:
            flags.append(f"cannot verify: {check.name} — {check.detail}")

    # 1. Per-item arithmetic: qty x unit price = line total.
    for i, item in enumerate(extract.items):
        name = f"items[{i}].arithmetic"
        if item.qty is None or item.unit_price is None or item.line_total is None:
            missing = [
                part
                for part, field in (
                    ("qty", item.qty),
                    ("unit_price", item.unit_price),
                    ("line_total", item.line_total),
                )
                if field is None
            ]
            add(Check(name, None, f"missing {', '.join(missing)}"))
            continue
        product = item.qty.amount * item.unit_price.amount
        add(
            Check(
                name,
                _close(product, item.line_total.amount),
                f"{item.qty.amount} x {item.unit_price.amount} = {_cents(product)} "
                f"vs line total {item.line_total.amount}",
            )
        )

    # 2. Items sum to the subtotal.
    if not extract.items or extract.subtotal is None:
        add(Check("items_sum", None, "missing items or subtotal"))
    elif any(item.line_total is None for item in extract.items):
        add(Check("items_sum", None, "an item has no line total"))
    else:
        total = sum((item.line_total.amount for item in extract.items), Decimal(0))
        add(
            Check(
                "items_sum",
                _close(total, extract.subtotal.amount),
                f"items sum {_cents(total)} vs subtotal {extract.subtotal.amount}",
            )
        )

    # 3. Tax basis: exclusive vs GST-inclusive; report which one matched.
    tax_basis = None
    if extract.subtotal is None or extract.tax is None or extract.total is None:
        add(Check("tax_basis", None, "missing subtotal, tax or total"))
    elif extract.tax_rate is None:
        add(Check("tax_basis", None, "tax rate not stated on the document"))
    else:
        subtotal = extract.subtotal.amount
        tax = extract.tax.amount
        total = extract.total.amount
        rate = extract.tax_rate
        exclusive_tax = subtotal * rate
        exclusive = _close(tax, exclusive_tax) and _close(subtotal + tax, total)
        inclusive_tax = total * rate / (1 + rate)
        inclusive = _close(tax, inclusive_tax) and _close(subtotal, total)
        if exclusive and inclusive:
            tax_basis = "indeterminate"
        elif exclusive:
            tax_basis = "exclusive"
        elif inclusive:
            tax_basis = "gst_inclusive"
        add(
            Check(
                "tax_basis",
                exclusive or inclusive,
                f"exclusive: tax {_cents(exclusive_tax)} + subtotal = "
                f"{_cents(subtotal + exclusive_tax)}; gst_inclusive: tax "
                f"{_cents(inclusive_tax)}, subtotal == total; stated tax {tax}, "
                f"total {total}" + (f" -> {tax_basis}" if tax_basis else " -> neither matches"),
            )
        )

    # 4. ABN checksum.
    if extract.abn is None:
        add(Check("abn_checksum", None, "no ABN extracted"))
    else:
        add(
            Check(
                "abn_checksum",
                abn_checksum_valid(extract.abn.text),
                f"ABN {extract.abn.text} mod-89",
            )
        )

    # 5. Confidence floor over every extracted value (labels excluded).
    for path, field in _all_fields(extract):
        if field.confidence < low_confidence:
            flags.append(f"low confidence: {path} {field.text!r} ({field.confidence:.0f})")

    # 6. Extraction warnings and per-item flags ride along.
    flags.extend(f"extraction: {w}" for w in extract.warnings)
    for i, item in enumerate(extract.items):
        flags.extend(f"items[{i}]: {f}" for f in item.flags)

    return Reconciliation(checks=tuple(checks), flags=tuple(flags), tax_basis=tax_basis)


def _all_fields(extract: InvoiceExtract) -> list[tuple[str, ExtractedField]]:
    fields: list[tuple[str, ExtractedField]] = []
    for name in ("invoice_no", "ref", "customer_po", "date", "abn", "subtotal", "tax", "total"):
        field = getattr(extract, name)
        if field is not None:
            fields.append((name, field))
    for i, item in enumerate(extract.items):
        for part in ("qty", "unit_price", "line_total"):
            field = getattr(item, part)
            if field is not None:
                fields.append((f"items[{i}].{part}", field))
    return fields
