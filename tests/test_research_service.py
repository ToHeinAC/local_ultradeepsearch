"""A Lite run end to end on fakes (PRD M5 AC1, AC2, AC3, AC5): the graph, the steps, the plan gate,
the ship gate and the export, through the service."""

import json
from pathlib import Path

import pytest
from research_rig import (
    TEMPLATES,
    FakePandoc,
    FakePreparer,
    ModelCrash,
    ResearchModels,
    german_text,
)
from research_run_rig import RAW_BRIEF, TEMPLATE, RunRig, make_rig

from app.adapters.outbound.errors import DenylistBlocked
from app.brief.errors import InvalidInput, NotFound, WrongState
from app.llm.errors import LLMUnavailableError
from app.research.errors import PlanBlocked, StalePlan, WorkerBusy
from app.research.manifest import LIGHT_STEPS, done_steps
from app.research.markdown import h2_list
from app.research.service import TierNotAvailable
from app.research.worker import WorkerLock

FULL_ONLY = (
    "loci.json",
    "comparisons.md",
    "critic-findings-dialectic.json",
    "patch-log.json",
    "cite-check-pairs.json",
)


@pytest.fixture
def rig(tmp_path: Path) -> RunRig:
    return make_rig(tmp_path)


def at_plan_gate(r: RunRig) -> str:
    run_id = r.create().run_id
    view = r.service.run(run_id)
    assert view.status == "awaiting_plan_approval"
    return run_id


# ---- AC1, AC3: the light sequence --------------------------------------------------------


def test_a_light_run_records_exactly_the_light_steps_and_no_full_only_artifact(rig: RunRig) -> None:
    run_id = at_plan_gate(rig)
    assert done_steps(rig.run_dir(run_id)) == ["0", "1", "2.1"]
    view = rig.service.approve_plan(run_id, str(rig.service.view(run_id).plan_sha256))
    assert (view.status, view.waiting_for) == ("done", "nothing")
    assert done_steps(rig.run_dir(run_id)) == list(LIGHT_STEPS)
    assert not [n for n in FULL_ONLY if (rig.run_dir(run_id) / n).exists()]
    assert not (rig.run_dir(run_id) / "temp" / "evidence-digest.md").exists()


def test_the_report_has_the_templates_headings_then_sources_and_the_appendix(rig: RunRig) -> None:
    run_id = at_plan_gate(rig)
    view = rig.service.approve_plan(run_id, str(rig.service.view(run_id).plan_sha256))
    report = Path(str(view.report_path)).read_text(encoding="utf-8")
    assert h2_list(report) == [
        *TEMPLATES[TEMPLATE].headings,
        "Quellen",
        "Anhang A — Recherche-Brief",
    ]
    assert view.exports == {"docx": "ok", "pdf": "ok"}
    for name in ("report.md", "report.docx", "report.pdf", "gate.json", "run.json"):
        assert (rig.run_dir(run_id) / name).exists()


def test_the_run_artifacts_of_each_step_exist(rig: RunRig) -> None:
    run_id = at_plan_gate(rig)
    rig.service.approve_plan(run_id, str(rig.service.view(run_id).plan_sha256))
    d = rig.run_dir(run_id)
    for name in (
        "query.md",
        "scaffold.md",
        "prompt-decomposition.json",
        "search-plan.json",
        "polish-log.json",
        "readability-decisions.json",
        "readability-recommendations.json",
        "temp/coverage-matrix.md",
        "temp/coverage-gaps.md",
        "temp/must-read.json",
        "temp/evidence-keys.json",
        "shims/research.md",
    ):
        assert (d / name).exists(), name
    assert (d / "query.md").read_text(encoding="utf-8").startswith("Method: extern übergeben")
    gate = json.loads((d / "gate.json").read_text(encoding="utf-8"))
    assert gate["passed"] is True
    assert [c["id"] for c in gate["checks"]] == [f"G{n}" for n in range(1, 13)]


def test_every_step_after_the_gate_works_from_stored_files_not_memory(
    rig: RunRig, tmp_path: Path
) -> None:
    run_id = at_plan_gate(rig)
    rig.service.approve_plan(run_id, str(rig.service.view(run_id).plan_sha256))
    saved = json.loads((rig.run_dir(run_id) / "run.json").read_text(encoding="utf-8"))
    assert saved["settings"]["template_id"] == TEMPLATE
    assert saved["gate"] == {"passed": True, "failed": []}
    assert saved["exports"] == {"docx": "ok", "pdf": "ok"}
    assert [s["step"] for s in saved["steps"] if s["status"] == "done"] == list(LIGHT_STEPS)


# ---- AC2: the plan gate ---------------------------------------------------------------------


def test_at_the_plan_gate_nothing_has_been_sent_and_no_worker_slot_is_held(rig: RunRig) -> None:
    run_id = at_plan_gate(rig)
    assert rig.searcher.calls == []
    view = rig.service.view(run_id)
    assert (view.waiting_for, view.step) == ("plan", None)
    assert view.plan is not None
    assert len(view.plan.queries) == 10
    with WorkerLock(rig.base / "worker.lock"):  # free: a waiting run does not hold it
        pass


def test_a_second_run_can_work_while_the_first_waits_for_its_plan(rig: RunRig) -> None:
    first = at_plan_gate(rig)
    second = rig.create().run_id
    assert rig.service.run(second).status == "awaiting_plan_approval"
    assert rig.service.view(first).status == "awaiting_plan_approval"


def test_a_run_started_while_another_is_active_is_refused(rig: RunRig) -> None:
    run_id = rig.create().run_id
    with WorkerLock(rig.base / "worker.lock"), pytest.raises(WorkerBusy):
        rig.service.run(run_id)
    assert rig.service.view(run_id).status == "queued"


def test_a_plan_with_a_denylisted_query_cannot_be_approved(tmp_path: Path) -> None:
    blocked = "Rückbau Forschungsreaktor Dauer"
    prep = FakePreparer(refuse={blocked: DenylistBlocked("term")})
    r = make_rig(tmp_path, preparer=prep)
    run_id = at_plan_gate(r)
    view = r.service.view(run_id)
    assert [q.blocked for q in view.plan.queries if q.blocked] == ["denylist"]  # type: ignore[union-attr]
    with pytest.raises(PlanBlocked, match="q01"):
        r.service.approve_plan(run_id, str(view.plan_sha256))
    assert r.service.view(run_id).status == "awaiting_plan_approval"
    assert r.searcher.calls == []


def test_a_blocked_query_can_be_edited_away_and_the_plan_approved(tmp_path: Path) -> None:
    blocked = "Rückbau Forschungsreaktor Dauer"
    prep = FakePreparer(refuse={blocked: DenylistBlocked("term")})
    r = make_rig(tmp_path, preparer=prep)
    run_id = at_plan_gate(r)
    edited = r.service.plan_text(run_id).replace(
        blocked, "Rückbau Forschungsreaktor Dauer allgemein"
    )
    view = r.service.update_plan(run_id, edited)
    assert not [q for q in view.plan.queries if q.blocked]  # type: ignore[union-attr]
    done = r.service.approve_plan(run_id, str(view.plan_sha256))
    assert done.status in ("done", "blocked")
    assert ("web", "Rückbau Forschungsreaktor Dauer allgemein") in r.searcher.calls


def test_a_stale_plan_hash_is_refused_and_an_edit_makes_the_old_hash_stale(rig: RunRig) -> None:
    run_id = at_plan_gate(rig)
    old = str(rig.service.view(run_id).plan_sha256)
    edited = rig.service.plan_text(run_id).replace(
        "Genehmigung Rückbau Atomgesetz", "Atomgesetz Genehmigung"
    )
    new = rig.service.update_plan(run_id, edited).plan_sha256
    assert new != old
    with pytest.raises(StalePlan):
        rig.service.approve_plan(run_id, old)
    assert rig.service.view(run_id).status == "awaiting_plan_approval"
    assert rig.service.approve_plan(run_id, str(new)).status in ("done", "blocked")


def test_the_plan_can_only_be_edited_or_approved_while_the_run_waits_for_it(rig: RunRig) -> None:
    run_id = rig.create().run_id
    with pytest.raises(WrongState, match="queued"):
        rig.service.update_plan(run_id, "q01 | Q1 | A | x")
    with pytest.raises(WrongState, match="queued"):
        rig.service.approve_plan(run_id, "0" * 64)
    with pytest.raises(WrongState, match="no search plan"):
        rig.service.plan_text(run_id)
    at_gate = at_plan_gate(rig)
    rig.service.approve_plan(at_gate, str(rig.service.view(at_gate).plan_sha256))
    with pytest.raises(WrongState, match="done"):
        rig.service.update_plan(at_gate, "q01 | Q1 | A | x")


def test_running_a_run_that_waits_or_is_finished_changes_nothing(rig: RunRig) -> None:
    run_id = at_plan_gate(rig)
    before = rig.models.calls.copy()
    assert rig.service.run(run_id).status == "awaiting_plan_approval"
    assert rig.models.calls == before
    rig.service.approve_plan(run_id, str(rig.service.view(run_id).plan_sha256))
    calls = rig.models.calls.copy()
    assert rig.service.run(run_id).status == "done"
    assert rig.models.calls == calls


# ---- external briefs ------------------------------------------------------------------------


def test_an_external_brief_is_archived_with_a_method_line_and_an_output_section(
    rig: RunRig,
) -> None:
    view = rig.create()
    row = rig.runs.get_run(view.run_id)
    assert row is not None
    assert (row.session_id, row.status, row.tier) == (None, "queued", "light")
    archived = Path(str(row.brief_path)).read_text(encoding="utf-8")
    assert archived.startswith("Method: extern übergeben\n\n# Wie lange dauert")
    assert RAW_BRIEF in archived
    assert "## Ausgabe" in archived
    assert "Technische Stellungnahme" in archived
    settings = json.loads(str(row.settings_json))
    assert (settings["report_language"], settings["template_id"], settings["tier"]) == (
        "de",
        TEMPLATE,
        "light",
    )


def test_an_external_brief_defaults_to_the_templates_language_and_format(rig: RunRig) -> None:
    view = rig.service.create_external_run(
        RAW_BRIEF, tier="light", template_id="literaturuebersicht"
    )
    row = rig.runs.get_run(view.run_id)
    assert row is not None
    settings = json.loads(str(row.settings_json))
    assert (settings["report_language"], settings["response_format"]) == ("de", "argumentative")
    short = rig.service.create_external_run(
        RAW_BRIEF, tier="light", template_id="auto", language="en", response_format="short"
    )
    stored = json.loads(str(rig.runs.get_run(short.run_id).settings_json))  # type: ignore[union-attr]
    assert (stored["report_language"], stored["response_format"]) == ("en", "short")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"tier": "dissertation"}, "tier"),
        ({"template_id": "gibt-es-nicht"}, "template"),
    ],
)
def test_an_external_run_with_bad_arguments_is_refused(
    rig: RunRig, kwargs: dict[str, str], message: str
) -> None:
    with pytest.raises(InvalidInput, match=message):
        rig.create(**kwargs)
    assert rig.runs.get_run("x") is None


def test_an_external_brief_that_cannot_be_parsed_is_refused_and_leaves_no_run(rig: RunRig) -> None:
    with pytest.raises(InvalidInput, match="title"):
        rig.service.create_external_run("Nur Text.\n1. Frage\n", tier="light", template_id=TEMPLATE)
    with pytest.raises(InvalidInput, match="numbered"):
        rig.service.create_external_run(
            "# Titel\n\nNur Text.\n", tier="light", template_id=TEMPLATE
        )
    assert not list((rig.base / "briefs").glob("*.md")) if (rig.base / "briefs").exists() else True


def test_the_full_tier_is_not_available_yet(rig: RunRig) -> None:
    run_id = rig.create(tier="full").run_id
    with pytest.raises(TierNotAvailable, match="M8"):
        rig.service.run(run_id)
    assert rig.service.view(run_id).status == "queued"


def test_an_unknown_run_is_not_found(rig: RunRig) -> None:
    for call in (rig.service.view, rig.service.run, rig.service.plan_text):
        with pytest.raises(NotFound):
            call("r-nope")


# ---- failing and resuming (AD10) ------------------------------------------------------------


def test_a_failing_step_makes_the_run_failed_with_its_reason_and_it_resumes_from_there(
    tmp_path: Path,
) -> None:
    models = ResearchModels(errors={"DecompositionDraft": LLMUnavailableError("model down")})
    r = make_rig(tmp_path, models=models)
    run_id = r.create().run_id
    view = r.service.run(run_id)
    assert (view.status, view.step) == ("failed", "1")
    assert "LLMUnavailableError: model down" in str(view.error)
    assert done_steps(r.run_dir(run_id)) == ["0"]
    models.errors.clear()
    again = r.service.run(run_id)
    assert again.status == "awaiting_plan_approval"
    assert done_steps(r.run_dir(run_id)) == ["0", "1", "2.1"]
    assert r.events.of_type("run_failed")


def test_a_crash_signal_is_not_swallowed_and_the_next_process_continues(tmp_path: Path) -> None:
    first = make_rig(tmp_path, models=ResearchModels(crash_on={"text": 3}))
    run_id = first.create().run_id
    first.service.run(run_id)
    with pytest.raises(ModelCrash):
        first.service.approve_plan(run_id, str(first.service.view(run_id).plan_sha256))
    assert first.runs.get_run(run_id).status == "running"  # type: ignore[union-attr]
    second = make_rig(tmp_path)
    view = second.service.run(run_id)
    assert view.status == "done"
    assert second.models.count("text") == 4  # sections three to six: one and two were saved
    assert second.searcher.calls == []  # every search was stored before the crash
    assert second.models.count("DecompositionDraft") == 0
    assert done_steps(second.run_dir(run_id)) == list(LIGHT_STEPS)


# ---- AC5: a gate that cannot be satisfied ---------------------------------------------------


def test_a_report_that_fails_the_gate_leaves_the_run_blocked_with_every_file_downloadable(
    tmp_path: Path,
) -> None:
    leaky = "Siehe Locus 3 dazu. " * 5 + "Der Rückbau dauert [S1]."
    models = ResearchModels(
        texts={"Management Summary": leaky}, answers={"LeakProposal": [{"hunks": []}]}
    )
    r = make_rig(tmp_path, models=models)
    run_id = at_plan_gate(r)
    view = r.service.approve_plan(run_id, str(r.service.view(run_id).plan_sha256))
    assert (view.status, view.waiting_for) == ("blocked", "nothing")
    assert "G7" in view.gate_failed
    d = r.run_dir(run_id)
    for name in ("report.md", "report.docx", "report.pdf"):
        assert (d / name).exists(), name
    gate = json.loads((d / "gate.json").read_text(encoding="utf-8"))
    assert gate["passed"] is False
    assert "G7" in [c["id"] for c in gate["checks"] if not c["passed"]]
    assert gate["rounds"]
    assert done_steps(d) == list(LIGHT_STEPS)  # the run still ran to its end


def test_a_missing_pandoc_does_not_change_the_gate_outcome(tmp_path: Path) -> None:
    r = make_rig(tmp_path, pandoc=FakePandoc(code=127))
    run_id = at_plan_gate(r)
    view = r.service.approve_plan(run_id, str(r.service.view(run_id).plan_sha256))
    assert view.status == "done"
    assert view.exports == {"docx": "pandoc_missing", "pdf": "ok"}
    assert (r.run_dir(run_id) / "report.md").exists()
    assert not (r.run_dir(run_id) / "report.docx").exists()
    assert (r.run_dir(run_id) / "report.pdf").read_bytes().startswith(b"%PDF")  # no pandoc needed
    assert any(e.data["reason"] == "pandoc_missing" for e in r.events.of_type("export_failed"))


def test_the_events_tell_the_story_of_the_run(rig: RunRig) -> None:
    run_id = at_plan_gate(rig)
    rig.service.approve_plan(run_id, str(rig.service.view(run_id).plan_sha256))
    assert [e.data["status"] for e in rig.events.of_type("run_finished")] == ["done"]
    assert [e.data["run_id"] for e in rig.events.of_type("plan_approved")] == [run_id]


# ---- mutants of the steps and the service ---------------------------------------------------


def test_a_run_whose_graph_waits_for_the_plan_is_not_left_running(rig: RunRig) -> None:
    """A row left `running` behind a graph that already waits (a crash between the two) must not
    be stuck: the status follows the graph, so the plan can be approved."""
    run_id = at_plan_gate(rig)
    rig.runs.set_status(run_id, "running")
    calls = dict(rig.models.calls)
    view = rig.service.run(run_id)
    assert (view.status, view.waiting_for) == ("awaiting_plan_approval", "plan")
    assert rig.models.calls == calls  # nothing was planned again
    assert rig.service.approve_plan(run_id, str(view.plan_sha256)).status == "done"


def test_the_approval_step_checks_the_plan_hash_itself(rig: RunRig) -> None:
    """Defence in depth: a resume that bypassed the service still cannot approve another plan."""
    run_id = at_plan_gate(rig)
    sha = str(rig.service.view(run_id).plan_sha256)
    with pytest.raises(StalePlan):
        rig.steps.confirm_plan(run_id, "0" * 64)
    rig.steps.confirm_plan(run_id, sha)


def test_the_must_read_set_is_chosen_once_and_then_read_from_its_file(rig: RunRig) -> None:
    run_id = at_plan_gate(rig)
    rig.service.approve_plan(run_id, str(rig.service.view(run_id).plan_sha256))
    path = rig.run_dir(run_id) / "temp" / "must-read.json"
    path.write_text(json.dumps({"note_ids": ["n0001"]}), encoding="utf-8")
    ctx = rig.steps._contexts(run_id)  # pyright: ignore[reportPrivateUsage]
    assert rig.steps._must_read(ctx) == ["n0001"]  # pyright: ignore[reportPrivateUsage]


def test_polish_cuts_reach_the_report_file_before_the_next_step_runs(tmp_path: Path) -> None:
    filler = "Es ist wichtig anzumerken, dass der"
    heading = TEMPLATES[TEMPLATE].headings[0]
    text = f"{german_text(60)} {filler} Rückbau zehn Jahre dauert [S1]."
    hunk = {"old": filler, "new": "Der", "reason": "Füllwort"}
    models = ResearchModels(
        texts={heading: text},
        polishes=[{"hunks": [hunk], "escalations": []}],
        crash_on={"ReadabilityProposal": 1},  # stop right after step 15
    )
    r = make_rig(tmp_path, models=models)
    run_id = r.create().run_id
    r.service.run(run_id)
    with pytest.raises(ModelCrash):
        r.service.approve_plan(run_id, str(r.service.view(run_id).plan_sha256))
    report = (r.run_dir(run_id) / "report.md").read_text(encoding="utf-8")
    assert filler not in report
    assert "Der Rückbau zehn Jahre dauert" in report
