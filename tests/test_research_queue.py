"""M6 Step 4: the service for a worker and an API. Approval queues, the worker runs, a run can be
cancelled inside long steps, resumed and deleted."""

import errno
import sqlite3
from collections import Counter
from pathlib import Path

import pytest
from research_rig import FakePandoc, ResearchModels
from research_run_rig import RunRig, make_rig

from app.brief.errors import InvalidInput, NotFound, StaleBrief, WrongState
from app.research.errors import ReportNotReady
from app.research.manifest import LIGHT_STEPS, done_steps, read_run_json
from app.research.sections import load_section


@pytest.fixture
def rig(tmp_path: Path) -> RunRig:
    return make_rig(tmp_path)


def at_plan_gate(r: RunRig) -> str:
    run_id = r.create().run_id
    assert r.service.run(run_id).status == "awaiting_plan_approval"
    return run_id


def plan_hash(r: RunRig, run_id: str) -> str:
    return str(r.service.view(run_id).plan_sha256)


def started(r: RunRig, run_id: str) -> Counter[str]:
    """How often each step began, from `run.json`."""
    steps = read_run_json(r.run_dir(run_id)).get("steps", [])
    return Counter(s["step"] for s in steps if s["status"] == "running")


# ---- approval without execution -----------------------------------------------------------


def test_approving_a_plan_only_queues_the_run(rig: RunRig) -> None:
    run_id = at_plan_gate(rig)
    view = rig.service.approve_plan(run_id, plan_hash(rig, run_id))
    assert (view.status, view.waiting_for) == ("queued", "work")
    assert rig.searcher.calls == []  # the graph was not touched
    assert done_steps(rig.run_dir(run_id)) == ["0", "1", "2.1"]
    row = rig.runs.get_run(run_id)
    assert row is not None
    assert row.pending_plan_sha256 == plan_hash(rig, run_id)


def test_the_worker_run_resumes_with_the_approved_plan_and_finishes(rig: RunRig) -> None:
    run_id = at_plan_gate(rig)
    rig.service.approve_plan(run_id, plan_hash(rig, run_id))
    view = rig.service.run(run_id)
    assert view.status == "done"
    assert done_steps(rig.run_dir(run_id)) == list(LIGHT_STEPS)
    row = rig.runs.get_run(run_id)
    assert row is not None
    assert row.pending_plan_sha256 is None


def test_a_second_approval_of_the_same_plan_is_refused(rig: RunRig) -> None:
    run_id = at_plan_gate(rig)
    rig.service.approve_plan(run_id, plan_hash(rig, run_id))
    with pytest.raises(WrongState):
        rig.service.approve_plan(run_id, plan_hash(rig, run_id))


def test_approve_and_run_does_both(rig: RunRig) -> None:
    run_id = at_plan_gate(rig)
    assert rig.service.approve_and_run(run_id, plan_hash(rig, run_id)).status == "done"


def test_a_run_that_waits_for_its_plan_or_is_cancelled_is_not_run(rig: RunRig) -> None:
    run_id = at_plan_gate(rig)
    assert rig.service.run(run_id).status == "awaiting_plan_approval"
    rig.service.request_cancel(run_id)
    assert rig.service.run(run_id).status == "cancelled"
    assert rig.searcher.calls == []


def cancel_on_model_call(r: RunRig, run_id: str, kind: str, number: int) -> None:
    def hook(called: str, n: int) -> None:
        if (called, n) == (kind, number):
            r.service.request_cancel(run_id)

    r.models.on_call = hook


def cancel_on_search(r: RunRig, run_id: str, number: int) -> None:
    def hook(n: int) -> None:
        if n == number:
            r.service.request_cancel(run_id)

    r.searcher.on_call = hook


# ---- cancel (D2) --------------------------------------------------------------------------


def test_cancel_takes_effect_at_the_next_node_boundary(rig: RunRig) -> None:
    run_id = rig.create().run_id
    cancel_on_model_call(rig, run_id, "DecompositionDraft", 1)
    view = rig.service.run(run_id)
    assert view.status == "cancelled"
    assert done_steps(rig.run_dir(run_id)) == ["0", "1"]  # the step in flight finished
    assert [e.data["step"] for e in rig.events.of_type("run_cancelled")] == [None]
    assert not rig.runs.cancel_requested(run_id)
    assert not rig.events.of_type("run_failed")


def test_a_cancelled_run_resumes_without_repeating_a_finished_step(rig: RunRig) -> None:
    run_id = rig.create().run_id
    cancel_on_model_call(rig, run_id, "DecompositionDraft", 1)
    rig.service.run(run_id)
    rig.models.on_call = None
    assert rig.service.resume(run_id).status == "queued"
    assert rig.service.run(run_id).status == "awaiting_plan_approval"
    assert rig.service.approve_and_run(run_id, plan_hash(rig, run_id)).status == "done"
    assert set(started(rig, run_id).values()) == {1}
    assert rig.models.count("DecompositionDraft") == 1


def test_cancel_stops_the_sweep_between_two_queries(rig: RunRig) -> None:
    run_id = at_plan_gate(rig)
    cancel_on_search(rig, run_id, 1)
    view = rig.service.approve_and_run(run_id, plan_hash(rig, run_id))
    assert view.status == "cancelled"
    assert len(rig.searcher.calls) == 1  # query two was never searched
    rig.searcher.on_call = None
    rig.service.resume(run_id)
    assert rig.service.run(run_id).status == "done"
    assert started(rig, run_id)["2"] == 2  # the cut step began again, the finished ones did not
    assert all(count == 1 for step, count in started(rig, run_id).items() if step != "2")


def test_cancel_stops_the_draft_between_two_sections(rig: RunRig) -> None:
    run_id = at_plan_gate(rig)
    cancel_on_model_call(rig, run_id, "text", 1)
    view = rig.service.approve_and_run(run_id, plan_hash(rig, run_id))
    assert view.status == "cancelled"
    assert load_section(rig.run_dir(run_id), 1) is not None
    assert load_section(rig.run_dir(run_id), 2) is None
    rig.models.on_call = None
    rig.service.resume(run_id)
    assert rig.service.run(run_id).status == "done"
    assert rig.models.count("text") == 6  # every section was written once


def test_cancelling_a_run_that_waits_for_its_plan_ends_it_at_once_and_it_can_come_back(
    rig: RunRig,
) -> None:
    run_id = at_plan_gate(rig)
    assert rig.service.request_cancel(run_id).status == "cancelled"
    assert rig.service.resume(run_id).status == "queued"
    assert rig.service.run(run_id).status == "awaiting_plan_approval"  # the graph still waits


def test_a_failed_run_can_be_resumed_and_a_running_or_done_one_cannot(rig: RunRig) -> None:
    models = ResearchModels(errors={"DecompositionDraft": OSError(errno.ENOSPC, "No space left")})
    r = make_rig(rig.base / "disk", models=models)
    run_id = r.create().run_id
    view = r.service.run(run_id)
    assert view.status == "failed"  # disk full: failed, with the reason
    assert "No space left" in str(view.error)
    assert r.service.resume(run_id).status == "queued"
    done = at_plan_gate(rig)
    rig.service.approve_and_run(done, plan_hash(rig, done))
    with pytest.raises(WrongState):
        rig.service.resume(done)


# ---- delete (A4) --------------------------------------------------------------------------


def checkpoint_rows(r: RunRig, run_id: str) -> int:
    with sqlite3.connect(r.base / "checkpoints.sqlite") as conn:
        return int(
            conn.execute(
                "SELECT count(*) FROM checkpoints WHERE thread_id = ?", (run_id,)
            ).fetchone()[0]
        )


def test_deleting_a_finished_run_leaves_nothing_behind(rig: RunRig) -> None:
    run_id = at_plan_gate(rig)
    rig.service.approve_and_run(run_id, plan_hash(rig, run_id))
    assert checkpoint_rows(rig, run_id) > 0
    assert rig.run_dir(run_id).exists()
    rig.service.delete(run_id)
    assert rig.runs.get_run(run_id) is None
    assert not rig.run_dir(run_id).exists()
    assert checkpoint_rows(rig, run_id) == 0
    with pytest.raises(NotFound):
        rig.service.view(run_id)


def test_deleting_removes_the_upload_directory_of_the_runs_session(rig: RunRig) -> None:
    run_id = rig.create().run_id
    uploads = rig.base / "uploads" / "s-1"
    uploads.mkdir(parents=True)
    (uploads / "a.txt").write_text("x")
    with rig.runs._db.tx():  # pyright: ignore[reportPrivateUsage]
        rig.runs._db.conn.execute(  # pyright: ignore[reportPrivateUsage]
            "INSERT INTO sessions (session_id, created_at, updated_at, status, "
            "interview_language) VALUES ('s-1', 'n', 'n', 'approved', 'de')"
        )
        rig.runs._db.conn.execute(  # pyright: ignore[reportPrivateUsage]
            "UPDATE runs SET session_id = 's-1' WHERE run_id = ?", (run_id,)
        )
    rig.service.delete(run_id)
    assert not uploads.exists()


def test_a_running_run_cannot_be_deleted(rig: RunRig) -> None:
    run_id = rig.create().run_id
    rig.runs.set_status(run_id, "running")
    with pytest.raises(WrongState):
        rig.service.delete(run_id)
    assert rig.runs.get_run(run_id) is not None


# ---- reading ------------------------------------------------------------------------------


def test_the_report_is_served_once_the_run_is_done_in_every_format(rig: RunRig) -> None:
    run_id = at_plan_gate(rig)
    with pytest.raises(ReportNotReady) as info:
        rig.service.report_file(run_id, "md")
    assert info.value.status == "awaiting_plan_approval"
    rig.service.approve_and_run(run_id, plan_hash(rig, run_id))
    assert rig.service.report_file(run_id, "md") == rig.run_dir(run_id) / "report.md"
    assert rig.service.report_file(run_id, "docx").suffix == ".docx"
    assert rig.service.report_file(run_id, "pdf").suffix == ".pdf"
    with pytest.raises(InvalidInput):
        rig.service.report_file(run_id, "odt")


def test_a_missing_export_is_not_found(tmp_path: Path) -> None:
    r = make_rig(tmp_path, pandoc=FakePandoc(write=False))
    run_id = at_plan_gate(r)
    r.service.approve_and_run(run_id, plan_hash(r, run_id))
    with pytest.raises(NotFound):
        r.service.report_file(run_id, "docx")


def test_runs_are_listed_newest_first(rig: RunRig) -> None:
    first = rig.create().run_id
    second = rig.create().run_id
    assert [v.run_id for v in rig.service.list_runs()] == [second, first]


# ---- creating with a creator (A1, D4, D10) ------------------------------------------------


def test_a_run_of_a_key_without_self_approval_waits_for_its_brief_to_be_approved(
    rig: RunRig,
) -> None:
    view = rig.create(approved=False, created_by="k-1")
    assert view.status == "awaiting_brief_approval"
    row = rig.runs.get_run(view.run_id)
    assert row is not None
    assert row.created_by == "k-1"
    with pytest.raises(StaleBrief):
        rig.service.approve_external(view.run_id, "0" * 64)
    approved = rig.service.approve_external(view.run_id, str(row.brief_sha256))
    assert approved.status == "queued"
    assert rig.service.run(view.run_id).status == "awaiting_plan_approval"


def test_run_refuses_a_run_that_still_waits_for_its_brief(rig: RunRig) -> None:
    view = rig.create(approved=False)
    assert rig.service.run(view.run_id).status == "awaiting_brief_approval"
    assert rig.searcher.calls == []


def test_tier_auto_becomes_light_and_says_so(rig: RunRig) -> None:
    view = rig.create(tier="auto")
    assert view.tier == "light"
    assert [e.data["run_id"] for e in rig.events.of_type("tier_auto_resolved")] == [view.run_id]
