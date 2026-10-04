"""The PDF of a report (PRD M5 step X): made in-process from `report.md`, checked by reading it
back. No network, no external binary."""

from pathlib import Path

import pypdfium2 as pdfium  # pyright: ignore[reportMissingTypeStubs]
import pytest

from app.research.pdf import render_pdf

REPORT = """# Rückbau von Forschungsreaktoren

## Zusammenfassung

Der Rückbau dauert **zehn Jahre** [1], laut *Bundesamt* und `Atomgesetz` [2].

- Erster Punkt mit Umlauten: Käse, Öl, Übung, Straße
- Zweiter Punkt

1. Eins
2. Zwei

| Anlage | Dauer |
|---|---|
| Garching | 10 Jahre |
| Jülich | 15 Jahre |

## Quellen

[1] A. Autor. 2020. Titel. https://a.example.org/x (abgerufen 2026-10-01)

[2] B. Autorin. 2021. https://b.example.org/y (abgerufen 2026-10-01)

## Anhang A — Recherche-Brief

```text
# Brief

## Ausgabe

- Berichtssprache: Deutsch
```
"""


def pdf_text(path: Path) -> str:
    document = pdfium.PdfDocument(str(path))
    try:
        return "\n".join(document[i].get_textpage().get_text_range() for i in range(len(document)))
    finally:
        document.close()


def pages(path: Path) -> int:
    document = pdfium.PdfDocument(str(path))
    try:
        return len(document)
    finally:
        document.close()


def make(tmp_path: Path, markdown: str = REPORT) -> Path:
    out = tmp_path / "report.pdf"
    render_pdf(markdown, out)
    return out


def test_the_file_is_a_pdf(tmp_path: Path) -> None:
    out = make(tmp_path)
    assert out.read_bytes().startswith(b"%PDF")
    assert pages(out) >= 1


def test_headings_text_and_umlauts_are_in_the_pdf(tmp_path: Path) -> None:
    text = pdf_text(make(tmp_path))
    for expected in (
        "Rückbau von Forschungsreaktoren",
        "Zusammenfassung",
        "Quellen",
        "Anhang A — Recherche-Brief",
        "Käse, Öl, Übung, Straße",
        "zehn Jahre",
    ):
        assert expected in text


def test_inline_markup_does_not_leak_into_the_text(tmp_path: Path) -> None:
    text = pdf_text(make(tmp_path))
    assert "**" not in text
    assert "`" not in text
    assert "zehn Jahre [1]" in text.replace("\r\n", " ").replace("\n", " ")


def test_lists_and_tables_keep_their_content(tmp_path: Path) -> None:
    text = pdf_text(make(tmp_path))
    for expected in (
        "Erster Punkt",
        "Zweiter Punkt",
        "Eins",
        "Zwei",
        "Garching",
        "Jülich",
        "15 Jahre",
    ):
        assert expected in text


def test_a_code_fence_is_kept_line_by_line(tmp_path: Path) -> None:
    lines = [line.strip() for line in pdf_text(make(tmp_path)).splitlines()]
    for expected in ("# Brief", "## Ausgabe", "- Berichtssprache: Deutsch"):
        assert expected in lines


def test_markup_characters_in_the_text_are_shown_not_interpreted(tmp_path: Path) -> None:
    text = pdf_text(
        make(tmp_path, "# T\n\nEin <b>Tag</b> & ein [Link](https://x.example.org/a?b=1&c=2).\n")
    )
    assert "<b>Tag</b> & ein" in text
    assert "Link" in text


def test_a_long_report_runs_over_several_pages(tmp_path: Path) -> None:
    body = "\n\n".join(f"Absatz {n}. " + "Satz über den Rückbau. " * 30 for n in range(40))
    assert pages(make(tmp_path, f"# Lang\n\n## Teil\n\n{body}\n")) > 2


def test_a_glyph_the_font_lacks_does_not_stop_the_export(tmp_path: Path) -> None:
    out = make(tmp_path, "# T\n\nPfeil → und 日本語 im Text.\n\n```text\nCode → ok\n```\n")
    text = pdf_text(out)
    assert "Pfeil" in text
    assert "im Text." in text
    assert "Code" in text


def test_a_rewritten_report_replaces_the_old_file(tmp_path: Path) -> None:
    out = make(tmp_path)
    render_pdf("# Neu\n\nNur das.\n", out)
    text = pdf_text(out)
    assert "Nur das." in text
    assert "Zusammenfassung" not in text


def test_a_report_without_text_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty"):
        render_pdf("  \n\n", tmp_path / "report.pdf")
    assert not (tmp_path / "report.pdf").exists()


def test_a_less_than_sign_and_an_ampersand_in_plain_text_are_shown(tmp_path: Path) -> None:
    text = pdf_text(make(tmp_path, "# T\n\nEs gilt a \\< b und R&D und &amp;-Zeichen.\n"))
    assert "a < b und R&D und &-Zeichen." in text
