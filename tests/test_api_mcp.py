"""M6 AC6: the MCP tools. An in-memory client lists them all and drives a run to its report;
over HTTP the endpoint needs a key."""

from pathlib import Path
from typing import Any

import anyio
import pytest
from api_rig import ApiRig, make_api_rig
from brief_rig import QUESTION
from fastapi.testclient import TestClient
from mcp import Client
from research_run_rig import RAW_BRIEF, TEMPLATE

from app.api.keys import ApiKey
from app.api.mcp import build_mcp

TOOLS = {
    "start_clarification",
    "answer_clarification",
    "get_session",
    "revise_brief",
    "approve_brief",
    "start_research",
    "get_run_status",
    "get_search_plan",
    "update_search_plan",
    "approve_search_plan",
    "get_report",
    "list_templates",
    "cancel_run",
}


@pytest.fixture
def api(tmp_path: Path) -> ApiRig:
    return make_api_rig(tmp_path)


def server(api: ApiRig, key: ApiKey | None = None):  # type: ignore[no-untyped-def]
    chosen = key or api.owner
    return build_mcp(api.facade, lambda: chosen)


def call(api: ApiRig, tool: str, arguments: dict[str, Any], key: ApiKey | None = None) -> Any:
    async def main() -> Any:
        async with Client(server(api, key)) as client:
            return await client.call_tool(tool, arguments)

    return anyio.run(main)


def test_the_server_lists_exactly_the_thirteen_tools(api: ApiRig) -> None:
    async def main() -> set[str]:
        async with Client(server(api)) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert anyio.run(main) == TOOLS


def test_a_research_goes_from_start_research_to_get_report(api: ApiRig) -> None:
    started = call(
        api,
        "start_research",
        {"brief": RAW_BRIEF, "tier": "light", "template_id": TEMPLATE, "language": "de"},
    )
    run_id = started.structured_content["run_id"]
    assert started.structured_content["status"] == "queued"
    assert api.research.service.run(run_id).status == "awaiting_plan_approval"  # the worker's turn
    plan = call(api, "get_search_plan", {"run_id": run_id}).structured_content
    assert plan["plan_sha256"]
    approved = call(
        api, "approve_search_plan", {"run_id": run_id, "plan_sha256": plan["plan_sha256"]}
    )
    assert approved.structured_content["status"] == "queued"
    assert api.research.service.run(run_id).status == "done"  # the worker again
    assert call(api, "get_run_status", {"run_id": run_id}).structured_content["status"] == "done"
    report = call(api, "get_report", {"run_id": run_id})
    assert report.structured_content["result"].startswith("# ")


def test_a_domain_error_is_a_tool_error_with_its_message(api: ApiRig) -> None:
    unknown = call(api, "get_run_status", {"run_id": "r-nope"})
    assert unknown.is_error
    assert "r-nope" in unknown.content[0].text
    early = call(
        api, "start_research", {"brief": RAW_BRIEF, "tier": "full", "template_id": TEMPLATE}
    )
    assert early.is_error
    assert "M8" in early.content[0].text


def test_the_approval_tools_need_a_key_that_may_approve(api: ApiRig) -> None:
    run_id = call(
        api, "start_research", {"brief": RAW_BRIEF, "tier": "light", "template_id": TEMPLATE}
    ).structured_content["run_id"]
    api.research.service.run(run_id)
    sha = api.research.service.view(run_id).plan_sha256
    denied = call(api, "approve_search_plan", {"run_id": run_id, "plan_sha256": sha}, key=api.agent)
    assert denied.is_error
    assert api.research.service.view(run_id).status == "awaiting_plan_approval"


def test_the_clarification_tools_walk_a_session_to_its_approval(api: ApiRig) -> None:
    started = call(api, "start_clarification", {"question": QUESTION}).structured_content
    sid = started["session_id"]
    api.jobs.wait_idle()
    assert (
        call(api, "get_session", {"session_id": sid}).structured_content["waiting_for"]
        == "questions"
    )
    call(
        api,
        "answer_clarification",
        {"session_id": sid, "answers": [{"kind": "accept"}, {"kind": "accept"}]},
    )
    api.jobs.wait_idle()
    view = call(api, "get_session", {"session_id": sid}).structured_content
    assert view["waiting_for"] == "decision"
    revised = call(api, "revise_brief", {"session_id": sid, "feedback": "kürzer"})
    assert not revised.is_error
    api.jobs.wait_idle()
    sha = call(api, "get_session", {"session_id": sid}).structured_content["brief_sha256"]
    done = call(
        api, "approve_brief", {"session_id": sid, "brief_sha256": sha, "tier": "light"}
    ).structured_content
    assert done["run_id"].startswith("r-")


def test_templates_are_listed_and_a_run_can_be_cancelled(api: ApiRig) -> None:
    templates = call(api, "list_templates", {}).structured_content["result"]
    assert TEMPLATE in [t["id"] for t in templates]
    run_id = call(
        api, "start_research", {"brief": RAW_BRIEF, "tier": "light", "template_id": TEMPLATE}
    ).structured_content["run_id"]
    assert call(api, "cancel_run", {"run_id": run_id}).structured_content["status"] == "cancelled"


# ---- over HTTP -----------------------------------------------------------------------------

LOCAL = "http://127.0.0.1:8541"  # the SDK refuses other Host headers (DNS rebinding)

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


def test_the_endpoint_needs_a_valid_key(api: ApiRig) -> None:
    with TestClient(api.app, base_url=LOCAL) as client:
        assert client.post("/mcp", json=INITIALIZE, headers=HEADERS).status_code == 401
        wrong = {**HEADERS, "Authorization": "Bearer udr_wrong"}
        assert client.post("/mcp", json=INITIALIZE, headers=wrong).status_code == 401


def test_a_valid_key_reaches_the_server_and_its_tools_see_that_key(api: ApiRig) -> None:
    headers = {**HEADERS, "Authorization": f"Bearer {api.agent_text}"}
    with TestClient(api.app, base_url=LOCAL) as client:
        ok = client.post("/mcp", json=INITIALIZE, headers=headers)
        assert ok.status_code == 200, ok.text
        call_run = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "start_research",
                "arguments": {"brief": RAW_BRIEF, "tier": "light", "template_id": TEMPLATE},
            },
        }
        result = client.post("/mcp", json=call_run, headers=headers).json()
    status = result["result"]["structuredContent"]["status"]
    assert status == "awaiting_brief_approval"  # the agent key may not approve: its key was used
