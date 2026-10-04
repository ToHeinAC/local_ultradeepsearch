"""One method per graph node of the Lite run (PRD M5). Each wraps its work in
`start_step`/`finish_step`, so `run.json` lists the steps in the order they ran (AC1). Every
method is safe to run again after a crash: the work underneath is idempotent."""

from collections.abc import Callable

from app.research.context import RunContext
from app.research.decompose import StepOneInput, StepOneResult, run_step_one
from app.research.errors import StalePlan
from app.research.plan import PlanScope, create_plan, plan_sha256
from app.research.shims import read_shim
from app.research.sweep import SweepScope, run_sweep
from app.research.workspace import bootstrap_workspace
from app.store.runs import RunStatus

Contexts = Callable[[str], RunContext]


def set_status(ctx: RunContext, status: RunStatus, reason: str = "") -> None:
    """The run's status in the database and in `run.json`."""
    ctx.runs.set_status(ctx.spec.run_id, status, reason)
    ctx.manifest.set_status(status, reason)


class LightSteps:
    def __init__(self, contexts: Contexts) -> None:
        self._contexts = contexts

    def context(self, run_id: str) -> RunContext:
        return self._contexts(run_id)

    def _step_one(self, ctx: RunContext) -> StepOneResult:
        """Step 1's result. Its answers are kept, so this makes no model call after the first."""
        spec = ctx.spec
        inp = StepOneInput(
            brief=ctx.brief,
            template=ctx.template,
            tier=spec.tier,
            response_format=spec.response_format,
            fmt=ctx.formats.named(spec.response_format),
            report_language=spec.report_language,
            domains=ctx.strategies.domains,
        )
        return run_step_one(ctx.llm, ctx.events, ctx.run_dir, inp, ctx.research)

    def plan_scope(self, run_id: str) -> PlanScope:
        ctx = self._contexts(run_id)
        result = self._step_one(ctx)
        scholarly = any(
            ctx.strategies.domains[d].scholarly_first for d in result.decomposition.domains
        )
        return PlanScope(run_id, ctx.run_dir, ctx.store, ctx.gateway, result.items, scholarly)

    # ---- the graph nodes ------------------------------------------------------------------

    def bootstrap(self, run_id: str) -> None:
        ctx = self._contexts(run_id)
        ctx.manifest.init(ctx.spec)
        ctx.manifest.start_step("0")
        bootstrap_workspace(ctx.run_dir, ctx.spec, ctx.brief)
        ctx.manifest.finish_step("0")
        set_status(ctx, "running")

    def decompose(self, run_id: str) -> None:
        ctx = self._contexts(run_id)
        ctx.manifest.start_step("1")
        self._step_one(ctx)
        ctx.manifest.finish_step("1")

    def plan(self, run_id: str) -> str:
        """Step 2.1 up to the approval point; returns the plan hash."""
        ctx = self._contexts(run_id)
        ctx.manifest.start_step("2.1")
        scope = self.plan_scope(run_id)
        rows = create_plan(
            scope,
            ctx.llm,
            ctx.events,
            brief=ctx.brief,
            profile=ctx.profile,
            shim=read_shim(ctx.run_dir, "research"),
        )
        set_status(ctx, "awaiting_plan_approval")
        return plan_sha256(rows)

    def approve(self, run_id: str, sha: str) -> None:
        """After the owner's approval: the hash must still be the current plan's."""
        ctx = self._contexts(run_id)
        if plan_sha256(ctx.store.rows(run_id, wave=1)) != sha:
            raise StalePlan("the plan changed after it was approved")
        ctx.manifest.finish_step("2.1")
        set_status(ctx, "running")

    def sweep(self, run_id: str) -> None:
        ctx = self._contexts(run_id)
        ctx.manifest.start_step("2")
        result = self._step_one(ctx)
        scope = SweepScope(
            plan=self.plan_scope(run_id),
            pipeline=ctx.pipeline,
            vault=ctx.vault,
            profile=ctx.profile,
            strategies=ctx.strategies,
            domains=result.decomposition.domains,
            events=ctx.events,
            llm=ctx.llm,
            brief=ctx.brief,
            shim=read_shim(ctx.run_dir, "research"),
        )
        run_sweep(scope)
        ctx.manifest.finish_step("2")

    def finish(self, run_id: str) -> None:
        set_status(self._contexts(run_id), "done")
