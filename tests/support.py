"""Helpers shared by test modules."""

from typing import Any

from app.config import Settings
from app.store.models import Note


def make_settings(**overrides: Any) -> Settings:
    """Settings that never read a developer's `.env` (init kwargs and `UDR_*` env still apply)."""
    return Settings(_env_file=None, **overrides)  # pyright: ignore[reportCallIssue]


def make_pdf(pages: list[str], title: str = "") -> bytes:
    """A minimal valid PDF with one Helvetica text line per page (ASCII text only)."""
    page_count = len(pages)
    font_id = 3 + 2 * page_count
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(page_count))
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>",
    ]
    for i, text in enumerate(pages):
        stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET"
        objects.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {4 + 2 * i} 0 R >>"
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    info_ref = ""
    if title:
        objects.append(f"<< /Title ({title}) >>")
        info_ref = f" /Info {len(objects)} 0 R"
    out = b"%PDF-1.4\n"
    offsets: list[int] = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    trailer = f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R{info_ref} >>\n"
    return out + f"{trailer}startxref\n{xref}\n%%EOF\n".encode()


def make_note(**overrides: Any) -> Note:
    """A `Note` with sensible defaults for pipeline tests."""
    fields: dict[str, Any] = {
        "run_id": "run-a",
        "note_id": "n0001",
        "kind": "source",
        "stage": "fetched",
        "url": "https://example.org/a",
        "final_url": "https://example.org/a",
        "canonical_url": "https://example.org/a",
        "doi": None,
        "title": "A source",
        "content_type": "text/html",
        "via": "local",
        "body": "Some body text. " * 30,
        "pages": (),
        "word_count": 90,
        "summary": "",
        "meta": {},
        "source_tier": "unknown",
        "utility": None,
        "derivative_of": None,
        "analysis_of": None,
        "links": (),
        "extract_failed": False,
        "claims_kept": 0,
        "claims_dropped": 0,
        "created_at": "2026-10-02T08:00:00+00:00",
    }
    return Note(**{**fields, **overrides})
