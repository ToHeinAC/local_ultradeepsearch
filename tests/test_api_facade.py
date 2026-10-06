"""M6 Step 7: the facade the routes and tools share. The approval rule (D4), the tier rule (D10),
the Phase-1 job state and the admin operations."""

from pathlib import Path

import pytest
from api_rig import ApiRig, make_api_rig
from brief_rig import QUESTION, kinds
from research_run_rig import RAW_BRIEF, TEMPLATE
from support import make_settings

from app.api.errors import Forbidden
from app.api.facade import Facade
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
    )
    shown = facade.config(api.owner)
    assert "geheim-123" not in repr(shown)
    assert shown["tavily_api_key"] == "set"
    assert shown["model_reason"] == settings.model_reason
