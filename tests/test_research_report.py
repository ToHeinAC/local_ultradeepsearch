"""Assembling the report: code owns citation numbers, the Sources list and the appendix (D7)."""

from datetime import UTC, datetime

import pytest
from support import make_note

from app.research.markdown import appendix_fence, h2_list, sources_entries
from app.research.report import (
    ApprovedBrief,
    RenderedReport,
    ReportSource,
    SectionText,
    render_report,
    source_from_note,
)

APPROVED = datetime(2026, 10, 2, 9, 30, 15, tzinfo=UTC)
BRIEF = "# Wie lange dauert der Rückbau?\n\n## Forschungsfragen\n\n1. Wie lange?\n"


def src(note_id: str = "n1", **overrides: object) -> ReportSource:
    fields: dict[str, object] = {
        "note_id": note_id,
        "url": f"https://{note_id}.example.org/a",
        "title": "Rückbau in Deutschland",
        "authors": ("Erika Mustermann",),
        "publisher": None,
        "year": 2020,
        "retrieved": "2026-10-01",
        "retracted": False,
    }
    return ReportSource(**{**fields, **overrides})  # type: ignore[arg-type]


KEYS = {f"S{i}": src(f"n{i}") for i in range(1, 7)}


def render(
    *texts: str,
    language: str = "de",
    brief: str = BRIEF,
    keys: dict[str, ReportSource] | None = None,
) -> RenderedReport:
    sections = [SectionText(f"Abschnitt {i}", text) for i, text in enumerate(texts, 1)]
    approved = ApprovedBrief(brief, APPROVED, "data/briefs/2026-10-02T09-30-15Z.md")
    return render_report("Der Titel", sections, KEYS if keys is None else keys, language, approved)


def test_the_structure_is_title_sections_sources_appendix() -> None:
    out = render("Erster Text [S1].", "Zweiter Text [S2].")
    assert out.markdown.startswith("# Der Titel\n\n## Abschnitt 1\n\nErster Text [1].\n")
    assert h2_list(out.markdown) == [
        "Abschnitt 1",
        "Abschnitt 2",
        "Quellen",
        "Anhang A — Recherche-Brief",
    ]


def test_english_headings_and_labels() -> None:
    out = render("Text [S1].", language="en", keys={"S1": src(year=None, authors=())})
    assert h2_list(out.markdown)[-2:] == ["Sources", "Appendix A — Research Brief"]
    entry = sources_entries(out.markdown)[1]
    assert entry.endswith("(accessed 2026-10-01)")
    assert "n.d." in entry
    assert "Approved on 2026-10-02 09:30:15 UTC" in out.markdown


def test_numbers_follow_the_first_appearance_across_sections() -> None:
    out = render("A [S3]. B [S1].", "C [S3] [S2].")
    assert "A [1]. B [2]." in out.markdown
    assert "C [1, 3]." in out.markdown
    assert out.cited == ((1, "S3"), (2, "S1"), (3, "S2"))
    assert [n for n in sorted(sources_entries(out.markdown))] == [1, 2, 3]
    assert "n3.example.org" in sources_entries(out.markdown)[1]


def test_adjacent_brackets_merge_and_numbers_are_sorted() -> None:
    out = render("A [S1][S2]. B [S2] [S1]. C [S1; S3]. D [S3, S2].")
    assert "A [1, 2]. B [1, 2]. C [1, 3]. D [2, 3]." in out.markdown.replace("[S", "[S")
    assert "][" not in out.markdown.split("## Quellen")[0]


def test_a_bracket_holds_at_most_three_numbers() -> None:
    out = render("A [S1, S2, S3, S4, S5].")
    assert "A [1, 2, 3] [4, 5]." in out.markdown


def test_repeated_keys_in_one_group_count_once() -> None:
    assert "A [1]." in render("A [S1, S1] [S1].").markdown


def test_unknown_keys_are_dropped_and_reported() -> None:
    out = render("A [S1, S9]. B [S8]. C [S7] and D.")
    assert "A [1]. B. C and D." in out.markdown
    assert out.dropped_keys == ("S9", "S8", "S7")
    assert len(sources_entries(out.markdown)) == 1


def test_only_cited_sources_are_listed_in_citation_order() -> None:
    out = render("A [S5]. B [S2].")
    assert [sources_entries(out.markdown)[n][:5] for n in (1, 2)] == ["Erika", "Erika"]
    assert "n5.example.org" in sources_entries(out.markdown)[1]
    assert "n2.example.org" in sources_entries(out.markdown)[2]
    assert "n1.example.org" not in out.markdown


def test_a_report_without_citations_still_has_a_sources_section() -> None:
    out = render("Kein Beleg.")
    assert h2_list(out.markdown)[-2] == "Quellen"
    assert "Es wurden keine Quellen zitiert." in out.markdown


def test_source_entry_format_with_authors_publisher_and_host_fallbacks() -> None:
    keys = {
        "S1": src("n1", authors=("A. Eins", "B. Zwei")),
        "S2": src("n2", authors=(), publisher="Atomforum"),
        "S3": src("n3", authors=(), publisher=None, url="https://www.bund.de/x"),
        "S4": src("n4", authors=("A", "B", "C", "D")),
        "S5": src("n5", title="Zwei\nZeilen  Titel"),
    }
    entries = sources_entries(render("[S1] [S2] [S3] [S4] [S5]", keys=keys).markdown)
    assert entries[1] == (
        "A. Eins; B. Zwei. Rückbau in Deutschland. 2020. https://n1.example.org/a "
        "(abgerufen 2026-10-01)"
    )
    assert entries[2].startswith("Atomforum. ")
    assert entries[3].startswith("bund.de. ")
    assert entries[4].startswith("A; B; C u. a. ")
    assert "Zwei Zeilen Titel" in entries[5]


def test_the_appendix_is_the_brief_byte_for_byte() -> None:
    out = render("Text [S1].")
    assert appendix_fence(out.markdown) == BRIEF
    assert out.markdown.rstrip().endswith("Archiviert unter data/briefs/2026-10-02T09-30-15Z.md.")


@pytest.mark.parametrize("ticks", ["```", "````", "```` ```"])
def test_the_fence_is_longer_than_any_backtick_run_in_the_brief(ticks: str) -> None:
    brief = f"# T\n\n{ticks}\ncode\n{ticks}\n\n1. Frage\n"
    out = render("Text [S1].", brief=brief)
    assert appendix_fence(out.markdown) == brief
    assert h2_list(out.markdown)[-1].startswith("Anhang")


def test_source_from_note_reads_the_search_and_page_metadata() -> None:
    note = make_note(
        url="https://www.example.org/p?utm_source=x",
        title="Ein   Titel",
        meta={"authors": ["Ada Lovelace"], "year": 2020, "venue": "Nature", "is_retracted": True},
        created_at="2026-10-01T08:00:00+00:00",
    )
    source = source_from_note(note)
    assert source == ReportSource(
        note_id="n0001",
        url="https://www.example.org/p?utm_source=x",
        title="Ein Titel",
        authors=("Ada Lovelace",),
        publisher="Nature",
        year=2020,
        retrieved="2026-10-01",
        retracted=True,
    )
    bare = source_from_note(make_note(meta={}))
    assert (bare.authors, bare.publisher, bare.year, bare.retracted) == ((), None, None, False)
