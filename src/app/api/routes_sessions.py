"""REST routes of Phase 1 (`/v1/sessions`). The model work runs in the background: these routes
answer 202 with the session view and the client polls `GET /v1/sessions/{id}`."""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.api.facade import Facade
from app.api.keys import ApiKey
from app.api.schemas import (
    ApprovedOut,
    FeedbackIn,
    MessageIn,
    SessionApproveIn,
    SettingsIn,
    TextIn,
)
from app.brief.service import SessionView
from app.brief.uploads import UploadFile as Upload


def read_uploads(files: list[UploadFile] | None) -> list[Upload]:
    return [Upload(f.filename or "upload", f.file.read()) for f in files or []]


def sessions_router(facade: Facade, auth: Callable[..., ApiKey]) -> APIRouter:
    router = APIRouter(prefix="/v1/sessions", tags=["sessions"])
    _interview_routes(router, facade, auth)
    _decision_routes(router, facade, auth)
    return router


def _interview_routes(router: APIRouter, facade: Facade, auth: Callable[..., ApiKey]) -> None:

    @router.post("", status_code=202)
    def start(
        question: Annotated[str, Form()],
        files: Annotated[list[UploadFile] | None, File()] = None,
        key: ApiKey = Depends(auth),
    ) -> SessionView:
        return facade.start_session(key, question, read_uploads(files))

    @router.post("/{session_id}/uploads", status_code=202)
    def uploads(
        session_id: str,
        files: Annotated[list[UploadFile] | None, File()] = None,
        key: ApiKey = Depends(auth),
    ) -> SessionView:
        return facade.add_uploads(key, session_id, read_uploads(files))

    @router.post("/{session_id}/messages", status_code=202)
    def messages(session_id: str, body: MessageIn, key: ApiKey = Depends(auth)) -> SessionView:
        return facade.send_message(
            key,
            session_id,
            answers=body.answers,
            note=body.note,
            genug=body.genug,
            offer=body.offer,
        )

    @router.get("/{session_id}")
    def get(session_id: str, key: ApiKey = Depends(auth)) -> SessionView:
        return facade.get_session(key, session_id)


def _decision_routes(router: APIRouter, facade: Facade, auth: Callable[..., ApiKey]) -> None:
    @router.post("/{session_id}/revise", status_code=202)
    def revise(session_id: str, body: FeedbackIn, key: ApiKey = Depends(auth)) -> SessionView:
        return facade.revise_brief(key, session_id, body.feedback)

    @router.put("/{session_id}/settings", status_code=202)
    def settings(session_id: str, body: SettingsIn, key: ApiKey = Depends(auth)) -> SessionView:
        return facade.set_settings(key, session_id, **body.model_dump())

    @router.put("/{session_id}/brief", status_code=202)
    def edit(session_id: str, body: TextIn, key: ApiKey = Depends(auth)) -> SessionView:
        return facade.edit_brief(key, session_id, body.text)

    @router.post("/{session_id}/approve")
    def approve(
        session_id: str, body: SessionApproveIn, key: ApiKey = Depends(auth)
    ) -> ApprovedOut:
        view = facade.approve_brief(
            key, session_id, body.brief_sha256, body.tier, body.summarize_model
        )
        return ApprovedOut(run_id=str(view.run_id))

    @router.post("/{session_id}/save", status_code=202)
    def save(session_id: str, key: ApiKey = Depends(auth)) -> SessionView:
        return facade.save_session(key, session_id)
