"""The ship gate's fixes that ask a model (PRD §3.10, D11): checked by code before they apply."""

from pathlib import Path
from typing import Any

import pytest
from research_rig import FORMATS, TEMPLATES, ResearchModels, Wired, wired

from app.llm.errors import LLMOutputError, LLMUnavailableError
from app.research.draft import DraftPlan
from app.research.sections import load_section, save_section
from app.templates import Section

SECTION = Section("Dauer", "Wie lange dauert es?")
PLAN_SECTIONS = (SECTION, *TEMPLATES["technische-stellungnahme"].sections[1:3])


def plan(w: Wired) -> DraftPlan:
    return DraftPlan(
        title="Der Titel",
        questions=("Wie lange dauert der Rückbau?",),
        sections=PLAN_SECTIONS,
        language="de",
        fmt=FORMATS.structured,
        must_read=tuple(w.ids),
        shim="Drafting posture. Voice: Analyze.",
    )


def hunk(old: str, new: str) -> dict[str, str]:
    return {"old": old, "new": new, "reason": ""}


def setup(tmp_path: Path, text: str, **models: Any) -> tuple[Wired, Path]:
    w = wired(tmp_path, ResearchModels(**models))
    run_dir = tmp_path / "run"
    save_section(run_dir, 1, text)
    return w, run_dir


def rejected(w: Wired) -> list[tuple[str, str]]:
    return [(e.data["fix"], e.data["reason"]) for e in w.events.of_type("fix_rejected")]


# ---- compress -------------------------------------------------------------------------------

LONG = "Der Rückbau dauert zehn Jahre [S1]. " + "Weitere Angaben folgen hier [S2]. " * 5


def test_compress_replaces_the_section_with_a_shorter_text_that_keeps_its_citations(
    tmp_path: Path,
) -> None:
    w, run_dir = setup(tmp_path, LONG, texts={"Dauer": "Der Rückbau dauert zehn Jahre [S1]."})
    assert w.fixes.compress(run_dir, 1, "Dauer", 7) is True
    assert load_section(run_dir, 1) == "Der Rückbau dauert zehn Jahre [S1]."
    prompt = w.models.prompts["text"][0]
    assert "Shorten this section to about 7 words" in prompt
    assert "Section: Dauer" in prompt
    assert LONG.strip() in prompt


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (LONG + "Mehr.", "not_shorter"),
        ("Kurz [S5].", "new_citation"),
        ("## Nur Überschrift", None),
    ],
)
def test_compress_rejects_a_text_that_is_not_shorter_or_cites_something_new(
    tmp_path: Path, answer: str, reason: str | None
) -> None:
    w, run_dir = setup(tmp_path, LONG, texts={"Dauer": answer})
    assert w.fixes.compress(run_dir, 1, "Dauer", 7) is False
    assert load_section(run_dir, 1) == LONG.strip()
    assert rejected(w) == ([("compress", reason)] if reason else [])


def test_compress_and_expand_need_a_real_change_in_length(tmp_path: Path) -> None:
    same = "Eins zwei drei vier [S1]."
    w, run_dir = setup(tmp_path, same, texts={"Dauer": "Fünf sechs sieben acht [S1]."})
    assert w.fixes.compress(run_dir, 1, "Dauer", 3) is False
    assert w.fixes.expand(run_dir, plan(w), 1, 30) is False
    assert rejected(w) == [("compress", "not_shorter"), ("expand", "not_longer")]


def test_compress_survives_unusable_model_output_and_stops_for_an_unavailable_model(
    tmp_path: Path,
) -> None:
    w, run_dir = setup(tmp_path, LONG, errors={"text": LLMOutputError("bad", raw="x")})
    assert w.fixes.compress(run_dir, 1, "Dauer", 7) is False
    assert rejected(w) == [("compress", "LLMOutputError")]
    w2, run_dir2 = setup(tmp_path / "b", LONG, errors={"text": LLMUnavailableError("down")})
    with pytest.raises(LLMUnavailableError):
        w2.fixes.compress(run_dir2, 1, "Dauer", 7)


# ---- expand ---------------------------------------------------------------------------------


def test_expand_extends_the_section_from_its_evidence(tmp_path: Path) -> None:
    longer = "Der Rückbau dauert zehn Jahre [S1]. Die Quelle bestätigt das [S2]."
    w, run_dir = setup(tmp_path, "Der Rückbau dauert zehn Jahre [S1].", texts={"Dauer": longer})
    assert w.fixes.expand(run_dir, plan(w), 1, 300) is True
    assert load_section(run_dir, 1) == longer
    prompt = w.models.prompts["text"][0]
    assert "Extend the section to about 300 words" in prompt
    assert "Der Rückbau dauert zehn Jahre [S1]." in prompt  # the current text
    assert '<untrusted-source url="https://seed1.example.org/a">' in prompt  # the evidence
    assert "Section instructions: Wie lange dauert es?" in prompt


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        ("Zu kurz.", "not_longer"),
        ("Der Rückbau dauert zehn Jahre [S1]. Und mehr [S9].", "unknown_citation"),
    ],
)
def test_expand_rejects_a_text_that_is_not_longer_or_cites_unknown_evidence(
    tmp_path: Path, answer: str, reason: str
) -> None:
    w, run_dir = setup(tmp_path, "Der Rückbau dauert zehn Jahre [S1].", texts={"Dauer": answer})
    assert w.fixes.expand(run_dir, plan(w), 1, 300) is False
    assert rejected(w) == [("expand", reason)]
    assert load_section(run_dir, 1) == "Der Rückbau dauert zehn Jahre [S1]."


def test_expand_needs_evidence(tmp_path: Path) -> None:
    w = wired(tmp_path, notes=0)
    run_dir = tmp_path / "run"
    save_section(run_dir, 1, "Text.")
    assert w.fixes.expand(run_dir, plan(w), 1, 300) is False
    assert rejected(w) == [("expand", "no_evidence")]
    assert w.models.calls == {}


# ---- citations ------------------------------------------------------------------------------

SOURCES = {"S1": "Quelle 1: Zusammenfassung 1", "S2": "Quelle 2: Zusammenfassung 2"}
UNCITED = "Der Rückbau dauert zehn Jahre. Die Kosten sind hoch."


def test_citations_are_added_by_hunks_that_only_add_known_keys(tmp_path: Path) -> None:
    hunks = [
        hunk("Der Rückbau dauert zehn Jahre.", "Der Rückbau dauert zehn Jahre [S1]."),
        hunk("Die Kosten sind hoch.", "Die Kosten sind hoch [S2]."),
    ]
    w, run_dir = setup(tmp_path, UNCITED, answers={"CitationProposal": [{"hunks": hunks}]})
    assert w.fixes.add_citations(run_dir, 1, "Dauer", SOURCES) == 2
    assert (
        load_section(run_dir, 1) == "Der Rückbau dauert zehn Jahre [S1]. Die Kosten sind hoch [S2]."
    )
    prompt = w.models.prompts["CitationProposal"][0]
    assert "S1: Quelle 1: Zusammenfassung 1" in prompt
    assert UNCITED in prompt


def test_a_citation_hunk_that_changes_a_word_or_uses_an_unknown_key_is_rejected(
    tmp_path: Path,
) -> None:
    hunks = [
        hunk("Der Rückbau dauert zehn Jahre.", "Der Rückbau dauert elf Jahre [S1]."),
        hunk("Die Kosten sind hoch.", "Die Kosten sind hoch [S9]."),
        hunk("Es gibt nichts.", "Es gibt nichts [S1]."),
    ]
    w, run_dir = setup(tmp_path, UNCITED, answers={"CitationProposal": [{"hunks": hunks}]})
    assert w.fixes.add_citations(run_dir, 1, "Dauer", SOURCES) == 0
    assert load_section(run_dir, 1) == UNCITED
    assert rejected(w) == [
        ("cite", "not_only_citations"),
        ("cite", "not_only_citations"),
        ("cite", "not_found"),
    ]


def test_a_section_is_not_rewritten_when_no_hunk_applies(tmp_path: Path) -> None:
    w, run_dir = setup(
        tmp_path, UNCITED, answers={"CitationProposal": [{"hunks": [hunk("gibt es nicht", "x")]}]}
    )
    path = run_dir / "temp" / "sections" / "01.md"
    path.write_text(UNCITED, encoding="utf-8")  # no trailing newline: a rewrite would add one
    assert w.fixes.add_citations(run_dir, 1, "Dauer", SOURCES) == 0
    assert path.read_text(encoding="utf-8") == UNCITED


def test_citation_repair_survives_unusable_model_output(tmp_path: Path) -> None:
    w, run_dir = setup(
        tmp_path, UNCITED, errors={"CitationProposal": LLMOutputError("bad", raw="x")}
    )
    assert w.fixes.add_citations(run_dir, 1, "Dauer", SOURCES) == 0
    assert rejected(w) == [("cite", "LLMOutputError")]


# ---- vocabulary -----------------------------------------------------------------------------

LEAKY = "Der Rückbau dauert zehn Jahre [S1]. Das ist nur interim gültig [S1]. Siehe Locus 3 dazu."


def test_sentences_with_pipeline_vocabulary_are_reworded_or_deleted(tmp_path: Path) -> None:
    hunks = [
        hunk("Das ist nur interim gültig [S1].", "Das gilt vorläufig [S1]."),
        hunk("Siehe Locus 3 dazu.", ""),
    ]
    w, run_dir = setup(tmp_path, LEAKY, answers={"LeakProposal": [{"hunks": hunks}]})
    assert w.fixes.reword_leaks(run_dir, 1, "Dauer") == 2
    assert (
        load_section(run_dir, 1) == "Der Rückbau dauert zehn Jahre [S1]. Das gilt vorläufig [S1]."
    )
    prompt = w.models.prompts["LeakProposal"][0]
    assert "- Das ist nur interim gültig [S1]." in prompt
    assert "- Siehe Locus 3 dazu." in prompt


def test_deleting_a_sentence_in_the_middle_leaves_one_space(tmp_path: Path) -> None:
    text = "Anfang gut. Siehe Locus 3 dazu. Ende gut."
    answers = {"LeakProposal": [{"hunks": [hunk("Siehe Locus 3 dazu.", "")]}]}
    w, run_dir = setup(tmp_path, text, answers=answers)
    assert w.fixes.reword_leaks(run_dir, 1, "Dauer") == 1
    assert load_section(run_dir, 1) == "Anfang gut. Ende gut."


def test_a_section_is_not_rewritten_when_no_leak_hunk_applies(tmp_path: Path) -> None:
    answers = {"LeakProposal": [{"hunks": [hunk("Siehe Locus 3 dazu.", "Siehe Locus 4.")]}]}
    w, run_dir = setup(tmp_path, LEAKY, answers=answers)
    path = run_dir / "temp" / "sections" / "01.md"
    path.write_text(LEAKY, encoding="utf-8")
    assert w.fixes.reword_leaks(run_dir, 1, "Dauer") == 0
    assert path.read_text(encoding="utf-8") == LEAKY


def test_a_rewording_that_still_leaks_or_adds_a_citation_is_rejected(tmp_path: Path) -> None:
    hunks = [
        hunk("Das ist nur interim gültig [S1].", "Das gilt interim [S1]."),
        hunk("Siehe Locus 3 dazu.", "Siehe die Quelle [S2]."),
    ]
    w, run_dir = setup(tmp_path, LEAKY, answers={"LeakProposal": [{"hunks": hunks}]})
    assert w.fixes.reword_leaks(run_dir, 1, "Dauer") == 0
    assert load_section(run_dir, 1) == LEAKY
    assert rejected(w) == [("leak", "still_leaks"), ("leak", "new_citation")]


def test_a_clean_section_is_not_sent_to_the_model(tmp_path: Path) -> None:
    w, run_dir = setup(tmp_path, "Alles in Ordnung [S1].")
    assert w.fixes.reword_leaks(run_dir, 1, "Dauer") == 0
    assert w.models.calls == {}


def test_vocabulary_repair_survives_unusable_model_output(tmp_path: Path) -> None:
    w, run_dir = setup(tmp_path, LEAKY, errors={"LeakProposal": LLMOutputError("bad", raw="x")})
    assert w.fixes.reword_leaks(run_dir, 1, "Dauer") == 0
    assert rejected(w) == [("leak", "LLMOutputError")]
