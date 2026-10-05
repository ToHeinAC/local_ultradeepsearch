"""Step 1 (PRD M5): the approved brief broken into items, checked for coverage, rendered into
shims and a scaffold."""

import json
from pathlib import Path

import pytest
from research_rig import (
    AUTO_SETTINGS,
    BRIEF,
    CLEAN_MATRIX,
    DRAFT,
    NOW,
    RULES,
    RUN_SETTINGS,
    TEMPLATES,
    ResearchModels,
    llm,
)

from app.events import MemoryEventSink
from app.llm.errors import LLMUnavailableError
from app.research.decompose import Decomposer, clean_headings, write_artifacts
from app.research.models import Decomposition, atomic_items

TECH = TEMPLATES["technische-stellungnahme"]
AUTO = TEMPLATES["auto"]


def make(models: ResearchModels) -> tuple[Decomposer, MemoryEventSink]:
    events = MemoryEventSink()
    return Decomposer(llm(models, events), RULES, events), events


def test_the_questions_come_from_the_brief_and_the_choices_from_the_settings() -> None:
    decomposer, _ = make(ResearchModels())
    result = decomposer.decompose(brief=BRIEF, settings=RUN_SETTINGS, template=TECH)
    d = result.decomposition
    assert d.sub_questions == ["Wie lange dauert der Rückbau?", "Welche Genehmigungen sind nötig?"]
    assert (d.pipeline_tier, d.response_format) == ("light", "structured")
    assert d.domains == ["regulation_de_eu"]
    assert d.levers.domain_notes.startswith("Behördliche Quellen")
    assert [e.name for e in d.entities] == ["Forschungsreaktor"]


def test_a_fixed_template_decides_the_headings_and_the_model_is_not_asked() -> None:
    models = ResearchModels(drafts=[{**DRAFT, "required_section_headings": ["Erfunden"]}])
    decomposer, _ = make(models)
    d = decomposer.decompose(brief=BRIEF, settings=RUN_SETTINGS, template=TECH).decomposition
    assert d.required_section_headings == list(TECH.headings)
    assert "leave required_section_headings empty" in models.prompts["DecompositionDraft"][0]


def test_auto_asks_for_headings_with_the_configured_bounds_and_cleans_them() -> None:
    raw = ["## 1. Hintergrund", "Genehmigungen", "genehmigungen", "Quellen", "", "Anhang B"]
    models = ResearchModels(drafts=[{**DRAFT, "required_section_headings": raw}])
    decomposer, _ = make(models)
    d = decomposer.decompose(brief=BRIEF, settings=AUTO_SETTINGS, template=AUTO).decomposition
    assert d.required_section_headings == ["1. Hintergrund", "Genehmigungen"]
    prompt = models.prompts["DecompositionDraft"][0]
    assert "between 2 and 15" in prompt


def test_clean_headings_falls_back_to_the_questions_when_the_model_gives_too_few() -> None:
    questions = ["Wie lange dauert der Rückbau?", "Welche Genehmigungen sind nötig?"]
    assert clean_headings(["Nur eine"], questions) == [
        "Wie lange dauert der Rückbau",
        "Welche Genehmigungen sind nötig",
    ]
    assert clean_headings([], ["x" * 200, "y"])[0] == "x" * 80


@pytest.mark.parametrize(
    ("question", "heading"),
    [  # from the first live Lite run, where the headings ended mid-word ("... in deutschen Bestan")
        (
            "Welche Jahresarbeitszahlen erreichen Luft-Wasser-Wärmepumpen in deutschen "
            "Bestandsgebäuden im Feldbetrieb?",
            "Welche Jahresarbeitszahlen erreichen Luft-Wasser-Wärmepumpen in deutschen",
        ),
        (
            "Welche Voraussetzungen (Dämmstandard, Vorlauftemperatur, Heizflächen) "
            "beeinflussen die Effizienz?",
            "Welche Voraussetzungen (Dämmstandard, Vorlauftemperatur, Heizflächen)",
        ),
        (
            "Wie hoch sind Investitions- und Betriebskosten, im Vergleich zu "
            + "Gasheizungen " * 5,
            "Wie hoch sind Investitions- und Betriebskosten, im Vergleich zu Gasheizungen",
        ),
    ],
)
def test_a_long_fallback_heading_is_cut_at_a_word_not_in_the_middle_of_one(
    question: str, heading: str
) -> None:
    result = clean_headings([], [question, "Zweite Frage"])[0]
    assert result == heading
    assert len(result) <= 80


def test_the_fallback_headings_are_unique_and_limited_too() -> None:
    assert clean_headings([], ["Gleiche Frage?", "Gleiche Frage", "Andere"]) == [
        "Gleiche Frage",
        "Andere",
    ]
    assert len(clean_headings([], [f"Frage {n}" for n in range(30)])) == 15


def test_clean_headings_keeps_at_most_the_template_maximum() -> None:
    raw = [f"Teil {n}" for n in range(30)]
    assert len(clean_headings(raw, ["Frage"])) == 15


def test_a_gap_in_the_matrix_sends_the_phrase_back_for_another_breakdown() -> None:
    gap = {"rows": [{"phrase": "Strahlenschutz", "items": [], "scope_ok": True}]}
    models = ResearchModels(matrices=[gap, CLEAN_MATRIX])
    decomposer, events = make(models)
    result = decomposer.decompose(brief=BRIEF, settings=RUN_SETTINGS, template=TECH)
    assert (models.count("DecompositionDraft"), models.count("CoverageMatrix")) == (2, 2)
    assert "Strahlenschutz" in models.prompts["DecompositionDraft"][1]
    assert "Strahlenschutz" not in models.prompts["DecompositionDraft"][0]
    assert result.gaps == ()
    assert not events.of_type("coverage_gaps_remaining")


def test_unknown_item_ids_and_a_narrowed_scope_count_as_gaps() -> None:
    rows = {
        "rows": [
            {"phrase": "A", "items": ["Q9"], "scope_ok": True},
            {"phrase": "B", "items": ["Q1"], "scope_ok": False},
            {"phrase": "C", "items": ["Q1", "Q9"], "scope_ok": True},
        ]
    }
    models = ResearchModels(matrices=[rows])
    decomposer, _ = make(models)
    result = decomposer.decompose(brief=BRIEF, settings=RUN_SETTINGS, template=TECH)
    assert result.gaps == ("A", "B")


def test_gaps_that_remain_after_the_last_iteration_are_reported() -> None:
    gap = {"rows": [{"phrase": "Strahlenschutz", "items": [], "scope_ok": True}]}
    models = ResearchModels(matrices=[gap])
    decomposer, events = make(models)
    result = decomposer.decompose(brief=BRIEF, settings=RUN_SETTINGS, template=TECH)
    assert models.count("CoverageMatrix") == RULES.coverage_matrix_max_iterations == 3
    assert models.count("DecompositionDraft") == 3
    assert result.gaps == ("Strahlenschutz",)
    (event,) = events.of_type("coverage_gaps_remaining")
    assert event.level == "warning"
    assert event.data["phrases"] == ["Strahlenschutz"]


def test_a_different_tier_recommendation_is_recorded_but_never_applied() -> None:
    models = ResearchModels(drafts=[{**DRAFT, "tier_recommendation": "full"}])
    decomposer, events = make(models)
    d = decomposer.decompose(brief=BRIEF, settings=RUN_SETTINGS, template=TECH).decomposition
    assert (d.pipeline_tier, d.tier_recommendation) == ("light", "full")
    (event,) = events.of_type("tier_recommendation_differs")
    assert event.data == {"chosen": "light", "recommended": "full"}


def test_an_agreeing_tier_recommendation_is_not_an_event() -> None:
    decomposer, events = make(ResearchModels())
    decomposer.decompose(brief=BRIEF, settings=RUN_SETTINGS, template=TECH)
    assert not events.of_type("tier_recommendation_differs")


def test_sections_demanded_by_the_brief_lose_against_a_template() -> None:
    models = ResearchModels(drafts=[{**DRAFT, "required_sections": ["Ein Fazit"]}])
    decomposer, events = make(models)
    decomposer.decompose(brief=BRIEF, settings=RUN_SETTINGS, template=TECH)
    assert events.of_type("template_overrides_brief")
    decomposer2, events2 = make(models)
    decomposer2.decompose(brief=BRIEF, settings=AUTO_SETTINGS, template=AUTO)
    assert not events2.of_type("template_overrides_brief")


def test_a_model_failure_propagates_so_the_run_can_resume() -> None:
    models = ResearchModels(errors={"DecompositionDraft": LLMUnavailableError("down")})
    decomposer, _ = make(models)
    with pytest.raises(LLMUnavailableError):
        decomposer.decompose(brief=BRIEF, settings=RUN_SETTINGS, template=TECH)


def test_atomic_items_have_stable_ids_by_kind() -> None:
    models = ResearchModels(
        drafts=[
            {
                **DRAFT,
                "time_periods": [{"period": "Q3 2024", "primary_source": "10-Q", "issuer": "Acme"}],
            }
        ]
    )
    decomposer, _ = make(models)
    d = decomposer.decompose(brief=BRIEF, settings=RUN_SETTINGS, template=TECH).decomposition
    assert [(i.id, i.kind) for i in atomic_items(d)] == [
        ("Q1", "question"),
        ("Q2", "question"),
        ("E1", "entity"),
        ("P1", "period"),
    ]
    assert atomic_items(d)[-1].text == "Q3 2024, 10-Q, Acme"


# ---- the files ------------------------------------------------------------------------------


def written(tmp_path: Path, models: ResearchModels | None = None, brief: str = BRIEF) -> Path:
    decomposer, _ = make(models or ResearchModels())
    result = decomposer.decompose(brief=brief, settings=RUN_SETTINGS, template=TECH)
    write_artifacts(tmp_path, brief, RUN_SETTINGS, TECH, result, NOW)
    return tmp_path


def test_the_decomposition_file_reads_back(tmp_path: Path) -> None:
    run_dir = written(tmp_path)
    loaded = Decomposition.model_validate_json(
        (run_dir / "prompt-decomposition.json").read_text(encoding="utf-8")
    )
    assert loaded.sub_questions[0] == "Wie lange dauert der Rückbau?"


def test_the_matrix_file_lists_phrases_and_flags_gaps(tmp_path: Path) -> None:
    gap = {"rows": [{"phrase": "Strahlenschutz", "items": [], "scope_ok": True}]}
    run_dir = written(tmp_path, ResearchModels(matrices=[gap]))
    text = (run_dir / "temp" / "coverage-matrix.md").read_text(encoding="utf-8")
    assert "| Strahlenschutz |" in text
    assert "**YES**" in text
    clean = written(tmp_path / "clean")
    assert "**YES**" not in (clean / "temp" / "coverage-matrix.md").read_text(encoding="utf-8")


def test_the_shims_carry_the_posture_and_the_scaffold_the_brief(tmp_path: Path) -> None:
    run_dir = written(tmp_path)
    research = (run_dir / "shims" / "research.md").read_text(encoding="utf-8")
    drafting = (run_dir / "shims" / "drafting.md").read_text(encoding="utf-8")
    polish = (run_dir / "shims" / "polish.md").read_text(encoding="utf-8")
    assert "Behördliche Quellen zuerst" in research
    assert "Analyze:" in drafting
    assert "Analyze:" in polish
    assert not (run_dir / "shims" / "critics.md").exists()
    scaffold = (run_dir / "scaffold.md").read_text(encoding="utf-8")
    assert BRIEF in scaffold
    assert "Eine begrenzte Frage mit klarer Antwort." in scaffold
    assert "technische-stellungnahme" in scaffold
    assert "Coverage gaps" not in scaffold


def test_the_owners_register_line_goes_into_the_drafting_and_polish_shims(tmp_path: Path) -> None:
    brief = BRIEF.replace(
        "## Ausgabe\n\n", "## Ausgabe\n\n- **Register:** nüchtern, für Ingenieure\n"
    )
    run_dir = written(tmp_path, brief=brief)
    for name in ("drafting", "polish"):
        text = (run_dir / "shims" / f"{name}.md").read_text(encoding="utf-8")
        assert "nüchtern, für Ingenieure" in text
    assert "nüchtern" not in (run_dir / "shims" / "research.md").read_text(encoding="utf-8")


def test_remaining_gaps_are_listed_in_the_scaffold(tmp_path: Path) -> None:
    gap = {"rows": [{"phrase": "Strahlenschutz", "items": [], "scope_ok": True}]}
    run_dir = written(tmp_path, ResearchModels(matrices=[gap]))
    scaffold = (run_dir / "scaffold.md").read_text(encoding="utf-8")
    assert "Coverage gaps" in scaffold
    assert "Strahlenschutz" in scaffold


def test_writing_twice_gives_the_same_files(tmp_path: Path) -> None:
    written(tmp_path)
    first = {p.name: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    written(tmp_path)
    assert {p.name: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == first
    assert json.loads((tmp_path / "prompt-decomposition.json").read_text("utf-8"))
