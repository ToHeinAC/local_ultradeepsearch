"""`udr run` (PRD M5, A7): every option path against a real service on fake models."""

from pathlib import Path
from typing import Any

import pytest
from fixtures_corpus import SimulatedCrash
from research_rig import BRIEF, ResearchModels, ResearchRig, build_research_rig
from typer.testing import CliRunner

from app import cli
from app.llm.errors import LLMModelMissingError

runner = CliRunner()
EXTERNAL = "# Wie teuer ist der Rückbau?\n\n1. Kosten\n2. Dauer\n"


@pytest.fixture
def r(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ResearchRig:
    rig = build_research_rig(tmp_path)
    monkeypatch.setattr(cli, "_research_service", lambda: rig.service)
    return rig


def run(*args: str, input_text: str = "") -> Any:
    return runner.invoke(cli.app, ["run", *args], input=input_text)


def brief_file(tmp_path: Path, text: str = EXTERNAL) -> str:
    path = tmp_path / "brief.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


# ---- a run from Phase 1 ---------------------------------------------------------------------


def test_a_queued_run_is_reviewed_approved_and_finished(r: ResearchRig) -> None:
    run_id = r.new_run()
    result = run(run_id, input_text="f\n")
    assert result.exit_code == 0, result.output
    assert "Gesendete Query" in result.output
    assert f"Fertig: {r.base / 'runs' / run_id}" in result.output
    assert r.service.get(run_id).status == "done"


def test_no_input_stops_at_the_plan_and_prints_the_resume_hint(r: ResearchRig) -> None:
    run_id = r.new_run()
    result = run(run_id, "--no-input")
    assert result.exit_code == 0, result.output
    assert f"Weiter mit: udr run {run_id}" in result.output
    assert r.service.get(run_id).waiting_for == "plan"
    assert r.gateway.web_calls == []


def test_a_later_command_continues_the_waiting_run(r: ResearchRig) -> None:
    run_id = r.new_run()
    run(run_id, "--no-input")
    result = run(run_id, input_text="f\n")
    assert result.exit_code == 0, result.output
    assert r.service.get(run_id).status == "done"
    assert r.models.count("plan") == 1


def test_quitting_the_review_leaves_the_run_waiting(r: ResearchRig) -> None:
    run_id = r.new_run()
    result = run(run_id, input_text="q\n")
    assert result.exit_code == 0
    assert r.service.get(run_id).waiting_for == "plan"


def test_a_finished_run_is_only_reported(r: ResearchRig) -> None:
    run_id = r.new_run()
    run(run_id, input_text="f\n")
    calls = len(r.models.calls)
    result = run(run_id)
    assert result.exit_code == 0
    assert "Fertig" in result.output
    assert len(r.models.calls) == calls


def test_a_failed_run_exits_1_and_the_next_call_resumes_it(tmp_path: Path) -> None:
    models = ResearchModels(errors={"decompose": LLMModelMissingError("gone")})
    rig = build_research_rig(tmp_path, models=models)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "_research_service", lambda: rig.service)
        run_id = rig.new_run()
        failed = run(run_id, "--no-input")
        assert failed.exit_code == 1
        assert "Fehlgeschlagen" in failed.output
        assert f"udr run {run_id}" in failed.output
        models.errors.clear()
        assert run(run_id, "--no-input").exit_code == 0
    assert rig.service.get(run_id).waiting_for == "plan"


def test_an_unknown_run_exits_1(r: ResearchRig) -> None:
    result = run("r-nope")
    assert result.exit_code == 1
    assert "r-nope" in result.output


# ---- --list and bad combinations ------------------------------------------------------------


def test_list_shows_the_runs(r: ResearchRig) -> None:
    assert "Keine Läufe." in run("--list").output
    run_id = r.new_run()
    result = run("--list")
    assert result.exit_code == 0
    assert run_id in result.output
    assert "queued" in result.output


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--list", "r-x"],
        ["--list", "--tier", "light"],
        ["r-x", "--brief", "b.md"],
        ["--brief", "b.md"],  # no tier and no template
        ["--brief", "b.md", "--tier", "light"],  # no template
    ],
)
def test_bad_combinations_exit_2(r: ResearchRig, args: list[str]) -> None:
    result = run(*args)
    assert result.exit_code == 2, result.output
    assert r.service.list_runs() == []


def test_the_full_tier_is_refused_with_a_message(r: ResearchRig, tmp_path: Path) -> None:
    result = run("--brief", brief_file(tmp_path), "--tier", "full", "--template", "auto")
    assert result.exit_code == 2
    assert "Full-Tier ab M8" in result.output


# ---- an external brief ----------------------------------------------------------------------


def test_an_external_brief_is_shown_confirmed_and_run(r: ResearchRig, tmp_path: Path) -> None:
    args = ["--brief", brief_file(tmp_path), "--tier", "light", "--template", "auto"]
    result = run(*args, input_text="j\nf\n")
    assert result.exit_code == 0, result.output
    assert "Method: extern geliefert" in result.output
    assert "sha256: " in result.output
    assert "Freigeben? [j/N]" in result.output
    (row,) = r.service.list_runs()
    assert (row.origin, row.status, row.response_format, row.report_language) == (
        "external",
        "done",
        "structured",  # the template's default
        "de",
    )


def test_declining_creates_no_run(r: ResearchRig, tmp_path: Path) -> None:
    args = ["--brief", brief_file(tmp_path), "--tier", "light", "--template", "auto"]
    result = run(*args, input_text="n\n")
    assert result.exit_code == 0
    assert "kein Lauf angelegt" in result.output
    assert r.service.list_runs() == []


def test_yes_and_no_input_create_and_stop_at_the_plan(r: ResearchRig, tmp_path: Path) -> None:
    args = ["--brief", brief_file(tmp_path), "--tier", "light", "--template", "auto"]
    result = run(*args, "--format", "short", "--language", "en", "--yes", "--no-input")
    assert result.exit_code == 0, result.output
    (row,) = r.service.list_runs()
    assert (row.response_format, row.report_language, row.status) == (
        "short",
        "en",
        "awaiting_plan_approval",
    )


def test_no_input_without_yes_refuses_to_approve_the_brief(r: ResearchRig, tmp_path: Path) -> None:
    args = ["--brief", brief_file(tmp_path), "--tier", "light", "--template", "auto"]
    result = run(*args, "--no-input")
    assert result.exit_code == 2
    assert "--yes" in result.output
    assert r.service.list_runs() == []


@pytest.mark.parametrize(
    "extra",
    [["--template", "nope"], ["--format", "essay"]],
)
def test_bad_settings_exit_2(r: ResearchRig, tmp_path: Path, extra: list[str]) -> None:
    base = ["--brief", brief_file(tmp_path), "--tier", "light", "--template", "auto", "--yes"]
    result = run(*base, *extra)
    assert result.exit_code == 2
    assert r.service.list_runs() == []


def test_an_unparseable_brief_and_a_missing_file_exit_2(r: ResearchRig, tmp_path: Path) -> None:
    bad = brief_file(tmp_path, "# Nur ein Titel\n\nohne Fragen\n")
    base = ["--tier", "light", "--template", "auto", "--yes"]
    assert run("--brief", bad, *base).exit_code == 2
    assert run("--brief", str(tmp_path / "gibt-es-nicht.md"), *base).exit_code == 2
    assert r.service.list_runs() == []


def test_a_phase1_brief_text_is_a_valid_external_brief(r: ResearchRig, tmp_path: Path) -> None:
    result = run(
        "--brief", brief_file(tmp_path, BRIEF), "--tier", "light", "--template", "auto", "--yes",
        "--no-input",
    )  # fmt: skip
    assert result.exit_code == 0, result.output


def test_a_run_a_crash_left_running_is_continued_by_the_next_command(tmp_path: Path) -> None:
    crashing = build_research_rig(tmp_path, models=ResearchModels(crash_on={"plan": 1}))
    run_id = crashing.new_run()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "_research_service", lambda: crashing.service)
        with pytest.raises(SimulatedCrash):
            run(run_id, "--no-input")  # a crash is never caught
        assert crashing.service.get(run_id).status == "running"
        restarted = build_research_rig(tmp_path)  # a new process on the same files
        mp.setattr(cli, "_research_service", lambda: restarted.service)
        result = run(run_id, "--no-input")
    assert result.exit_code == 0, result.output
    assert restarted.service.get(run_id).waiting_for == "plan"
    assert restarted.models.count("decompose") == 0  # step 1 was kept
