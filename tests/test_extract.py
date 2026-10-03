import pytest
from support import make_pdf

from app.adapters.outbound.extract import (
    ExtractionError,
    decode_html,
    detect_kind,
    html_to_text,
    pdf_to_text,
    plain_text,
)

PARAGRAPH = "Der Rückbau kerntechnischer Anlagen erfordert eine Genehmigung nach § 7 AtG. "
ARTICLE = (
    "<html><head><title>Rückbau &amp; Genehmigung</title></head><body><nav>Menü Start</nav>"
    "<article><h1>Rückbau</h1>"
    + "".join(f"<p>Absatz {i}: {PARAGRAPH * 2}</p>" for i in range(4))
    + "<table><tr><th>Phase</th><th>Dauer</th></tr><tr><td>Abbau</td><td>12 Jahre</td></tr></table>"
    "</article><footer>Impressum</footer></body></html>"
)


@pytest.mark.parametrize(
    ("content_type", "url", "head", "kind"),
    [
        ("text/html; charset=utf-8", "https://x.org/a", b"<!doctype html>", "html"),
        ("application/xhtml+xml", "https://x.org/a", b"<?xml", "html"),
        ("application/pdf", "https://x.org/a", b"%PDF-1.7", "pdf"),
        ("application/octet-stream", "https://x.org/paper.PDF", b"\x00\x01", "pdf"),
        (None, "https://x.org/a", b"%PDF-1.4\n", "pdf"),
        (None, "https://x.org/a", b"  <!DOCTYPE HTML><html>", "html"),
        ("text/plain", "https://x.org/a.txt", b"hello", "text"),
        ("text/markdown", "https://x.org/a.md", b"# hi", "text"),
        ("image/png", "https://x.org/a.png", b"\x89PNG", "other"),
        ("application/zip", "https://x.org/a.zip", b"PK", "other"),
        (None, "https://x.org/a", b"\x00\x01\x02", "other"),
    ],
)
def test_detect_kind(content_type: str | None, url: str, head: bytes, kind: str) -> None:
    assert detect_kind(content_type, url, head) == kind


def test_decode_uses_the_declared_header_charset() -> None:
    body = "<p>Größe</p>".encode("iso-8859-1")
    assert decode_html(body, "text/html; charset=ISO-8859-1") == "<p>Größe</p>"


def test_decode_uses_a_meta_charset() -> None:
    html = '<html><head><meta charset="windows-1252"></head><body>Grüße</body></html>'
    assert "Grüße" in decode_html(html.encode("cp1252"), "text/html")


def test_detects_undeclared_charset() -> None:
    text = "<html><body><p>" + PARAGRAPH * 6 + "Überprüfung, Größe, Äußerung.</p></body></html>"
    assert "Überprüfung, Größe, Äußerung" in decode_html(text.encode("cp1252"), None)


def test_an_unknown_declared_charset_falls_back_to_detection() -> None:
    body = ("<p>" + PARAGRAPH * 4 + "</p>").encode("utf-8")
    assert "Rückbau" in decode_html(body, "text/html; charset=bogus-9")


def test_html_to_text_keeps_the_article_and_drops_boilerplate() -> None:
    out = html_to_text(ARTICLE, "https://example.org/rueckbau")
    assert "Absatz 3" in out.text
    assert "| Abbau | 12 Jahre |" in out.text  # tables survive as markdown
    assert "Impressum" not in out.text
    assert out.title in ("Rückbau", "Rückbau & Genehmigung")
    assert out.pages == ()


def test_html_without_an_article_gives_next_to_no_text() -> None:
    # trafilatura falls back to any text it finds; the gateway treats < 300 chars as a failure
    out = html_to_text("<html><body><nav>x</nav></body></html>", "https://e.org")
    assert len(out.text) < 10


def test_pdf_pages_and_title() -> None:
    out = pdf_to_text(make_pdf(["Hello first page", "Second page text"], title="A Report"))
    assert out.pages == ("Hello first page", "Second page text")
    assert out.text == "Hello first page\n\nSecond page text"
    assert out.title == "A Report"


def test_pdf_without_title_metadata() -> None:
    assert pdf_to_text(make_pdf(["x"])).title is None


def test_html_metadata_gives_author_site_and_date() -> None:
    head = (
        '<meta name="author" content="Erika Mustermann">'
        '<meta property="og:site_name" content="Atomforum">'
        '<meta property="article:published_time" content="2024-05-06T10:00:00Z">'
    )
    out = html_to_text(ARTICLE.replace("</head>", f"{head}</head>"), "https://example.org/a")
    assert (out.author, out.sitename, out.date) == ("Erika Mustermann", "Atomforum", "2024-05-06")


def test_html_without_metadata_has_none() -> None:
    out = html_to_text(ARTICLE, "https://example.org/a")
    assert (out.author, out.sitename, out.date) == (None, None, None)


def test_pdf_author_comes_from_the_document_info() -> None:
    assert pdf_to_text(make_pdf(["x"], title="T", author="A. Author")).author == "A. Author"
    assert pdf_to_text(make_pdf(["x"], title="T")).author is None


def test_corrupt_pdf_raises() -> None:
    with pytest.raises(ExtractionError, match="PDF"):
        pdf_to_text(b"%PDF-1.4 this is not a pdf")


def test_plain_text_is_decoded() -> None:
    out = plain_text("Grüße\n".encode(), "text/plain; charset=utf-8")
    assert out.text == "Grüße"
    assert out.title is None
