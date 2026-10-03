"""Step 16 (PRD M5, D10): readability recommendations, applied only in the allowed categories."""

import json
from pathlib import Path
from typing import Any

import pytest
from research_rig import LIGHT, RULES, SETTINGS, ModelCrash, ResearchModels, llm

from app.events import MemoryEventSink
from app.llm.errors import LLMOutputError, LLMUnavailableError
from app.pipeline.profiles import ResearchBudget
from app.research.readability import DECISIONS, RECOMMENDATIONS, ReadabilityAuditor
from app.research.sections import load_section, save_section

HEADINGS = ["Eins", "Zwei"]
ONE = "Satz eins.\nSatz zwei [S1]."
TWO = "Alt A.\n\n---\n\nAlt B."
NOTHING: dict[str, Any] = {"recommendations": []}


def rec(category: str, current: str, recommended: str) -> dict[str, str]:
    return {
        "category": category,
        "current": current,
        "recommended": recommended,
        "rationale": "lesbarer",
    }


def rig(
    models: ResearchModels, budget: ResearchBudget = LIGHT
) -> tuple[ReadabilityAuditor, MemoryEventSink]:
    events = MemoryEventSink()
    return ReadabilityAuditor(llm(models, events), RULES, budget, events), events


def sections(run_dir: Path) -> None:
    save_section(run_dir, 1, ONE)
    save_section(run_dir, 2, TWO)


def read(run_dir: Path, name: str) -> Any:
    return json.loads((run_dir / name).read_text(encoding="utf-8"))


def test_allowed_recommendations_are_applied_and_both_files_say_what_happened(
    tmp_path: Path,
) -> None:
    sections(tmp_path)
    models = ResearchModels(
        readabilities=[
            {
                "recommendations": [
                    rec("add-whitespace", "Satz eins.\nSatz zwei", "Satz eins.\n\nSatz zwei")
                ]
            },
            {
                "recommendations": [
                    rec("remove-hr", "---", ""),
                    rec("split-sentence", "Alt A.", "Alt. A."),
                ]
            },
        ]
    )
    auditor, _ = rig(models)
    auditor.audit_all(tmp_path, headings=HEADINGS)
    assert load_section(tmp_path, 1) == "Satz eins.\n\nSatz zwei [S1]."
    assert load_section(tmp_path, 2) == "Alt A.\n\n\n\nAlt B."
    recs = read(tmp_path, RECOMMENDATIONS)
    assert [(r["id"], r["section"], r["category"]) for r in recs] == [
        ("r01", 1, "add-whitespace"),
        ("r02", 2, "remove-hr"),
        ("r03", 2, "split-sentence"),
    ]
    decisions = read(tmp_path, DECISIONS)
    assert decisions["applied"] == ["r01", "r02"]
    assert decisions["skipped"] == [{"id": "r03", "reason": "category_not_allowed"}]
    assert decisions["edit_failures"] == []
    assert decisions["total_recommendations"] == 3
    assert decisions["net_char_delta_actual"] == 1 - 3
    assert decisions["sections_done"] == [1, 2]


def test_a_recommendation_whose_text_is_not_in_the_section_is_an_edit_failure(
    tmp_path: Path,
) -> None:
    sections(tmp_path)
    models = ResearchModels(
        readabilities=[
            {"recommendations": [rec("add-whitespace", "gibt es nicht", "gibt  es nicht")]},
            NOTHING,
        ]
    )
    auditor, _ = rig(models)
    auditor.audit_all(tmp_path, headings=HEADINGS)
    assert read(tmp_path, DECISIONS)["edit_failures"] == [{"id": "r01", "reason": "not_found"}]
    assert load_section(tmp_path, 1) == ONE


def test_a_recommendation_that_changes_words_or_touches_a_heading_is_skipped(
    tmp_path: Path,
) -> None:
    sections(tmp_path)
    bad = [
        rec("add-whitespace", "Satz eins.", "Satz drei."),
        rec("merge-paragraphs", "Satz eins.\nSatz zwei", "Satz eins. Satz zwei"),
    ]
    auditor, _ = rig(ResearchModels(readabilities=[{"recommendations": bad}, NOTHING]))
    auditor.audit_all(tmp_path, headings=HEADINGS)
    assert [s["reason"] for s in read(tmp_path, DECISIONS)["skipped"]] == [
        "bad_change",
        "bad_change",
    ]
    assert load_section(tmp_path, 1) == ONE


def test_the_total_number_of_recommendations_is_capped_by_the_profile(tmp_path: Path) -> None:
    sections(tmp_path)
    many = [rec("add-whitespace", "Satz eins.\nSatz zwei", "Satz eins.\n\nSatz zwei")] * 5
    budget = LIGHT.model_copy(update={"readability_cap": 3})
    models = ResearchModels(readabilities=[{"recommendations": many}])
    auditor, events = rig(models, budget)
    auditor.audit_all(tmp_path, headings=HEADINGS)
    assert len(read(tmp_path, RECOMMENDATIONS)) == 3
    assert read(tmp_path, DECISIONS)["total_recommendations"] == 3
    assert events.of_type("readability_cap_reached")
    assert models.count("ReadabilityProposal") == 1  # the cap was reached after section one
    assert "at most 3 changes" in models.prompts["ReadabilityProposal"][0]


def test_each_section_is_one_call_to_the_summarize_role_without_thinking(tmp_path: Path) -> None:
    sections(tmp_path)
    models = ResearchModels()
    auditor, _ = rig(models)
    auditor.audit_all(tmp_path, headings=HEADINGS)
    assert models.count("ReadabilityProposal") == 2
    assert models.models_used["ReadabilityProposal"] == {SETTINGS.model_summarize}
    assert models.thinks["ReadabilityProposal"] == [False, False]
    assert "Section: Zwei" in models.prompts["ReadabilityProposal"][1]
    assert TWO in models.prompts["ReadabilityProposal"][1]
    assert f"at most {RULES.hunk_max_chars} characters" in models.prompts["ReadabilityProposal"][0]


def test_both_files_exist_when_nothing_is_recommended(tmp_path: Path) -> None:
    sections(tmp_path)
    auditor, _ = rig(ResearchModels())
    auditor.audit_all(tmp_path, headings=HEADINGS)
    assert read(tmp_path, RECOMMENDATIONS) == []
    assert read(tmp_path, DECISIONS) == {
        "total_recommendations": 0,
        "applied": [],
        "skipped": [],
        "edit_failures": [],
        "net_char_delta_actual": 0,
        "sections_done": [1, 2],
    }


def test_a_resumed_audit_continues_after_the_last_finished_section(tmp_path: Path) -> None:
    sections(tmp_path)
    first = {
        "recommendations": [
            rec("add-whitespace", "Satz eins.\nSatz zwei", "Satz eins.\n\nSatz zwei")
        ]
    }
    crashing = ResearchModels(readabilities=[first], crash_on={"ReadabilityProposal": 2})
    auditor, _ = rig(crashing)
    with pytest.raises(ModelCrash):
        auditor.audit_all(tmp_path, headings=HEADINGS)
    again = ResearchModels()
    auditor2, _ = rig(again)
    auditor2.audit_all(tmp_path, headings=HEADINGS)
    assert again.count("ReadabilityProposal") == 1  # section two only
    assert read(tmp_path, DECISIONS)["applied"] == ["r01"]
    assert [r["id"] for r in read(tmp_path, RECOMMENDATIONS)] == ["r01"]


def test_recommendations_of_a_section_that_was_not_finished_are_discarded_on_resume(
    tmp_path: Path,
) -> None:
    sections(tmp_path)
    (tmp_path / RECOMMENDATIONS).write_text(
        json.dumps(
            [{"id": "r01", "section": 1, "category": "x", "current": "", "recommended": ""}]
        ),
        encoding="utf-8",
    )
    auditor, _ = rig(ResearchModels())
    auditor.audit_all(tmp_path, headings=HEADINGS)  # no decisions file: nothing was finished
    assert read(tmp_path, RECOMMENDATIONS) == []


def test_unusable_model_output_skips_the_section(tmp_path: Path) -> None:
    sections(tmp_path)
    models = ResearchModels(errors={"ReadabilityProposal": LLMOutputError("bad", raw="x")})
    auditor, events = rig(models)
    auditor.audit_all(tmp_path, headings=HEADINGS)
    assert len(events.of_type("readability_skipped")) == 2
    assert read(tmp_path, DECISIONS)["sections_done"] == [1, 2]


def test_an_unavailable_model_stops_the_step(tmp_path: Path) -> None:
    sections(tmp_path)
    models = ResearchModels(errors={"ReadabilityProposal": LLMUnavailableError("down")})
    auditor, _ = rig(models)
    with pytest.raises(LLMUnavailableError):
        auditor.audit_all(tmp_path, headings=HEADINGS)
    assert read(tmp_path, DECISIONS)["sections_done"] == []
