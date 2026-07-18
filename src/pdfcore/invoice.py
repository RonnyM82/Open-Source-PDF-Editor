"""Invoice field extraction from OCR word boxes (O3).

Layout-based parsing of invoice-style pages (the invoice-print family the OCR
ingestion targets): visual lines are rebuilt from word boxes, labelled values
("Sub Total: $3,600.00", "ABN Number: …") are taken as the first value-shaped
token RIGHT of their label on the same line (two-column pages share visual
lines, so tokens left of the label are ignored), and tabular values map to
columns derived from their own header row's x-positions. This is NOT a
universal invoice parser — it is keyword+geometry driven and tested against
the real sample plus a generated fixture; extend the label tables when a new
layout appears.

Money is Decimal end to end (float arithmetic on money invites cent drift —
the O4 reconciliation needs exactness). All coordinates are unrotated page
points, straight from :class:`~pdfcore.ocr.OcrWord`.

Column-straddle rule (probe-grounded): a token assigned to a column but
overrunning that column's RIGHT edge by more than ``_STRADDLE_RIGHT`` points
is flagged — that is the OCR merge signature (the real sample's
``XY99Z000__+`` merges the Code column with the description's ``+`` and
overruns by 4.1 pt). Right-aligned money legitimately bleeds a few points
LEFT of its header, so the left tolerance is looser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from pdfcore.ocr import OcrWord
from pdfcore.textsource import group_lines as _group_lines

_MONEY = re.compile(r"^\$?-?[\d,]{1,12}\.\d{2}$")
_INT = re.compile(r"^\d{1,6}$")
_RATE = re.compile(r"\((\d+(?:\.\d+)?)\s*%\)")

_STRADDLE_RIGHT = 3.0  # pt a token may overrun its column's right edge
_STRADDLE_LEFT = 8.0  # pt of legitimate right-aligned bleed past the left edge
_ANCHOR_SLACK = 6.0  # pt of slack when a value starts left of its column anchor


def parse_money(text: str) -> Decimal | None:
    """``"$3,600.00"`` → ``Decimal("3600.00")``; None when not money-shaped."""
    if not _MONEY.match(text):
        return None
    return Decimal(text.replace("$", "").replace(",", ""))


@dataclass(frozen=True)
class ExtractedField:
    """One extracted value with provenance.

    ``confidence`` is the minimum Tesseract confidence over the words that
    make up the VALUE (not its label). ``amount`` is the parsed Decimal for
    money/quantity fields, None for free text.
    """

    name: str
    text: str
    confidence: float
    bbox: tuple[float, float, float, float]
    amount: Decimal | None = None


@dataclass(frozen=True)
class LineItem:
    """One item row-group from the items table."""

    description: str
    qty: ExtractedField | None
    unit_price: ExtractedField | None
    line_total: ExtractedField | None
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class InvoiceExtract:
    """Everything O3 pulls out of one page; O4 reconciles it."""

    invoice_no: ExtractedField | None
    ref: ExtractedField | None
    customer_po: ExtractedField | None
    date: ExtractedField | None
    abn: ExtractedField | None
    items: tuple[LineItem, ...]
    subtotal: ExtractedField | None
    tax: ExtractedField | None
    tax_rate: Decimal | None
    total: ExtractedField | None
    warnings: tuple[str, ...]


# --- visual lines -------------------------------------------------------------
# The line-grouping rule was lifted into pdfcore.textsource at X0 (imported
# above as _group_lines) so field extraction and the search/extract read
# features share one definition of "a visual line".


def _line_text(line: list[OcrWord]) -> str:
    return " ".join(w.text for w in line)


def _field(name: str, words: list[OcrWord], amount: Decimal | None = None) -> ExtractedField:
    return ExtractedField(
        name=name,
        text=" ".join(w.text for w in words),
        confidence=min(w.confidence for w in words),
        bbox=(
            min(w.bbox[0] for w in words),
            min(w.bbox[1] for w in words),
            max(w.bbox[2] for w in words),
            max(w.bbox[3] for w in words),
        ),
        amount=amount,
    )


# --- labelled values ----------------------------------------------------------

_STRIP_PUNCT = ":.,()"


def _norm(token: str) -> str:
    return token.strip(_STRIP_PUNCT).lower()


def _find_label_money(
    lines: list[list[OcrWord]], label_patterns: list[list[str]], name: str
) -> ExtractedField | None:
    """First money token RIGHT of a label match, first pattern that hits wins.

    A pattern is a sequence of normalised words that must appear consecutively
    in a line ("sub total" survives OCR as two words). Tokens LEFT of the
    label are ignored — two-column layouts share visual lines.
    """
    for pattern in label_patterns:
        for line in lines:
            tokens = [_norm(w.text) for w in line]
            for start in range(len(tokens) - len(pattern) + 1):
                if tokens[start : start + len(pattern)] != pattern:
                    continue
                for word in line[start + len(pattern) :]:
                    amount = parse_money(word.text)
                    if amount is not None:
                        return _field(name, [word], amount)
    return None


def _find_tax(lines: list[list[OcrWord]]) -> tuple[ExtractedField | None, Decimal | None]:
    """The tax line: value plus the rate captured from "(10%)" when present."""
    for line in lines:
        tokens = [_norm(w.text) for w in line]
        if "tax" not in tokens and "gst" not in tokens:
            continue
        label_at = tokens.index("tax") if "tax" in tokens else tokens.index("gst")
        # "Tax Invoice Total"/"Tax Invoice" headers are not the tax line —
        # require the rate marker or a colon-ish label directly before money.
        rate = None
        rate_match = _RATE.search(_line_text(line))
        if rate_match:
            rate = Decimal(rate_match.group(1)) / 100
        elif "invoice" in tokens[label_at : label_at + 2]:
            continue
        for word in line[label_at + 1 :]:
            amount = parse_money(word.text)
            if amount is not None:
                return _field("tax", [word], amount), rate
    return None, None


def _find_abn(lines: list[list[OcrWord]]) -> ExtractedField | None:
    """11 digits right of an ABN label; consecutive digit groups are joined
    ("51 824 753 556" and "51824753556" both occur in the wild)."""
    for line in lines:
        tokens = [_norm(w.text) for w in line]
        if "abn" not in tokens:
            continue
        digits: list[OcrWord] = []
        for word in line[tokens.index("abn") + 1 :]:
            if word.text.strip(_STRIP_PUNCT).isdigit():
                digits.append(word)
            elif digits:
                break
        joined = "".join(w.text.strip(_STRIP_PUNCT) for w in digits)
        if len(joined) == 11:
            field = _field("abn", digits)
            return ExtractedField(
                name=field.name,
                text=joined,
                confidence=field.confidence,
                bbox=field.bbox,
            )
    return None


# --- header-column tables -------------------------------------------------------

# (field name, header word sequence) — matched greedily left-to-right.
_ID_COLUMNS = [
    ("date", ["invoice", "date"]),
    ("ref", ["ref"]),
    ("invoice_no", ["invoice", "no"]),
    ("customer_po", ["customer", "po", "no"]),
]
_ITEM_COLUMNS = [
    ("code", ["code"]),
    ("description", ["item"]),
    ("options", ["options"]),
    ("qty", ["qty"]),
    ("unit_price", ["unit", "price"]),
    ("discount", ["discount"]),
    ("line_total", ["subtotal"]),
]


@dataclass(frozen=True)
class _Column:
    name: str
    x0: float  # range start (anchor x0)
    x1: float  # range end (next anchor's x0; last column open-ended)


def _match_header(line: list[OcrWord], specs: list[tuple[str, list[str]]]) -> list[_Column]:
    """Map a header line's tokens onto column specs, greedy left-to-right.
    Returns [] unless at least half the specs matched (wrong line)."""
    tokens = [_norm(w.text) for w in line]
    anchors: list[tuple[str, float]] = []
    i = 0
    while i < len(tokens):
        for name, seq in specs:
            if any(name == a for a, _ in anchors):
                continue
            if tokens[i : i + len(seq)] == seq:
                anchors.append((name, line[i].bbox[0]))
                i += len(seq)
                break
        else:
            i += 1
    if len(anchors) < max(2, len(specs) // 2):
        return []
    columns = []
    for idx, (name, x0) in enumerate(anchors):
        x1 = anchors[idx + 1][1] if idx + 1 < len(anchors) else float("inf")
        columns.append(_Column(name, x0, x1))
    return columns


def _assign_column(word: OcrWord, columns: list[_Column]) -> tuple[str | None, bool]:
    """(column name, straddles) by maximum x-overlap with the column ranges.

    ``straddles`` is True when the token overruns its column's right edge by
    more than ``_STRADDLE_RIGHT`` (the OCR column-merge signature) or starts
    implausibly far left of it.
    """
    x0, _, x1, _ = word.bbox
    best: tuple[float, _Column | None] = (0.0, None)
    for col in columns:
        left = col.x0 - _ANCHOR_SLACK
        overlap = min(x1, col.x1) - max(x0, left)
        if overlap > best[0]:
            best = (overlap, col)
    if best[1] is None:
        return None, False
    col = best[1]
    straddles = x1 > col.x1 + _STRADDLE_RIGHT or x0 < col.x0 - _STRADDLE_LEFT
    return col.name, straddles


def _row_cells(
    line: list[OcrWord], columns: list[_Column]
) -> tuple[dict[str, list[OcrWord]], list[str]]:
    """Bucket a row's words into columns; straddling tokens are flagged and
    kept OUT of every bucket (their content cannot be trusted to one column)."""
    cells: dict[str, list[OcrWord]] = {}
    flags: list[str] = []
    for word in line:
        name, straddles = _assign_column(word, columns)
        if straddles:
            flags.append(f"column-straddle: {word.text!r}")
            continue
        if name is not None:
            cells.setdefault(name, []).append(word)
    return cells, flags


def _extract_id_row(
    lines: list[list[OcrWord]],
) -> tuple[dict[str, ExtractedField], list[str]]:
    """The Invoice Date / Ref / Invoice No / Customer PO header + value row."""
    for idx, line in enumerate(lines):
        columns = _match_header(line, _ID_COLUMNS)
        if not columns or idx + 1 >= len(lines):
            continue
        height = line[0].bbox[3] - line[0].bbox[1]
        value_line = lines[idx + 1]
        if min(w.bbox[1] for w in value_line) - max(w.bbox[3] for w in line) > 3 * height:
            continue  # nothing directly under the header
        cells, flags = _row_cells(value_line, columns)
        fields = {name: _field(name, words) for name, words in cells.items()}
        return fields, flags
    return {}, []


def _extract_items(
    lines: list[list[OcrWord]],
) -> tuple[tuple[LineItem, ...], list[str]]:
    """The items table: header-derived columns, rows until the layout breaks,
    money-bearing rows start items and bare rows continue the description."""
    header_idx = None
    columns: list[_Column] = []
    for idx, line in enumerate(lines):
        columns = _match_header(line, _ITEM_COLUMNS)
        if len(columns) >= 4:
            header_idx = idx
            break
    if header_idx is None:
        return (), ["items: table header not found"]

    header_line = lines[header_idx]
    header_height = max(w.bbox[3] for w in header_line) - min(w.bbox[1] for w in header_line)
    rows: list[list[OcrWord]] = []
    prev_bottom = max(w.bbox[3] for w in header_line)
    for line in lines[header_idx + 1 :]:
        top = min(w.bbox[1] for w in line)
        if top - prev_bottom > 2.5 * header_height:
            break  # the table's vertical rhythm ended
        rows.append(line)
        prev_bottom = max(w.bbox[3] for w in line)

    items: list[LineItem] = []
    warnings: list[str] = []
    open_cells: dict[str, list[OcrWord]] | None = None
    desc: list[str] = []
    flags: list[str] = []

    def close_open() -> None:
        """Finish the item being accumulated (no-op before the first one)."""
        nonlocal open_cells, desc, flags
        if open_cells is None:
            warnings.extend(flags)  # stray flags from rows before any item
        else:
            qty_words = open_cells.get("qty", [])
            qty = None
            if qty_words and _INT.match(qty_words[0].text):
                qty = _field("qty", [qty_words[0]], Decimal(qty_words[0].text))
            items.append(
                LineItem(
                    description=" ".join(desc),
                    qty=qty,
                    unit_price=_money_cell(open_cells.get("unit_price", []), "unit_price"),
                    line_total=_money_cell(open_cells.get("line_total", []), "line_total"),
                    flags=tuple(flags),
                )
            )
        open_cells, desc, flags = None, [], []

    for row in rows:
        cells, row_flags = _row_cells(row, columns)
        starts_item = any(parse_money(w.text) is not None for w in cells.get("line_total", []))
        if starts_item:
            close_open()
            open_cells = cells
        flags.extend(row_flags)
        desc.extend(w.text for w in cells.get("description", []))
    close_open()
    return tuple(items), warnings


def _money_cell(words: list[OcrWord], name: str) -> ExtractedField | None:
    for word in words:
        amount = parse_money(word.text)
        if amount is not None:
            return _field(name, [word], amount)
    return None


# --- top level ------------------------------------------------------------------


def extract_invoice(words: list[OcrWord]) -> InvoiceExtract:
    """Parse OCR words (one page) into invoice fields.

    Never raises on missing content — absent fields come back None with a
    warning, so O4 can flag them for human confirmation.
    """
    lines = _group_lines(words)
    warnings: list[str] = []

    id_fields, id_flags = _extract_id_row(lines)
    warnings.extend(id_flags)
    items, item_warnings = _extract_items(lines)
    warnings.extend(item_warnings)

    tax, tax_rate = _find_tax(lines)
    values: dict[str, ExtractedField | None] = {
        "invoice_no": id_fields.get("invoice_no"),
        "ref": id_fields.get("ref"),
        "customer_po": id_fields.get("customer_po"),
        "date": id_fields.get("date"),
        "abn": _find_abn(lines),
        "subtotal": _find_label_money(lines, [["sub", "total"], ["subtotal"]], "subtotal"),
        "tax": tax,
        "total": _find_label_money(
            lines, [["invoice", "total"], ["total", "aud"], ["total"]], "total"
        ),
    }
    for name in ("invoice_no", "date", "abn", "subtotal", "tax", "total"):
        if values[name] is None:
            warnings.append(f"{name}: not found")
    if not items:
        warnings.append("items: none extracted")
    return InvoiceExtract(items=items, tax_rate=tax_rate, warnings=tuple(warnings), **values)
