"""Step 15 (PRD M5, AD5, D10): cut-only polish, applied by code, logged in `polish-log.json`."""

import json
from pathlib import Path
from typing import Any

import pytest
from research_rig import LIGHT, RULES, SETTINGS, ModelCrash, ResearchModels, llm

from app.events import MemoryEventSink
from app.llm.errors import LLMOutputError, LLMUnavailableError
from app.research.polish import POLISH_LOG, Polisher
from app.research.sections import load_section, save_section

HEADINGS = ["Eins", "Zwei", "Drei"]
SHIM = "Polish posture. Keep the voice."
FILLER = "Es ist wichtig anzumerken, dass der Rückbau zehn Jahre dauert [S1]."


def hunk(old: str, new: str) -> dict[str, str]:
    return {"old": old, "new": new, "reason": "Füllwort"}


def rig(tmp_path: Path, models: ResearchModels) -> tuple[Polisher, MemoryEventSink]:
    events = MemoryEventSink()
    return Polisher(llm(models, events), RULES, events), events


def sections(tmp_path: Path, *texts: str) -> None:
    for index, text in enumerate(texts, 1):
        save_section(tmp_path, index, text)


def polish(p: Polisher, run_dir: Path, language: str = "de") -> None:
    p.polish_all(run_dir, headings=HEADINGS, language=language, shim=SHIM)


def log(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / POLISH_LOG).read_text(encoding="utf-8"))


def test_valid_cuts_are_applied_to_the_section_files_and_logged(tmp_path: Path) -> None:
    sections(tmp_path, FILLER, "Zwei [S1].", "Drei [S1].")
    models = ResearchModels(
        polishes=[
            {"hunks": [hunk("Es ist wichtig anzumerken, dass der", "Der")], "escalations": []},
            {"hunks": [], "escalations": ["Eine Tabelle fehlt"]},
            {"hunks": [], "escalations": []},
        ]
    )
    p, _ = rig(tmp_path, models)
    polish(p, tmp_path)
    assert (
        load_section(tmp_path, 1) == "Der Rückbau zehn Jahre dauert [S1]."
        or load_section(tmp_path, 1) == "Der Rückbau dauert zehn Jahre [S1]."
    )
    entry = log(tmp_path)
    assert entry["applied"] == [
        {"section": 1, "old": "Es ist wichtig anzumerken, dass der", "new": "Der"}
    ]
    assert entry["escalations"] == [{"section": 2, "text": "Eine Tabelle fehlt"}]
    assert entry["net_chars"] < 0
    assert entry["sections_done"] == [1, 2, 3]


def test_hunks_that_add_text_drop_citations_or_numbers_are_rejected_with_their_reason(
    tmp_path: Path,
) -> None:
    sections(tmp_path, FILLER, "Zwei [S1].", "Drei [S1].")
    bad = [
        hunk("der Rückbau zehn Jahre dauert", "der Rückbau dauert sehr lange und zehn Jahre"),
        hunk("zehn Jahre dauert [S1]", "dauert"),
        hunk("gibt es nicht", ""),
    ]
    nothing: dict[str, object] = {"hunks": [], "escalations": []}
    p, _ = rig(tmp_path, ResearchModels(polishes=[{"hunks": bad, "escalations": []}, nothing]))
    polish(p, tmp_path)
    assert load_section(tmp_path, 1) == FILLER
    assert [r["reason"] for r in log(tmp_path)["rejected"]] == [
        "adds_text",
        "drops_citation",
        "not_found",
    ]
    assert log(tmp_path)["applied"] == []
    assert log(tmp_path)["net_chars"] == 0


def test_each_section_is_one_call_with_the_section_the_language_and_the_shim(
    tmp_path: Path,
) -> None:
    sections(tmp_path, "Erster Text [S1].", "Zweiter Text [S1].", "Dritter Text [S1].")
    models = ResearchModels()
    p, _ = rig(tmp_path, models)
    polish(p, tmp_path, language="en")
    assert models.count("PolishProposal") == 3
    prompt = models.prompts["PolishProposal"][1]
    assert "Section: Zwei" in prompt
    assert "Zweiter Text [S1]." in prompt
    assert "Erster Text" not in prompt
    assert "Report language: English" in prompt
    assert SHIM in prompt
    assert f"at most {RULES.hunk_max_chars} characters" in prompt
    assert models.thinks["PolishProposal"] == [False, False, False]
    assert models.models_used["PolishProposal"] == {SETTINGS.model_reason}


def test_the_log_exists_even_when_nothing_was_changed(tmp_path: Path) -> None:
    sections(tmp_path, "A [S1].", "B [S1].", "C [S1].")
    p, _ = rig(tmp_path, ResearchModels())
    polish(p, tmp_path)
    assert log(tmp_path) == {
        "applied": [],
        "rejected": [],
        "skipped": [],
        "escalations": [],
        "net_chars": 0,
        "sections_done": [1, 2, 3],
    }


def test_empty_sections_are_not_sent_to_the_model(tmp_path: Path) -> None:
    sections(tmp_path, "A [S1].")
    models = ResearchModels()
    p, _ = rig(tmp_path, models)
    polish(p, tmp_path)
    assert models.count("PolishProposal") == 1
    assert log(tmp_path)["sections_done"] == [1, 2, 3]


def test_a_resumed_polish_skips_finished_sections_and_keeps_the_log(tmp_path: Path) -> None:
    sections(tmp_path, FILLER, "Zwei [S1].", "Drei [S1].")
    crashing = ResearchModels(
        polishes=[
            {"hunks": [hunk("Es ist wichtig anzumerken, dass der", "Der")], "escalations": []}
        ],
        crash_on={"PolishProposal": 2},
    )
    p, _ = rig(tmp_path, crashing)
    with pytest.raises(ModelCrash):
        polish(p, tmp_path)
    assert log(tmp_path)["sections_done"] == [1]
    again = ResearchModels()
    p2, _ = rig(tmp_path, again)
    polish(p2, tmp_path)
    assert again.count("PolishProposal") == 2  # sections two and three
    assert len(log(tmp_path)["applied"]) == 1  # section one's cut is still logged
    assert log(tmp_path)["sections_done"] == [1, 2, 3]


def test_unusable_model_output_skips_the_section_and_goes_on(tmp_path: Path) -> None:
    sections(tmp_path, "A [S1].", "B [S1].", "C [S1].")
    models = ResearchModels(errors={"PolishProposal": LLMOutputError("bad", raw="x")})
    p, events = rig(tmp_path, models)
    polish(p, tmp_path)
    assert [s["reason"] for s in log(tmp_path)["skipped"]] == ["LLMOutputError"] * 3
    assert len(events.of_type("polish_skipped")) == 3
    assert log(tmp_path)["sections_done"] == [1, 2, 3]


def test_an_unavailable_model_stops_the_step_so_it_can_resume(tmp_path: Path) -> None:
    sections(tmp_path, "A [S1].", "B [S1].", "C [S1].")
    models = ResearchModels(errors={"PolishProposal": LLMUnavailableError("down")})
    p, _ = rig(tmp_path, models)
    with pytest.raises(LLMUnavailableError):
        polish(p, tmp_path)
    assert log(tmp_path)["sections_done"] == []


def test_polish_never_makes_a_report_longer(tmp_path: Path) -> None:
    sections(tmp_path, FILLER, "Zwei [S1].", "Drei [S1].")
    before = sum(len(load_section(tmp_path, n) or "") for n in (1, 2, 3))
    hunks = [
        hunk("Es ist wichtig anzumerken, dass der", "Der"),
        hunk("zehn Jahre", "zehn Jahre und mehr"),
    ]
    p, _ = rig(tmp_path, ResearchModels(polishes=[{"hunks": hunks, "escalations": []}]))
    polish(p, tmp_path)
    assert sum(len(load_section(tmp_path, n) or "") for n in (1, 2, 3)) < before
    assert LIGHT.readability_cap > 0
