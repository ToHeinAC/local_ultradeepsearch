"""The Phase-2 service (PRD M5): what `udr run` calls now, and REST and MCP in M6.

One lock per run id keeps two calls from interleaving. A model error or a `ResearchError` in the
graph does not lose the run: it is marked `failed` with the reason and `start` continues it from
the last checkpoint. A crash (any `BaseException`) is never caught."""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from app.adapters.outbound.denylist import Denylist
from app.brief.archive import archive_brief
from app.brief.errors import InvalidInput, NotFound, WrongState
from app.brief.render import brief_sha256
from app.graphs.research import ResearchRunner
from app.llm.errors import LLMError
from app.pipeline.artifacts import read_run_json
from app.pipeline.profiles import ResponseFormats
from app.research.errors import PlanBlocked, ResearchError, StalePlan
from app.research.external import prepare_external_brief
from app.research.manifest import Manifest
from app.research.models import PlanView, RunView, StepInfo
from app.research.plan import (
    PlanScope,
    add_query,
    delete_query,
    edit_query,
    plan_sha256,
    write_plan_file,
)
from app.research.steps import LightSteps
from app.research.workspace import read_brief
from app.store.research import ResearchStore
from app.store.runs import RunRow, RunStore
from app.templates import ReportTemplate, TemplateError, get_template

PLAN_KIND = "plan_approval"
STARTABLE = ("queued", "failed")


@dataclass(frozen=True)
class ResearchDeps:
    runs: RunStore
    store: ResearchStore
    runner: ResearchRunner
    steps: LightSteps
    templates: dict[str, ReportTemplate]
    formats: ResponseFormats
    briefs_dir: Path
    run_dir: Callable[[str], Path]
    credits: Callable[[str], int]  # Tavily credits a run has spent (its outbound log)
    load_denylist: Callable[[], Denylist]  # read fresh on every approval
    now: Callable[[], datetime]


class ResearchService:
    def __init__(self, deps: ResearchDeps) -> None:
        self._d = deps
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock(self, run_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(run_id, threading.Lock())

    def _row(self, run_id: str) -> RunRow:
        row = self._d.runs.get_run(run_id)
        if row is None:
            raise NotFound(run_id)
        return row

    def _waiting_for_plan(self, run_id: str) -> bool:
        waiting = self._d.runner.snapshot(run_id).interrupt
        return waiting is not None and waiting.get("kind") == PLAN_KIND

    def _require_plan_wait(self, run_id: str) -> None:
        self._row(run_id)
        if not self._waiting_for_plan(run_id):
            raise WrongState("the run is not waiting for the plan approval")

    def _drive(self, run_id: str, action: Callable[[], None]) -> RunView:
        """Run graph work; a fatal step error marks the run `failed` instead of being raised."""
        try:
            action()
        except (LLMError, ResearchError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
            self._d.runs.set_status(run_id, "failed", reason)
            self._manifest_status(run_id, "failed", reason)
        return self.get(run_id)

    def _manifest_status(self, run_id: str, status: str, reason: str) -> None:
        directory = self._d.run_dir(run_id)
        if directory.exists():
            Manifest(directory).set_status(status, reason)

    # ---- running --------------------------------------------------------------------------

    def start(self, run_id: str) -> RunView:
        """Run a `queued` or `failed` run to the plan approval, or to its end."""
        row = self._row(run_id)
        read_brief(row.brief_path, row.brief_sha256)  # no hash, or tampered archive: WrongState
        if row.tier != "light":
            raise WrongState("Full-Tier ab M8")
        if row.status not in STARTABLE:
            raise WrongState(f"the run is {row.status}, not queued or failed")
        with self._lock(run_id):
            self._d.runs.set_status(run_id, "running")
            started = bool(self._d.runner.snapshot(run_id).values)
            action = self._d.runner.proceed if started else self._d.runner.start
            return self._drive(run_id, lambda: action(run_id))

    def resume(self, run_id: str) -> RunView:
        """Continue from the checkpoint; a run waiting for the approval is only shown."""
        self._row(run_id)
        with self._lock(run_id):
            snapshot = self._d.runner.snapshot(run_id)
            if snapshot.interrupt is None and snapshot.next_nodes:
                return self._drive(run_id, lambda: self._d.runner.proceed(run_id))
        return self.get(run_id)

    # ---- reading --------------------------------------------------------------------------

    def get(self, run_id: str) -> RunView:
        row = self._row(run_id)
        directory = self._d.run_dir(run_id)
        steps = cast("list[dict[str, str]]", read_run_json(directory).get("steps", []))
        waiting = self._waiting_for_plan(run_id)
        snapshot = self._d.runner.snapshot(run_id)
        busy = snapshot.interrupt is None and bool(snapshot.next_nodes)
        return RunView(
            run_id=run_id,
            status=row.status,
            reason=row.status_reason,
            tier=row.tier,
            template_id=row.template_id,
            steps=tuple(StepInfo(s["id"], s["status"]) for s in steps),
            waiting_for="plan" if waiting else "work" if busy else "nothing",
            credits=self._d.credits(run_id),
            run_dir=str(directory),
            plan=self._plan_view(run_id) if waiting else None,
        )

    def _plan_view(self, run_id: str) -> PlanView:
        scope = self._d.steps.plan_scope(run_id)
        rows = tuple(scope.store.rows(run_id, wave=1))
        live = [r for r in rows if r.state != "deleted"]
        approvable = bool(live) and all(r.state != "blocked" for r in live)
        approvable = approvable and any(r.state == "planned" for r in live)
        return PlanView(plan_sha256(rows), tuple(scope.items), rows, approvable)

    def plan(self, run_id: str) -> PlanView:
        self._require_plan_wait(run_id)
        return self._plan_view(run_id)

    def list_runs(self) -> list[RunRow]:
        return self._d.runs.list_runs()

    # ---- the owner's plan edits and approval ----------------------------------------------

    def _edit(self, run_id: str, action: Callable[[PlanScope], Any]) -> PlanView:
        with self._lock(run_id):
            self._require_plan_wait(run_id)
            action(self._d.steps.plan_scope(run_id))
            return self._plan_view(run_id)

    def edit_query(self, run_id: str, query_id: str, text: str) -> PlanView:
        return self._edit(run_id, lambda scope: edit_query(scope, query_id, text))

    def delete_query(self, run_id: str, query_id: str) -> PlanView:
        return self._edit(run_id, lambda scope: delete_query(scope, query_id))

    def add_query(self, run_id: str, item_id: str, lens: str, text: str) -> PlanView:
        return self._edit(run_id, lambda scope: add_query(scope, item_id, lens, text))

    def _recheck_denylist(self, scope: PlanScope) -> list[str]:
        """Block every planned query the current denylist now covers; returns their ids."""
        denylist = self._d.load_denylist()
        hit: list[str] = []
        for row in scope.store.rows(scope.run_id, wave=1):
            if row.state == "planned" and denylist.contains_any(row.sent):
                scope.store.set_sanitized(
                    scope.run_id, row.query_id, row.sent, row.removed, "blocked", "denylist"
                )
                hit.append(row.query_id)
        if hit:
            write_plan_file(scope)
        return hit

    def approve_plan(self, run_id: str, sha: str) -> RunView:
        """Approve exactly the plan whose hash is ``sha`` (M5 AC2) and continue the run."""
        with self._lock(run_id):
            self._require_plan_wait(run_id)
            view = self._plan_view(run_id)
            if view.plan_sha256 != sha:
                raise StalePlan("the hash does not belong to the current plan")
            if not view.approvable:
                raise PlanBlocked("the plan has a blocked query or nothing to search")
            hit = self._recheck_denylist(self._d.steps.plan_scope(run_id))
            if hit:
                raise PlanBlocked(f"the denylist now covers {', '.join(hit)}")
            value = {"kind": PLAN_KIND, "plan_sha256": sha}
            return self._drive(run_id, lambda: self._d.runner.resume(run_id, value))

    # ---- external briefs ------------------------------------------------------------------

    def create_external_run(
        self,
        text: str,
        *,
        tier: str,
        template_id: str,
        response_format: str,
        report_language: str,
    ) -> RunRow:
        """A queued run from a brief written outside Phase 1 (`udr run --brief`)."""
        if tier not in ("light", "full"):
            raise InvalidInput(f"tier must be light or full, got {tier!r}")
        try:
            template = get_template(self._d.templates, template_id)
            fmt = self._d.formats.named(response_format)
        except (TemplateError, ValueError) as exc:
            raise InvalidInput(str(exc)) from exc
        final = prepare_external_brief(
            text,
            template=template,
            fmt_name=response_format,
            fmt=fmt,
            language=report_language,
        )
        approved = self._d.now()
        path = archive_brief(self._d.briefs_dir, final, approved)
        return self._d.runs.create_external_run(
            sha256=brief_sha256(final),
            brief_path=str(path),
            tier=tier,
            template_id=template_id,
            response_format=response_format,
            report_language=report_language,
            approved_at=approved,
        )
