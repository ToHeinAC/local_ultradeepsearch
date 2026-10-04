"""`udr run` (PRD M5 D1, D3, D4): every option path against the real service on fakes."""

from pathlib import Path
from typing import Any

import pytest
from research_rig import FakePandoc, ModelCrash, ResearchModels
from research_run_rig import RAW_BRIEF, TEMPLATE, RunRig, make_rig
from typer.testing import CliRunner

from app import cli
from app.llm.errors import LLMUnavailableError
from app.research.worker import WorkerLock

runner = CliRunner()


@pytest.fixture
def r(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunRig:
    rig = make_rig(tmp_path)
    monkeypatch.setattr(cli, "_research_service", lambda: rig.service)
    return rig


def run(*args: str, input_text: str = "") -> Any:
    return runner.invoke(cli.app, ["run", *args], input=input_text)


def brief_file(tmp_path: Path, text: str = RAW_BRIEF) -> str:
    path = tmp_path / "brief.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


# ---- an approved run ------------------------------------------------------------------------


def test_a_queued_run_is_reviewed_approved_and_finished(r: RunRig) -> None:
    run_id = r.create().run_id
    result = run(run_id, input_text="f\n")
    assert result.exit_code == 0, result.output
    assert "Gesendete Query" in result.output
    assert f"Fertig: {r.run_dir(run_id) / 'report.md'}" in result.output
    assert "docx: ok" in result.output
    assert r.service.view(run_id).status == "done"


def test_no_input_prints_the_plan_and_the_command_that_approves_it(r: RunRig) -> None:
    run_id = r.create().run_id
    result = run(run_id, "--no-input")
    assert result.exit_code == 0, result.output
    sha = r.service.view(run_id).plan_sha256
    assert f"udr run {run_id} --approve-plan {sha}" in result.output
    assert r.service.view(run_id).waiting_for == "plan"
    assert r.searcher.calls == []


def test_approve_plan_with_the_hash_finishes_the_run(r: RunRig) -> None:
    run_id = r.create().run_id
    run(run_id, "--no-input")
    result = run(run_id, "--approve-plan", str(r.service.view(run_id).plan_sha256))
    assert result.exit_code == 0, result.output
    assert r.service.view(run_id).status == "done"
    assert r.models.count("PlanDraft") == 1  # the plan was kept


def test_a_stale_hash_exits_2_and_the_plan_keeps_waiting(r: RunRig) -> None:
    run_id = r.create().run_id
    result = run(run_id, "--approve-plan", "0" * 64)
    assert result.exit_code == 2
    assert "Hash" in result.output or "hash" in result.output
    assert r.service.view(run_id).waiting_for == "plan"
    assert r.searcher.calls == []


def test_quitting_the_review_leaves_the_run_waiting(r: RunRig) -> None:
    run_id = r.create().run_id
    result = run(run_id, input_text="q\n")
    assert result.exit_code == 0
    assert r.service.view(run_id).status == "awaiting_plan_approval"


def test_a_waiting_run_continues_in_the_next_command(r: RunRig) -> None:
    run_id = r.create().run_id
    run(run_id, "--no-input")
    assert run(run_id, input_text="f\n").exit_code == 0
    assert r.service.view(run_id).status == "done"


def test_a_finished_run_is_only_reported(r: RunRig) -> None:
    run_id = r.create().run_id
    run(run_id, input_text="f\n")
    calls = dict(r.models.calls)
    result = run(run_id)
    assert result.exit_code == 0
    assert "Fertig" in result.output
    assert r.models.calls == calls


def test_an_unknown_run_exits_1(r: RunRig) -> None:
    result = run("r-nope")
    assert result.exit_code == 1
    assert "r-nope" in result.output


# ---- failures and the worker slot -----------------------------------------------------------


def test_a_failed_run_exits_1_and_the_next_command_resumes_it(tmp_path: Path) -> None:
    models = ResearchModels(errors={"DecompositionDraft": LLMUnavailableError("model down")})
    rig = make_rig(tmp_path, models=models)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "_research_service", lambda: rig.service)
        run_id = rig.create().run_id
        failed = run(run_id, "--no-input")
        assert failed.exit_code == 1
        assert "Fehlgeschlagen im Schritt 1" in failed.output
        assert f"udr run {run_id}" in failed.output
        models.errors.clear()
        assert run(run_id, "--no-input").exit_code == 0
    assert rig.service.view(run_id).waiting_for == "plan"


def test_a_second_run_while_the_slot_is_held_exits_1(r: RunRig) -> None:
    run_id = r.create().run_id
    with WorkerLock(r.base / "worker.lock"):
        result = run(run_id, "--no-input")
    assert result.exit_code == 1
    assert "Ein anderer Lauf ist aktiv." in result.output


def test_a_run_a_crash_left_running_is_continued_by_the_next_command(tmp_path: Path) -> None:
    crashing = make_rig(tmp_path, models=ResearchModels(crash_on={"PlanDraft": 1}))
    run_id = crashing.create().run_id
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "_research_service", lambda: crashing.service)
        with pytest.raises(ModelCrash):
            run(run_id, "--no-input")  # the crash signal is never caught
        restarted = make_rig(tmp_path)
        mp.setattr(cli, "_research_service", lambda: restarted.service)
        result = run(run_id, "--no-input")
    assert result.exit_code == 0, result.output
    assert restarted.service.view(run_id).waiting_for == "plan"
    assert restarted.models.count("DecompositionDraft") == 0


def test_a_blocked_run_exits_1_and_names_the_failed_checks(tmp_path: Path) -> None:
    leaky = "Siehe Locus 3 dazu. " * 5 + "Der Rückbau dauert [S1]."
    models = ResearchModels(
        texts={"Management Summary": leaky}, answers={"LeakProposal": [{"hunks": []}]}
    )
    rig = make_rig(tmp_path, models=models, pandoc=FakePandoc())
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "_research_service", lambda: rig.service)
        run_id = rig.create().run_id
        run(run_id, "--no-input")
        result = run(run_id, "--approve-plan", str(rig.service.view(run_id).plan_sha256))
    assert result.exit_code == 1
    assert "Ship-Gate nicht bestanden" in result.output
    assert "G7" in result.output


# ---- an external brief ----------------------------------------------------------------------


def test_an_external_brief_creates_a_run_and_goes_to_the_plan(r: RunRig, tmp_path: Path) -> None:
    result = run(
        "--brief", brief_file(tmp_path), "--tier", "light", "--template", TEMPLATE, "--no-input"
    )
    assert result.exit_code == 0, result.output
    assert "Lauf angelegt: r-" in result.output
    run_id = next(w for w in result.output.split() if w.startswith("r-"))
    view = r.service.view(run_id)
    assert (view.status, view.tier) == ("awaiting_plan_approval", "light")
    text = Path(str(r.runs.get_run(run_id).brief_path)).read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert "## Ausgabe" in text


def test_the_options_override_the_templates_defaults(r: RunRig, tmp_path: Path) -> None:
    result = run(
        "--brief", brief_file(tmp_path), "--tier", "light", "--template", TEMPLATE,
        "--format", "short", "--language", "en", "--no-input",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    run_id = next(w for w in result.output.split() if w.startswith("r-"))
    saved = r.runs.get_run(run_id)
    assert saved is not None
    assert '"response_format": "short"' in str(saved.settings_json)
    assert '"report_language": "en"' in str(saved.settings_json)


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["r-x", "--brief", "b.md"],
        ["--brief", "b.md"],
        ["--brief", "b.md", "--tier", "light"],
        ["--brief", "b.md", "--tier", "full", "--template", "auto"],
        ["--brief", "gibt-es-nicht.md", "--tier", "light", "--template", "auto"],
    ],
)
def test_bad_combinations_exit_2_and_create_no_run(r: RunRig, args: list[str]) -> None:
    result = run(*args)
    assert result.exit_code == 2, result.output
    assert r.runs.get_run("r-x") is None


def test_an_unknown_template_or_an_unparseable_brief_exits_2(r: RunRig, tmp_path: Path) -> None:
    base = ["--tier", "light", "--no-input"]
    good = brief_file(tmp_path)
    assert run("--brief", good, "--template", "nope", *base).exit_code == 2
    bad = brief_file(tmp_path, "# Nur ein Titel\n\nohne Fragen\n")
    assert run("--brief", bad, "--template", TEMPLATE, *base).exit_code == 2


def test_the_full_tier_run_of_an_existing_row_exits_2(r: RunRig) -> None:
    run_id = r.service.create_external_run(
        RAW_BRIEF, tier="full", template_id=TEMPLATE, language="de"
    ).run_id
    result = run(run_id)
    assert result.exit_code == 2
    assert "M8" in result.output
