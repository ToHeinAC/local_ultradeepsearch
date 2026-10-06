"""The settings a run was approved with (PRD M5 D2).

They are frozen into the run when its brief is approved (`runs.settings_json`), so Phase 2 never
parses them out of the brief text. Runs approved before that was done fall back to their session,
with the same defaults the brief was rendered with.
"""

from collections.abc import Mapping

from app.events import EventSink
from app.llm.service import LLMService
from app.llm.types import Role
from app.research.manifest import RunSettings
from app.store.runs import RunRow
from app.store.sessions import SessionRow
from app.templates import ReportTemplate, get_template

DEFAULT_TEMPLATE = "auto"


def resolve_run_settings(
    run: RunRow, session: SessionRow | None, templates: Mapping[str, ReportTemplate]
) -> RunSettings:
    """The run's settings: the frozen ones, else its session's choices with the defaults.

    Raises `ValueError` if there are neither (an external run always has them), or if the stored
    ones are damaged."""
    if run.settings_json is not None:
        return RunSettings.model_validate_json(run.settings_json)
    if session is None or run.tier is None:
        raise ValueError(f"run {run.run_id} has no settings and no session to take them from")
    template_id = session.template_id or DEFAULT_TEMPLATE
    return RunSettings(
        report_language=session.report_language or session.interview_language,
        response_format=session.response_format  # type: ignore[arg-type]  # validated by the model
        or get_template(templates, template_id).default_response_format,
        template_id=template_id,
        interview_language=session.interview_language,
        tier=run.tier,  # type: ignore[arg-type]  # validated by the model
        summarize_model=run.summarize_model,
    )


def llm_for_run(
    base: LLMService, settings: RunSettings, default_summarize: str, run_id: str, events: EventSink
) -> LLMService:
    """The model service of one run (PRD M6 D9): the run's `summarize_model` drives the
    `summarize` and the `extract` role (R2). Another model than the configured one is a warning."""
    model = settings.summarize_model
    if model is None or model == default_summarize:
        return base
    events.emit("summarize_model_override", level="warning", run_id=run_id, model=model)
    return base.with_models({Role.SUMMARIZE: model, Role.EXTRACT: model})
