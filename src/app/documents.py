"""Reading documents: PDF text and page images, DOCX text, plain text (PRD M4 uploads).

Pure functions over bytes, no network. Used for uploads in Phase 1 and for fetched PDFs.
"""

import io
import re
import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol, cast

import pypdfium2  # pyright: ignore[reportMissingTypeStubs]  # publishes no stubs
from charset_normalizer import from_bytes
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.pipeline.chunking import split_paragraph_chunks

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PASSWORD_ERROR = cast("int", pypdfium2.raw.FPDF_ERR_PASSWORD)  # pyright: ignore[reportUnknownMemberType]
_BOMS = {b"\xef\xbb\xbf": "utf-8-sig", b"\xff\xfe": "utf-16", b"\xfe\xff": "utf-16"}
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BINARY_RATIO = 0.05
_BINARY_WINDOW = 2000


class DocumentError(Exception):
    """The bytes are not a readable document of the expected kind."""


class _TextPage(Protocol):
    def get_text_bounded(self) -> str: ...
    def close(self) -> None: ...


class _Bitmap(Protocol):
    width: int
    height: int
    stride: int
    n_channels: int
    buffer: object

    def close(self) -> None: ...


class _Page(Protocol):
    def get_textpage(self) -> _TextPage: ...
    def render(self, scale: float, *, grayscale: bool) -> _Bitmap: ...
    def close(self) -> None: ...


class _Pdf(Protocol):
    def __len__(self) -> int: ...
    def __getitem__(self, index: int) -> _Page: ...
    def get_metadata_dict(self) -> dict[str, str]: ...
    def close(self) -> None: ...


class _XmlElement(Protocol):
    tag: str


class _DocxBody(Protocol):
    def iterchildren(self) -> Iterator[_XmlElement]: ...


@dataclass(frozen=True)
class PdfText:
    title: str | None
    pages: tuple[str, ...]  # one entry per page; a page without a text layer is ""
    author: str | None = None


def _open_pdf(body: bytes) -> _Pdf:
    try:
        return cast("_Pdf", pypdfium2.PdfDocument(body))
    except pypdfium2.PdfiumError as exc:
        if getattr(exc, "err_code", None) == _PASSWORD_ERROR:
            raise DocumentError("the PDF is encrypted (password-protected)") from exc
        raise DocumentError(f"unreadable PDF: {exc}") from exc


def pdf_page_count(body: bytes) -> int:
    pdf = _open_pdf(body)
    try:
        return len(pdf)
    finally:
        pdf.close()


def read_pdf(body: bytes) -> PdfText:
    """Text per page via PDFium. No OCR: scanned pages come back empty."""
    pdf = _open_pdf(body)
    try:
        pages: list[str] = []
        for index in range(len(pdf)):
            page = pdf[index]
            textpage = page.get_textpage()
            pages.append(textpage.get_text_bounded().strip())
            textpage.close()
            page.close()
        info = pdf.get_metadata_dict()
        title = str(info.get("Title") or "").strip() or None
        author = str(info.get("Author") or "").strip() or None
    finally:
        pdf.close()
    return PdfText(title=title, pages=tuple(pages), author=author)


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def encode_png_gray(width: int, height: int, pixels: bytes, *, stride: int) -> bytes:
    """An 8-bit greyscale PNG from ``height`` rows of ``stride`` bytes (padding is dropped)."""
    if len(pixels) < stride * (height - 1) + width:
        raise ValueError("pixel buffer is shorter than height x stride")
    rows = b"".join(b"\x00" + pixels[y * stride : y * stride + width] for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(rows, 6))
        + _chunk(b"IEND", b"")
    )


def render_page_png(body: bytes, index: int, dpi: int) -> bytes:
    """One PDF page as a greyscale PNG at ``dpi``, for the OCR model."""
    pdf = _open_pdf(body)
    try:
        if not 0 <= index < len(pdf):
            raise DocumentError(f"page {index} does not exist ({len(pdf)} pages)")
        page = pdf[index]
        bitmap = page.render(dpi / 72, grayscale=True)
        try:
            pixels = bytes(cast("bytes", bitmap.buffer))
            return encode_png_gray(bitmap.width, bitmap.height, pixels, stride=bitmap.stride)
        finally:
            bitmap.close()
            page.close()
    finally:
        pdf.close()


def _table_text(table: Table) -> str:
    return "\n".join(" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows)


def docx_text(body: bytes) -> str:
    """Paragraphs and tables of a DOCX in document order, blocks separated by a blank line."""
    try:
        document = Document(io.BytesIO(body))
        blocks: list[str] = []
        root = cast("_DocxBody", document.element.body)  # pyright: ignore[reportUnknownMemberType]
        for child in root.iterchildren():
            tag = child.tag
            if tag.endswith("}p"):
                blocks.append(Paragraph(child, document).text.strip())  # type: ignore[arg-type]
            elif tag.endswith("}tbl"):
                blocks.append(_table_text(Table(child, document)))  # type: ignore[arg-type]
    except Exception as exc:  # any failure to parse an untrusted file means "unreadable"
        raise DocumentError(f"unreadable DOCX: {exc}") from exc
    return "\n\n".join(block for block in blocks if block.strip())


def _decode(body: bytes) -> str | None:
    for bom, codec in _BOMS.items():
        if body.startswith(bom):
            return body.decode(codec, errors="replace")
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        best = from_bytes(body).best()
        return str(best) if best is not None else None


def _looks_binary(text: str) -> bool:
    head = text[:_BINARY_WINDOW]
    return len(_CONTROL.findall(head)) > _BINARY_RATIO * len(head)


def decode_text(body: bytes) -> str:
    """Text of an MD/TXT upload: UTF-8 (BOM allowed), else a detected legacy encoding."""
    text = _decode(body)
    if text is None or not text.strip() or _looks_binary(text):
        raise DocumentError("the file has no readable text")
    return text.replace("\r\n", "\n").replace("\r", "\n")


_OCR_LINE_BREAK = re.compile(r"<br\s*/?>\n?")
_OCR_MARKUP = re.compile(r"<\|[^<>|]*\|>|<\|(?=<)|</?(?:seg_\d+|md_start|md_end)>")


def clean_ocr(text: str) -> str:
    """OCR output without the model's markup (special tokens, `<seg_n>`, `<md_*>`); `<br/>` is a
    line break. Text without markup is only stripped."""
    return _OCR_MARKUP.sub("", _OCR_LINE_BREAK.sub("\n", text)).strip()


def split_pages(text: str, chars: int) -> list[str]:
    """Pseudo pages of at most ``chars`` characters for formats without pages (DOCX, MD, TXT)."""
    return split_paragraph_chunks(text, chars)
