"""M6 AC1, AC2: every route needs a valid key; approvals need `self_approve`."""

import re
from pathlib import Path

import pytest
from api_rig import ApiRig, make_api_rig
from fastapi.routing import APIRoute

from app.api.rest import build_auth, build_routers

EXPECTED = {
    ("POST", "/v1/sessions"),
    ("POST", "/v1/sessions/{session_id}/uploads"),
    ("POST", "/v1/sessions/{session_id}/messages"),
    ("GET", "/v1/sessions/{session_id}"),
    ("POST", "/v1/sessions/{session_id}/revise"),
    ("PUT", "/v1/sessions/{session_id}/settings"),
    ("PUT", "/v1/sessions/{session_id}/brief"),
    ("POST", "/v1/sessions/{session_id}/approve"),
    ("POST", "/v1/sessions/{session_id}/save"),
    ("GET", "/v1/sessions"),
    ("POST", "/v1/sessions/{session_id}/retry"),
    ("POST", "/v1/runs"),
    ("GET", "/v1/runs"),
    ("GET", "/v1/runs/{run_id}"),
    ("GET", "/v1/runs/{run_id}/summary"),
    ("POST", "/v1/runs/{run_id}/approve"),
    ("GET", "/v1/runs/{run_id}/events"),
    ("GET", "/v1/runs/{run_id}/stream"),
    ("GET", "/v1/runs/{run_id}/search-plan"),
    ("PUT", "/v1/runs/{run_id}/search-plan"),
    ("POST", "/v1/runs/{run_id}/search-plan/approve"),
    ("GET", "/v1/runs/{run_id}/report"),
    ("GET", "/v1/runs/{run_id}/gate"),
    ("GET", "/v1/runs/{run_id}/outbound"),
    ("POST", "/v1/runs/{run_id}/cancel"),
    ("POST", "/v1/runs/{run_id}/resume"),
    ("DELETE", "/v1/runs/{run_id}"),
    ("GET", "/v1/templates"),
    ("POST", "/v1/templates"),
    ("GET", "/v1/denylist"),
    ("PUT", "/v1/denylist"),
    ("GET", "/v1/health"),
    ("GET", "/v1/config"),
    ("GET", "/v1/run-summaries"),
    ("GET", "/v1/doctor"),
}


@pytest.fixture
def api(tmp_path: Path) -> ApiRig:
    return make_api_rig(tmp_path)


@pytest.fixture(scope="module")
def shared(tmp_path_factory: pytest.TempPathFactory) -> tuple[ApiRig, str]:
    """One rig for the many requests that change nothing, and a key revoked in it."""
    rig = make_api_rig(tmp_path_factory.mktemp("auth"))
    revoked, text = rig.keys.create("revoked", self_approve=True)
    rig.keys.revoke(revoked.key_id)
    return rig, text


def route_table(api: ApiRig) -> set[tuple[str, str]]:
    """Method and path of every route the app is built from (the routers, before inclusion)."""
    routers = build_routers(api.facade, build_auth(api.keys))
    return {
        (method, route.path)
        for router in routers
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    }


def test_the_route_table_is_the_expected_one(shared: tuple[ApiRig, str]) -> None:
    """A route added without a row here (and so without an auth check below) fails this test."""
    assert route_table(shared[0]) == EXPECTED


def fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


@pytest.mark.parametrize(("method", "path"), sorted(EXPECTED))
def test_a_route_without_a_key_says_401(shared: tuple[ApiRig, str], method: str, path: str) -> None:
    response = shared[0].client().request(method, fill(path), headers={"Authorization": ""})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(("method", "path"), sorted(EXPECTED))
def test_a_route_with_a_wrong_key_says_401(
    shared: tuple[ApiRig, str], method: str, path: str
) -> None:
    assert shared[0].client("udr_wrong").request(method, fill(path)).status_code == 401


@pytest.mark.parametrize(("method", "path"), sorted(EXPECTED))
def test_a_route_with_a_revoked_key_says_401(
    shared: tuple[ApiRig, str], method: str, path: str
) -> None:
    rig, revoked_text = shared
    assert rig.client(revoked_text).request(method, fill(path)).status_code == 401


@pytest.mark.parametrize("header", ["Bearer", "Basic abc", "udr_abc", "Bearer  ", "bearer"])
def test_a_malformed_authorization_header_says_401(shared: tuple[ApiRig, str], header: str) -> None:
    response = shared[0].client().get("/v1/health", headers={"Authorization": header})
    assert response.status_code == 401


def test_a_valid_key_passes_and_the_docs_are_off(shared: tuple[ApiRig, str]) -> None:
    client = shared[0].client()
    assert client.get("/v1/health").status_code == 200
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# ---- AC2 ---------------------------------------------------------------------------------


def test_a_key_without_self_approve_gets_403_on_every_approval_and_nothing_moves(
    api: ApiRig,
) -> None:
    agent = api.client(api.agent_text)
    created = agent.post(
        "/v1/runs",
        json={"brief": _brief(), "tier": "light", "template_id": _template()},
    )
    assert created.status_code == 201
    assert created.json()["status"] == "awaiting_brief_approval"
    run_id = created.json()["run_id"]
    row = api.research.runs.get_run(run_id)
    assert row is not None
    denied = agent.post(f"/v1/runs/{run_id}/approve", json={"brief_sha256": row.brief_sha256})
    assert denied.status_code == 403
    assert agent.get(f"/v1/runs/{run_id}").json()["status"] == "awaiting_brief_approval"
    api.research.service.approve_external(run_id, str(row.brief_sha256))
    api.research.service.run(run_id)
    plan_sha = api.research.service.view(run_id).plan_sha256
    denied = agent.post(f"/v1/runs/{run_id}/search-plan/approve", json={"plan_sha256": plan_sha})
    assert denied.status_code == 403
    assert agent.get(f"/v1/runs/{run_id}").json()["status"] == "awaiting_plan_approval"
    owner = api.client()
    ok = owner.post(f"/v1/runs/{run_id}/search-plan/approve", json={"plan_sha256": plan_sha})
    assert ok.status_code == 202
    assert ok.json()["status"] == "queued"


def test_a_run_of_a_key_without_self_approve_is_approved_by_a_key_with_it(api: ApiRig) -> None:
    created = api.client(api.agent_text).post(
        "/v1/runs", json={"brief": _brief(), "tier": "light", "template_id": _template()}
    )
    run_id = created.json()["run_id"]
    row = api.research.runs.get_run(run_id)
    assert row is not None
    approved = api.client().post(
        f"/v1/runs/{run_id}/approve", json={"brief_sha256": row.brief_sha256}
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "queued"


def _brief() -> str:
    from research_run_rig import RAW_BRIEF

    return RAW_BRIEF


def _template() -> str:
    from research_run_rig import TEMPLATE

    return TEMPLATE
