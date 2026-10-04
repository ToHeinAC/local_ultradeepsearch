"""The objects one run works with, built once per run id and kept for the life of the process.

The factory takes the adapter-dependent pieces as functions (the composition root supplies the real
ones, the tests fakes), so this module stays free of I/O choices."""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.brief.errors import NotFound, WrongState
from app.brief.parse import parse_brief
from app.events import EventSink
from app.llm.service import LLMService
from app.pipeline.extraction import Focus
from app.pipeline.fetch import FetchPipeline
from app.pipeline.profiles import (
    Profile,
    ResearchConfig,
    ResponseFormats,
    load_profile,
    load_research_config,
)
from app.pipeline.strategies import SourceStrategies, load_strategies
from app.research.manifest import Manifest
from app.research.models import RunSpec
from app.research.ports import ResearchGateway
from app.research.workspace import read_brief
from app.store.research import ResearchStore
from app.store.runs import RunStore
from app.store.vault import Vault
from app.templates import ReportTemplate, get_template


@dataclass(frozen=True)
class RunContext:
    spec: RunSpec
    brief: str
    run_dir: Path
    vault: Vault
    gateway: ResearchGateway
    pipeline: FetchPipeline
    events: EventSink
    llm: LLMService
    template: ReportTemplate
    formats: ResponseFormats
    profile: Profile
    strategies: SourceStrategies
    research: ResearchConfig
    store: ResearchStore
    runs: RunStore
    manifest: Manifest


@dataclass(frozen=True)
class ContextBuilders:
    """What differs between the real wiring and a test rig."""

    run_dir: Callable[[str], Path]
    events: Callable[[Path], EventSink]
    llm: Callable[[RunSpec], LLMService]
    gateway: Callable[[str, Path, str, Profile, EventSink], ResearchGateway]  # + brief
    vault: Callable[[str], Vault]
    pipeline: Callable[[str, ResearchGateway, Focus, Profile, EventSink], FetchPipeline]


class ContextFactory:
    def __init__(
        self,
        *,
        runs: RunStore,
        store: ResearchStore,
        templates: dict[str, ReportTemplate],
        formats: ResponseFormats,
        config_dir: Path,
        build: ContextBuilders,
    ) -> None:
        self._runs, self._store, self._templates = runs, store, templates
        self._formats, self._config_dir, self._build = formats, config_dir, build
        self._cache: dict[str, RunContext] = {}
        self._lock = threading.Lock()

    def __call__(self, run_id: str) -> RunContext:
        with self._lock:
            if run_id not in self._cache:
                self._cache[run_id] = self._make(run_id)
            return self._cache[run_id]

    def _spec(self, run_id: str) -> RunSpec:
        row = self._runs.get_run(run_id)
        if row is None:
            raise NotFound(run_id)
        if row.tier != "light":
            raise WrongState("Full-Tier ab M8")
        if not (row.template_id and row.response_format and row.report_language):
            raise WrongState("the run has no report settings")
        return RunSpec(
            run_id,
            row.tier,
            row.brief_sha256 or "",
            row.brief_path or "",
            row.template_id,
            row.response_format,
            row.report_language,
            row.summarize_model,
        )

    def _make(self, run_id: str) -> RunContext:
        spec = self._spec(run_id)
        brief = read_brief(spec.brief_path, spec.brief_sha256)
        b, directory = self._build, self._build.run_dir(run_id)
        events = b.events(directory)
        profile = load_profile(spec.tier, self._config_dir)
        gateway = b.gateway(run_id, directory, brief, profile, events)
        parsed = parse_brief(brief)
        focus = Focus(parsed.title, parsed.research_questions)
        return RunContext(
            spec=spec,
            brief=brief,
            run_dir=directory,
            vault=b.vault(run_id),
            gateway=gateway,
            pipeline=b.pipeline(run_id, gateway, focus, profile, events),
            events=events,
            llm=b.llm(spec),
            template=get_template(self._templates, spec.template_id),
            formats=self._formats,
            profile=profile,
            strategies=load_strategies(self._config_dir),
            research=load_research_config(self._config_dir),
            store=self._store,
            runs=self._runs,
            manifest=Manifest(directory),
        )
