"""M6 Step 7: the facade the routes and tools share. The approval rule (D4), the tier rule (D10),
the Phase-1 job state and the admin operations."""

import json
from pathlib import Path

import pytest
from api_rig import ApiRig, fake_doctor, make_api_rig
from brief_rig import QUESTION, kinds
from research_run_rig import RAW_BRIEF, TEMPLATE
from support import make_settings

from app.adapters.outbound.ledger import MonthLedger
from app.api.errors import Forbidden
from app.api.facade import Facade
from app.api.keys import ApiKey
from app.brief.errors import InvalidInput, NotFound, StaleBrief, WrongState
from app.brief.service import SessionView
from app.research.service import TierNotAvailable
from app.templates import TemplateError


@pytest.fixture
def api(tmp_path: Path) -> ApiRig:
    return make_api_rig(tmp_path)


def at_decision(api: ApiRig) -> SessionView:
    sid = api.facade.start_session(api.owner, QUESTION).session_id
    api.jobs.wait_idle()
    api.facade.send_message(api.owner, sid, answers=kinds("accept", "accept"))
    api.jobs.wait_idle()
    view = api.facade.get_session(api.owner, sid)
    assert view.waiting_for == "decision"
    return view


def a_run(api: ApiRig, key_text: str = "owner") -> str:
    key = api.owner if key_text == "owner" else api.agent
    return api.facade.create_run(key, RAW_BRIEF, tier="light", template_id=TEMPLATE).run_id


# ---- D4: approvals ------------------------------------------------------------------------


def test_a_key_without_self_approve_cannot_approve_a_brief_and_the_session_stays(
    api: ApiRig,
) -> None:
    view = at_decision(api)
    sha = str(view.brief_sha256)
    with pytest.raises(Forbidden):
        api.facade.approve_brief(api.agent, view.session_id, sha, "light")
    assert api.facade.get_session(api.agent, view.session_id).waiting_for == "decision"
    approved = api.facade.approve_brief(api.owner, view.session_id, sha, "light")
    assert approved.run_id is not None
    assert approved.status == "approved"


def test_approving_a_brief_with_a_stale_hash_is_stale(api: ApiRig) -> None:
    view = at_decision(api)
    with pytest.raises(StaleBrief):
        api.facade.approve_brief(api.owner, view.session_id, "0" * 64, "light")


def test_the_full_tier_is_refused_when_asked_for_and_when_recommended(api: ApiRig) -> None:
    view = at_decision(api)
    sha = str(view.brief_sha256)
    with pytest.raises(TierNotAvailable, match="M8"):
        api.facade.approve_brief(api.owner, view.session_id, sha, "full")
    assert api.facade.get_session(api.owner, view.session_id).waiting_for == "decision"


def test_a_run_created_without_self_approve_waits_for_a_brief_approval(api: ApiRig) -> None:
    run_id = a_run(api, "agent")
    view = api.facade.get_run(api.agent, run_id)
    assert view.status == "awaiting_brief_approval"
    row = api.research.runs.get_run(run_id)
    assert row is not None
    assert row.created_by == api.agent.key_id
    with pytest.raises(Forbidden):
        api.facade.approve_run(api.agent, run_id, str(row.brief_sha256))
    assert api.facade.get_run(api.agent, run_id).status == "awaiting_brief_approval"
    assert api.facade.approve_run(api.owner, run_id, str(row.brief_sha256)).status == "queued"


def test_a_run_created_with_self_approve_is_queued_and_auto_means_light(api: ApiRig) -> None:
    assert api.facade.get_run(api.owner, a_run(api)).status == "queued"
    view = api.facade.create_run(api.owner, RAW_BRIEF, tier="auto", template_id=TEMPLATE)
    assert view.tier == "light"
    with pytest.raises(TierNotAvailable, match="M8"):
        api.facade.create_run(api.owner, RAW_BRIEF, tier="full", template_id=TEMPLATE)


def test_a_plan_is_approved_only_by_a_key_with_self_approve(api: ApiRig) -> None:
    run_id = a_run(api)
    api.research.service.run(run_id)
    run, text = api.facade.get_plan(api.agent, run_id)
    assert run.plan_sha256 is not None
    assert "|" in text
    with pytest.raises(Forbidden):
        api.facade.approve_plan(api.agent, run_id, run.plan_sha256)
    assert api.facade.get_run(api.agent, run_id).status == "awaiting_plan_approval"
    assert api.facade.approve_plan(api.owner, run_id, run.plan_sha256).status == "queued"


def test_editing_a_plan_needs_no_approval_right(api: ApiRig) -> None:
    run_id = a_run(api)
    api.research.service.run(run_id)
    _run, text = api.facade.get_plan(api.agent, run_id)
    changed = api.facade.update_plan(api.agent, run_id, text)
    assert changed.status == "awaiting_plan_approval"


# ---- Phase 1 over jobs --------------------------------------------------------------------


def test_the_session_view_says_busy_while_a_job_works_and_keeps_the_jobs_error(
    api: ApiRig,
) -> None:
    import threading

    sid = at_decision(api).session_id
    release = threading.Event()

    def hold() -> str | None:
        release.wait(5)
        return "LLMUnavailableError: model down"

    api.jobs.submit(sid, hold)
    assert api.facade.get_session(api.owner, sid).busy
    release.set()
    api.jobs.wait_idle()
    after = api.facade.get_session(api.owner, sid)
    assert not after.busy
    assert after.error == "LLMUnavailableError: model down"


def test_a_second_request_while_a_job_runs_is_refused_and_so_is_the_approval(api: ApiRig) -> None:
    import threading

    view = at_decision(api)
    release = threading.Event()

    def hold() -> str | None:
        release.wait(5)
        return None

    api.jobs.submit(view.session_id, hold)
    with pytest.raises(WrongState, match="busy"):
        api.facade.revise_brief(api.owner, view.session_id, "kürzer")
    with pytest.raises(WrongState, match="busy"):
        api.facade.approve_brief(api.owner, view.session_id, str(view.brief_sha256), "light")
    release.set()
    api.jobs.wait_idle()


def test_an_unknown_offer_is_invalid(api: ApiRig) -> None:
    sid = api.facade.start_session(api.owner, QUESTION).session_id
    api.jobs.wait_idle()
    with pytest.raises(InvalidInput, match="offer"):
        api.facade.send_message(api.owner, sid, offer="burn")


def test_an_unknown_session_is_not_found(api: ApiRig) -> None:
    with pytest.raises(NotFound):
        api.facade.get_session(api.owner, "s-nope")


# ---- runs: events and unknown ids ---------------------------------------------------------


def test_the_events_of_a_run_come_with_a_cursor(api: ApiRig) -> None:
    run_id = a_run(api)
    assert api.facade.run_events(api.owner, run_id, 0) == ([], 0)
    api.research.service.run(run_id)
    events, cursor = api.facade.run_events(api.owner, run_id, 0)
    assert events
    assert api.facade.run_events(api.owner, run_id, cursor) == ([], cursor)
    with pytest.raises(NotFound):
        api.facade.run_events(api.owner, "r-nope", 0)


def test_cancel_resume_and_delete_pass_through(api: ApiRig) -> None:
    run_id = a_run(api)
    assert api.facade.cancel_run(api.owner, run_id).status == "cancelled"
    assert api.facade.resume_run(api.owner, run_id).status == "queued"
    api.facade.delete_run(api.owner, run_id)
    with pytest.raises(NotFound):
        api.facade.get_run(api.owner, run_id)
    assert api.facade.list_runs(api.owner) == []


# ---- admin --------------------------------------------------------------------------------

NEW_TEMPLATE = (
    "---\nid: eigene\nname: Eigene\ndescription: Test\nlanguage: de\n"
    "default_response_format: short\n---\n\n## Eins\n\n## Zwei\n"
)


def test_an_uploaded_template_is_validated_kept_and_usable_at_once(api: ApiRig) -> None:
    template = api.facade.add_template(api.agent, "x.md", NEW_TEMPLATE.encode())
    assert template.id == "eigene"
    assert (api.data_dir / "templates" / "eigene.md").read_text("utf-8") == NEW_TEMPLATE
    assert "eigene" in [t.id for t in api.facade.list_templates(api.agent)]
    view = api.facade.create_run(api.owner, RAW_BRIEF, tier="light", template_id="eigene")
    assert view.status == "queued"


def test_a_template_with_a_known_id_is_not_replaced(api: ApiRig) -> None:
    api.facade.add_template(api.owner, "x.md", NEW_TEMPLATE.encode())
    with pytest.raises(WrongState, match="exists"):
        api.facade.add_template(api.owner, "x.md", NEW_TEMPLATE.encode())
    builtin = NEW_TEMPLATE.replace("id: eigene", f"id: {TEMPLATE}")
    with pytest.raises(WrongState):
        api.facade.add_template(api.owner, "y.md", builtin.encode())


@pytest.mark.parametrize(
    ("content", "error"),
    [
        (b"kein front matter", TemplateError),
        (NEW_TEMPLATE.replace("## Zwei", "").encode(), TemplateError),
        (
            NEW_TEMPLATE.replace("language: de", "language: de\nreference_docx: a.docx").encode(),
            TemplateError,
        ),
        (b"\xff\xfe", InvalidInput),
    ],
)
def test_a_bad_template_is_refused_and_nothing_is_kept(
    api: ApiRig, content: bytes, error: type[Exception]
) -> None:
    with pytest.raises(error):
        api.facade.add_template(api.owner, "x.md", content)
    assert not (api.data_dir / "templates").exists() or not list(
        (api.data_dir / "templates").iterdir()
    )


def test_the_denylist_is_read_and_replaced(api: ApiRig) -> None:
    assert api.facade.get_denylist(api.owner) == ()
    written = api.facade.put_denylist(api.owner, ["Projekt Atlas", "Atlas"])
    assert written == ("Projekt Atlas", "Atlas")
    assert api.facade.get_denylist(api.owner) == written
    with pytest.raises(InvalidInput):
        api.facade.put_denylist(api.owner, ["!!!"])


def test_health_shows_the_slot_and_the_queue(api: ApiRig) -> None:
    a_run(api)
    assert api.facade.health(api.owner) == {"api": "ok", "worker_lock": "free", "queued": 1}


def test_the_config_hides_secrets(tmp_path: Path) -> None:
    api = make_api_rig(tmp_path)
    settings = make_settings(data_dir=api.data_dir, tavily_api_key="geheim-123")
    facade = Facade(
        api.briefs.service,
        api.research.service,
        api.jobs,
        settings,
        api.templates,
        tmp_path / "d.txt",
        keys=api.keys,
        month=MonthLedger(tmp_path / "ledger.json", 1000),
        doctor=fake_doctor,
    )
    shown = facade.config(api.owner)
    assert "geheim-123" not in repr(shown)
    assert shown["tavily_api_key"] == "set"
    assert shown["model_reason"] == settings.model_reason


# ---- PRD M7: the per-run Tavily cap (at most the tier's) ---------------------------------------


def test_a_tavily_cap_above_the_tiers_is_refused_for_runs_and_approvals(api: ApiRig) -> None:
    with pytest.raises(InvalidInput, match="tavily_cap"):
        api.facade.create_run(
            api.owner, RAW_BRIEF, tier="light", template_id=TEMPLATE, tavily_cap=61
        )
    view = at_decision(api)
    with pytest.raises(InvalidInput, match="tavily_cap"):
        api.facade.approve_brief(
            api.owner, view.session_id, str(view.brief_sha256), "light", tavily_cap=61
        )
    assert api.facade.get_session(api.owner, view.session_id).waiting_for == "decision"


def test_a_tavily_cap_within_the_tiers_reaches_the_run(api: ApiRig) -> None:
    run_id = api.facade.create_run(
        api.owner, RAW_BRIEF, tier="light", template_id=TEMPLATE, tavily_cap=60
    ).run_id
    row = api.research.runs.get_run(run_id)
    assert row is not None
    assert json.loads(str(row.settings_json))["tavily_cap"] == 60


def test_a_session_started_over_the_api_records_its_key(api: ApiRig) -> None:
    sid = api.facade.start_session(api.agent, QUESTION).session_id
    api.jobs.wait_idle()
    assert api.briefs.parts.sessions.get(sid).created_by == api.agent.key_id  # type: ignore[union-attr]


# ---- PRD M7 D1: run summaries, session list, doctor ---------------------------------------


def finished(api: ApiRig, key: ApiKey) -> str:
    run_id = api.facade.create_run(key, RAW_BRIEF, tier="light", template_id=TEMPLATE).run_id
    if not key.self_approve:
        row = api.research.runs.get_run(run_id)
        assert row is not None
        api.facade.approve_run(api.owner, run_id, str(row.brief_sha256))
    api.research.service.run(run_id)
    service = api.research.service
    service.approve_and_run(run_id, str(service.view(run_id).plan_sha256))
    return run_id


def test_a_finished_run_summary_names_its_creator_steps_and_cap(api: ApiRig) -> None:
    run_id = finished(api, api.agent)
    summary = api.facade.run_summary(api.owner, run_id)
    assert (summary.run_id, summary.status, summary.tier) == (run_id, "done", "light")
    assert summary.title == "Wie lange dauert der Rückbau eines Forschungsreaktors?"
    assert summary.created_by == "agent"
    assert [s.step for s in summary.steps][:2] == ["0", "1"]
    assert {s.status for s in summary.steps} == {"done"}
    assert summary.started_at is not None
    assert summary.ended_at is not None
    assert summary.elapsed_s is not None
    assert (summary.credits_run, summary.credit_cap) == (0, 60)
    assert (summary.credits_month, summary.month_limit) == (
        0,
        api.facade.config(api.owner)["tavily_monthly_limit"],
    )
    assert summary.brief_sha256 is not None


def test_a_waiting_run_has_no_end_and_the_runs_own_cap(api: ApiRig) -> None:
    run_id = api.facade.create_run(
        api.agent, RAW_BRIEF, tier="light", template_id=TEMPLATE, tavily_cap=5
    ).run_id
    summary = api.facade.run_summary(api.agent, run_id)
    assert (summary.status, summary.credit_cap) == ("awaiting_brief_approval", 5)
    assert (summary.started_at, summary.ended_at, summary.elapsed_s) == (None, None, None)
    assert summary.steps == ()


def test_summaries_list_every_run_newest_first(api: ApiRig) -> None:
    first = a_run(api)
    second = a_run(api)
    assert [s.run_id for s in api.facade.run_summaries(api.owner)] == [second, first]


def test_the_session_list_names_the_key_and_the_brief_title(api: ApiRig) -> None:
    sid = api.facade.start_session(api.agent, QUESTION).session_id
    api.jobs.wait_idle()
    (row,) = api.facade.list_sessions(api.owner)
    assert (row.session_id, row.status, row.title, row.created_by) == (
        sid,
        "interviewing",
        None,
        "agent",
    )
    api.facade.send_message(api.agent, sid, answers=kinds("accept", "accept"))
    api.jobs.wait_idle()
    (row,) = api.facade.list_sessions(api.owner)
    assert (row.status, row.created_by) == ("awaiting_decision", "agent")
    assert row.title is not None


def test_the_doctor_is_what_the_composition_reports(api: ApiRig) -> None:
    report = api.facade.doctor(api.owner)
    assert report["checks"][0]["name"] == "shared_endpoint"
    assert report["roles"][0]["role"] == "reason"
