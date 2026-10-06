"""The Phase-2 service (PRD M5): start, resume and watch a research run, and handle its search
plan. The CLI calls it today; REST, MCP and the GUI call the same object later (M6, M7).

It owns the run's status: `running` while a graph executes (holding the worker slot), `awaiting_
plan_approval` while the owner reviews the plan (holding nothing), `done` or `blocked` at the
end, `failed` if a step raised (the run continues from its last checkpoint when run again).
"""

import json
import shutil
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.brief.archive import archive_brief
from app.brief.errors import InvalidInput, NotFound, WrongState
from app.brief.models import SessionSettings
from app.brief.parse import BriefParseError
from app.brief.render import BriefContext, brief_sha256, render_external
from app.events import EventSink
from app.graphs.research import ResearchRunner
from app.pipeline.profiles import ResponseFormats
from app.research.errors import ReportNotReady, ResearchError, RunCancelled, WorkerBusy
from app.research.manifest import read_run_json, record_failure
from app.research.models import Decomposition, SearchPlan, atomic_items
from app.research.plan import PLAN_FILE, check_approvable, load_plan, plan_hash, save_plan
from app.research.plan_edit import apply_edits, parse_lines, render_lines
from app.research.steps import RunContext
from app.research.worker import WorkerLock
from app.store.runs import RunRow, RunStore
from app.templates import ReportTemplate, TemplateError, get_template

TIERS = ("light", "full")
REPORT_FORMATS = ("md", "docx", "pdf")


class TierNotAvailable(ResearchError):
    """The tier's pipeline does not exist yet."""


@dataclass(frozen=True)
class RunView:
    """Where a run stands, for any caller."""

    run_id: str
    status: str
    tier: str
    waiting_for: str  # "plan" (the owner's approval), "work" (a run is due) or "nothing"
    step: str | None  # the step running, or the one that failed
    plan: SearchPlan | None
    plan_sha256: str | None
    gate_failed: tuple[str, ...]  # checks still failing; empty unless the run is blocked
    report_path: str | None
    exports: dict[str, str]
    error: str | None


def _no_binding(run_id: str, run_dir: Path) -> AbstractContextManager[None]:
    return nullcontext()


@dataclass(frozen=True)
class ServiceDeps:
    runs: RunStore
    runner: ResearchRunner
    events: EventSink
    contexts: Callable[[str], RunContext]
    templates: dict[str, ReportTemplate]
    formats: ResponseFormats
    data_dir: Path
    lock_path: Path
    now: Callable[[], datetime]
    # While it runs, a run's events also go to its own file (`RunScopedEventSink.bound`).
    bind: Callable[[str, Path], AbstractContextManager[None]] = _no_binding


def _current_step(data: dict[str, Any]) -> str | None:
    """The last step that began and did not finish."""
    done = {s["step"] for s in data.get("steps", []) if s["status"] == "done"}
    open_steps = [s["step"] for s in data.get("steps", []) if s["step"] not in done]
    return open_steps[-1] if open_steps else None


class ResearchService:
    def __init__(self, deps: ServiceDeps) -> None:
        self._d = deps

    # ---- creating a run -------------------------------------------------------------------

    def create_external_run(
        self,
        brief: str,
        *,
        tier: str,
        template_id: str,
        language: str | None = None,
        response_format: str | None = None,
        approved: bool = True,
        created_by: str | None = None,
    ) -> RunView:
        """A run for a brief written elsewhere (PRD M5 `udr run --brief`): code adds the Method
        line and the Output section, archives the bytes and queues the run. Starting it from the
        owner's shell is the approval; an API caller without `approved` leaves it
        `awaiting_brief_approval`. Tier `auto` becomes `light` until the full tier exists."""
        auto = tier == "auto"
        tier = "light" if auto else tier
        if tier not in TIERS:
            raise InvalidInput(f"tier must be light or full, got {tier!r}")
        settings, template = self._external_settings(template_id, language, response_format)
        ctx = self._external_context(settings, template)
        try:
            text = render_external(brief, ctx)
        except BriefParseError as exc:
            raise InvalidInput(str(exc)) from exc
        path = archive_brief(self._d.data_dir / "briefs", text, self._d.now())
        stored = {
            **settings.model_dump(),
            "interview_language": settings.report_language,
            "tier": tier,
            "summarize_model": None,
        }
        row = self._d.runs.create_external(
            brief_sha256=brief_sha256(text),
            brief_path=str(path),
            tier=tier,
            settings_json=json.dumps(stored),
            status="queued" if approved else "awaiting_brief_approval",
            created_by=created_by,
        )
        if auto:
            self._d.events.emit("tier_auto_resolved", run_id=row.run_id, tier="light")
        return self.view(row.run_id)

    def _external_settings(
        self, template_id: str, language: str | None, response_format: str | None
    ) -> tuple[SessionSettings, ReportTemplate]:
        try:
            template = get_template(self._d.templates, template_id)
        except TemplateError as exc:
            raise InvalidInput(str(exc)) from exc
        settings = SessionSettings(
            report_language=language or template.language,
            response_format=response_format or template.default_response_format,  # type: ignore[arg-type]
            template_id=template.id,
        )
        return settings, template

    def _external_context(
        self, settings: SessionSettings, template: ReportTemplate
    ) -> BriefContext:
        return BriefContext(
            settings=settings,
            template=template,
            fmt=self._d.formats.named(settings.response_format),
            rounds=0,
            missing=(),
            unknown_questions=(),
            upload_digest="",
            interview_language=settings.report_language,
        )

    # ---- running --------------------------------------------------------------------------

    def next_runnable(self) -> str | None:
        """The run a worker takes next: one a dead worker left `running`, else the oldest
        `queued`."""
        row = self._d.runs.next_runnable()
        return row.run_id if row else None

    def give_up(self, run_id: str, reason: str) -> None:
        """Mark a run that cannot be executed (before any step ran) as `failed`."""
        self._d.runs.set_status(run_id, "failed")
        self._d.events.emit("run_failed", level="error", run_id=run_id, step="?", reason=reason)

    def run(self, run_id: str) -> RunView:
        """Start the run, or continue it from its last checkpoint, until it waits for the plan
        approval, finishes, is cancelled or fails. A run that is finished, waiting for an approval
        or cancelled is left alone. An approved plan waiting for the worker is carried on.
        Raises `WorkerBusy` while another run is active."""
        row = self._row(run_id)
        if row.tier not in TIERS or row.tier == "full":
            raise TierNotAvailable("the full tier arrives with milestones M8 and M9")
        if row.status not in ("queued", "running", "failed", "created"):
            return self.view(run_id)
        with WorkerLock(self._d.lock_path):
            self._d.runs.set_status(run_id, "running")
            self._guarded(run_id, lambda: self._start_or_continue(run_id))
        return self.view(run_id)

    def _start_or_continue(self, run_id: str) -> None:
        snapshot = self._d.runner.snapshot(run_id)
        pending = self._d.runs.pending_plan(run_id)
        if snapshot.interrupt is not None:
            if pending is None:  # the graph waits and nobody approved: the status follows it
                self._d.runs.set_status(run_id, "awaiting_plan_approval")
                return
            self._d.runner.resume(run_id, {"action": "approve", "plan_sha256": pending})
        elif snapshot.values:
            self._d.runner.proceed(run_id)
        else:
            self._d.runner.start(run_id)
        self._d.runs.clear_pending_plan(run_id)

    def _guarded(self, run_id: str, work: Callable[[], None]) -> None:
        """Run ``work``; a step that raises makes the run `failed`, with its reason; a
        cancellation makes it `cancelled`. A crash signal (`BaseException`) is never swallowed.
        The run's events go to its own file."""
        ctx = self._d.contexts(run_id)
        with self._d.bind(run_id, ctx.run_dir):
            try:
                work()
            except RunCancelled:
                self._cancelled(run_id)
            except Exception as exc:
                self._fail(run_id, exc)

    def _cancelled(self, run_id: str) -> None:
        step = _current_step(read_run_json(self._d.contexts(run_id).run_dir))
        self._d.runs.set_status(run_id, "cancelled")
        self._d.runs.clear_cancel(run_id)
        self._d.events.emit("run_cancelled", run_id=run_id, step=step)

    def _fail(self, run_id: str, exc: Exception) -> None:
        ctx = self._d.contexts(run_id)
        step = _current_step(read_run_json(ctx.run_dir)) or "?"
        reason = f"{type(exc).__name__}: {exc}"
        record_failure(ctx.run_dir, step, reason, self._d.now())
        self._d.runs.set_status(run_id, "failed")
        self._d.events.emit("run_failed", level="error", run_id=run_id, step=step, reason=reason)

    # ---- the search plan ------------------------------------------------------------------

    def plan_text(self, run_id: str) -> str:
        """The plan as editable text (see `app.research.plan_edit`)."""
        return render_lines(self._plan(run_id))

    def update_plan(self, run_id: str, text: str) -> RunView:
        """Replace the plan with the edited lines; changed and new queries are checked again. Only
        while the run waits for the approval."""
        self._require_awaiting(run_id)
        ctx = self._d.contexts(run_id)
        deco = Decomposition.model_validate_json(
            (ctx.run_dir / "prompt-decomposition.json").read_text(encoding="utf-8")
        )
        known = {item.id for item in atomic_items(deco)}
        plan = apply_edits(
            self._plan(run_id), parse_lines(text), preparer=ctx.preparer, known_items=known
        )
        save_plan(ctx.run_dir, plan)
        return self.view(run_id)

    def approve_plan(self, run_id: str, sha256: str) -> RunView:
        """Approve the plan whose hash is ``sha256``: the run becomes `queued` and the worker
        (or `run`) carries it on. A stale hash is `StalePlan`, a plan with queries that cannot be
        sent is `PlanBlocked`, a second approval is `WrongState`."""
        self._require_awaiting(run_id)
        check_approvable(self._plan(run_id), sha256)
        self._d.runs.set_pending_plan(run_id, sha256)
        return self.view(run_id)

    def approve_and_run(self, run_id: str, sha256: str) -> RunView:
        """Approve the plan and carry the run on to its end in this process (`udr run`)."""
        self.approve_plan(run_id, sha256)
        return self.run(run_id)

    def approve_external(self, run_id: str, brief_sha256: str) -> RunView:
        """Approve the brief of a run that an API key without `self_approve` created."""
        self._d.runs.approve_external(run_id, brief_sha256)
        return self.view(run_id)

    # ---- control --------------------------------------------------------------------------

    def request_cancel(self, run_id: str) -> RunView:
        """Cancel at once a run that does not execute; a running one stops at the next node
        boundary, between two sweep queries or between two draft sections."""
        self._d.runs.request_cancel(run_id)
        return self.view(run_id)

    def resume(self, run_id: str) -> RunView:
        """Queue a `failed` or `cancelled` run again; it continues from its last checkpoint."""
        status = self._row(run_id).status
        if status not in ("failed", "cancelled"):
            raise WrongState(f"the run is {status}, only a failed or cancelled run resumes")
        self._d.runs.set_status(run_id, "queued")
        return self.view(run_id)

    def delete(self, run_id: str) -> None:
        """Remove a run: its rows, its checkpoints, its directory and the uploads of its session.
        The archived brief stays. A running run is `WrongState`."""
        row = self._d.runs.delete_run(run_id)
        self._d.runner.delete_thread(run_id)
        shutil.rmtree(self._d.data_dir / "runs" / run_id, ignore_errors=True)
        if row.session_id is not None:
            shutil.rmtree(self._d.data_dir / "uploads" / row.session_id, ignore_errors=True)

    # ---- reading --------------------------------------------------------------------------

    def list_runs(self, limit: int = 100) -> list[RunView]:
        return [self.view(row.run_id) for row in self._d.runs.list_runs(limit)]

    def report_file(self, run_id: str, fmt: str) -> Path:
        """The report as `md`, `docx` or `pdf`. Only a finished run has one (`done`, or `blocked`
        with every file downloadable); a format whose export failed is `NotFound`."""
        if fmt not in REPORT_FORMATS:
            raise InvalidInput(f"format must be one of {', '.join(REPORT_FORMATS)}")
        status = self._row(run_id).status
        if status not in ("done", "blocked"):
            raise ReportNotReady(status)
        path = self._d.contexts(run_id).run_dir / f"report.{fmt}"
        if not path.exists():
            raise NotFound(f"the run has no {fmt} report")
        return path

    def gate_report(self, run_id: str) -> dict[str, Any]:
        """`gate.json`: every check and every fix round. `NotFound` before the gate has run."""
        self._row(run_id)
        path = self._d.contexts(run_id).run_dir / "gate.json"
        if not path.exists():
            raise NotFound("the run has no gate report yet")
        return json.loads(path.read_text(encoding="utf-8"))

    def outbound_log(self, run_id: str) -> list[dict[str, Any]]:
        """What the run sent over the internet: the lines of `outbound.jsonl`."""
        self._row(run_id)
        path = self._d.contexts(run_id).run_dir / "outbound.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def worker_busy(self) -> bool:
        """Whether some process holds the worker slot right now."""
        try:
            with WorkerLock(self._d.lock_path):
                return False
        except WorkerBusy:
            return True

    def queue_depth(self) -> int:
        """Runs that wait for the worker (`queued`)."""
        return sum(1 for row in self._d.runs.list_runs(limit=10_000) if row.status == "queued")

    def view(self, run_id: str) -> RunView:
        row = self._row(run_id)
        run_dir = self._d.contexts(run_id).run_dir
        data = read_run_json(run_dir)
        plan = load_plan(run_dir) if (run_dir / PLAN_FILE).exists() else None
        gate = data.get("gate", {})
        failure = data.get("failure")
        report = run_dir / "report.md"
        return RunView(
            run_id=run_id,
            status=row.status,
            tier=str(row.tier),
            waiting_for=_waiting_for(row.status),
            step=_current_step(data),
            plan=plan,
            plan_sha256=plan_hash(plan) if plan else None,
            gate_failed=tuple(gate.get("failed", ())) if row.status == "blocked" else (),
            report_path=str(report) if report.exists() else None,
            exports=dict(data.get("exports", {})),
            error=failure["reason"] if failure and row.status == "failed" else None,
        )

    # ---- helpers --------------------------------------------------------------------------

    def _row(self, run_id: str) -> RunRow:
        row = self._d.runs.get_run(run_id)
        if row is None:
            raise NotFound(run_id)
        return row

    def _plan(self, run_id: str) -> SearchPlan:
        self._row(run_id)
        run_dir = self._d.contexts(run_id).run_dir
        if not (run_dir / PLAN_FILE).exists():
            raise WrongState("the run has no search plan yet")
        return load_plan(run_dir)

    def _require_awaiting(self, run_id: str) -> None:
        status = self._row(run_id).status
        if status != "awaiting_plan_approval":
            raise WrongState(f"the run is {status}, not waiting for the approval of its plan")


def _waiting_for(status: str) -> str:
    if status == "awaiting_plan_approval":
        return "plan"
    return "nothing" if status in ("done", "blocked") else "work"
