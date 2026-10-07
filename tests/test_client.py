"""PRD M7 D2: the typed client the GUI talks to the API with. A MockTransport stands in for the
API, so no socket is opened."""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.client import ApiClient, ApiDown, ApiError, plan_rows, rows_to_lines
from app.research.models import PlannedQuery, SearchPlan
from app.research.plan_edit import parse_lines

Handler = Callable[[httpx.Request], httpx.Response]
BASE = "http://127.0.0.1:8541"


def client_for(handler: Handler, key: str = "udr_k") -> ApiClient:
    def factory(timeout: float) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)

    return ApiClient(BASE, key, factory)


def recording(body: Any = None, status: int = 200) -> tuple[ApiClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)

    return client_for(handler), seen


# ---- construction and errors ----------------------------------------------------------------


@pytest.mark.parametrize(
    "url", ["http://api.example.com:8541", "http://192.168.1.5:8541", "ftp://x"]
)
def test_a_base_url_that_is_not_loopback_is_refused(url: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        ApiClient(url, "k")


def test_every_request_carries_the_bearer_key() -> None:
    client, seen = recording({"api": "ok"})
    client.health()
    assert seen[0].headers["authorization"] == "Bearer udr_k"
    assert str(seen[0].url) == f"{BASE}/v1/health"


def test_a_refused_connection_is_api_down() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ApiDown):
        client_for(refuse).health()


def test_an_error_body_becomes_an_api_error_with_the_status() -> None:
    client, _ = recording({"detail": "stale", "status": "awaiting_plan_approval"}, 409)
    with pytest.raises(ApiError) as caught:
        client.health()
    assert (caught.value.status, caught.value.detail, caught.value.run_status) == (
        409,
        "stale",
        "awaiting_plan_approval",
    )


def test_a_validation_error_shows_its_message() -> None:
    client, _ = recording({"detail": [{"loc": ["body", "tier"], "msg": "bad tier"}]}, 422)
    with pytest.raises(ApiError) as caught:
        client.health()
    assert "bad tier" in caught.value.detail


# ---- one request per method -----------------------------------------------------------------


def body_of(request: httpx.Request) -> Any:
    return json.loads(request.content)


def test_session_calls() -> None:
    client, seen = recording({"session_id": "s1"}, 202)
    client.start_session("Frage?", [("a.pdf", b"%PDF")])
    client.send_message("s1", [{"kind": "accept"}], note="n", genug=True)
    client.send_message("s1", offer="install")
    client.revise("s1", "kürzer")
    client.set_settings("s1", template_id="auto", report_language="de")
    client.edit_brief("s1", "# T")
    client.save("s1")
    client.retry("s1")
    client.add_uploads("s1", [("b.txt", b"x")])
    got = [(r.method, r.url.path) for r in seen]
    assert got == [
        ("POST", "/v1/sessions"),
        ("POST", "/v1/sessions/s1/messages"),
        ("POST", "/v1/sessions/s1/messages"),
        ("POST", "/v1/sessions/s1/revise"),
        ("PUT", "/v1/sessions/s1/settings"),
        ("PUT", "/v1/sessions/s1/brief"),
        ("POST", "/v1/sessions/s1/save"),
        ("POST", "/v1/sessions/s1/retry"),
        ("POST", "/v1/sessions/s1/uploads"),
    ]
    assert b"Frage?" in seen[0].content
    assert body_of(seen[1]) == {
        "answers": [{"kind": "accept"}],
        "note": "n",
        "genug": True,
        "offer": None,
    }
    assert body_of(seen[2])["offer"] == "install"
    assert body_of(seen[3]) == {"feedback": "kürzer"}
    assert body_of(seen[4]) == {"template_id": "auto", "report_language": "de"}
    assert body_of(seen[5]) == {"text": "# T"}


def test_approval_sends_the_hash_and_only_the_choices_made() -> None:
    client, seen = recording({"run_id": "r1"})
    client.approve_session("s1", "a" * 64, "light", summarize_model="gemma4:e2b", tavily_cap=5)
    client.approve_session("s1", "b" * 64, "light")
    assert body_of(seen[0]) == {
        "brief_sha256": "a" * 64,
        "tier": "light",
        "summarize_model": "gemma4:e2b",
        "tavily_cap": 5,
    }
    assert body_of(seen[1]) == {"brief_sha256": "b" * 64, "tier": "light"}


def test_run_calls() -> None:
    client, seen = recording({"ok": True})
    client.run_summaries()
    client.run_summary("r1")
    client.events("r1", after=7)
    client.get_plan("r1")
    client.put_plan("r1", "q01 | 1 | A | text")
    client.approve_plan("r1", "c" * 64)
    client.approve_run("r1", "d" * 64)
    client.cancel("r1")
    client.resume("r1")
    client.gate("r1")
    client.outbound("r1")
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/v1/run-summaries"),
        ("GET", "/v1/runs/r1/summary"),
        ("GET", "/v1/runs/r1/events"),
        ("GET", "/v1/runs/r1/search-plan"),
        ("PUT", "/v1/runs/r1/search-plan"),
        ("POST", "/v1/runs/r1/search-plan/approve"),
        ("POST", "/v1/runs/r1/approve"),
        ("POST", "/v1/runs/r1/cancel"),
        ("POST", "/v1/runs/r1/resume"),
        ("GET", "/v1/runs/r1/gate"),
        ("GET", "/v1/runs/r1/outbound"),
    ]
    assert seen[2].url.params["after"] == "7"
    assert body_of(seen[4]) == {"text": "q01 | 1 | A | text"}
    assert body_of(seen[5]) == {"plan_sha256": "c" * 64}
    assert body_of(seen[6]) == {"brief_sha256": "d" * 64}


def test_delete_expects_no_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    assert client_for(handler).delete("r1") is None


def test_a_report_comes_back_as_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["format"] == "pdf"
        return httpx.Response(200, content=b"%PDF-1.7")

    assert client_for(handler).report("r1", "pdf") == b"%PDF-1.7"


def test_admin_calls() -> None:
    client, seen = recording({"terms": []})
    client.get_denylist()
    client.put_denylist(["Müller"])
    client.templates()
    client.upload_template("t.md", b"---\n")
    client.doctor()
    client.list_sessions()
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/v1/denylist"),
        ("PUT", "/v1/denylist"),
        ("GET", "/v1/templates"),
        ("POST", "/v1/templates"),
        ("GET", "/v1/doctor"),
        ("GET", "/v1/sessions"),
    ]
    assert body_of(seen[1]) == {"terms": ["Müller"]}


# ---- the plan as a table (PRD M7, D3) -------------------------------------------------------


def query(n: int, text: str, **changes: Any) -> PlannedQuery:
    base: dict[str, Any] = {
        "query_id": f"q{n:02d}",
        "item": "1.1",
        "lens": "A",
        "kind": "web",
        "original": text,
        "sent": text,
    }
    return PlannedQuery(**{**base, **changes})


PLAN = SearchPlan(
    queries=(
        query(1, "heat pump efficiency"),
        query(2, "Müller costs", sent="", blocked="denylist"),
        query(3, "scholarly", lens="B", kind="scholarly", sent="scholar", removed_terms=["x"]),
    )
)


def test_plan_rows_show_what_is_sent_and_what_blocks() -> None:
    rows = plan_rows(PLAN.model_dump(mode="json"))
    assert [r["id"] for r in rows] == ["q01", "q02", "q03"]
    assert rows[1]["blocked"] == "denylist"
    assert rows[2]["removed"] == "x"
    assert rows[2]["sent"] == "scholar"
    assert rows[0]["blocked"] == ""


def test_rows_become_lines_the_server_parses_back() -> None:
    rows = plan_rows(PLAN.model_dump(mode="json"))
    rows.append({"id": "", "item": "1.2", "lens": "C", "original": "a new | query"})
    del rows[1]
    edits = parse_lines(rows_to_lines(rows))
    assert [(e.query_id, e.item, e.lens, e.text) for e in edits] == [
        ("q01", "1.1", "A", "heat pump efficiency"),
        ("q03", "1.1", "B", "scholarly"),
        (None, "1.2", "C", "a new | query"),
    ]


def test_blank_rows_are_dropped_and_a_missing_id_means_new() -> None:
    rows = [
        {"id": None, "item": "1.1", "lens": "A", "original": "fresh"},
        {"id": "", "item": "", "lens": "", "original": ""},
        {"id": "q01", "item": "1.1", "lens": "A", "original": "   "},
    ]
    assert [(e.query_id, e.text) for e in parse_lines(rows_to_lines(rows))] == [(None, "fresh")]
