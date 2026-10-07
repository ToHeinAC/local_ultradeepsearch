"""M6 AC3 and the REST behaviour: a whole session over HTTP, runs, the plan, events and SSE,
control, files and admin."""

import json
from pathlib import Path

import pytest
from api_rig import ApiRig, make_api_rig
from brief_rig import QUESTION
from fastapi.testclient import TestClient
from research_run_rig import RAW_BRIEF, TEMPLATE
from support import make_pdf

TWO_ANSWERS = {"answers": [{"kind": "accept"}, {"kind": "accept"}]}


@pytest.fixture
def api(tmp_path: Path) -> ApiRig:
    return make_api_rig(tmp_path)


@pytest.fixture
def client(api: ApiRig) -> TestClient:
    return api.client()


def create_run(client: TestClient, **overrides: object) -> str:
    body = {"brief": RAW_BRIEF, "tier": "light", "template_id": TEMPLATE, **overrides}
    response = client.post("/v1/runs", json=body)
    assert response.status_code == 201, response.text
    return response.json()["run_id"]


def finished_run(api: ApiRig, client: TestClient) -> str:
    run_id = create_run(client)
    api.research.service.run(run_id)
    api.research.service.approve_and_run(run_id, str(api.research.service.view(run_id).plan_sha256))
    return run_id


@pytest.fixture(scope="module")
def done(tmp_path_factory: pytest.TempPathFactory) -> tuple[TestClient, str]:
    """One finished run for the tests that only read: a whole run costs a third of a second."""
    api = make_api_rig(tmp_path_factory.mktemp("done"))
    client = api.client()
    return client, finished_run(api, client)


# ---- Phase 1 over HTTP ---------------------------------------------------------------------


def test_a_whole_session_runs_over_http_with_202_and_polling(
    api: ApiRig, client: TestClient
) -> None:
    started = client.post("/v1/sessions", data={"question": QUESTION})
    assert started.status_code == 202
    sid = started.json()["session_id"]
    api.jobs.wait_idle()
    assert client.get(f"/v1/sessions/{sid}").json()["waiting_for"] == "questions"
    answered = client.post(f"/v1/sessions/{sid}/messages", json=TWO_ANSWERS)
    assert answered.status_code == 202
    api.jobs.wait_idle()
    view = client.get(f"/v1/sessions/{sid}").json()
    assert (view["waiting_for"], view["busy"]) == ("decision", False)
    approved = client.post(
        f"/v1/sessions/{sid}/approve", json={"brief_sha256": view["brief_sha256"], "tier": "light"}
    )
    assert approved.status_code == 200
    assert approved.json()["run_id"].startswith("r-")


def test_a_stale_brief_hash_is_409_and_a_full_tier_is_422(api: ApiRig, client: TestClient) -> None:
    sid = client.post("/v1/sessions", data={"question": QUESTION}).json()["session_id"]
    api.jobs.wait_idle()
    client.post(f"/v1/sessions/{sid}/messages", json=TWO_ANSWERS)
    api.jobs.wait_idle()
    sha = client.get(f"/v1/sessions/{sid}").json()["brief_sha256"]
    stale = client.post(
        f"/v1/sessions/{sid}/approve", json={"brief_sha256": "0" * 64, "tier": "light"}
    )
    assert stale.status_code == 409
    full = client.post(f"/v1/sessions/{sid}/approve", json={"brief_sha256": sha, "tier": "full"})
    assert full.status_code == 422
    assert "M8" in full.json()["detail"]


def test_a_rejected_upload_is_422_and_leaves_no_session(api: ApiRig, client: TestClient) -> None:
    response = client.post(
        "/v1/sessions",
        data={"question": QUESTION},
        files=[("files", ("virus.exe", b"MZ", "application/octet-stream"))],
    )
    assert response.status_code == 422
    assert api.briefs.service.list_sessions() == []


def test_uploads_are_read_from_multipart(api: ApiRig, client: TestClient) -> None:
    pdf = make_pdf(["Diese Seite hat genug Text, damit keine OCR noetig ist."])
    response = client.post(
        "/v1/sessions",
        data={"question": QUESTION},
        files=[("files", ("a.pdf", pdf, "application/pdf"))],
    )
    assert response.status_code == 202
    assert [u["name"] for u in response.json()["uploads"]] == ["a.pdf"]
    api.jobs.wait_idle()


def test_unknown_ids_are_404(client: TestClient) -> None:
    assert client.get("/v1/sessions/s-nope").status_code == 404
    assert client.get("/v1/runs/r-nope").status_code == 404
    assert client.get("/v1/runs/r-nope/events").status_code == 404
    assert client.get("/v1/runs/r-nope/stream").status_code == 404
    assert client.delete("/v1/runs/r-nope").status_code == 404


def test_a_bad_body_is_422_with_the_fields(client: TestClient) -> None:
    response = client.post("/v1/runs", json={"tier": "light"})
    assert response.status_code == 422
    assert (
        client.post("/v1/runs", json={"brief": "x", "tier": "huge", "template_id": "t"}).status_code
        == 422
    )


# ---- runs ----------------------------------------------------------------------------------


def test_a_run_is_created_listed_and_read(client: TestClient) -> None:
    run_id = create_run(client)
    assert client.get(f"/v1/runs/{run_id}").json()["status"] == "queued"
    assert [r["run_id"] for r in client.get("/v1/runs").json()] == [run_id]
    assert (
        client.post(
            "/v1/runs", json={"brief": RAW_BRIEF, "tier": "full", "template_id": TEMPLATE}
        ).status_code
        == 422
    )


def test_a_report_before_the_run_is_done_is_409_with_the_status(client: TestClient) -> None:
    run_id = create_run(client)
    response = client.get(f"/v1/runs/{run_id}/report")
    assert response.status_code == 409
    assert response.json()["status"] == "queued"


def test_the_report_comes_in_every_format_once_done(done: tuple[TestClient, str]) -> None:
    client, run_id = done
    md = client.get(f"/v1/runs/{run_id}/report")
    assert md.status_code == 200
    assert md.text.startswith("# ")
    assert md.headers["content-type"].startswith("text/markdown")
    assert client.get(f"/v1/runs/{run_id}/report?format=pdf").content.startswith(b"%PDF")
    assert client.get(f"/v1/runs/{run_id}/report?format=docx").status_code == 200
    assert client.get(f"/v1/runs/{run_id}/report?format=odt").status_code == 422
    assert client.get(f"/v1/runs/{run_id}/gate").json()["passed"] is True
    assert client.get(f"/v1/runs/{run_id}/outbound").json() == {"lines": []}


def test_the_plan_is_read_edited_and_approved_once(api: ApiRig, client: TestClient) -> None:
    run_id = create_run(client)
    api.research.service.run(run_id)
    plan = client.get(f"/v1/runs/{run_id}/search-plan").json()
    assert plan["plan_sha256"]
    assert plan["text"]
    edited = client.put(f"/v1/runs/{run_id}/search-plan", json={"text": plan["text"]})
    assert edited.status_code == 200
    sha = edited.json()["plan_sha256"]
    stale = client.post(f"/v1/runs/{run_id}/search-plan/approve", json={"plan_sha256": "0" * 64})
    assert stale.status_code == 409
    ok = client.post(f"/v1/runs/{run_id}/search-plan/approve", json={"plan_sha256": sha})
    assert (ok.status_code, ok.json()["status"]) == (202, "queued")
    again = client.post(f"/v1/runs/{run_id}/search-plan/approve", json={"plan_sha256": sha})
    assert again.status_code == 409


def test_cancel_resume_and_delete(api: ApiRig, client: TestClient) -> None:
    run_id = create_run(client)
    assert client.post(f"/v1/runs/{run_id}/cancel").json()["status"] == "cancelled"
    resumed = client.post(f"/v1/runs/{run_id}/resume")
    assert (resumed.status_code, resumed.json()["status"]) == (202, "queued")
    api.research.runs.set_status(run_id, "running")
    assert client.delete(f"/v1/runs/{run_id}").status_code == 409
    api.research.runs.set_status(run_id, "failed")
    assert client.delete(f"/v1/runs/{run_id}").status_code == 204
    assert client.get(f"/v1/runs/{run_id}").status_code == 404


def test_deleting_a_run_removes_its_files(api: ApiRig, client: TestClient) -> None:
    run_id = create_run(client)
    api.research.service.run(run_id)
    assert api.research.run_dir(run_id).exists()
    assert client.delete(f"/v1/runs/{run_id}").status_code == 204
    assert not api.research.run_dir(run_id).exists()


# ---- events and SSE ------------------------------------------------------------------------


def test_events_come_with_a_cursor(done: tuple[TestClient, str]) -> None:
    client, run_id = done
    first = client.get(f"/v1/runs/{run_id}/events").json()
    assert first["events"]
    assert first["next"] == len(first["events"])
    rest = client.get(f"/v1/runs/{run_id}/events?after={first['next'] - 1}").json()
    assert [e["type"] for e in rest["events"]] == ["run_finished"]
    assert client.get(f"/v1/runs/{run_id}/events?after=-1").status_code == 422


def sse_events(text: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for block in text.split("\n\n"):
        fields = dict(
            line.split(": ", 1)
            for line in block.splitlines()
            if ": " in line and not line.startswith(":")
        )
        if "id" in fields:
            found.append((int(fields["id"]), fields["event"]))
    return found


def test_the_stream_replays_a_finished_run_and_ends(done: tuple[TestClient, str]) -> None:
    client, run_id = done
    response = client.get(f"/v1/runs/{run_id}/stream")
    assert response.headers["content-type"].startswith("text/event-stream")
    events = sse_events(response.text)
    total = client.get(f"/v1/runs/{run_id}/events").json()["next"]
    assert [n for n, _ in events] == list(range(1, total + 1))
    assert events[-1][1] == "run_finished"
    payload = [b for b in response.text.split("\n\n") if "run_finished" in b][-1]
    assert json.loads(payload.split("data: ", 1)[1])["data"]["status"] == "done"


def test_the_stream_resumes_after_last_event_id(done: tuple[TestClient, str]) -> None:
    client, run_id = done
    total = client.get(f"/v1/runs/{run_id}/events").json()["next"]
    response = client.get(f"/v1/runs/{run_id}/stream", headers={"Last-Event-ID": str(total - 1)})
    assert [n for n, _ in sse_events(response.text)] == [total]
    junk = client.get(f"/v1/runs/{run_id}/stream", headers={"Last-Event-ID": "abc"})
    assert sse_events(junk.text)[0][0] == 1


# ---- admin ---------------------------------------------------------------------------------


NEW_TEMPLATE = (
    "---\nid: eigene\nname: Eigene\ndescription: Test\nlanguage: de\n"
    "default_response_format: short\n---\n\n## Eins\n\n## Zwei\n"
)


def test_templates_are_listed_uploaded_and_never_replaced(client: TestClient) -> None:
    ids = [t["id"] for t in client.get("/v1/templates").json()]
    assert TEMPLATE in ids
    files = {"file": ("eigene.md", NEW_TEMPLATE.encode(), "text/markdown")}
    created = client.post("/v1/templates", files=files)
    assert created.status_code == 201
    assert created.json()["sections"] == ["Eins", "Zwei"]
    assert client.post("/v1/templates", files=files).status_code == 409
    bad = {"file": ("x.md", b"kein front matter", "text/markdown")}
    assert client.post("/v1/templates", files=bad).status_code == 422


def test_the_denylist_is_read_and_replaced(client: TestClient) -> None:
    assert client.get("/v1/denylist").json() == {"terms": []}
    put = client.put("/v1/denylist", json={"terms": ["Projekt Atlas"]})
    assert put.json() == {"terms": ["Projekt Atlas"]}
    assert client.get("/v1/denylist").json() == {"terms": ["Projekt Atlas"]}
    assert client.put("/v1/denylist", json={"terms": ["!!!"]}).status_code == 422


def test_health_and_config(api: ApiRig, client: TestClient) -> None:
    create_run(client)
    assert client.get("/v1/health").json() == {"api": "ok", "worker_lock": "free", "queued": 1}
    config = client.get("/v1/config").json()
    assert config["model_reason"]


def test_the_tavily_cap_is_validated_by_the_schema_and_by_the_tier(client: TestClient) -> None:
    body = {"brief": RAW_BRIEF, "tier": "light", "template_id": TEMPLATE}
    assert client.post("/v1/runs", json={**body, "tavily_cap": -1}).status_code == 422
    assert client.post("/v1/runs", json={**body, "tavily_cap": 61}).status_code == 422
    assert client.post("/v1/runs", json={**body, "tavily_cap": 10}).status_code == 201


def test_the_session_list_and_the_retry_route(api: ApiRig, client: TestClient) -> None:
    sid = client.post("/v1/sessions", data={"question": QUESTION}).json()["session_id"]
    api.jobs.wait_idle()
    rows = client.get("/v1/sessions").json()
    assert [(r["session_id"], r["status"], r["created_by"]) for r in rows] == [
        (sid, "interviewing", "owner")
    ]
    assert client.post(f"/v1/sessions/{sid}/retry").status_code == 202
    assert client.post("/v1/sessions/s-nope/retry").status_code == 404


def test_run_summaries_over_http(client: TestClient) -> None:
    run_id = create_run(client)
    listed = client.get("/v1/run-summaries").json()
    assert [r["run_id"] for r in listed] == [run_id]
    one = client.get(f"/v1/runs/{run_id}/summary").json()
    assert (one["status"], one["created_by"], one["credit_cap"]) == ("queued", "owner", 60)
    assert client.get("/v1/runs/r-nope/summary").status_code == 404


def test_doctor_over_http(client: TestClient) -> None:
    body = client.get("/v1/doctor").json()
    assert {"checks", "roles"} <= set(body)
