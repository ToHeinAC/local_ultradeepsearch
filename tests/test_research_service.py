"""The research graph and service up to the width sweep (PRD M5, A6): the plan gate (AC2),
refusals, failure and resume."""

import json
from pathlib import Path

import pytest
from research_rig import BRIEF, PLAN, ResearchModels, ResearchRig, build_research_rig

from app.adapters.outbound.denylist import Denylist
from app.brief.errors import InvalidInput, NotFound, WrongState
from app.llm.errors import LLMModelMissingError
from app.research.errors import PlanBlocked, StalePlan
from app.research.models import RunView


def at_plan(rig: ResearchRig) -> tuple[str, RunView]:
    run_id = rig.new_run()
    return run_id, rig.service.start(run_id)


def step_states(view: RunView) -> list[tuple[str, str]]:
    return [(s.step_id, s.status) for s in view.steps]


# ---- starting and the plan gate -------------------------------------------------------------


def test_start_stops_at_the_plan_approval_and_searches_nothing(tmp_path: Path) -> None:
    rig = build_research_rig(tmp_path)
    run_id, view = at_plan(rig)
    assert (view.status, view.waiting_for, view.reason) == ("awaiting_plan_approval", "plan", "")
    assert step_states(view) == [("0", "done"), ("1", "done"), ("2.1", "running")]
    assert view.plan is not None
    assert len(view.plan.rows) == len(PLAN)
    assert all(r.state == "planned" and r.sent for r in view.plan.rows)
    assert view.plan.approvable
    assert rig.gateway.web_calls == []  # no search before the owner approved
    assert rig.models.count("plan") == 1
    assert (tmp_path / "runs" / run_id / "search-plan.json").is_file()


def test_the_run_can_stop_at_the_plan_and_a_new_process_shows_the_same_plan(
    tmp_path: Path,
) -> None:
    """M5 AC2: the plan survives a restart; nothing is drafted or sent again."""
    first = build_research_rig(tmp_path)
    run_id, view = at_plan(first)
    assert view.plan is not None
    second = build_research_rig(tmp_path)  # a restarted process on the same files
    again = second.service.plan(run_id)
    assert again.plan_sha256 == view.plan.plan_sha256
    assert [r.sent for r in again.rows] == [r.sent for r in view.plan.rows]
    assert second.models.calls == []
    assert second.service.get(run_id).waiting_for == "plan"
    assert second.service.resume(run_id).waiting_for == "plan"
    assert second.models.calls == []


def test_approval_runs_the_sweep_to_the_end(tmp_path: Path) -> None:
    rig = build_research_rig(tmp_path)
    run_id, view = at_plan(rig)
    assert view.plan is not None
    done = rig.service.approve_plan(run_id, view.plan.plan_sha256)
    assert (done.status, done.waiting_for) == ("done", "nothing")
    assert step_states(done) == [("0", "done"), ("1", "done"), ("2.1", "done"), ("2", "done")]
    assert len(rig.gateway.web_calls) == len(PLAN)
    assert rig.builts[run_id].vault.stats().notes_by_kind["source"] == 2 * len(PLAN)
    data = json.loads((tmp_path / "runs" / run_id / "run.json").read_text(encoding="utf-8"))
    assert [s["id"] for s in data["steps"]] == ["0", "1", "2.1", "2"]
    assert data["status"] == "done"


def test_a_stale_hash_is_refused_and_nothing_runs(tmp_path: Path) -> None:
    rig = build_research_rig(tmp_path)
    run_id, view = at_plan(rig)
    assert view.plan is not None
    rig.service.delete_query(run_id, "q009")
    with pytest.raises(StalePlan):
        rig.service.approve_plan(run_id, view.plan.plan_sha256)
    assert rig.gateway.web_calls == []
    assert rig.service.get(run_id).waiting_for == "plan"


def test_the_approval_step_checks_the_hash_again(tmp_path: Path) -> None:
    """Defence in depth: even a resume that bypassed the service cannot approve another plan."""
    rig = build_research_rig(tmp_path)
    run_id, view = at_plan(rig)
    assert view.plan is not None
    rig.steps.approve(run_id, view.plan.plan_sha256)  # the current plan passes
    with pytest.raises(StalePlan):
        rig.steps.approve(run_id, "0" * 64)


def test_a_plan_with_a_blocked_query_cannot_be_approved(tmp_path: Path) -> None:
    rig = build_research_rig(tmp_path)
    run_id, _ = at_plan(rig)
    edited = rig.service.edit_query(run_id, "q001", "Geheimprojekt Kosten")
    assert not edited.approvable
    with pytest.raises(PlanBlocked):
        rig.service.approve_plan(run_id, edited.plan_sha256)
    assert rig.gateway.web_calls == []


def test_a_query_the_denylist_covers_after_planning_blocks_the_approval(tmp_path: Path) -> None:
    rig = build_research_rig(tmp_path)
    run_id, view = at_plan(rig)
    assert view.plan is not None
    rig.denylist.add("Obrigheim")  # added after the plan was drafted
    with pytest.raises(PlanBlocked, match="q003"):
        rig.service.approve_plan(run_id, view.plan.plan_sha256)
    blocked = [r for r in rig.service.plan(run_id).rows if r.state == "blocked"]
    assert [(r.query_id, r.reason) for r in blocked] == [("q003", "denylist")]
    assert rig.gateway.web_calls == []


def test_a_plan_with_nothing_to_search_cannot_be_approved(tmp_path: Path) -> None:
    rig = build_research_rig(tmp_path)
    run_id, _ = at_plan(rig)
    view = None
    for n in range(1, len(PLAN) + 1):
        view = rig.service.delete_query(run_id, f"q{n:03d}")
    assert view is not None
    assert not view.approvable
    with pytest.raises(PlanBlocked):
        rig.service.approve_plan(run_id, view.plan_sha256)


def test_plan_edits_change_the_plan_and_need_the_waiting_state(tmp_path: Path) -> None:
    rig = build_research_rig(tmp_path)
    run_id, view = at_plan(rig)
    assert view.plan is not None
    edited = rig.service.edit_query(run_id, "q001", "Firma X Kosten Rückbau")
    row = next(r for r in edited.rows if r.query_id == "q001")
    assert (row.sent, row.removed) == ("Kosten Rückbau", ("Firma X",))
    added = rig.service.add_query(run_id, "i02", "depth", "Obrigheim Gutachten")
    assert added.rows[-1].query_id == "q010"
    assert len({view.plan.plan_sha256, edited.plan_sha256, added.plan_sha256}) == 3
    with pytest.raises(InvalidInput):
        rig.service.add_query(run_id, "i99", "breadth", "x")
    done = rig.service.approve_plan(run_id, added.plan_sha256)
    assert done.status == "done"
    with pytest.raises(WrongState):
        rig.service.edit_query(run_id, "q001", "zu spät")
    with pytest.raises(WrongState):
        rig.service.plan(run_id)


# ---- refusals -------------------------------------------------------------------------------


def test_unknown_runs_and_wrong_states(tmp_path: Path) -> None:
    rig = build_research_rig(tmp_path)
    with pytest.raises(NotFound):
        rig.service.start("r-nope")
    run_id, _ = at_plan(rig)
    with pytest.raises(WrongState):
        rig.service.start(run_id)  # waiting, not queued or failed


def test_a_run_without_an_approved_brief_is_refused(tmp_path: Path) -> None:
    rig = build_research_rig(tmp_path)
    rig.builts.clear()
    from fixtures_corpus import build

    build(tmp_path, {}, run_id="r-bare")  # the vault's own run row: no brief, no hash
    with pytest.raises(WrongState, match="no approved brief"):
        rig.service.start("r-bare")


def test_the_full_tier_is_refused(tmp_path: Path) -> None:
    rig = build_research_rig(tmp_path)
    row = rig.service.create_external_run(
        BRIEF, tier="full", template_id="auto", response_format="structured", report_language="de"
    )
    with pytest.raises(WrongState, match="Full-Tier"):
        rig.service.start(row.run_id)


def test_a_tampered_archive_is_refused(tmp_path: Path) -> None:
    rig = build_research_rig(tmp_path)
    run_id = rig.new_run()
    row = rig.runs.get_run(run_id)
    assert row is not None
    assert row.brief_path is not None
    path = Path(row.brief_path)
    path.chmod(0o644)
    path.write_text(path.read_text(encoding="utf-8") + "\n3. Eingeschmuggelt\n", encoding="utf-8")
    with pytest.raises(WrongState, match="hash"):
        rig.service.start(run_id)
    assert rig.models.calls == []


# ---- failure and resume ---------------------------------------------------------------------


def test_a_model_error_in_step_one_fails_the_run_and_start_resumes_it(tmp_path: Path) -> None:
    models = ResearchModels(errors={"decompose": LLMModelMissingError("gone")})
    rig = build_research_rig(tmp_path, models=models)
    run_id = rig.new_run()
    failed = rig.service.start(run_id)
    assert failed.status == "failed"
    assert "LLMModelMissingError" in failed.reason
    assert failed.waiting_for == "work"
    models.errors.clear()
    view = rig.service.start(run_id)
    assert (view.status, view.waiting_for) == ("awaiting_plan_approval", "plan")
    assert models.count("decompose") == 2


def test_a_failed_run_keeps_its_reason_in_run_json(tmp_path: Path) -> None:
    models = ResearchModels(errors={"plan": LLMModelMissingError("gone")})
    rig = build_research_rig(tmp_path, models=models)
    run_id = rig.new_run()
    rig.service.start(run_id)
    data = json.loads((tmp_path / "runs" / run_id / "run.json").read_text(encoding="utf-8"))
    assert data["status"] == "failed"
    assert "LLMModelMissingError" in data["status_reason"]


# ---- external runs --------------------------------------------------------------------------


def test_an_external_run_archives_the_prepared_brief(tmp_path: Path) -> None:
    rig = build_research_rig(tmp_path)
    run_id = rig.new_run()
    row = rig.runs.get_run(run_id)
    assert row is not None
    assert row.brief_path is not None
    text = Path(row.brief_path).read_text(encoding="utf-8")
    assert "Method: extern geliefert" in text
    assert "## Ausgabe" in text
    assert (row.origin, row.status, row.session_id) == ("external", "queued", None)
    assert rig.service.list_runs()[0].run_id == run_id


@pytest.mark.parametrize(
    "kwargs",
    [
        {"template_id": "nope"},
        {"response_format": "essay"},
        {"tier": "medium"},
    ],
)
def test_bad_external_settings_are_rejected(tmp_path: Path, kwargs: dict[str, str]) -> None:
    rig = build_research_rig(tmp_path)
    params = {
        "tier": "light",
        "template_id": "auto",
        "response_format": "structured",
        "report_language": "de",
        **kwargs,
    }
    with pytest.raises(InvalidInput):
        rig.service.create_external_run(BRIEF, **params)


def test_denylist_object_is_shared_with_the_rig(tmp_path: Path) -> None:
    deny = Denylist([])
    rig = build_research_rig(tmp_path, denylist=deny)
    assert rig.denylist is deny
