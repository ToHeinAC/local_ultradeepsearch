"""Section files and the report rendered from them."""

from pathlib import Path

import pytest
from research_rig import NOW, seed_note

from app.events import MemoryEventSink
from app.research.markdown import appendix_fence, h2_list, sources_entries
from app.research.report import ApprovedBrief
from app.research.sections import (
    load_section,
    load_sections,
    save_section,
    section_path,
    sources_of_keys,
    write_report,
)
from app.store.models import SourceMeta
from app.store.vault import Vault

BRIEF = "# Frage\n\n## Forschungsfragen\n\n1. Wie?\n"
APPROVED = ApprovedBrief(BRIEF, NOW, "data/briefs/x.md")


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return Vault(tmp_path / "udr.sqlite", "run-a")


def test_a_section_is_stored_trimmed_in_a_numbered_file(tmp_path: Path) -> None:
    save_section(tmp_path, 2, "\n  Ein Text [S1].  \n\n")
    assert section_path(tmp_path, 2).name == "02.md"
    assert section_path(tmp_path, 2).read_text(encoding="utf-8") == "Ein Text [S1].\n"
    assert load_section(tmp_path, 2) == "Ein Text [S1]."
    assert load_section(tmp_path, 1) is None


def test_sections_are_stripped_of_thinking_when_saved(tmp_path: Path) -> None:
    save_section(tmp_path, 1, "<think>geheim</think>Sichtbar [S1].")
    assert load_section(tmp_path, 1) == "Sichtbar [S1]."


def test_missing_sections_load_as_empty_text_in_heading_order(tmp_path: Path) -> None:
    save_section(tmp_path, 2, "Zweiter [S1].")
    loaded = load_sections(tmp_path, ["Eins", "Zwei", "Drei"])
    assert [(s.heading, s.text) for s in loaded] == [
        ("Eins", ""),
        ("Zwei", "Zweiter [S1]."),
        ("Drei", ""),
    ]


def test_sources_of_keys_reads_the_notes_and_skips_unknown_ones(vault: Vault) -> None:
    note = seed_note(
        vault, 1, title="Eine Quelle", meta=SourceMeta(authors=("A. Autor",), year=2021)
    )
    found = sources_of_keys(vault, {"S1": note.note_id, "S2": "n9999"})
    assert list(found) == ["S1"]
    assert (found["S1"].title, found["S1"].authors, found["S1"].year) == (
        "Eine Quelle",
        ("A. Autor",),
        2021,
    )


def test_the_report_is_rendered_from_the_section_files(tmp_path: Path, vault: Vault) -> None:
    note = seed_note(vault, 1, title="Eine Quelle", meta=SourceMeta(year=2021))
    save_section(tmp_path, 1, "Erster Text [S1].")
    save_section(tmp_path, 2, "Zweiter Text [S1].")
    events = MemoryEventSink()
    rendered = write_report(
        tmp_path,
        title="Der Titel",
        headings=["Eins", "Zwei"],
        keys={"S1": note.note_id},
        vault=vault,
        language="de",
        brief=APPROVED,
        events=events,
    )
    on_disk = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert on_disk == rendered.markdown
    assert h2_list(on_disk) == ["Eins", "Zwei", "Quellen", "Anhang A — Recherche-Brief"]
    assert "Erster Text [1]." in on_disk
    assert "Eine Quelle" in sources_entries(on_disk)[1]
    assert appendix_fence(on_disk) == BRIEF
    assert not events.of_type("citations_dropped")


def test_citations_of_keys_without_evidence_are_dropped_and_reported(
    tmp_path: Path, vault: Vault
) -> None:
    save_section(tmp_path, 1, "Text [S9].")
    events = MemoryEventSink()
    rendered = write_report(
        tmp_path,
        title="T",
        headings=["Eins"],
        keys={},
        vault=vault,
        language="de",
        brief=APPROVED,
        events=events,
    )
    assert "Text." in rendered.markdown
    (event,) = events.of_type("citations_dropped")
    assert event.level == "warning"
    assert event.data["keys"] == ["S9"]


def test_the_appendix_keeps_the_brief_even_if_it_mentions_thinking_tags(
    tmp_path: Path, vault: Vault
) -> None:
    brief = ApprovedBrief("# T\n\n1. Was ist <think> hier?\n", NOW, "x")
    save_section(tmp_path, 1, "Text.")
    write_report(
        tmp_path,
        title="T",
        headings=["Eins"],
        keys={},
        vault=vault,
        language="de",
        brief=brief,
        events=MemoryEventSink(),
    )
    assert appendix_fence((tmp_path / "report.md").read_text(encoding="utf-8")) == brief.text
