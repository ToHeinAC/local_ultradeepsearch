"""Composition root: turns settings into a wired runtime. The only place that picks adapters."""

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from app.adapters.ollama_instance import (
    InstanceProbe,
    InstanceState,
    InstanceStatus,
    LiveProbe,
    Spawner,
    endpoint_urls,
    ensure_own_instance,
    subprocess_spawner,
)
from app.adapters.ollama_transport import LoadedModel, OllamaAdmin, OllamaTransport
from app.adapters.outbound.ddgs_search import DdgsSearch
from app.adapters.outbound.denylist import Denylist
from app.adapters.outbound.gateway import OutboundGateway, Providers
from app.adapters.outbound.http_get import HttpGetter
from app.adapters.outbound.ledger import MonthLedger, RunLedger
from app.adapters.outbound.log import OutboundLog
from app.adapters.outbound.sanitizer import Sanitizer
from app.adapters.outbound.scholarly import ArxivApi, CrossrefApi, OpenAlexApi
from app.adapters.outbound.tavily import TavilyApi
from app.adapters.outbound.types import HttpFactory, default_http
from app.adapters.pandoc import SubprocessPandoc
from app.adapters.system_probe import Gpu
from app.brief.archive import approved_at_of
from app.brief.errors import NotFound
from app.brief.interview import Interviewer
from app.brief.parse import parse_brief
from app.brief.service import BriefService, ServiceDeps
from app.brief.uploads import UploadIngestor
from app.calibration import Calibration, calibration_for, load_calibration, run_calibration
from app.config import Settings
from app.doctor import DoctorSnapshot
from app.events import EventSink, RunScopedEventSink
from app.graphs.brief import BriefDeps, BriefRunner, build_brief_graph, open_checkpointer
from app.graphs.research import ResearchRunner, build_research_graph
from app.llm.roles import build_registry
from app.llm.service import LLMService
from app.llm.structured import CHARS_PER_TOKEN
from app.llm.types import Endpoint, Role, RoleSpec, Transport
from app.pipeline.analysis import SourceAnalyzer
from app.pipeline.extraction import Focus, NoteExtractor
from app.pipeline.fetch import Fetcher, FetchPipeline
from app.pipeline.profiles import (
    Profile,
    load_phase1,
    load_profile,
    load_research_budget,
    load_response_formats,
    load_run_rules,
)
from app.pipeline.strategies import load_strategies
from app.research.export import PandocRunner
from app.research.plan import QueryPreparer
from app.research.report import ApprovedBrief
from app.research.service import ResearchService
from app.research.service import ServiceDeps as ResearchServiceDeps
from app.research.settings import resolve_run_settings
from app.research.steps import ResearchSteps, RunContext, StepDeps
from app.research.sweep import Searcher
from app.store.db import Database
from app.store.research import SearchStore
from app.store.runs import RunRow, RunStore
from app.store.sessions import SessionRow, SessionStore
from app.store.vault import Vault
from app.templates import ReportTemplate, load_templates

CALIBRATION_FILE = "calibration.json"
DENYLIST_FILE = "denylist.txt"
TAVILY_LEDGER_FILE = "tavily-ledger.json"
VAULT_FILE = "udr.sqlite"
CHECKPOINT_FILE = "checkpoints.sqlite"
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
TIERS = ("light", "full")
MIB = 1024 * 1024


class HostProbe(InstanceProbe, Protocol):
    """What the instance manager and the doctor ask of the machine."""

    def gpus(self) -> list[Gpu] | None: ...
    def free_disk_bytes(self, path: Path) -> int: ...


@dataclass(frozen=True)
class Runtime:
    settings: Settings
    events: RunScopedEventSink  # also writes a per-run file while a run is bound
    status: InstanceStatus
    urls: Mapping[Endpoint, str]
    registry: Mapping[Role, RoleSpec]
    transport: Transport
    admin: OllamaAdmin
    probe: HostProbe
    service: LLMService


def load_settings() -> Settings:
    return Settings()


def calibration_path(settings: Settings) -> Path:
    return settings.data_dir / CALIBRATION_FILE


def build_runtime(
    settings: Settings,
    events: EventSink,
    *,
    probe: HostProbe | None = None,
    admin: OllamaAdmin | None = None,
    transport: Transport | None = None,
    spawner: Spawner = subprocess_spawner,
    env: Mapping[str, str] | None = None,
) -> Runtime:
    """Ensure our Ollama instance (adopt, start or fail open) and wire the LLM service."""
    admin = admin or OllamaAdmin()
    probe = probe or LiveProbe(admin)
    status = ensure_own_instance(
        settings, probe, spawner, events, base_env=os.environ if env is None else env
    )
    urls = endpoint_urls(settings, status)
    stored = load_calibration(calibration_path(settings))
    registry = build_registry(settings, calibration_for(stored, settings))
    transport = transport or OllamaTransport()
    scoped = RunScopedEventSink(events)
    service = LLMService(registry, urls, transport, scoped, timeout_s=settings.llm_timeout_s)
    return Runtime(settings, scoped, status, urls, registry, transport, admin, probe, service)


def _own_loaded_vram(rt: Runtime) -> int:
    if rt.status.state not in (InstanceState.ADOPTED, InstanceState.STARTED):
        return 0
    loaded: list[LoadedModel] = rt.admin.ps(rt.urls[Endpoint.OWN]) or []
    return sum(m.size_vram for m in loaded)


def collect_snapshot(rt: Runtime) -> DoctorSnapshot:
    """Ask the machine everything the doctor needs. Reads the calibration file fresh."""
    stored = load_calibration(calibration_path(rt.settings))
    by_url = {url: rt.admin.tags(url) for url in set(rt.urls.values())}
    return DoctorSnapshot(
        settings=rt.settings,
        instance=rt.status,
        urls=rt.urls,
        tags={endpoint: by_url[url] for endpoint, url in rt.urls.items()},
        free_disk_bytes=rt.probe.free_disk_bytes(rt.settings.data_dir),
        gpus=rt.probe.gpus(),
        own_loaded_vram_bytes=_own_loaded_vram(rt),
        calibration_ctx=calibration_for(stored, rt.settings),
    )


def calibrate(rt: Runtime) -> Calibration | None:
    """Measure the largest `reason` context that fits fully in VRAM on our own instance."""
    return run_calibration(
        rt.registry[Role.REASON],
        rt.urls[Endpoint.OWN],
        rt.settings.own_ollama_gpu,
        rt.transport,
        rt.admin,
        rt.events,
        timeout_s=rt.settings.llm_timeout_s,
    )


def build_providers(settings: Settings, *, http: HttpFactory = default_http) -> Providers:
    """The outbound clients. Without `TAVILY_API_KEY`, web search uses ddgs from the start."""
    timeout = settings.fetch_timeout_s
    key = settings.tavily_api_key
    openalex_key = settings.openalex_api_key
    return Providers(
        tavily=TavilyApi(key.get_secret_value(), http, timeout_s=timeout) if key else None,
        ddgs=DdgsSearch(),
        openalex=OpenAlexApi(
            http,
            mailto=settings.openalex_mailto,
            api_key=openalex_key.get_secret_value() if openalex_key else None,
            timeout_s=timeout,
        ),
        crossref=CrossrefApi(http, mailto=settings.openalex_mailto, timeout_s=timeout),
        arxiv=ArxivApi(http, timeout_s=timeout),
        http=HttpGetter(
            http,
            timeout_s=timeout,
            max_html_bytes=int(settings.max_html_mb * MIB),
            max_pdf_bytes=int(settings.max_pdf_mb * MIB),
        ),
    )


def build_gateway(
    rt: Runtime,
    run_dir: Path,
    *,
    credit_cap: int,
    confidential_context: str,
    providers: Providers | None = None,
) -> OutboundGateway:
    """The outbound gateway of one run: its log in ``run_dir``, shared denylist and month ledger."""
    settings = rt.settings
    log = OutboundLog(run_dir / "outbound.jsonl")
    return OutboundGateway(
        providers=providers or build_providers(settings),
        denylist=Denylist.load(settings.data_dir / DENYLIST_FILE),
        sanitizer=Sanitizer(rt.service, confidential_context),
        log=log,
        run_ledger=RunLedger(credit_cap, used=log.total_credits()),
        month_ledger=MonthLedger(
            settings.data_dir / TAVILY_LEDGER_FILE, settings.tavily_monthly_limit
        ),
        events=rt.events,
        internal_domains=settings.internal_domains,
    )


def run_dir(settings: Settings, run_id: str) -> Path:
    """``data/runs/<run_id>``: the inspectable files of one run (the id is one safe path part)."""
    if not RUN_ID.fullmatch(run_id):
        raise ValueError(f"invalid run id {run_id!r}: use letters, digits, '.', '_' and '-' only")
    return settings.data_dir / "runs" / run_id


def open_vault(settings: Settings, run_id: str, *, label: str = "") -> Vault:
    """The run's view of the shared database ``data/udr.sqlite``; reopening it resumes the run."""
    run_dir(settings, run_id)  # validates the id before anything is created
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return Vault(settings.data_dir / VAULT_FILE, run_id, label=label)


def build_pipeline(
    rt: Runtime,
    run_id: str,
    *,
    tier: str,
    focus: Focus,
    confidential_context: str = "",
    fetcher: Fetcher | None = None,
) -> FetchPipeline:
    """The ingestion pipeline of one run. Call ``resume()`` before new work (PRD AD10).

    Without ``fetcher`` the run's outbound gateway is built, capped at the tier's credit limit.
    """
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}: expected one of {', '.join(TIERS)}")
    settings = rt.settings
    profile = load_profile(tier, settings.config_dir)
    directory = run_dir(settings, run_id)
    return FetchPipeline(
        vault=open_vault(settings, run_id),
        fetcher=fetcher
        or build_gateway(
            rt,
            directory,
            credit_cap=profile.credit_cap,
            confidential_context=confidential_context,
        ),
        extractor=NoteExtractor(rt.service, rt.events),
        analyzer=SourceAnalyzer(rt.service, rt.events),
        strategies=load_strategies(settings.config_dir),
        profile=profile,
        focus=focus,
        run_dir=directory,
        events=rt.events,
    )


def load_report_templates(settings: Settings) -> dict[str, ReportTemplate]:
    """The built-in templates (`templates/`) and the owner's uploads (`data/templates/`)."""
    return load_templates([settings.templates_dir, settings.data_dir / "templates"])


def _utcnow() -> datetime:
    return datetime.now(UTC)


def build_brief_service(rt: Runtime, *, now: Callable[[], datetime] = _utcnow) -> BriefService:
    """The Phase-1 service on the runtime's models, ``data/udr.sqlite`` and the checkpointer in
    ``data/checkpoints.sqlite``. Call ``recover()`` once after a start to continue what a crash
    left unfinished (PRD AD10). It never touches the outbound gateway."""
    settings = rt.settings
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.data_dir / VAULT_FILE)
    sessions, runs = SessionStore(db), RunStore(db)
    limits = load_phase1(settings.config_dir)
    formats = load_response_formats(settings.config_dir)
    templates = load_report_templates(settings)
    briefs_dir = settings.data_dir / "briefs"
    drafts_dir = briefs_dir / "drafts"
    ingestor = UploadIngestor(
        sessions, rt.service, limits, settings.data_dir / "uploads", rt.events
    )
    graph = build_brief_graph(
        BriefDeps(
            store=sessions,
            runs=runs,
            ingestor=ingestor,
            interviewer=Interviewer(rt.service, limits, formats),
            templates=templates,
            formats=formats,
            limits=limits,
            briefs_dir=briefs_dir,
            drafts_dir=drafts_dir,
        ),
        open_checkpointer(settings.data_dir / CHECKPOINT_FILE),
    )
    return BriefService(
        ServiceDeps(
            store=sessions,
            runs=runs,
            ingestor=ingestor,
            runner=BriefRunner(graph),
            limits=limits,
            templates=templates,
            formats=formats,
            drafts_dir=drafts_dir,
            now=now,
        )
    )


# ---- Phase 2 (M5) ---------------------------------------------------------------------------

LOCK_FILE = "worker.lock"
CONDENSE_SHARE = 0.6  # of summarize's context one condensing call may fill


class _RunGateway(QueryPreparer, Searcher, Fetcher, Protocol):
    """What one run needs from the outbound side: the real gateway, or a fake in tests."""


GatewayFactory = Callable[[Path, str, Profile], _RunGateway]


def _step_deps(
    rt: Runtime,
    searches: SearchStore,
    runs: RunStore,
    pandoc: PandocRunner,
    now: Callable[[], datetime],
) -> StepDeps:
    settings = rt.settings
    reason, summarize = rt.registry[Role.REASON], rt.registry[Role.SUMMARIZE]
    return StepDeps(
        service=rt.service,
        runs=runs,
        searches=searches,
        rules=load_run_rules(settings.config_dir),
        budgets={t: load_research_budget(t, settings.config_dir) for t in TIERS},
        strategies=load_strategies(settings.config_dir),
        formats=load_response_formats(settings.config_dir),
        events=rt.events,
        pandoc=pandoc,
        prompt_chars=(reason.num_ctx - reason.num_predict) * CHARS_PER_TOKEN,
        condense_chars=int(
            (summarize.num_ctx - summarize.num_predict) * CHARS_PER_TOKEN * CONDENSE_SHARE
        ),
        now=now,
    )


def _build_run_context(
    rt: Runtime,
    row: RunRow,
    session: SessionRow | None,
    templates: dict[str, ReportTemplate],
    make_gateway: GatewayFactory,
) -> RunContext:
    settings = rt.settings
    run_settings = resolve_run_settings(row, session, templates)
    archive = Path(str(row.brief_path))
    text = archive.read_text(encoding="utf-8")
    parsed = parse_brief(text)
    directory = run_dir(settings, row.run_id)
    gateway = make_gateway(directory, text, load_profile(run_settings.tier, settings.config_dir))
    template = templates[run_settings.template_id]
    pipeline = build_pipeline(
        rt,
        row.run_id,
        tier=run_settings.tier,
        focus=Focus(parsed.title, parsed.research_questions),
        fetcher=gateway,
    )
    reference = (
        settings.data_dir / "templates" / template.reference_docx
        if template.reference_docx
        else None
    )
    return RunContext(
        run=row,
        run_dir=directory,
        settings=run_settings,
        brief=ApprovedBrief(text, approved_at_of(archive), archive.name),
        template=template,
        vault=open_vault(settings, row.run_id),
        preparer=gateway,
        searcher=gateway,
        ingestor=pipeline,
        reference_docx=reference,
    )


def _run_context_factory(
    rt: Runtime,
    runs: RunStore,
    sessions: SessionStore,
    templates: dict[str, ReportTemplate],
    make_gateway: GatewayFactory,
) -> Callable[[str], RunContext]:
    """The per-run objects, built on first use and kept: one gateway (and so one credit counter)
    and one fetch pipeline per run and process."""
    cache: dict[str, RunContext] = {}

    def context(run_id: str) -> RunContext:
        if run_id not in cache:
            row = runs.get_run(run_id)
            if row is None:
                raise NotFound(run_id)
            session = sessions.get(row.session_id) if row.session_id else None
            cache[run_id] = _build_run_context(rt, row, session, templates, make_gateway)
        return cache[run_id]

    return context


def build_research_service(
    rt: Runtime,
    *,
    now: Callable[[], datetime] = _utcnow,
    gateway_factory: GatewayFactory | None = None,
    pandoc: PandocRunner | None = None,
) -> ResearchService:
    """The Phase-2 service on the runtime's models, ``data/udr.sqlite`` and the checkpointer in
    ``data/checkpoints.sqlite``. ``gateway_factory`` and ``pandoc`` replace the real outbound
    gateway and the pandoc binary (tests). The worker slot is ``data/worker.lock``."""
    settings = rt.settings
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.data_dir / VAULT_FILE)
    runs, sessions, searches = RunStore(db), SessionStore(db), SearchStore(db)
    templates = load_report_templates(settings)

    def real_gateway(directory: Path, brief: str, profile: Profile) -> _RunGateway:
        return build_gateway(
            rt, directory, credit_cap=profile.credit_cap, confidential_context=brief
        )

    context = _run_context_factory(rt, runs, sessions, templates, gateway_factory or real_gateway)
    deps = _step_deps(rt, searches, runs, pandoc or SubprocessPandoc(), now)
    graph = build_research_graph(
        ResearchSteps(deps, context), open_checkpointer(settings.data_dir / CHECKPOINT_FILE)
    )
    return ResearchService(
        ResearchServiceDeps(
            runs=runs,
            runner=ResearchRunner(graph),
            events=rt.events,
            bind=rt.events.bound,
            contexts=context,
            templates=templates,
            formats=deps.formats,
            data_dir=settings.data_dir,
            lock_path=settings.data_dir / LOCK_FILE,
            now=now,
        )
    )
