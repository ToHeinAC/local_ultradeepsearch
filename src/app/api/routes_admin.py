"""REST routes for templates, the denylist, health and the effective configuration."""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, UploadFile

from app.api.facade import Facade
from app.api.keys import ApiKey
from app.api.schemas import DenylistIO, HealthOut, TemplateOut
from app.api.summary import RunSummary
from app.templates import ReportTemplate


def template_out(template: ReportTemplate) -> TemplateOut:
    return TemplateOut(
        id=template.id,
        name=template.name,
        description=template.description,
        language=template.language,
        default_response_format=template.default_response_format,
        sections=list(template.headings),
    )


def admin_router(facade: Facade, auth: Callable[..., ApiKey]) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["admin"])

    @router.get("/templates")
    def list_templates(key: ApiKey = Depends(auth)) -> list[TemplateOut]:
        return [template_out(t) for t in facade.list_templates(key)]

    @router.post("/templates", status_code=201)
    def add_template(
        file: Annotated[UploadFile, File()], key: ApiKey = Depends(auth)
    ) -> TemplateOut:
        template = facade.add_template(key, file.filename or "template.md", file.file.read())
        return template_out(template)

    @router.get("/denylist")
    def get_denylist(key: ApiKey = Depends(auth)) -> DenylistIO:
        return DenylistIO(terms=list(facade.get_denylist(key)))

    @router.put("/denylist")
    def put_denylist(body: DenylistIO, key: ApiKey = Depends(auth)) -> DenylistIO:
        return DenylistIO(terms=list(facade.put_denylist(key, body.terms)))

    @router.get("/run-summaries")
    def run_summaries(key: ApiKey = Depends(auth)) -> list[RunSummary]:
        return facade.run_summaries(key)

    @router.get("/doctor")
    def doctor(key: ApiKey = Depends(auth)) -> dict[str, Any]:
        return facade.doctor(key)

    @router.get("/health")
    def health(key: ApiKey = Depends(auth)) -> HealthOut:
        return HealthOut.model_validate(facade.health(key))

    @router.get("/config")
    def config(key: ApiKey = Depends(auth)) -> dict[str, Any]:
        return facade.config(key)

    return router
