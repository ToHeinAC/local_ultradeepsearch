"""PRD M7, page "Suchplan", AC2 (part 2): the plan table, editing, and no approval while a query
is blocked. AppTest cannot type into `st.data_editor`, so the tests replace it with a function
that returns the rows the owner "edited"."""

from collections.abc import Callable
from typing import Any

import pytest
import streamlit as st
from gui_rig import FakeApi, run_app
from streamlit.testing.v1 import AppTest

from app.client import ApiError

SHA = "a" * 64
NEW_SHA = "b" * 64


def query(n: int, text: str, **changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "query_id": f"q{n:02d}",
        "item": "1.1",
        "lens": "A",
        "kind": "web",
        "original": text,
        "sent": text,
        "removed_terms": [],
        "blocked": None,
    }
    return {**base, **changes}


GOOD = {"queries": [query(1, "heat pump efficiency"), query(2, "cop values", removed_terms=["x"])]}
BAD = {"queries": [*GOOD["queries"], query(3, "Müller costs", sent="", blocked="denylist")]}


def plan(queries: dict[str, Any], sha: str = SHA) -> dict[str, Any]:
    return {"plan": queries, "plan_sha256": sha, "text": "# plan"}


def summary(status: str = "awaiting_plan_approval") -> dict[str, Any]:
    return {"run_id": "r-1", "status": status, "title": "T", "brief_sha256": "c" * 64}


def api(
    queries: dict[str, Any] = GOOD, status: str = "awaiting_plan_approval", **more: Any
) -> FakeApi:
    return FakeApi(run_summary=summary(status), get_plan=plan(queries), **more)


def open_page(fake: FakeApi) -> AppTest:
    return run_app(fake, page="suchplan", run="r-1")


def button(at: AppTest, label: str) -> Any:
    return next(b for b in at.button if b.label == label)


def edited(monkeypatch: pytest.MonkeyPatch, change: Callable[[list[dict[str, Any]]], None]) -> None:
    real = st.data_editor

    def fake(data: Any, **kwargs: Any) -> Any:
        rows = [dict(r) for r in data]
        change(rows)
        return rows

    monkeypatch.setattr(st, "data_editor", fake)
    assert real is not fake


def test_the_table_shows_every_query_with_what_is_sent() -> None:
    at = open_page(api())
    assert not at.exception
    assert at.header[0].value == "Suchplan"
    assert not button(at, "Freigeben").disabled
    assert not at.error


def test_a_blocked_query_is_highlighted_and_blocks_the_approval() -> None:
    fake = api(BAD)
    at = open_page(fake)
    assert any("q03" in e.value and "denylist" in e.value for e in at.error)
    assert button(at, "Freigeben").disabled
    assert fake.called("approve_plan") == []


def test_freigeben_sends_the_hash_of_the_plan_shown_and_goes_to_the_runs() -> None:
    fake = api(approve_plan={"status": "queued"})
    at = open_page(fake)
    button(at, "Freigeben").click().run()
    assert fake.called("approve_plan") == [(("r-1", SHA), {})]
    assert (at.query_params["page"], at.query_params["run"]) == ("laeufe", "r-1")


def test_an_edit_is_sent_as_lines_and_the_plan_is_checked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    edited(monkeypatch, lambda rows: rows[0].update(original="heat pump COP"))
    fake = api(put_plan=plan(GOOD, NEW_SHA))
    at = open_page(fake)
    assert button(at, "Freigeben").disabled  # an unchecked edit cannot be approved
    button(at, "Änderungen prüfen").click().run()
    ((args, _),) = fake.called("put_plan")
    assert args[0] == "r-1"
    assert args[1].splitlines()[0] == "q01 | 1.1 | A | heat pump COP"


def test_a_deleted_and_an_added_row_become_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    def change(rows: list[dict[str, Any]]) -> None:
        del rows[1]
        rows.append({"id": None, "item": "1.2", "lens": "C", "original": "criticism of cop"})

    edited(monkeypatch, change)
    fake = api(put_plan=plan(GOOD, NEW_SHA))
    button(open_page(fake), "Änderungen prüfen").click().run()
    lines = fake.called("put_plan")[0][0][1].splitlines()
    assert lines == ["q01 | 1.1 | A | heat pump efficiency", "- | 1.2 | C | criticism of cop"]


def test_a_rejected_edit_shows_the_servers_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    edited(monkeypatch, lambda rows: rows[0].update(item="9.9"))
    fake = api(put_plan=ApiError(422, "unknown item 9.9"))
    at = open_page(fake)
    button(at, "Änderungen prüfen").click().run()
    assert any("unknown item 9.9" in e.value for e in at.error)


def test_a_stale_plan_is_explained_and_reloaded() -> None:
    fake = api(approve_plan=ApiError(409, "the hash does not belong to the plan"))
    at = open_page(fake)
    button(at, "Freigeben").click().run()
    assert not at.exception
    assert any("geändert" in e.value for e in at.error)


def test_a_plan_that_is_not_open_for_approval_is_read_only() -> None:
    at = open_page(api(status="running"))
    assert any("running" in i.value for i in at.info)
    assert [b.label for b in at.button if b.label not in ("Beenden",)] == []


def test_without_a_run_the_runs_waiting_for_a_plan_are_offered() -> None:
    fake = FakeApi(
        run_summaries=[
            {"run_id": "r-1", "status": "awaiting_plan_approval", "title": "Eins"},
            {"run_id": "r-2", "status": "done", "title": "Zwei"},
        ]
    )
    at = run_app(fake, page="suchplan")
    assert at.selectbox[0].options == ["r-1 - Eins"]
    button(at, "Öffnen").click().run()
    assert at.query_params["run"] == "r-1"
