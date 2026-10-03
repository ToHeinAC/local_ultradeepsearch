"""Step 10 (PRD M5, AD4): the report written one section at a time from its own evidence."""

from pathlib import Path
from typing import Any

import pytest
from research_rig import (
    FORMATS,
    RULES,
    TEMPLATES,
    ModelCrash,
    ResearchModels,
    claim,
    llm,
    seed_note,
)

from app.events import MemoryEventSink
from app.llm.errors import LLMUnavailableError
from app.pipeline.profiles import FormatRange
from app.research.draft import (
    NO_EVIDENCE,
    Drafter,
    DraftPlan,
    clean_section,
    sections_of,
    words_per_section,
)
from app.research.evidence import EvidenceKeys, PackBuilder
from app.research.models import Decomposition, Levers
from app.research.sections import load_section, save_section
from app.store.vault import Vault
from app.templates import Section

TECH = TEMPLATES["technische-stellungnahme"]
QUESTIONS = ["Wie lange dauert der Rückbau?", "Welche Genehmigungen sind nötig?"]
SHIM = "Drafting posture. Voice: Analyze: weigh the evidence."


def rig(tmp_path: Path, models: ResearchModels | None = None, *, notes: int = 2):
    vault = Vault(tmp_path / "udr.sqlite", "run-a")
    ids = [
        seed_note(
            vault,
            n,
            title=f"Quelle {n}",
            body="Der Rückbau dauert zehn Jahre.",
            summary=f"Zusammenfassung {n}",
            claims=[claim("Der Rückbau dauert zehn Jahre.", "Der Rückbau dauert zehn Jahre.")],
        ).note_id
        for n in range(1, notes + 1)
    ]
    events = MemoryEventSink()
    models = models or ResearchModels()
    service = llm(models, events)
    keys = EvidenceKeys(tmp_path / "keys.json")
    packs = PackBuilder(vault, keys, service, RULES, events)
    drafter = Drafter(
        service, packs, keys, RULES, events, prompt_chars=40_000, condense_chars=20_000
    )
    return drafter, ids, models, events, keys


def plan_of(
    ids: list[str],
    sections: list[Section] | None = None,
    *,
    language: str = "de",
    fmt: FormatRange = FORMATS.structured,
) -> DraftPlan:
    return DraftPlan(
        title="Der Titel",
        questions=tuple(QUESTIONS),
        sections=tuple(sections or TECH.sections),
        language=language,
        fmt=fmt,
        must_read=tuple(ids),
        shim=SHIM,
    )


def draft(
    drafter: Drafter,
    tmp_path: Path,
    ids: list[str],
    sections: list[Section] | None = None,
    **kw: Any,
) -> None:
    drafter.draft_all(tmp_path / "run", plan_of(ids, sections, **kw))


def test_the_word_budget_is_the_middle_of_the_range_split_over_the_sections() -> None:
    assert words_per_section(FORMATS.structured, 6) == round(3500 / 6)
    assert words_per_section(FormatRange(words=(500, 1500), citations=(1, 2)), 4) == 250
    assert words_per_section(FORMATS.short, 0) == 1250  # never divides by zero


def test_sections_carry_the_templates_instructions_or_none_for_derived_headings() -> None:
    deco = Decomposition.model_validate(
        {
            "sub_questions": ["F"],
            "entities": [],
            "required_formats": [],
            "required_sections": [],
            "required_section_headings": [TECH.headings[1], "Abgeleitet"],
            "time_horizons": [],
            "time_periods": [],
            "scope_conditions": [],
            "domains": [],
            "pipeline_tier": "light",
            "tier_recommendation": "light",
            "tier_rationale": "",
            "response_format": "short",
            "modality": "collect",
            "levers": Levers().model_dump(),
        }
    )
    found = sections_of(deco, TECH.sections)
    assert found[0] == TECH.sections[1]
    assert found[1] == Section("Abgeleitet", "")


def test_every_section_is_one_model_call_with_its_own_prompt(tmp_path: Path) -> None:
    drafter, ids, models, _, _ = rig(tmp_path)
    draft(drafter, tmp_path, ids)
    assert models.count("text") == len(TECH.sections) == 6
    first = models.prompts["text"][0]
    assert "Section to write: Management Summary" in first
    assert "Section instructions: Die Antwort auf die Fragestellung" in first
    assert "Write about 583 words" in first
    assert "Report language: German" in first
    assert SHIM in first
    assert "> Management Summary" in first
    assert "- Fragestellung und Abgrenzung" in first
    assert "Report title: Der Titel" in first
    assert "1. Wie lange dauert der Rückbau?" in first
    assert '<untrusted-source url="https://seed1.example.org/a">' in first
    assert "[S1] Quelle 1" in first
    assert "> Fragestellung und Abgrenzung" in models.prompts["text"][1]
    assert "untrusted data" in first  # the system prompt fences the sources


def test_sections_without_instructions_say_so_and_drafting_does_not_think(tmp_path: Path) -> None:
    drafter, ids, models, _, _ = rig(tmp_path)
    draft(drafter, tmp_path, ids, [Section("Abgeleitet", "")])
    assert "Section instructions: none; cover what the heading says" in models.prompts["text"][0]
    assert models.thinks["text"] == [False]


def test_sections_are_saved_one_by_one_in_numbered_files(tmp_path: Path) -> None:
    drafter, ids, _, _, _ = rig(
        tmp_path, ResearchModels(texts={"Management Summary": "Kurz [S1]."})
    )
    draft(drafter, tmp_path, ids)
    assert load_section(tmp_path / "run", 1) == "Kurz [S1]."
    assert load_section(tmp_path / "run", 6) is not None


def test_a_resumed_draft_continues_with_the_first_missing_section(tmp_path: Path) -> None:
    drafter, ids, models, _, _ = rig(tmp_path)
    draft(drafter, tmp_path, ids, list(TECH.sections)[:2])
    assert models.count("text") == 2
    first = load_section(tmp_path / "run", 1)
    drafter2, ids2, models2, _, _ = rig(
        tmp_path, ResearchModels(texts={"Management Summary": "neu"})
    )
    draft(drafter2, tmp_path, ids2)
    assert models2.count("text") == 4  # sections three to six
    assert load_section(tmp_path / "run", 1) == first  # a saved section is never rewritten


def test_a_crash_between_sections_loses_only_the_section_in_flight(tmp_path: Path) -> None:
    drafter, ids, _, _, _ = rig(tmp_path, ResearchModels(crash_on={"text": 3}))
    with pytest.raises(ModelCrash):
        draft(drafter, tmp_path, ids)
    run_dir = tmp_path / "run"
    assert load_section(run_dir, 1) is not None
    assert load_section(run_dir, 2) is not None
    assert load_section(run_dir, 3) is None
    again, ids2, models, _, _ = rig(tmp_path)
    draft(again, tmp_path, ids2)
    assert models.count("text") == 4  # the crashed one and the three after it
    assert load_section(run_dir, 6) is not None


def test_a_section_can_be_written_again_in_the_report_language(tmp_path: Path) -> None:
    models = ResearchModels(texts={"Management Summary": "Ein neuer deutscher Text [S1]."})
    drafter, ids, _, _, _ = rig(tmp_path, models)
    run_dir = tmp_path / "run"
    save_section(run_dir, 1, "An English text [S1].")
    drafter.redraft_in_language(run_dir, plan_of(ids), 1)
    assert load_section(run_dir, 1) == "Ein neuer deutscher Text [S1]."
    prompt = models.prompts["text"][0]
    assert "was not written in German" in prompt
    assert "in no other language" in prompt
    assert models.count("text") == 1


def test_a_model_failure_propagates_and_keeps_what_was_saved(tmp_path: Path) -> None:
    models = ResearchModels(errors={"text": LLMUnavailableError("down")})
    drafter, ids, _, _, _ = rig(tmp_path, models)
    with pytest.raises(LLMUnavailableError):
        draft(drafter, tmp_path, ids)
    assert load_section(tmp_path / "run", 1) is None


def test_a_section_without_any_evidence_states_the_gap_without_asking_the_model(
    tmp_path: Path,
) -> None:
    drafter, _, models, events, _ = rig(tmp_path, notes=0)
    draft(drafter, tmp_path, [])
    assert models.calls == {}
    assert load_section(tmp_path / "run", 1) == NO_EVIDENCE["de"]
    assert len(events.of_type("section_without_evidence")) == 6
    other, _, _, _, _ = rig(tmp_path / "en", notes=0)
    draft(other, tmp_path / "en", [], language="en")
    assert load_section(tmp_path / "en" / "run", 1) == NO_EVIDENCE["en"]


def test_an_answer_that_is_empty_after_cleaning_becomes_the_gap_statement(tmp_path: Path) -> None:
    models = ResearchModels(texts={"Management Summary": "## Nur eine Überschrift"})
    drafter, ids, _, events, _ = rig(tmp_path, models)
    draft(drafter, tmp_path, ids)
    assert load_section(tmp_path / "run", 1) == NO_EVIDENCE["de"]
    assert events.of_type("section_empty")


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Text [S1].", "Text [S1]."),
        ("## Überschrift\n\nText [S1].", "Text [S1]."),
        ("Text.\n\n### Unterpunkt\n\nMehr.", "Text.\n\nMehr."),
        ("```markdown\nText [S1].\n```", "Text [S1]."),
        ("---\ntitle: x\n---\nText [S1].", "Text [S1]."),
        ("Eins.\n\n\n\n\nZwei.", "Eins.\n\nZwei."),
        ("  \n Text.  \n", "Text."),
        ("#kein Heading und #1 Platz", "#kein Heading und #1 Platz"),
    ],
)
def test_model_output_is_made_fit_for_a_section(raw: str, clean: str) -> None:
    assert clean_section(raw) == clean


def test_evidence_that_exceeds_the_budget_is_condensed_not_cut(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "udr.sqlite", "run-a")
    claims = [claim(f"Behauptung {n} zur Dauer " + "wort " * 40, f"Beleg {n}") for n in range(10)]
    ids = [seed_note(vault, 1, summary="Zusammenfassung", claims=claims).note_id]
    events = MemoryEventSink()
    models = ResearchModels()
    service = llm(models, events)
    keys = EvidenceKeys(tmp_path / "keys.json")
    drafter = Drafter(
        service,
        PackBuilder(vault, keys, service, RULES, events),
        keys,
        RULES,
        events,
        prompt_chars=5_000,  # leaves little room for evidence
        condense_chars=20_000,
    )
    draft(drafter, tmp_path, ids)
    assert events.of_type("evidence_condensed")
    assert models.count("CondensedEvidence") >= 1
