"""A whole Lite run on fakes: the real service, graph, steps, vault and fetch pipeline, over a
scripted model, a scripted search and fetch, and a fake pandoc. Shared by the service, graph and
crash tests, and by the child process the SIGKILL tests kill."""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fixtures_corpus import Built, Outcome, article, build, doc, site
from langgraph.checkpoint.sqlite import SqliteSaver
from research_rig import (
    FORMATS,
    LIGHT,
    REGISTRY,
    RULES,
    SETTINGS,
    TEMPLATES,
    FakePandoc,
    FakePreparer,
    FakeSearcher,
    ResearchModels,
    llm,
)

from app.adapters.outbound.types import ScholarlyRecord, SearchHit
from app.events import MemoryEventSink
from app.graphs.research import ResearchRunner, build_research_graph
from app.llm.structured import CHARS_PER_TOKEN
from app.llm.types import Role
from app.pipeline.strategies import load_strategies
from app.research.report import ApprovedBrief
from app.research.service import ResearchService, RunView, ServiceDeps
from app.research.settings import resolve_run_settings
from app.research.steps import ResearchSteps, RunContext, StepDeps
from app.store.db import Database
from app.store.research import SearchStore
from app.store.runs import RunStore

NOW = datetime(2026, 10, 3, 9, 0, 0, tzinfo=UTC)
RAW_BRIEF = (
    "# Wie lange dauert der Rückbau eines Forschungsreaktors?\n\n"
    "## Forschungsfragen\n\n"
    "1. Wie lange dauert der Rückbau?\n"
    "2. Welche Genehmigungen sind nötig?\n"
)
TEMPLATE = "technische-stellungnahme"
CSS = Path(__file__).parents[1] / "templates" / "report.css"


def hit(i: int) -> SearchHit:
    return SearchHit(f"Page {i}", site(i), "snippet", "tavily")


def paper(i: int) -> ScholarlyRecord:
    return ScholarlyRecord(
        source="openalex", title=f"Paper {i}", url=site(i), doi=f"10.1234/p{i}", year=2020
    )


def make_searcher(*, crash_at: int | None = None, hang_at: int | None = None) -> FakeSearcher:
    """Every plan query of the rig's default plan finds pages, so no item is thin."""
    web = {
        "Rückbau Forschungsreaktor Dauer": [hit(1), hit(2), hit(3)],
        "Rückbau Forschungsreaktor Verzögerungen Kritik": [hit(4)],
        "Genehmigung Rückbau Atomgesetz": [hit(5), hit(6)],
        "Genehmigung Rückbau Probleme Klage": [hit(7)],
        "Forschungsreaktor Stilllegung Stand": [hit(8), hit(9)],
        "Forschungsreaktor Rückbau gescheitert": [hit(10)],
    }
    scholarly = {
        ("openalex", "decommissioning research reactor duration study"): [paper(11)],
        ("openalex", "nuclear decommissioning licensing review"): [paper(12)],
    }
    return FakeSearcher(web=web, scholarly=scholarly, crash_at=crash_at, hang_at=hang_at)


def served() -> dict[str, Outcome]:
    return {site(i): doc(site(i), article(i), title=f"Article {i}") for i in range(1, 16)}


@dataclass
class RunRig:
    base: Path
    service: ResearchService
    runs: RunStore
    searches: SearchStore
    models: ResearchModels
    searcher: FakeSearcher
    preparer: FakePreparer
    pandoc: FakePandoc
    events: MemoryEventSink
    built: dict[str, Built]
    steps: ResearchSteps

    def create(self, **kwargs: str) -> RunView:
        """An external run of the rig's brief, queued."""
        args = {"tier": "light", "template_id": TEMPLATE, "language": "de"}
        return self.service.create_external_run(RAW_BRIEF, **{**args, **kwargs})  # type: ignore[arg-type]

    def run_dir(self, run_id: str) -> Path:
        return self.base / "runs" / run_id


def make_rig(
    base: Path,
    *,
    models: ResearchModels | None = None,
    searcher: FakeSearcher | None = None,
    preparer: FakePreparer | None = None,
    pandoc: FakePandoc | None = None,
) -> RunRig:
    """One 'process' over the files in ``base``; calling it again is a restart."""
    events = MemoryEventSink()
    models = models or ResearchModels()
    searcher = searcher or make_searcher()
    preparer = preparer or FakePreparer()
    pandoc = pandoc or FakePandoc()
    db = Database(base / "udr.sqlite", now=lambda: NOW)
    runs, searches = RunStore(db), SearchStore(db)
    built: dict[str, Built] = {}
    context = _contexts(base, runs, events, built, searcher, preparer)
    deps = _step_deps(models, events, runs, searches, pandoc)
    saver = SqliteSaver(sqlite3.connect(base / "checkpoints.sqlite", check_same_thread=False))
    steps = ResearchSteps(deps, context)
    runner = ResearchRunner(build_research_graph(steps, saver))
    service = ResearchService(
        ServiceDeps(
            runs=runs,
            runner=runner,
            events=events,
            contexts=context,
            templates=TEMPLATES,
            formats=FORMATS,
            data_dir=base,
            lock_path=base / "worker.lock",
            now=lambda: NOW,
        )
    )
    return RunRig(
        base, service, runs, searches, models, searcher, preparer, pandoc, events, built, steps
    )


def _contexts(
    base: Path,
    runs: RunStore,
    events: MemoryEventSink,
    built: dict[str, Built],
    searcher: FakeSearcher,
    preparer: FakePreparer,
) -> Callable[[str], RunContext]:
    """The per-run objects, built on first use and kept: a run's searcher and pipeline persist
    across its steps, as in the real service."""
    contexts: dict[str, RunContext] = {}

    def context(run_id: str) -> RunContext:
        if run_id not in contexts:
            row = runs.get_run(run_id)
            assert row is not None
            b = built.setdefault(run_id, build(base, served(), events=events, run_id=run_id))
            settings = resolve_run_settings(row, None, TEMPLATES)
            text = Path(str(row.brief_path)).read_text(encoding="utf-8")
            brief = ApprovedBrief(text, NOW, f"briefs/{Path(str(row.brief_path)).name}")
            contexts[run_id] = RunContext(
                run=row,
                run_dir=base / "runs" / run_id,
                settings=settings,
                brief=brief,
                template=TEMPLATES[settings.template_id],
                vault=b.vault,
                preparer=preparer,
                searcher=searcher,
                ingestor=b.pipeline,
                reference_docx=None,
            )
        return contexts[run_id]

    return context


def _step_deps(
    models: ResearchModels,
    events: MemoryEventSink,
    runs: RunStore,
    searches: SearchStore,
    pandoc: FakePandoc,
) -> StepDeps:
    spec, summ = REGISTRY[Role.REASON], REGISTRY[Role.SUMMARIZE]
    return StepDeps(
        service=llm(models, events),
        runs=runs,
        searches=searches,
        rules=RULES,
        budgets={"light": LIGHT, "full": LIGHT},
        strategies=load_strategies(SETTINGS.config_dir),
        formats=FORMATS,
        events=events,
        pandoc=pandoc,
        css=CSS,
        prompt_chars=(spec.num_ctx - spec.num_predict) * CHARS_PER_TOKEN,
        condense_chars=int((summ.num_ctx - summ.num_predict) * CHARS_PER_TOKEN * 0.6),
        now=lambda: NOW,
    )
