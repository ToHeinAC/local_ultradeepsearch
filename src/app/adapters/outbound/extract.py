"""Turn fetched bytes into text: content-type sniffing, charset handling, HTML and PDF extraction.

No network here; the fetcher hands over bytes. Junk judgement (login walls etc.) is M3's job.
"""

import codecs
import re
from dataclasses import dataclass
from email.message import Message
from typing import Literal
from urllib.parse import urlsplit

import trafilatura
from charset_normalizer import from_bytes

from app.documents import DocumentError, read_pdf

Kind = Literal["html", "pdf", "text", "other"]
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([\w.:-]+)""", re.IGNORECASE)
_HTML_TYPES = ("text/html", "application/xhtml+xml")
_TEXT_TYPES = ("text/plain", "text/markdown", "text/x-markdown")


class ExtractionError(Exception):
    """The bytes could not be turned into text (for example a corrupt PDF)."""


@dataclass(frozen=True)
class Extracted:
    title: str | None
    text: str
    pages: tuple[str, ...] = ()  # PDF only: one entry per page, for page-level provenance


def _media_type(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _sniff(head: bytes) -> Kind:
    stripped = head.lstrip()[:512].lower()
    if stripped.startswith(b"%pdf"):
        return "pdf"
    if stripped.startswith((b"<!doctype html", b"<html")):
        return "html"
    return "other"


def detect_kind(content_type: str | None, url: str, head: bytes) -> Kind:
    """Classify a response by its declared type, its magic bytes and its URL path."""
    media = _media_type(content_type)
    if media == "application/pdf" or head.lstrip().startswith(b"%PDF"):
        return "pdf"
    if media in _HTML_TYPES:
        return "html"
    if media in _TEXT_TYPES:
        return "text"
    if urlsplit(url).path.lower().endswith(".pdf"):
        return "pdf"
    if not media or media == "application/octet-stream":
        return _sniff(head)
    return "other"


def _known_codec(name: str | None) -> str | None:
    if not name:
        return None
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


def _header_charset(content_type: str | None) -> str | None:
    if not content_type:
        return None
    message = Message()
    message["content-type"] = content_type
    charset = message.get_param("charset")
    return charset if isinstance(charset, str) else None


def decode_html(body: bytes, content_type: str | None) -> str:
    """Decode with the header charset, else a `<meta>` charset, else a detected one."""
    meta = _META_CHARSET.search(body[:4096])
    for candidate in (_header_charset(content_type), meta.group(1).decode() if meta else None):
        codec = _known_codec(candidate)
        if codec:
            return body.decode(codec, errors="replace")
    best = from_bytes(body).best()
    return str(best) if best is not None else body.decode("utf-8", errors="replace")


def html_to_text(html: str, url: str) -> Extracted:
    """Main content as markdown (tables kept, navigation and footers dropped)."""
    text = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_tables=True,
        include_comments=False,
        include_links=False,
        include_images=False,
    )
    title = trafilatura.extract_metadata(html).title or None
    return Extracted(title=title, text=(text or "").strip())


def pdf_to_text(body: bytes) -> Extracted:
    """Text per page via PDFium. No OCR: scanned pages come back empty."""
    try:
        pdf = read_pdf(body)
    except DocumentError as exc:
        raise ExtractionError(str(exc)) from exc
    return Extracted(title=pdf.title, text="\n\n".join(p for p in pdf.pages if p), pages=pdf.pages)


def plain_text(body: bytes, content_type: str | None) -> Extracted:
    return Extracted(title=None, text=decode_html(body, content_type).strip())
