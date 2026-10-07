"""The service layer REST and MCP share (PRD M6). One method per endpoint; the routes and tools
call nothing else. No HTTP types here: errors are the domain's (`app.api.errors` maps them).

Every method takes the calling `ApiKey` first and enforces the approval rule (PRD §3.2): an item
created by a key without `self_approve` waits until a key with it approves it.
"""

import dataclasses
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from app.adapters.outbound.denylist import Denylist
from app.adapters.outbound.ledger import MonthLedger
from app.adapters.outbound.log import OutboundLog
from app.api.errors import Forbidden
from app.api.jobs import SessionJobs
from app.api.keys import ApiKey, KeyStore
from app.api.summary import (
    RunSummary,
    SessionSummary,
    elapsed_seconds,
    step_spans,
    title_of,
)
from app.brief.errors import InvalidInput, WrongState
from app.brief.protocol import AnswerInput
from app.brief.service import BriefService, SessionView, Tier
from app.brief.uploads import UploadFile
from app.config import Settings
from app.events import read_run_events
from app.pipeline.profiles import load_profile
from app.research.manifest import read_run_json
from app.research.service import ResearchService, RunView, TierNotAvailable
from app.templates import ReportTemplate, TemplateError, parse_template

FULL_TIER_MESSAGE = "Full-Tier ab M8"
OFFERS = ("strengthen", "install")


def _require_self_approve(key: ApiKey) -> None:
    if not key.self_approve:
        raise Forbidden("this key may not approve; a key with self_approve must")


def _refuse_full(tier: str) -> None:
    if tier == "full":
        raise TierNotAvailable(FULL_TIER_MESSAGE)


class Facade:
    def __init__(
        self,
        briefs: BriefService,
        research: ResearchService,
        jobs: SessionJobs,
        settings: Settings,
        templates: dict[str, ReportTemplate],
        denylist_path: Path,
        *,
        keys: KeyStore,
        month: MonthLedger,
        doctor: Callable[[], dict[str, Any]],
    ) -> None:
        self._keys = keys
        self._month = month
        self._doctor = doctor
        self._briefs = briefs
        self._research = research
        self._jobs = jobs
        self._settings = settings
        self._templates = templates  # shared with the services: an upload is seen at once
        self._denylist_path = denylist_path

    # ---- sessions (Phase 1, asynchronous: the view says `busy` while a job works) ---------

    def _shown(self, view: SessionView) -> SessionView:
        busy = self._jobs.busy(view.session_id)
        error = view.error or self._jobs.error(view.session_id)
        return dataclasses.replace(view, busy=busy, error=error)

    def start_session(
        self, key: ApiKey, question: str, files: Sequence[UploadFile] = ()
    ) -> SessionView:
        return self._shown(self._briefs.start(question, files, created_by=key.key_id))

    def list_sessions(self, key: ApiKey) -> list[SessionSummary]:
        names = self._key_names()
        return [
            SessionSummary(
                session_id=row.session_id,
                status=row.status,
                title=title_of(row.brief_text),
                created_at=row.created_at,
                updated_at=row.updated_at,
                created_by=names.get(row.created_by or "", row.created_by),
                run_id=row.run_id,
            )
            for row in self._briefs.list_sessions()
        ]

    def retry_session(self, key: ApiKey, session_id: str) -> SessionView:
        """Continue a session that stopped on a model error (background; poll the session)."""
        return self._shown(self._briefs.retry(session_id))

    def _key_names(self) -> dict[str, str]:
        return {k.key_id: k.name for k in self._keys.list()}

    def add_uploads(self, key: ApiKey, session_id: str, files: Sequence[UploadFile]) -> SessionView:
        return self._shown(self._briefs.add_files(session_id, files))

    def send_message(
        self,
        key: ApiKey,
        session_id: str,
        *,
        answers: Sequence[AnswerInput] = (),
        note: str = "",
        genug: bool = False,
        offer: str | None = None,
    ) -> SessionView:
        """Answer the open questions, or the offer for a pasted finished prompt."""
        if offer is not None:
            if offer not in OFFERS:
                raise InvalidInput(f"offer must be one of {', '.join(OFFERS)}")
            return self._shown(
                self._briefs.choose_offer(session_id, strengthen=offer == "strengthen")
            )
        return self._shown(self._briefs.answer(session_id, answers, note=note, genug=genug))

    def get_session(self, key: ApiKey, session_id: str) -> SessionView:
        return self._shown(self._briefs.get(session_id))

    def revise_brief(self, key: ApiKey, session_id: str, feedback: str) -> SessionView:
        return self._shown(self._briefs.revise(session_id, feedback))

    def edit_brief(self, key: ApiKey, session_id: str, text: str) -> SessionView:
        return self._shown(self._briefs.edit(session_id, text))

    def set_settings(
        self,
        key: ApiKey,
        session_id: str,
        *,
        report_language: str | None = None,
        response_format: str | None = None,
        template_id: str | None = None,
    ) -> SessionView:
        return self._shown(
            self._briefs.set_settings(
                session_id,
                report_language=report_language,
                response_format=response_format,
                template_id=template_id,
            )
        )

    def save_session(self, key: ApiKey, session_id: str) -> SessionView:
        return self._shown(self._briefs.save(session_id))

    def approve_brief(
        self,
        key: ApiKey,
        session_id: str,
        brief_sha256: str,
        tier: Tier,
        summarize_model: str | None = None,
        tavily_cap: int | None = None,
    ) -> SessionView:
        """Approve the brief and create its run. `auto` takes the session's recommendation; the
        full tier does not exist before M8. ``tavily_cap`` is at most the tier's credit cap."""
        _require_self_approve(key)
        if self._jobs.busy(session_id):
            raise WrongState("the session is busy with an earlier request")
        _refuse_full(tier)
        if tier == "auto":
            recommended = self._briefs.get(session_id).recommendation
            tier = "light" if recommended is None else recommended["tier"]
            _refuse_full(tier)
        self._check_tavily_cap(tier, tavily_cap)
        approved = self._briefs.approve(session_id, brief_sha256, tier, summarize_model, tavily_cap)
        return self._shown(approved)

    def _check_tavily_cap(self, tier: str, cap: int | None) -> None:
        if cap is None:
            return
        limit = load_profile(tier, self._settings.config_dir).credit_cap
        if not 0 <= cap <= limit:
            raise InvalidInput(f"tavily_cap must be between 0 and {limit} for the {tier} tier")

    # ---- runs ------------------------------------------------------------------------------

    def create_run(
        self,
        key: ApiKey,
        brief: str,
        *,
        tier: str,
        template_id: str,
        language: str | None = None,
        response_format: str | None = None,
        tavily_cap: int | None = None,
    ) -> RunView:
        """A run for a brief written elsewhere; without `self_approve` it waits for a brief
        approval."""
        _refuse_full(tier)
        self._check_tavily_cap("light" if tier == "auto" else tier, tavily_cap)
        return self._research.create_external_run(
            brief,
            tier=tier,
            template_id=template_id,
            language=language,
            response_format=response_format,
            approved=key.self_approve,
            created_by=key.key_id,
            tavily_cap=tavily_cap,
        )

    def run_summary(self, key: ApiKey, run_id: str) -> RunSummary:
        return self._summarize(run_id, self._key_names())

    def run_summaries(self, key: ApiKey) -> list[RunSummary]:
        names = self._key_names()
        return [self._summarize(row.run_id, names) for row in self._research.rows()]

    def _summarize(self, run_id: str, names: dict[str, str]) -> RunSummary:
        view = self._research.view(run_id)
        row = self._research.row(run_id)
        run_dir = self._research.run_dir(run_id)
        data = read_run_json(run_dir)
        spans = step_spans(data.get("steps", []))
        started = spans[0].started_at if spans else None
        over = row.status in ("done", "blocked", "failed", "cancelled")
        last = spans[-1] if spans else None
        ended = (last.ended_at or last.started_at) if over and last else None
        brief = Path(row.brief_path).read_text(encoding="utf-8") if row.brief_path else None
        notes = data.get("stats", {}).get("notes_by_kind", {})
        return RunSummary(
            run_id=run_id,
            status=row.status,
            tier=view.tier,
            waiting_for=view.waiting_for,
            title=title_of(brief),
            brief_sha256=row.brief_sha256,
            created_at=row.created_at,
            created_by=names.get(row.created_by or "", row.created_by),
            started_at=started,
            ended_at=ended,
            elapsed_s=elapsed_seconds(started, ended),
            step=view.step,
            steps=spans,
            credits_run=OutboundLog(run_dir / "outbound.jsonl").total_credits(),
            credit_cap=self._credit_cap(row.settings_json, view.tier),
            credits_month=self._month.used(),
            month_limit=self._month.limit,
            sources=notes.get("source") if "source" in notes else None,
            warnings=sum(
                1 for e in read_run_events(run_dir, 0)[0] if e.get("level") in ("warning", "error")
            ),
        )

    def _credit_cap(self, settings_json: str | None, tier: str) -> int:
        chosen = json.loads(settings_json).get("tavily_cap") if settings_json else None
        if chosen is not None:
            return int(chosen)
        return load_profile(tier, self._settings.config_dir).credit_cap

    def doctor(self, key: ApiKey) -> dict[str, Any]:
        """The doctor's checks and the role to model map (read only)."""
        return self._doctor()

    def list_runs(self, key: ApiKey) -> list[RunView]:
        return self._research.list_runs()

    def get_run(self, key: ApiKey, run_id: str) -> RunView:
        return self._research.view(run_id)

    def approve_run(self, key: ApiKey, run_id: str, brief_sha256: str) -> RunView:
        _require_self_approve(key)
        return self._research.approve_external(run_id, brief_sha256)

    def run_events(self, key: ApiKey, run_id: str, after: int) -> tuple[list[dict[str, Any]], int]:
        self._research.view(run_id)
        return read_run_events(self._run_dir(run_id), after)

    def _run_dir(self, run_id: str) -> Path:
        return self._settings.data_dir / "runs" / run_id

    # ---- the search plan ---------------------------------------------------------------------

    def get_plan(self, key: ApiKey, run_id: str) -> tuple[RunView, str]:
        """The run (with its plan and hash) and the plan as editable text."""
        return self._research.view(run_id), self._research.plan_text(run_id)

    def update_plan(self, key: ApiKey, run_id: str, text: str) -> RunView:
        return self._research.update_plan(run_id, text)

    def approve_plan(self, key: ApiKey, run_id: str, plan_sha256: str) -> RunView:
        _require_self_approve(key)
        return self._research.approve_plan(run_id, plan_sha256)

    # ---- output and control ------------------------------------------------------------------

    def report_file(self, key: ApiKey, run_id: str, fmt: str) -> Path:
        return self._research.report_file(run_id, fmt)

    def gate_report(self, key: ApiKey, run_id: str) -> dict[str, Any]:
        return self._research.gate_report(run_id)

    def outbound_log(self, key: ApiKey, run_id: str) -> list[dict[str, Any]]:
        return self._research.outbound_log(run_id)

    def cancel_run(self, key: ApiKey, run_id: str) -> RunView:
        return self._research.request_cancel(run_id)

    def resume_run(self, key: ApiKey, run_id: str) -> RunView:
        return self._research.resume(run_id)

    def delete_run(self, key: ApiKey, run_id: str) -> None:
        self._research.delete(run_id)

    # ---- admin -------------------------------------------------------------------------------

    def list_templates(self, key: ApiKey) -> list[ReportTemplate]:
        return list(self._templates.values())

    def add_template(self, key: ApiKey, filename: str, content: bytes) -> ReportTemplate:
        """Validate an uploaded template and keep it in `data/templates/`. An id that exists
        (built-in or uploaded) is `WrongState`: an upload never replaces a template."""
        try:
            template = parse_template(content.decode("utf-8"), filename)
        except UnicodeDecodeError as exc:
            raise InvalidInput(f"{filename}: not valid UTF-8") from exc
        if template.id in self._templates:
            raise WrongState(f"template {template.id!r} exists already")
        if template.reference_docx is not None:
            raise TemplateError(f"{filename}: reference_docx cannot be uploaded through the API")
        target = self._settings.data_dir / "templates" / f"{template.id}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        self._templates[template.id] = template
        return template

    def get_denylist(self, key: ApiKey) -> tuple[str, ...]:
        return Denylist.load(self._denylist_path).terms

    def put_denylist(self, key: ApiKey, terms: Sequence[str]) -> tuple[str, ...]:
        try:
            denylist = Denylist(terms)
        except ValueError as exc:
            raise InvalidInput(str(exc)) from exc
        denylist.save(self._denylist_path)
        return denylist.terms

    def health(self, key: ApiKey) -> dict[str, Any]:
        return {
            "api": "ok",
            "worker_lock": "held" if self._research.worker_busy() else "free",
            "queued": self._research.queue_depth(),
        }

    def config(self, key: ApiKey) -> dict[str, Any]:
        """The effective settings; a secret shows only whether it is set."""
        shown: dict[str, Any] = self._settings.model_dump(mode="json")
        for name, value in self._settings:
            if isinstance(value, SecretStr):
                shown[name] = "set"
        return shown
