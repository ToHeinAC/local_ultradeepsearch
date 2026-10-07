"""The steps of a Lite run (PRD M5, AD1): what each node of the `research` graph does.

Every step is idempotent and resumable (AD10): it records its transitions in `run.json`, reads
what earlier steps stored (never graph state), and a step that already finished does nothing when
the graph reaches it again. Work inside a step is saved item by item by the modules it calls.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.artifacts import write_text
from app.brief.parse import parse_brief
from app.events import EventSink
from app.llm.service import LLMService
from app.pipeline.artifacts import merge_run_json
from app.pipeline.profiles import ResearchBudget, ResponseFormats, RunRules
from app.pipeline.scoring import quality_scores
from app.pipeline.strategies import SourceStrategies
from app.research.candidates import collect_candidates, provenance
from app.research.decompose import Decomposer, write_artifacts
from app.research.draft import Drafter, DraftPlan, sections_of
from app.research.errors import check_stop, never_stop
from app.research.evidence import EvidenceKeys, PackBuilder, select_must_read
from app.research.export import PandocRunner, export_report
from app.research.fixes_model import ModelFixes
from app.research.manifest import (
    RunSettings,
    begin_step,
    finish_step,
    write_settings,
)
from app.research.models import Decomposition, atomic_items
from app.research.plan import (
    Planner,
    QueryPreparer,
    check_approvable,
    load_plan,
    save_plan,
)
from app.research.polish import Polisher
from app.research.readability import ReadabilityAuditor
from app.research.report import ApprovedBrief
from app.research.sections import write_report
from app.research.shipgate import GateContext, ShipGate
from app.research.sweep import Ingestor, Searcher, SweepDeps, Sweeper, stored_wave2
from app.store.research import SearchStore
from app.store.runs import RunRow, RunStore
from app.store.vault import Vault
from app.templates import ReportTemplate

MUST_READ_FILE = Path("temp") / "must-read.json"
KEYS_FILE = Path("temp") / "evidence-keys.json"


@dataclass(frozen=True)
class RunContext:
    """Everything one run needs that belongs to this run alone."""

    run: RunRow
    run_dir: Path
    settings: RunSettings
    brief: ApprovedBrief
    template: ReportTemplate
    vault: Vault
    preparer: QueryPreparer  # the gateway's `prepare_query`
    searcher: Searcher
    ingestor: Ingestor
    llm: LLMService  # the run's models: its own summarize model, if it chose one (PRD M6 D9)
    reference_docx: Path | None
    should_stop: Callable[[], bool] = never_stop  # true once the owner cancelled the run


@dataclass(frozen=True)
class StepDeps:
    """What the steps share across runs."""

    runs: RunStore
    searches: SearchStore
    rules: RunRules
    budgets: Mapping[str, ResearchBudget]
    strategies: SourceStrategies
    formats: ResponseFormats
    events: EventSink
    pandoc: PandocRunner
    prompt_chars: int  # what reason's prompt may hold, in characters
    condense_chars: int  # what one summarize call may hold, in characters
    now: Callable[[], datetime]


class ResearchSteps:
    def __init__(self, deps: StepDeps, contexts: Callable[[str], RunContext]) -> None:
        self._d = deps
        self._contexts = contexts

    def _step(self, ctx: RunContext, step: str, work: Callable[[], None]) -> None:
        """Run ``work`` as step ``step`` unless it finished before. A cancelled run stops here,
        before the step begins."""
        check_stop(ctx.should_stop)
        if begin_step(ctx.run_dir, step, self._d.now()):
            self._d.events.emit("step_started", step=step)
            work()
            finish_step(ctx.run_dir, step, self._d.now())
            self._d.events.emit("step_finished", step=step)

    # ---- 0, 1: bootstrap and decomposition -----------------------------------------------

    def bootstrap(self, run_id: str) -> None:
        """Step 0: the brief verbatim as `query.md`, and the settings the run was approved with."""
        ctx = self._contexts(run_id)

        def work() -> None:
            write_text(ctx.run_dir / "query.md", ctx.brief.text, scrub=False)
            write_settings(ctx.run_dir, ctx.settings)

        self._step(ctx, "0", work)

    def decompose(self, run_id: str) -> None:
        ctx = self._contexts(run_id)

        def work() -> None:
            decomposer = Decomposer(ctx.llm, self._d.rules, self._d.events)
            result = decomposer.decompose(
                brief=ctx.brief.text, settings=ctx.settings, template=ctx.template
            )
            write_artifacts(
                ctx.run_dir, ctx.brief.text, ctx.settings, ctx.template, result, self._d.now()
            )

        self._step(ctx, "1", work)

    # ---- 2.1: the search plan and its approval -------------------------------------------

    def plan(self, run_id: str) -> None:
        ctx = self._contexts(run_id)

        def work() -> None:
            planner = Planner(
                ctx.llm,
                self._d.rules,
                self._d.budgets[ctx.settings.tier],
                ctx.preparer,
                self._d.events,
            )
            shim = (ctx.run_dir / "shims" / "research.md").read_text(encoding="utf-8")
            plan = planner.plan(ctx.brief.text, self._decomposition(ctx), shim)
            save_plan(ctx.run_dir, plan)

        self._step(ctx, "2.1", work)

    def mark_awaiting(self, run_id: str) -> None:
        """The run waits for the owner's approval of its search plan; it holds no worker slot."""
        self._d.runs.set_status(run_id, "awaiting_plan_approval")

    def confirm_plan(self, run_id: str, plan_sha256: str) -> None:
        """Take the approval: the hash must be the current plan's and the plan sendable."""
        ctx = self._contexts(run_id)
        check_approvable(load_plan(ctx.run_dir), plan_sha256)
        merge_run_json(ctx.run_dir, {"plan_approved_sha256": plan_sha256})
        self._d.runs.set_status(run_id, "running")
        self._d.events.emit("plan_approved", run_id=run_id, plan_sha256=plan_sha256)

    # ---- 2: the width sweep --------------------------------------------------------------

    def sweep(self, run_id: str) -> None:
        ctx = self._contexts(run_id)

        def work() -> None:
            d = self._d
            sweeper = Sweeper(
                SweepDeps(
                    run_id=run_id,
                    run_dir=ctx.run_dir,
                    searches=d.searches,
                    searcher=ctx.searcher,
                    preparer=ctx.preparer,
                    ingestor=ctx.ingestor,
                    vault=ctx.vault,
                    service=ctx.llm,
                    strategies=d.strategies,
                    budget=d.budgets[ctx.settings.tier],
                    rules=d.rules,
                    events=d.events,
                    stop=ctx.should_stop,
                )
            )
            sweeper.run(ctx.brief.text, load_plan(ctx.run_dir), self._decomposition(ctx))

        self._step(ctx, "2", work)

    # ---- 10: drafting --------------------------------------------------------------------

    def draft(self, run_id: str) -> None:
        ctx = self._contexts(run_id)

        def work() -> None:
            plan = self._draft_plan(ctx, self._must_read(ctx))
            keys, drafter, _ = self._writers(ctx)
            drafter.draft_all(ctx.run_dir, plan, ctx.should_stop)
            self._render(ctx, plan, keys)

        self._step(ctx, "10", work)

    def _must_read(self, ctx: RunContext) -> list[str]:
        """The sources the sections are written from, chosen once and stored."""
        path = ctx.run_dir / MUST_READ_FILE
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["note_ids"]
        d = self._d
        queries = {
            q.query_id: q for q in [*load_plan(ctx.run_dir).queries, *stored_wave2(ctx.run_dir)]
        }
        candidates = collect_candidates(queries, d.searches.all(ctx.run.run_id), d.strategies)
        notes = ctx.vault.notes(kind="source")
        items = [item.id for item in atomic_items(self._decomposition(ctx))]
        chosen = select_must_read(
            notes,
            quality_scores(ctx.vault, d.strategies),
            provenance(notes, candidates),
            items,
            d.budgets[ctx.settings.tier].must_read_notes,
        )
        write_text(path, json.dumps({"note_ids": chosen}) + "\n")
        return chosen

    def _draft_plan(self, ctx: RunContext, must_read: list[str]) -> DraftPlan:
        parsed = parse_brief(ctx.brief.text)
        return DraftPlan(
            title=parsed.title,
            questions=parsed.research_questions,
            sections=tuple(sections_of(self._decomposition(ctx), ctx.template.sections)),
            language=ctx.settings.report_language,
            fmt=self._d.formats.named(ctx.settings.response_format),
            must_read=tuple(must_read),
            shim=(ctx.run_dir / "shims" / "drafting.md").read_text(encoding="utf-8"),
        )

    def _writers(self, ctx: RunContext) -> tuple[EvidenceKeys, Drafter, ModelFixes]:
        d = self._d
        keys = EvidenceKeys(ctx.run_dir / KEYS_FILE)
        packs = PackBuilder(ctx.vault, keys, ctx.llm, d.rules, d.events)
        sizes = {"prompt_chars": d.prompt_chars, "condense_chars": d.condense_chars}
        drafter = Drafter(ctx.llm, packs, keys, d.rules, d.events, **sizes)
        return keys, drafter, ModelFixes(ctx.llm, packs, keys, d.rules, d.events, **sizes)

    def _render(self, ctx: RunContext, plan: DraftPlan, keys: EvidenceKeys) -> None:
        write_report(
            ctx.run_dir,
            title=plan.title,
            headings=[s.heading for s in plan.sections],
            keys=keys.mapping(),
            vault=ctx.vault,
            language=plan.language,
            brief=ctx.brief,
            events=self._d.events,
        )

    # ---- 15, 16: polish and readability --------------------------------------------------

    def polish(self, run_id: str) -> None:
        ctx = self._contexts(run_id)

        def work() -> None:
            plan = self._draft_plan(ctx, self._must_read(ctx))
            shim = (ctx.run_dir / "shims" / "polish.md").read_text(encoding="utf-8")
            Polisher(ctx.llm, self._d.rules, self._d.events).polish_all(
                ctx.run_dir,
                headings=[s.heading for s in plan.sections],
                language=plan.language,
                shim=shim,
            )
            self._render(ctx, plan, EvidenceKeys(ctx.run_dir / KEYS_FILE))

        self._step(ctx, "15", work)

    def readability(self, run_id: str) -> None:
        ctx = self._contexts(run_id)

        def work() -> None:
            plan = self._draft_plan(ctx, self._must_read(ctx))
            auditor = ReadabilityAuditor(
                ctx.llm, self._d.rules, self._d.budgets[ctx.settings.tier], self._d.events
            )
            auditor.audit_all(ctx.run_dir, headings=[s.heading for s in plan.sections])
            self._render(ctx, plan, EvidenceKeys(ctx.run_dir / KEYS_FILE))

        self._step(ctx, "16", work)

    # ---- G, X: the ship gate and the export ----------------------------------------------

    def gate(self, run_id: str) -> None:
        ctx = self._contexts(run_id)

        def work() -> None:
            plan = self._draft_plan(ctx, self._must_read(ctx))
            keys, drafter, fixes = self._writers(ctx)
            gate = ShipGate(ctx.vault, keys, self._d.rules, self._d.events, fixes, drafter)
            outcome = gate.run(
                GateContext(
                    ctx.run_dir,
                    ctx.settings.tier,
                    ctx.brief,
                    str(ctx.run.brief_sha256),
                    plan,
                )
            )
            summary = {"passed": outcome.passed, "failed": outcome.result.failed}
            merge_run_json(ctx.run_dir, {"gate": summary})

        self._step(ctx, "G", work)

    def export(self, run_id: str) -> None:
        """Step X, then the run's final status: `done` if the gate passed, else `blocked`."""
        ctx = self._contexts(run_id)

        def work() -> None:
            result = export_report(
                ctx.run_dir,
                self._d.pandoc,
                reference_docx=ctx.reference_docx,
                events=self._d.events,
            )
            merge_run_json(ctx.run_dir, {"exports": {"docx": result.docx, "pdf": result.pdf}})

        self._step(ctx, "X", work)
        gate = json.loads((ctx.run_dir / "run.json").read_text(encoding="utf-8"))["gate"]
        status = "done" if gate["passed"] else "blocked"
        # The event first: whoever stops reading at a final status has seen every event.
        self._d.events.emit("run_finished", run_id=run_id, status=status)
        self._d.runs.set_status(run_id, status)

    def _decomposition(self, ctx: RunContext) -> Decomposition:
        text = (ctx.run_dir / "prompt-decomposition.json").read_text(encoding="utf-8")
        return Decomposition.model_validate_json(text)
