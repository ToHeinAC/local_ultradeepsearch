"""`report.pdf` from `report.md` (PRD M5 step X), made in-process: markdown-it-py (MIT) parses the
Markdown, ReportLab (BSD) lays it out. No external binary and no network.

Text uses the Bitstream Vera family that ships with ReportLab; code uses Courier. A character a
font lacks is shown as `?` instead of stopping the export (a report in a script outside Latin
would lose its text, which the export check on the file cannot see)."""

from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any, cast
from xml.sax.saxutils import escape

from markdown_it import MarkdownIt
from markdown_it.token import Token
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    ListFlowable,
    ListItem,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Table,
    TableStyle,
)
from reportlab.platypus.flowables import Flowable

MARGIN = 2.2 * cm
CODE_COLUMNS = 92
BODY, BOLD, ITALIC, BOLD_ITALIC, CODE = "Vera", "VeraBd", "VeraIt", "VeraBI", "Courier"
_FILES = {BODY: "Vera.ttf", BOLD: "VeraBd.ttf", ITALIC: "VeraIt.ttf", BOLD_ITALIC: "VeraBI.ttf"}
_HEADINGS = {"h1": (20, 26), "h2": (14.5, 20), "h3": (12, 17), "h4": (10.5, 15)}
_BLOCKS = {"fence", "code_block", "hr", "html_block"}


@lru_cache(maxsize=1)
def _glyphs() -> frozenset[int]:
    """The code points the body font can draw; registers the font family on first use."""
    metrics = cast("Any", pdfmetrics)  # ReportLab's type hints are partial
    for name, file in _FILES.items():
        metrics.registerFont(TTFont(name, file))
    metrics.registerFontFamily(BODY, normal=BODY, bold=BOLD, italic=ITALIC, boldItalic=BOLD_ITALIC)
    return frozenset(TTFont(BODY, _FILES[BODY]).face.charToGlyph)


def _body_text(text: str) -> str:
    glyphs = _glyphs()
    return "".join(ch if ch.isspace() or ord(ch) in glyphs else "?" for ch in text)


def _code_text(text: str) -> str:
    return text.encode("cp1252", "replace").decode("cp1252")


def _styles() -> dict[str, ParagraphStyle]:
    _glyphs()
    body = ParagraphStyle("body", fontName=BODY, fontSize=10, leading=14.5, spaceAfter=6)
    styles = {"body": body, "cell": ParagraphStyle("cell", parent=body, fontSize=9, spaceAfter=0)}
    for tag, (size, leading) in _HEADINGS.items():
        styles[tag] = ParagraphStyle(
            tag,
            parent=body,
            fontName=BOLD,
            fontSize=size,
            leading=leading,
            spaceBefore=12 if tag != "h1" else 0,
            spaceAfter=8,
            keepWithNext=1,
        )
    styles["code"] = ParagraphStyle(
        "code", fontName=CODE, fontSize=8, leading=10.5, leftIndent=8, spaceAfter=8,
        backColor=colors.HexColor("#f3f3f3"),
    )  # fmt: skip
    return styles


# ---- inline text ----------------------------------------------------------------------------

_OPEN = {"strong_open": "<b>", "em_open": "<i>", "link_open": ""}
_CLOSE = {"strong_close": "</b>", "em_close": "</i>"}


def _inline(token: Token) -> str:
    """ReportLab's mini markup for an inline token; all text is escaped."""
    out: list[str] = []
    link: str | None = None
    for child in token.children or []:
        kind = child.type
        if kind == "text":
            out.append(escape(_body_text(child.content)))
        elif kind == "code_inline":
            out.append(f'<font name="{CODE}">{escape(_code_text(child.content))}</font>')
        elif kind in ("softbreak", "hardbreak"):
            out.append(" " if kind == "softbreak" else "<br/>")
        elif kind == "link_open":
            link = str(child.attrGet("href") or "")
        elif kind == "link_close" and link:
            out.append(escape(_body_text(f" ({link})")))
            link = None
        elif kind in ("image", "html_inline"):
            out.append(escape(_body_text(child.content)))
        else:
            out.append(_OPEN.get(kind) or _CLOSE.get(kind) or "")
    return "".join(out)


# ---- blocks ---------------------------------------------------------------------------------


class _Layout:
    """Turns the token stream of one report into flowables."""

    def __init__(self, tokens: Sequence[Token]) -> None:
        self._t = tokens
        self._s = _styles()

    def blocks_until(self, i: int, end: str | None) -> tuple[list[Flowable], int]:
        flows: list[Flowable] = []
        while i < len(self._t) and self._t[i].type != end:
            more, i = self._block(i)
            flows += more
        return flows, i + 1

    def _block(self, i: int) -> tuple[list[Flowable], int]:
        token = self._t[i]
        kind = token.type
        if kind == "heading_open":
            return [Paragraph(_inline(self._t[i + 1]), self._s[token.tag])], i + 3
        if kind == "paragraph_open":
            return [Paragraph(_inline(self._t[i + 1]), self._s["body"])], i + 3
        if kind in ("bullet_list_open", "ordered_list_open"):
            return self._list(i)
        if kind == "table_open":
            return self._table(i)
        if kind == "blockquote_open":
            return self._quote(i)
        if kind in _BLOCKS:
            return [self._plain(token)], i + 1
        return [], i + 1

    def _plain(self, token: Token) -> Flowable:
        if token.type == "hr":
            return HRFlowable(width="100%", color=colors.lightgrey, spaceBefore=6, spaceAfter=10)
        if token.type == "html_block":
            return Paragraph(escape(_body_text(token.content.strip())), self._s["body"])
        return Preformatted(
            _code_text(token.content.rstrip("\n")), self._s["code"], maxLineLength=CODE_COLUMNS
        )

    def _list(self, i: int) -> tuple[list[Flowable], int]:
        ordered = self._t[i].type == "ordered_list_open"
        close = "ordered_list_close" if ordered else "bullet_list_close"
        items: list[ListItem] = []
        i += 1
        while self._t[i].type != close:
            flows, i = self.blocks_until(i + 1, "list_item_close")
            items.append(ListItem(flows))
        kind: dict[str, Any] = {"bulletType": "1"} if ordered else {"bulletType": "bullet"}
        flow: Flowable = ListFlowable(
            cast("Any", items), leftIndent=16, bulletFontName=BODY, **kind
        )
        return [flow], i + 1

    def _quote(self, i: int) -> tuple[list[Flowable], int]:
        flows, i = self.blocks_until(i + 1, "blockquote_close")
        box = Table([[flows]], colWidths=["100%"])
        box.setStyle(TableStyle([("LINEBEFORE", (0, 0), (0, -1), 2, colors.lightgrey)]))
        return [box], i

    def _table(self, i: int) -> tuple[list[Flowable], int]:
        rows: list[list[Paragraph]] = []
        header_rows = 0
        i += 1
        while self._t[i].type != "table_close":
            token = self._t[i]
            if token.type == "tr_open":
                rows.append([])
            elif token.type in ("th_open", "td_open"):
                style = self._s["cell"]
                text = _inline(self._t[i + 1])
                rows[-1].append(
                    Paragraph(f"<b>{text}</b>" if token.type == "th_open" else text, style)
                )
            elif token.type == "thead_close":
                header_rows = len(rows)
            i += 1
        return [self._grid(rows, header_rows)], i + 1

    @staticmethod
    def _grid(rows: list[list[Paragraph]], header_rows: int) -> Table:
        width = (A4[0] - 2 * MARGIN) / max(1, len(rows[0]))
        table = Table(rows, colWidths=[width] * len(rows[0]), repeatRows=header_rows)
        style: list[Any] = [
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]
        if header_rows:
            style.append(("BACKGROUND", (0, 0), (-1, header_rows - 1), colors.HexColor("#ececec")))
        table.setStyle(TableStyle(style))
        return table


def _page_number(canvas: Any, doc: Any) -> None:
    canvas.setFont(BODY, 8)
    canvas.drawCentredString(A4[0] / 2, 1.2 * cm, str(doc.page))


def render_pdf(markdown: str, out: Path) -> None:
    """Write ``markdown`` as a PDF to ``out``, replacing it. Raises `ValueError` if it is empty.

    The file appears complete or not at all; a failed layout leaves no half-written PDF."""
    if not markdown.strip():
        raise ValueError("the report is empty")
    tokens = MarkdownIt("commonmark").enable("table").parse(markdown)
    flows, _ = _Layout(tokens).blocks_until(0, None)
    title = next((t.content for t in tokens if t.type == "inline"), "")
    tmp = out.with_name(f".{out.name}.tmp")
    doc = SimpleDocTemplate(
        str(tmp), pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN,
        bottomMargin=2 * cm, title=title, invariant=True,
    )  # fmt: skip
    try:
        doc.build(flows, onFirstPage=_page_number, onLaterPages=_page_number)
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
