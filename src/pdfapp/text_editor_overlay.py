"""In-place RICH text editors floated over the clicked span or paragraph (E9).

Both editors are QTextEdit-based so the style toolbar can format the current
SELECTION (make just two words bold, superscript one character). They emit
``committed(text)`` / ``cancelled()``; the committed rich content is exposed
as ``committed_pieces_for(text)`` — a list of ``(text, QTextCharFormat)``
pieces (with ``"\\n"`` entries for line breaks) that DocumentView converts to
engine StyledRuns. Callers that drive ``_on_*_committed`` directly (tests)
get a single-style fallback when no matching pieces exist.

``TextEditorOverlay`` is single-line-ish (Enter commits, text selected on
open so a value is retyped); ``ParagraphEditorOverlay`` commits on Ctrl+Enter
(Enter = line break, or the next item of a live list — v2) and opens
with the cursor at the END so typing APPENDS.
Escape cancels. Focus-out does NOT dismiss (the style toolbar needs clicks);
DocumentView commits an open editor on click-away or when a new edit begins.
Both editors carry a visible bottom-right resize grip.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPen,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextListFormat,
)
from PySide6.QtWidgets import QTextEdit, QWidget

from pdfapp import theme

# List v2: Qt list styles per (kind, level % 3) — the same per-level ladder
# the engine's markers use (• ◦ ▪ and 1. a. i.), so the editor's WYSIWYG
# markers match what the commit writes to the page.
_LIST_STYLES = {
    ("bullet", 0): QTextListFormat.Style.ListDisc,
    ("bullet", 1): QTextListFormat.Style.ListCircle,
    ("bullet", 2): QTextListFormat.Style.ListSquare,
    ("number", 0): QTextListFormat.Style.ListDecimal,
    ("number", 1): QTextListFormat.Style.ListLowerAlpha,
    ("number", 2): QTextListFormat.Style.ListLowerRoman,
}
_STYLE_KINDS = {style: kind for (kind, _lvl), style in _LIST_STYLES.items()}

# Forced light chrome: the editors float over a white page, and the dark app
# theme must NOT bleed into them (a dark box over the white page —
# disorienting; manual pass). Border + grip take the ONE themed colour,
# theme.accent(); everything else stays hard light in every mode.
_LIGHT_CHROME = (
    " background: white; color: black;"
    " border: 1px solid {accent}; border-radius: 0px;"
    " padding: 0px 1px; margin: 0px;"
    " selection-background-color: #cde2f7; selection-color: black;"
)


def _overlay_qss(base_font: QFont | None) -> str:
    """Widget-level stylesheet: light chrome, plus the base font BAKED IN.

    The qt-material app stylesheet sets a global font-family; for properties
    a widget's own stylesheet doesn't declare, the app rule wins over
    ``setFont``. So the matched span font must ride in the widget stylesheet
    too — otherwise the editor renders the theme font over the page and
    ``_auto_grow``'s fontMetrics measure the wrong line height.
    """
    rules = _LIGHT_CHROME.format(accent=theme.accent())
    if base_font is not None:
        if base_font.pixelSize() > 0:
            size = f" font-size: {base_font.pixelSize()}px;"
        else:
            size = f" font-size: {base_font.pointSizeF():g}pt;"
        rules += (
            f' font-family: "{base_font.family()}";{size}'
            f" font-weight: {'bold' if base_font.bold() else 'normal'};"
            f" font-style: {'italic' if base_font.italic() else 'normal'};"
        )
    return "QTextEdit {" + rules + "}"


# Char formats carry the TRUE point size as a custom property: the visible
# font uses integer PIXEL sizes (pt x zoom) for on-page visual fidelity, and
# integer rounding at small zooms cannot round-trip points — the property can.
# Typing inherits the neighbouring format, custom properties included.
PT_PROPERTY = QTextCharFormat.Property.UserProperty


class _CornerGrip(QWidget):
    """A visible bottom-right resize handle drawn over its target editor."""

    _SIZE = 16

    def __init__(self, target: _RichOverlayBase) -> None:
        super().__init__(target)
        self._target = target
        self.setFixedSize(self._SIZE, self._SIZE)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self._press_global = None
        self._start_size = None

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        pen = QPen(QColor(theme.accent()))
        pen.setWidth(2)
        painter.setPen(pen)
        w, h = self.width(), self.height()
        for offset in (4, 8, 12):
            painter.drawLine(w - offset, h - 3, w - 3, h - offset)

    def mousePressEvent(self, event) -> None:
        self._press_global = event.globalPosition().toPoint()
        self._start_size = self._target.size()
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._press_global is None:
            return
        delta = event.globalPosition().toPoint() - self._press_global
        self._target.resize(
            max(60, self._start_size.width() + delta.x()),
            max(24, self._start_size.height() + delta.y()),
        )
        self._target.mark_user_sized()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._press_global = None
        event.accept()


class _RichOverlayBase(QTextEdit):
    """Shared rich-editor behaviour: grip, formatting API, commit/cancel."""

    committed = Signal(str)
    cancelled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.hide()
        self._active = False
        self._user_sized = False
        self.setStyleSheet(_overlay_qss(None))
        self.document().setDocumentMargin(1)
        self.setAcceptRichText(False)  # pastes come in plain, styled by cursor
        self._line_height_px: float | None = None
        self._base_size_pt: float | None = None  # true pt the pin was built for
        self._fit_anchor = "left"  # which edge stays put when the box widens
        # Grow downward while typing/formatting so content stays visible —
        # BOTH editors (a tall word clipped in the single-line editor too).
        self.textChanged.connect(self._auto_grow)
        self._grip = _CornerGrip(self)
        self._committed_text: str | None = None
        self._committed_pieces: list[tuple[str, QTextCharFormat]] = []
        self._committed_blocks: list[tuple[str | None, int, int]] | None = None
        # A scrollbar appearing/disappearing must shove the grip clear of it.
        self.verticalScrollBar().rangeChanged.connect(lambda *_: self._reposition_grip())
        self.horizontalScrollBar().rangeChanged.connect(lambda *_: self._reposition_grip())

    # --- state ------------------------------------------------------------
    @property
    def is_editing(self) -> bool:
        return self._active

    @property
    def user_sized_width(self) -> int | None:
        """Widget width in px if the user resized the box this session."""
        return self.width() if self._user_sized else None

    def mark_user_sized(self) -> None:
        self._user_sized = True

    # --- lists (v2: live QTextList editing, the Acrobat model) --------------
    def _block_list_state(self, block) -> tuple[str | None, int, int]:
        """``(kind, level, ordinal)`` of a block from its QTextList — level
        from the list format's indent (Qt lists start at indent 1), ordinal
        from the list's start + the block's position in it."""
        text_list = block.textList()
        if text_list is None:
            return (None, 0, 1)
        fmt = text_list.format()
        kind = _STYLE_KINDS.get(fmt.style(), "bullet")  # unknown styles read as bullets
        level = max(0, fmt.indent() - 1)
        start = fmt.start() if fmt.start() > 0 else 1
        return (kind, level, start + text_list.itemNumber(block))

    def blocks_state(self) -> list[tuple[str | None, int, int]]:
        """Every block's ``(kind, level, ordinal)`` in order — what the
        commit conversion turns into engine ListBlocks."""
        out: list[tuple[str | None, int, int]] = []
        block = self.document().begin()
        while block.isValid():
            out.append(self._block_list_state(block))
            block = block.next()
        return out

    def caret_list_state(self) -> tuple[str | None, int]:
        """The caret block's (kind, level) — the toolbar toggles' state."""
        kind, level, _ordinal = self._block_list_state(self.textCursor().block())
        return kind, level

    def _selected_blocks(self) -> list:
        cursor = self.textCursor()
        doc = self.document()
        block = doc.findBlock(cursor.selectionStart())
        last = doc.findBlock(cursor.selectionEnd())
        blocks = []
        while block.isValid():
            blocks.append(block)
            if block == last:
                break
            block = block.next()
        return blocks

    def _apply_list_format(self, block, kind: str, level: int, start: int = 1) -> None:
        fmt = QTextListFormat()
        fmt.setStyle(_LIST_STYLES[(kind, level % 3)])
        fmt.setIndent(level + 1)
        if start > 1:
            fmt.setStart(start)
        QTextCursor(block).createList(fmt)

    def _remove_list_format(self, block) -> None:
        text_list = block.textList()
        if text_list is None:
            return
        text_list.remove(block)
        fmt = QTextBlockFormat()
        fmt.setIndent(0)
        QTextCursor(block).mergeBlockFormat(fmt)

    def toggle_list(self, kind: str) -> None:
        """Format the selection's blocks as a ``kind`` list — or, when they
        already ARE that kind, remove the formatting (Acrobat: clicking the
        highlighted list type unlists). Converting keeps each block's level."""
        blocks = self._selected_blocks()
        if not blocks:
            return
        if all(self._block_list_state(b)[0] == kind for b in blocks):
            for block in blocks:
                self._remove_list_format(block)
        else:
            for block in blocks:
                current, level, _ = self._block_list_state(block)
                self._apply_list_format(block, kind, level if current is not None else 0)
        self._normalize_lists()
        self._refresh_line_heights()
        self._auto_grow()

    def indent_list(self, delta: int) -> bool:
        """Step the selection's LIST blocks one level deeper/shallower.
        Outdenting a level-0 block removes its list formatting (Word).
        False when no list block is selected (the caller beeps/refuses)."""
        blocks = [b for b in self._selected_blocks() if b.textList() is not None]
        if not blocks:
            return False
        for block in blocks:
            kind, level, _ = self._block_list_state(block)
            new_level = level + (1 if delta > 0 else -1)
            if new_level < 0:
                self._remove_list_format(block)
            else:
                self._apply_list_format(block, kind, new_level)
        self._normalize_lists()
        return True

    def _normalize_lists(self) -> None:
        """Merge consecutive same-style same-level runs into ONE QTextList so
        numbering is continuous — Qt numbers per list object, and two
        adjacent lists would both read "1." A nested run does not break its
        parent's numbering (the outer list resumes); a plain block does."""
        open_lists: dict[int, object] = {}  # indent -> the QTextList to join
        block = self.document().begin()
        while block.isValid():
            text_list = block.textList()
            if text_list is None:
                open_lists.clear()
            else:
                fmt = text_list.format()
                indent = fmt.indent()
                for deeper in [i for i in open_lists if i > indent]:
                    del open_lists[deeper]  # returning shallower closes deeper runs
                existing = open_lists.get(indent)
                if (
                    existing is not None
                    and existing.format().style() == fmt.style()
                    and text_list is not existing
                ):
                    existing.add(block)
                else:
                    open_lists[indent] = block.textList()
            block = block.next()

    # --- open/commit/cancel -------------------------------------------------
    def open_pieces(
        self,
        rect: QRect,
        pieces: list[tuple[str, QTextCharFormat]],
        base_font: QFont,
        select_all: bool,
        line_height_px: float | None = None,
        fit_content: bool = False,
        alignment: Qt.AlignmentFlag | None = None,
        base_size_pt: float | None = None,
        list_specs: list[tuple[str, int, int] | None] | None = None,
        indent_width_px: float | None = None,
    ) -> None:
        """Show the editor prefilled with formatted pieces ("\\n" = break).

        ``line_height_px`` pins every line to a FIXED height (the paragraph's
        pitch at the current zoom) — QTextEdit's natural spacing (~1.15 em) is
        looser than tight-set PDF blocks, which made small paragraphs render
        taller in the editor than on the page. ``fit_content`` grows the box
        (never shrinks it) so the prefill fits without a spurious wrap: the
        paragraph bbox alone leaves no room for the border/margins, and the
        substituted editor font can measure wider than the page metrics —
        that wrapped "$1,410.47" mid-number in a narrow totals column.
        """
        self.setFont(base_font)
        self.setStyleSheet(_overlay_qss(base_font))  # font must beat app QSS
        self._user_sized = False
        self._committed_text = None
        self._committed_pieces = []
        self._committed_blocks = None
        self._line_height_px = line_height_px
        self._base_size_pt = base_size_pt
        self.setGeometry(rect)
        self.clear()
        if indent_width_px is not None and indent_width_px > 0:
            # A list level's on-screen indent matches the page's 18 pt step
            # at the current zoom (Qt's default is a fixed 40 px).
            self.document().setIndentWidth(indent_width_px)
        cursor = self.textCursor()
        for text, fmt in pieces:
            if text == "\n":
                cursor.insertBlock()
            elif text:
                cursor.insertText(text, fmt)
        if list_specs:
            # Seed QTextLists AFTER the text is in place: one spec per block
            # ((kind, level, start_ordinal) or None); consecutive same-kind
            # runs then merge so live numbering is continuous.
            block = self.document().begin()
            for spec in list_specs:
                if not block.isValid():
                    break
                if spec is not None:
                    kind, level, start = spec
                    self._apply_list_format(block, kind, level, start)
                block = block.next()
            self._normalize_lists()
        self._fit_anchor = "left"
        if alignment == Qt.AlignmentFlag.AlignRight:
            self._fit_anchor = "right"
        elif alignment == Qt.AlignmentFlag.AlignHCenter:
            self._fit_anchor = "center"
        if (line_height_px is not None and line_height_px > 0) or alignment is not None:
            block_fmt = QTextBlockFormat()
            if line_height_px is not None and line_height_px > 0:
                block_fmt.setLineHeight(
                    float(line_height_px), QTextBlockFormat.LineHeightTypes.FixedHeight.value
                )
            if alignment is not None:  # match the paragraph's justification
                block_fmt.setAlignment(alignment)
            everything = self.textCursor()
            everything.select(QTextCursor.SelectionType.Document)
            everything.mergeBlockFormat(block_fmt)  # Enter-made blocks inherit
        if fit_content:
            self._fit_to_content(rect)
        if select_all:
            self.selectAll()
        else:
            self.moveCursor(QTextCursor.MoveOperation.End)
        # Typing at the end continues in the last piece's format.
        self._active = True
        self.show()
        self.setFocus()
        self.raise_()
        self._reposition_grip()

    def _content_width_px(self) -> float:
        """Widest logical line, measured with each fragment's own font.

        Fragment metrics, not ``QTextDocument.idealWidth()`` — the latter
        reports stale numbers before the widget has had a real layout pass.
        """
        widest = 0.0
        block = self.document().begin()
        while block.isValid():
            width = 0.0
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                if fragment.isValid() and fragment.text():
                    font = fragment.charFormat().font()
                    if font.pixelSize() <= 0 and font.pointSizeF() <= 0:
                        font = self.font()
                    width += QFontMetricsF(font).horizontalAdvance(fragment.text())
                it += 1
            widest = max(widest, width)
            block = block.next()
        return widest

    def _content_height_px(self, text_width: float) -> int:
        """Content height from a REAL layout pass at ``text_width``.

        Measured on a widget-free CLONE: the live document belongs to the
        QTextEdit, whose page size bleeds into ``size()`` (an offscreen
        editor reported the viewport height, not the content's).
        """
        probe = self.document().clone()
        probe.setDefaultFont(self.font())
        probe.setTextWidth(max(20.0, text_width))
        height = int(probe.size().height())
        probe.deleteLater()
        return height

    def _fit_to_content(self, rect: QRect) -> None:
        """Grow (never shrink) the box so the prefill fits unwrapped.

        Clamped to the parent viewport only when it is VISIBLE — offscreen
        tests carry degenerate never-laid-out parent sizes.
        """
        doc = self.document()
        # Generous width slack: fontMetrics advances undershoot QTextLine's
        # wrap decision by a few px at larger pixel sizes, and a borderline
        # box wraps a short value ("1.55") into fixed-height lines that then
        # stack up behind a scrollbar.
        chrome = 2 * self.frameWidth() + 2 * int(doc.documentMargin()) + 14
        desired_w = int(self._content_width_px()) + chrome
        parent = self.parentWidget()
        if parent is not None and parent.isVisible():  # keep inside the viewport
            desired_w = min(desired_w, max(60, parent.width() - rect.x() - 2))
        final_w = max(rect.width(), desired_w)
        # Height from the REAL layout at the final width: the old
        # blockCount x line-height estimate diverges from QTextEdit's actual
        # layout (tiny pixel sizes especially), so boxes opened with their
        # scrollbar already showing (which then sat over the resize grip).
        desired_h = self._content_height_px(final_w - chrome) + 2 * self.frameWidth() + 4
        if parent is not None and parent.isVisible():
            desired_h_limit = max(24, parent.height() - rect.y() - 2)
            if desired_h > desired_h_limit:  # a scrollbar will appear: allow for it
                final_w = min(
                    final_w + self.verticalScrollBar().sizeHint().width(),
                    max(60, parent.width() - rect.x() - 2),
                )
                desired_h = desired_h_limit
        final_h = max(rect.height(), desired_h)
        # Widening must not drift the anchored edge: a right-aligned block
        # keeps its right edge over the page text, growing leftward.
        grown = final_w - rect.width()
        x = rect.x()
        if grown > 0 and self._fit_anchor == "right":
            x = max(0, x - grown)
        elif grown > 0 and self._fit_anchor == "center":
            x = max(0, x - grown // 2)
        self.setGeometry(x, rect.y(), final_w, final_h)

    def commit(self) -> None:
        self._commit()

    def cancel(self) -> None:
        self._cancel()

    def _commit(self) -> None:
        if not self._active:
            return
        self._active = False
        self._committed_text = self.toPlainText()
        self._committed_pieces = self._pieces()
        self._committed_blocks = self.blocks_state()
        self.hide()
        self.committed.emit(self._committed_text)

    def _cancel(self) -> None:
        if not self._active:
            return
        self._active = False
        self.hide()
        self.cancelled.emit()

    def committed_pieces_for(self, text: str) -> list[tuple[str, QTextCharFormat]] | None:
        """The rich pieces of the last commit, iff they match ``text``.

        None means the commit did not come through this editor (direct calls
        in tests) — the caller falls back to a single uniform style.
        """
        if self._committed_text is not None and self._committed_text == text:
            return self._committed_pieces
        return None

    def committed_blocks_for(self, text: str) -> list[tuple[str | None, int, int]] | None:
        """Per-block ``(kind, level, ordinal)`` of the last commit, iff it
        matches ``text`` — None for direct calls (tests), like the pieces."""
        if self._committed_text is not None and self._committed_text == text:
            return self._committed_blocks
        return None

    def _pieces(self) -> list[tuple[str, QTextCharFormat]]:
        pieces: list[tuple[str, QTextCharFormat]] = []
        block = self.document().begin()
        first = True
        while block.isValid():
            if not first:
                pieces.append(("\n", QTextCharFormat()))
            first = False
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                if fragment.isValid() and fragment.text():
                    pieces.append((fragment.text(), fragment.charFormat()))
                it += 1
            block = block.next()
        return pieces

    def set_alignment(self, alignment: Qt.AlignmentFlag) -> None:
        """Justify the WHOLE editor (alignment is a paragraph property, not a
        selection one) — the on-page layout the commit will produce, shown
        while it is still being typed. The fit anchor follows, so a later
        content-fit widening keeps the anchored edge over the page text."""
        block_fmt = QTextBlockFormat()  # alignment ONLY — a merge leaves the
        block_fmt.setAlignment(alignment)  # pinned line heights untouched
        cursor = self.textCursor()
        everything = QTextCursor(self.document())
        everything.select(QTextCursor.SelectionType.Document)
        everything.mergeBlockFormat(block_fmt)
        self.setTextCursor(cursor)  # merging must not move the caret/selection
        self._fit_anchor = {
            Qt.AlignmentFlag.AlignRight: "right",
            Qt.AlignmentFlag.AlignHCenter: "center",
        }.get(alignment, "left")

    # --- selection formatting (driven by the style toolbar) ----------------
    def merge_selection_format(self, fmt: QTextCharFormat) -> None:
        """Apply to the selection, or set the typing format at the cursor."""
        cursor = self.textCursor()
        cursor.mergeCharFormat(fmt)
        self.mergeCurrentCharFormat(fmt)
        # A size change can make text taller than its pinned line height /
        # the box — refresh both (format-only changes don't always reach
        # textChanged reliably; explicit beats implicit here).
        self._refresh_line_heights()
        self._auto_grow()

    def _refresh_line_heights(self) -> None:
        """Scale each block's PINNED line height by its tallest fragment.

        Tight-set blocks keep their pitch (deliberately tighter than font
        metrics, like the page); a block containing ENLARGED text gets a
        proportionally taller line so ascenders stop clipping (user report:
        a grown word was chopped off at the box edge). Converges: writes
        only when the value actually changes."""
        if not self._line_height_px:
            return  # natural line heights (insert/span editors) need no pin
        base_pt = float(self._base_size_pt or 0.0)
        base_px = float(self.font().pixelSize() or 1)
        block = self.document().begin()
        while block.isValid():
            ratio = 1.0
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                if fragment.isValid() and fragment.text():
                    fmt = fragment.charFormat()
                    # TRUE pt ratio when available — pixel sizes are floored
                    # at small zooms and would understate the enlargement.
                    pt = fmt.property(PT_PROPERTY)
                    if base_pt > 0 and isinstance(pt, float) and pt > 0:
                        ratio = max(ratio, pt / base_pt)
                    else:
                        px = fmt.font().pixelSize()
                        if px > 0:
                            ratio = max(ratio, float(px) / base_px)
                it += 1
            fixed = float(self._line_height_px) * ratio
            if abs(block.blockFormat().lineHeight() - fixed) > 0.5:
                cursor = QTextCursor(block)
                block_fmt = QTextBlockFormat()
                block_fmt.setLineHeight(fixed, QTextBlockFormat.LineHeightTypes.FixedHeight.value)
                cursor.mergeBlockFormat(block_fmt)
            block = block.next()

    def _auto_grow(self) -> None:
        """Grow the box downward so the content stays fully visible (never
        shrinks; clamped to a VISIBLE parent viewport)."""
        if not self._active:
            return
        needed = self._content_height_px(self.viewport().width()) + 2 * self.frameWidth() + 4
        if needed <= self.height():
            return
        parent = self.parentWidget()
        if parent is None or not parent.isVisible():  # offscreen: nothing to clamp to
            limit = needed
        else:
            limit = max(self.height(), parent.height() - self.y() - 2)
        self.resize(self.width(), min(needed, limit))

    def current_char_format(self) -> QTextCharFormat:
        return self.currentCharFormat()

    # --- events -------------------------------------------------------------
    def _reposition_grip(self) -> None:
        # Sit CLEAR of any scrollbar (range-based, not isVisible() — that is
        # always False offscreen): the vertical bar otherwise covers the grip.
        vbar = self.verticalScrollBar()
        hbar = self.horizontalScrollBar()
        v_w = (
            vbar.sizeHint().width()
            if (
                self.verticalScrollBarPolicy() != Qt.ScrollBarPolicy.ScrollBarAlwaysOff
                and vbar.maximum() > 0
            )
            else 0
        )
        h_h = (
            hbar.sizeHint().height()
            if (
                self.horizontalScrollBarPolicy() != Qt.ScrollBarPolicy.ScrollBarAlwaysOff
                and hbar.maximum() > 0
            )
            else 0
        )
        self._grip.move(
            self.width() - self._grip.width() - v_w,
            self.height() - self._grip.height() - h_h,
        )
        self._grip.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reposition_grip()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._reposition_grip()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._cancel()
            event.accept()
            return
        super().keyPressEvent(event)


class TextEditorOverlay(_RichOverlayBase):
    """Single-line rich editor for one span (a value like a price).

    Enter commits (``returnPressed`` kept as a compatibility signal); the
    value is selected on open so a keystroke replaces it. Rich formatting
    still applies to selections within the line.
    """

    returnPressed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.returnPressed.connect(self._commit)

    # QLineEdit-compatible helpers (existing tests and call sites).
    def setText(self, text: str) -> None:
        # Preserve the current char format (setPlainText would reset it to
        # the widget default, losing size/colour/pt-property).
        fmt = self.currentCharFormat()
        self.selectAll()
        cursor = self.textCursor()
        cursor.insertText(text, fmt)
        self.setTextCursor(cursor)

    def text(self) -> str:
        return self.toPlainText()

    def open_at(self, rect: QRect, text: str, font: QFont | None = None) -> None:
        base = font or self.font()
        fmt = QTextCharFormat()
        fmt.setFont(base)
        self.open_pieces(rect, [(text, fmt)], base, select_all=True)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.returnPressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class ParagraphEditorOverlay(_RichOverlayBase):
    """Multi-line rich editor for a paragraph. Ctrl+Enter commits.

    Opens with the cursor at the END and nothing selected, so typing APPENDS
    (select-all wiped the paragraph on a stray keystroke). The corner grip
    (and an invisible right-edge band) set the box width → the paragraph's
    wrap width on commit; height is visual editing room.

    In a LIST block (v2) plain Enter continues the list (Qt advances the
    QTextList's numbering), Enter on an empty item ends it, Shift+Enter is
    the line break WITHIN an item, Tab / Shift+Tab step the nesting level,
    and Backspace at an item's start outdents.
    """

    _EDGE_PX = 7  # right-edge width-drag band

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._resizing = False
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

    # Plain-text helpers kept for existing call sites/tests.
    def open_at(self, rect: QRect, text: str, font: QFont | None = None) -> None:
        base = font or self.font()
        fmt = QTextCharFormat()
        fmt.setFont(base)
        pieces: list[tuple[str, QTextCharFormat]] = []
        for i, line in enumerate(text.split("\n")):
            if i:
                pieces.append(("\n", QTextCharFormat()))
            pieces.append((line, fmt))
        self.open_pieces(rect, pieces, base, select_all=False)

    def _near_right_edge(self, x: float) -> bool:
        return x >= self.viewport().width() - self._EDGE_PX

    def mousePressEvent(self, event) -> None:
        if self._near_right_edge(event.position().x()):
            self._resizing = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._resizing:
            new_width = max(60, int(event.position().x()) + self._EDGE_PX + 2)
            self.resize(new_width, self.height())
            self._user_sized = True
            event.accept()
            return
        if self._near_right_edge(event.position().x()):
            self.viewport().setCursor(Qt.CursorShape.SizeHorCursor)
        else:
            self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._resizing:
            self._resizing = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        cursor = self.textCursor()
        in_list = cursor.block().textList() is not None
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            mods = event.modifiers()
            if mods & Qt.KeyboardModifier.ControlModifier:
                self._commit()  # apply (and, in list mode, end the list)
                event.accept()
                return
            if mods & Qt.KeyboardModifier.ShiftModifier:
                if in_list:
                    # A line break WITHIN the item: QTextEdit's native soft
                    # break (U+2028) stays inside the block, so the item
                    # remains ONE engine block and the engine translates the
                    # separator (probe-verified at L3).
                    super().keyPressEvent(event)
                else:
                    # Plain blocks keep the explicit BLOCK break (the L3
                    # decision — the two line-break keys behave identically
                    # outside a list).
                    self.textCursor().insertBlock()
                event.accept()
                return
            if in_list and not cursor.block().text().strip() and not cursor.hasSelection():
                # Enter on an EMPTY item ends the list (Word/Acrobat): the
                # empty block becomes a plain paragraph instead of item N+1.
                self._remove_list_format(cursor.block())
                event.accept()
                return
            # Enter inside a list item: Qt continues the list (the new block
            # joins the same QTextList and numbering advances) — default.
        elif key == Qt.Key.Key_Tab and in_list and cursor.atBlockStart():
            if self.indent_list(+1):
                event.accept()
                return
        elif key == Qt.Key.Key_Backtab and in_list:
            if self.indent_list(-1):
                event.accept()
                return
        elif (
            key == Qt.Key.Key_Backspace
            and in_list
            and cursor.atBlockStart()
            and not cursor.hasSelection()
        ):
            # Backspace at the start of an item outdents it a level, then
            # (at level 0) removes the list formatting — the Word habit.
            self.indent_list(-1)
            event.accept()
            return
        super().keyPressEvent(event)
