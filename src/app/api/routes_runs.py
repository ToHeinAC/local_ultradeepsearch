"""REST routes of Phase 2 (`/v1/runs`): create, watch, approve the plan, fetch the report,
control. A run is executed by the worker process, never by these routes."""

import json
import time
from collections.abc import Callable, Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.responses import FileResponse, StreamingResponse

from app.api.facade import Facade
from app.api.keys import ApiKey
from app.api.schemas import (
    EventsOut,
    OutboundOut,
    PlanApproveIn,
    PlanOut,
    RunApproveIn,
    RunCreateIn,
    TextIn,
)
from app.research.service import RunView

FINAL_STATUSES = ("done", "blocked", "failed", "cancelled")
MEDIA_TYPES = {
    "md": "text/markdown; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}


def sse_lines(
    facade: Facade,
    key: ApiKey,
    run_id: str,
    after: int,
    *,
    poll_s: float,
    sleep: Callable[[float], None],
) -> Iterator[str]:
    """The run's events as server-sent events (`id:` is the line number in the run's event file),
    until the run reaches a final status. The status is read before the events: the last event
    is written before the final status, so nothing is missed."""
    cursor = after
    while True:
        status = facade.get_run(key, run_id).status
        events, new_cursor = facade.run_events(key, run_id, cursor)
        for number, event in enumerate(events, start=cursor + 1):
            yield f"id: {number}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"
        cursor = new_cursor
        if status in FINAL_STATUSES:
            return
        yield ": waiting\n\n"
        sleep(poll_s)


def _last_event_id(value: str | None) -> int:
    return int(value) if value is not None and value.isdigit() else 0


def runs_router(
    facade: Facade,
    auth: Callable[..., ApiKey],
    *,
    sse_poll_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> APIRouter:
    router = APIRouter(prefix="/v1/runs", tags=["runs"])

    @router.post("", status_code=201)
    def create(body: RunCreateIn, key: ApiKey = Depends(auth)) -> RunView:
        return facade.create_run(
            key,
            body.brief,
            tier=body.tier,
            template_id=body.template_id,
            language=body.language,
            response_format=body.response_format,
        )

    _watch_routes(router, facade, auth, sse_poll_s=sse_poll_s, sleep=sleep)
    _plan_routes(router, facade, auth)
    _output_routes(router, facade, auth)
    _control_routes(router, facade, auth)
    return router


def _watch_routes(
    router: APIRouter,
    facade: Facade,
    auth: Callable[..., ApiKey],
    *,
    sse_poll_s: float,
    sleep: Callable[[float], None],
) -> None:
    @router.get("")
    def list_runs(key: ApiKey = Depends(auth)) -> list[RunView]:
        return facade.list_runs(key)

    @router.get("/{run_id}")
    def get(run_id: str, key: ApiKey = Depends(auth)) -> RunView:
        return facade.get_run(key, run_id)

    @router.post("/{run_id}/approve")
    def approve(run_id: str, body: RunApproveIn, key: ApiKey = Depends(auth)) -> RunView:
        return facade.approve_run(key, run_id, body.brief_sha256)

    @router.get("/{run_id}/events")
    def events(
        run_id: str,
        after: Annotated[int, Query(ge=0)] = 0,
        key: ApiKey = Depends(auth),
    ) -> EventsOut:
        found, cursor = facade.run_events(key, run_id, after)
        return EventsOut(events=found, next=cursor)

    @router.get("/{run_id}/stream")
    def stream(
        run_id: str,
        last_event_id: Annotated[str | None, Header()] = None,
        key: ApiKey = Depends(auth),
    ) -> StreamingResponse:
        facade.get_run(key, run_id)  # an unknown run is a 404, not an empty stream
        lines = sse_lines(
            facade, key, run_id, _last_event_id(last_event_id), poll_s=sse_poll_s, sleep=sleep
        )
        return StreamingResponse(lines, media_type="text/event-stream")


def _plan_routes(router: APIRouter, facade: Facade, auth: Callable[..., ApiKey]) -> None:
    @router.get("/{run_id}/search-plan")
    def get_plan(run_id: str, key: ApiKey = Depends(auth)) -> PlanOut:
        view, text = facade.get_plan(key, run_id)
        return PlanOut(plan=view.plan, plan_sha256=view.plan_sha256, text=text)

    @router.put("/{run_id}/search-plan")
    def put_plan(run_id: str, body: TextIn, key: ApiKey = Depends(auth)) -> PlanOut:
        view = facade.update_plan(key, run_id, body.text)
        return PlanOut(
            plan=view.plan, plan_sha256=view.plan_sha256, text=facade.get_plan(key, run_id)[1]
        )

    @router.post("/{run_id}/search-plan/approve", status_code=202)
    def approve_plan(run_id: str, body: PlanApproveIn, key: ApiKey = Depends(auth)) -> RunView:
        return facade.approve_plan(key, run_id, body.plan_sha256)


def _output_routes(router: APIRouter, facade: Facade, auth: Callable[..., ApiKey]) -> None:
    @router.get("/{run_id}/report")
    def report(
        run_id: str,
        fmt: Annotated[str, Query(alias="format")] = "md",
        key: ApiKey = Depends(auth),
    ) -> FileResponse:
        path = facade.report_file(key, run_id, fmt)
        return FileResponse(path, media_type=MEDIA_TYPES[fmt], filename=f"{run_id}.{fmt}")

    @router.get("/{run_id}/gate")
    def gate(run_id: str, key: ApiKey = Depends(auth)) -> dict[str, Any]:
        return facade.gate_report(key, run_id)

    @router.get("/{run_id}/outbound")
    def outbound(run_id: str, key: ApiKey = Depends(auth)) -> OutboundOut:
        return OutboundOut(lines=facade.outbound_log(key, run_id))


def _control_routes(router: APIRouter, facade: Facade, auth: Callable[..., ApiKey]) -> None:
    @router.post("/{run_id}/cancel")
    def cancel(run_id: str, key: ApiKey = Depends(auth)) -> RunView:
        return facade.cancel_run(key, run_id)

    @router.post("/{run_id}/resume", status_code=202)
    def resume(run_id: str, key: ApiKey = Depends(auth)) -> RunView:
        return facade.resume_run(key, run_id)

    @router.delete("/{run_id}", status_code=204)
    def delete(run_id: str, key: ApiKey = Depends(auth)) -> Response:
        facade.delete_run(key, run_id)
        return Response(status_code=204)
