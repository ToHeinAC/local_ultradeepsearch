"""PRD M7, pages "Läufe" and "Bericht": history, the step timeline, polling, control, pending
approvals of other keys, and the rendered report with its downloads."""

from typing import Any

import pytest
from gui_rig import FakeApi, run_app
from streamlit.testing.v1 import AppTest

from app.client import ApiError
from app.gui.pages import run_detail

T = "2026-10-07T09:0{}:00+00:00"


def summary(run_id: str = "r-1", status: str = "done", **changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": run_id,
        "status": status,
        "tier": "light",
        "waiting_for": "nothing",
        "title": "Wärmepumpen",
        "brief_sha256": "c" * 64,
        "created_at": T.format(0),
        "created_by": "gui",
        "started_at": T.format(1),
        "ended_at": T.format(4),
        "elapsed_s": 180.0,
        "step": None,
        "steps": [
            {"step": "0", "status": "done", "started_at": T.format(1), "ended_at": T.format(2)},
            {"step": "1", "status": "done", "started_at": T.format(2), "ended_at": T.format(4)},
        ],
        "credits_run": 12,
        "credit_cap": 60,
        "credits_month": 100,
        "month_limit": 1000,
        "sources": 19,
        "warnings": 2,
    }
    return {**base, **changes}


def api(*runs: dict[str, Any], **more: Any) -> FakeApi:
    by_id = {r["run_id"]: r for r in runs}
    return FakeApi(
        run_summaries=list(runs),
        run_summary=lambda run_id: by_id[run_id],
        events={"events": [], "next": 0},
        outbound={"lines": []},
        **more,
    )


def button(at: AppTest, label: str) -> Any:
    return next(b for b in at.button if b.label == label)


def open_runs(fake: FakeApi, **query: str) -> AppTest:
    return run_app(fake, page="laeufe", **query)


# ---- Läufe ----------------------------------------------------------------------------------


def test_the_history_lists_every_run_and_the_selected_one_is_shown_in_full() -> None:
    at = open_runs(api(summary("r-1"), summary("r-2", "failed", title="Zwei")), run="r-1")
    assert not at.exception
    assert at.header[0].value == "Läufe"
    labels = [m.label for m in at.metric]
    assert {
        "Status",
        "Quellen",
        "Tavily-Credits (Lauf)",
        "Tavily-Credits (Monat)",
        "Warnungen",
    } <= set(labels)
    values = {m.label: m.value for m in at.metric}
    assert values["Tavily-Credits (Lauf)"] == "12 / 60"
    assert values["Tavily-Credits (Monat)"] == "100 / 1000"
    assert values["Quellen"] == "19"


def test_the_timeline_shows_each_step_with_its_state() -> None:
    at = open_runs(api(summary()), run="r-1")
    text = " ".join(m.value for m in at.markdown)
    assert "Schritt 0" in text
    assert "Schritt 1" in text


def test_choosing_a_run_puts_it_into_the_url() -> None:
    at = open_runs(api(summary("r-1"), summary("r-2")))
    at.selectbox[0].set_value("r-2").run()
    assert at.query_params["run"] == "r-2"


def test_a_finished_run_links_to_its_report() -> None:
    at = open_runs(api(summary()), run="r-1")
    button(at, "Zum Bericht").click().run()
    assert (at.query_params["page"], at.query_params["run"]) == ("bericht", "r-1")


def test_cancel_resume_and_delete_call_the_api() -> None:
    running = summary("r-1", "running", ended_at=None, elapsed_s=None, step="2")
    fake = api(running, cancel={}, resume={}, delete=None)
    button(open_runs(fake, run="r-1"), "Abbrechen").click().run()
    assert fake.called("cancel")[0][0] == ("r-1",)
    failed = api(summary("r-1", "failed"), resume={})
    button(open_runs(failed, run="r-1"), "Fortsetzen").click().run()
    assert failed.called("resume")[0][0] == ("r-1",)


def test_delete_needs_a_confirmation() -> None:
    fake = api(summary(), delete=None)
    at = open_runs(fake, run="r-1")
    assert button(at, "Löschen").disabled
    at.checkbox(key="confirm-delete").set_value(True).run()
    button(at, "Löschen").click().run()
    assert fake.called("delete")[0][0] == ("r-1",)
    assert "run" not in at.query_params


def test_runs_waiting_for_approval_are_listed_with_what_to_do() -> None:
    fake = api(
        summary("r-1", "awaiting_brief_approval", created_by="agent"),
        summary("r-2", "awaiting_plan_approval"),
        approve_run={},
        list_sessions=[
            {
                "session_id": "s5",
                "status": "awaiting_decision",
                "title": "Alt",
                "created_by": "agent",
            }
        ],
    )
    at = open_runs(fake)
    text = " ".join(m.value for m in at.markdown)
    assert "r-1" in text
    assert "agent" in text
    button(at, "Briefing freigeben (r-1)").click().run()
    assert fake.called("approve_run")[0][0] == ("r-1", "c" * 64)
    assert any(b.label == "Plan prüfen (r-2)" for b in at.button)
    assert any(b.label == "Öffnen (s5)" for b in at.button)


def test_the_events_are_read_with_a_cursor_and_never_twice() -> None:
    log = [{"type": f"e{i}", "level": "info", "ts": T.format(i), "data": {}} for i in range(5)]

    def events(run_id: str, after: int = 0) -> dict[str, Any]:
        return {"events": log[after:], "next": len(log)}

    fake = FakeApi(events=events)
    state: dict[str, Any] = {"lines": [], "next": 0}
    run_detail.merge_events(fake, "r-1", state)
    run_detail.merge_events(fake, "r-1", state)
    assert [e["type"] for e in state["lines"]] == ["e0", "e1", "e2", "e3", "e4"]
    assert [a for (a, _k) in fake.called("events")] == [("r-1",), ("r-1",)]
    assert [k["after"] for (_a, k) in fake.called("events")] == [0, 5]


def test_progress_is_polled_at_most_every_five_seconds() -> None:
    assert 1 <= run_detail.POLL_SECONDS <= 5


def test_an_api_that_goes_away_while_polling_does_not_crash_the_page() -> None:
    fake = api(summary("r-1", "running", ended_at=None, elapsed_s=None))
    at = open_runs(fake, run="r-1")
    fake.down = True
    at.run()
    assert not at.exception


# ---- Bericht --------------------------------------------------------------------------------

REPORT = "# Titel\n\n## Einleitung\n\nText.\n\n## Befunde\n\nMehr.\n"


def report_api(status: str = "done", gate: dict[str, Any] | None = None, **more: Any) -> FakeApi:
    def report(run_id: str, fmt: str) -> bytes:
        if fmt == "docx":
            raise ApiError(404, "docx: pandoc_missing")
        return REPORT.encode() if fmt == "md" else b"%PDF-1.7"

    return api(
        summary(status=status),
        gate=gate or {"passed": True, "failed": []},
        report=report,
        **more,
    )


def open_report(fake: FakeApi) -> AppTest:
    return run_app(fake, page="bericht", run="r-1")


def test_the_report_is_rendered_with_the_gate_result_and_downloads() -> None:
    at = open_report(report_api())
    assert not at.exception
    assert at.header[0].value == "Bericht"
    assert any("Einleitung" in m.value for m in at.markdown)
    assert any("Gate bestanden" in s.value for s in at.success)


def test_a_missing_docx_is_a_hint_not_an_error() -> None:
    at = open_report(report_api())
    assert not at.error
    assert any("pandoc" in c.value for c in at.caption)


def test_a_blocked_run_shows_the_failed_checks() -> None:
    at = open_report(report_api("blocked", gate={"passed": False, "failed": ["G3", "G7"]}))
    assert any("G3" in e.value and "G7" in e.value for e in at.error)


def test_a_huge_report_is_split_into_collapsible_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    big = "# Titel\n\n" + "".join(f"## Abschnitt {i}\n\n{'x' * 20_000}\n\n" for i in range(6))
    fake = report_api()
    fake.data["report"] = lambda run_id, fmt: big.encode() if fmt == "md" else b"%PDF"
    at = open_report(fake)
    assert not at.exception
    assert [e.label for e in at.expander if e.label.startswith("Abschnitt")] == [
        f"Abschnitt {i}" for i in range(6)
    ]


def test_a_run_without_a_report_yet_says_so() -> None:
    fake = report_api()
    fake.data["report"] = ApiError(409, "report not ready", "running")
    at = open_report(fake)
    assert not at.exception
    assert any("running" in i.value for i in at.info)
