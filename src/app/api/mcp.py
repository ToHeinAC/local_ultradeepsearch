"""The MCP server (PRD M6): the same facade as REST, as 13 tools over streamable HTTP at `/mcp`.

Each tool is a thin call into the facade with the key of the request. A domain error becomes a
tool error carrying the HTTP status and message of `app.api.errors`.
"""

import contextvars
from collections.abc import Callable
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import TypeAdapter
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.errors import to_http
from app.api.facade import Facade
from app.api.keys import ApiKey, KeyStore, bearer_token
from app.api.schemas import TemplateOut
from app.brief.protocol import AnswerInput
from app.brief.service import SessionView
from app.research.service import RunView

Tier = Literal["light", "full", "auto"]
_request_key: contextvars.ContextVar[ApiKey | None] = contextvars.ContextVar(
    "request_key", default=None
)


def current_key() -> ApiKey:
    """The key of the request being served; set by `KeyMiddleware`."""
    key = _request_key.get()
    if key is None:
        raise ToolError("401: a valid API key is required")
    return key


def _plain(adapter_type: Any, value: Any) -> dict[str, Any]:
    return TypeAdapter(adapter_type).dump_python(value, mode="json")


def _guard(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except Exception as exc:
        mapped = to_http(exc)
        if mapped is None:
            raise
        status, body = mapped
        raise ToolError(f"{status}: {body['detail']}") from exc


def build_mcp(facade: Facade, key_of: Callable[[], ApiKey]) -> MCPServer[Any]:
    server: MCPServer[Any] = MCPServer("UltraDeepSearch")
    _clarification_tools(server, facade, key_of)
    _approval_tools(server, facade, key_of)
    _research_tools(server, facade, key_of)
    _plan_tools(server, facade, key_of)
    _output_tools(server, facade, key_of)
    return server


def _clarification_tools(
    server: MCPServer[Any], facade: Facade, key_of: Callable[[], ApiKey]
) -> None:
    def session(view: SessionView) -> dict[str, Any]:
        return _plain(SessionView, view)

    @server.tool()
    def start_clarification(question: str) -> dict[str, Any]:
        """Open a Phase-1 session from a question. It works in the background: poll
        get_session until `busy` is false."""
        return session(_guard(lambda: facade.start_session(key_of(), question)))

    @server.tool()
    def answer_clarification(
        session_id: str, answers: list[AnswerInput], note: str = "", genug: bool = False
    ) -> dict[str, Any]:
        """Answer the open questions (one answer per question), or set genug to stop asking."""
        return session(
            _guard(
                lambda: facade.send_message(
                    key_of(), session_id, answers=answers, note=note, genug=genug
                )
            )
        )

    @server.tool()
    def get_session(session_id: str) -> dict[str, Any]:
        """Where a session stands: `waiting_for`, the questions, the brief and its hash."""
        return session(_guard(lambda: facade.get_session(key_of(), session_id)))

    @server.tool()
    def revise_brief(session_id: str, feedback: str) -> dict[str, Any]:
        """Ask for another draft of the brief (background; poll get_session)."""
        return session(_guard(lambda: facade.revise_brief(key_of(), session_id, feedback)))


def _approval_tools(server: MCPServer[Any], facade: Facade, key_of: Callable[[], ApiKey]) -> None:
    @server.tool()
    def approve_brief(
        session_id: str,
        brief_sha256: str,
        tier: Tier,
        summarize_model: str | None = None,
        tavily_cap: int | None = None,
    ) -> dict[str, Any]:
        """Approve the brief with this hash and create its run. Needs a key with self_approve."""
        return _plain(
            SessionView,
            _guard(
                lambda: facade.approve_brief(
                    key_of(), session_id, brief_sha256, tier, summarize_model, tavily_cap
                )
            ),
        )


def _research_tools(server: MCPServer[Any], facade: Facade, key_of: Callable[[], ApiKey]) -> None:
    def run(view: RunView) -> dict[str, Any]:
        return _plain(RunView, view)

    @server.tool()
    def start_research(
        brief: str,
        tier: Tier,
        template_id: str,
        language: str | None = None,
        response_format: str | None = None,
        tavily_cap: int | None = None,
    ) -> dict[str, Any]:
        """Queue a run for a finished brief (a title and numbered research questions)."""
        return run(
            _guard(
                lambda: facade.create_run(
                    key_of(),
                    brief,
                    tier=tier,
                    template_id=template_id,
                    language=language,
                    response_format=response_format,
                    tavily_cap=tavily_cap,
                )
            )
        )

    @server.tool()
    def get_run_status(run_id: str) -> dict[str, Any]:
        """Where a run stands: status, step, error, the plan and its hash."""
        return run(_guard(lambda: facade.get_run(key_of(), run_id)))

    @server.tool()
    def cancel_run(run_id: str) -> dict[str, Any]:
        """Cancel a run: at once if it does not execute, else at its next safe point."""
        return run(_guard(lambda: facade.cancel_run(key_of(), run_id)))


def _plan_tools(server: MCPServer[Any], facade: Facade, key_of: Callable[[], ApiKey]) -> None:
    def run(view: RunView) -> dict[str, Any]:
        return _plain(RunView, view)

    @server.tool()
    def get_search_plan(run_id: str) -> dict[str, Any]:
        """The search plan as editable text, with the hash an approval needs."""
        view, text = _guard(lambda: facade.get_plan(key_of(), run_id))
        return {"plan_sha256": view.plan_sha256, "text": text}

    @server.tool()
    def update_search_plan(run_id: str, text: str) -> dict[str, Any]:
        """Replace the plan by the edited lines (only while it waits for approval)."""
        return run(_guard(lambda: facade.update_plan(key_of(), run_id, text)))

    @server.tool()
    def approve_search_plan(run_id: str, plan_sha256: str) -> dict[str, Any]:
        """Approve the plan with this hash; the worker then carries the run on. Needs a key with
        self_approve."""
        return run(_guard(lambda: facade.approve_plan(key_of(), run_id, plan_sha256)))


def _output_tools(server: MCPServer[Any], facade: Facade, key_of: Callable[[], ApiKey]) -> None:
    @server.tool()
    def get_report(run_id: str) -> str:
        """The finished report as Markdown."""
        path = _guard(lambda: facade.report_file(key_of(), run_id, "md"))
        return path.read_text(encoding="utf-8")

    @server.tool()
    def list_templates() -> list[dict[str, Any]]:
        """The report templates a run can use."""
        return [
            TemplateOut(
                id=t.id,
                name=t.name,
                description=t.description,
                language=t.language,
                default_response_format=t.default_response_format,
                sections=list(t.headings),
            ).model_dump()
            for t in facade.list_templates(key_of())
        ]


class KeyMiddleware:
    """Pure ASGI: the bearer key of the request must verify (else 401); the tools read it from
    a context variable."""

    def __init__(self, app: ASGIApp, keys: KeyStore) -> None:
        self._app = app
        self._keys = keys

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        token = bearer_token(Request(scope))
        key = self._keys.verify(token) if token else None
        if key is None:
            response = JSONResponse(
                {"detail": "a valid API key is required", "status": None},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        reset = _request_key.set(key)
        try:
            await self._app(scope, receive, send)
        finally:
            _request_key.reset(reset)
