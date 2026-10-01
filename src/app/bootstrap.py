"""Composition root: turns settings into a wired runtime. The only place that picks adapters."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
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
from app.adapters.system_probe import Gpu
from app.calibration import Calibration, calibration_for, load_calibration, run_calibration
from app.config import Settings
from app.doctor import DoctorSnapshot
from app.events import EventSink
from app.llm.roles import build_registry
from app.llm.service import LLMService
from app.llm.types import Endpoint, Role, RoleSpec, Transport

CALIBRATION_FILE = "calibration.json"
DENYLIST_FILE = "denylist.txt"
TAVILY_LEDGER_FILE = "tavily-ledger.json"
MIB = 1024 * 1024


class HostProbe(InstanceProbe, Protocol):
    """What the instance manager and the doctor ask of the machine."""

    def gpus(self) -> list[Gpu] | None: ...
    def free_disk_bytes(self, path: Path) -> int: ...


@dataclass(frozen=True)
class Runtime:
    settings: Settings
    events: EventSink
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
    service = LLMService(registry, urls, transport, events, timeout_s=settings.llm_timeout_s)
    return Runtime(settings, events, status, urls, registry, transport, admin, probe, service)


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
    return OutboundGateway(
        providers=providers or build_providers(settings),
        denylist=Denylist.load(settings.data_dir / DENYLIST_FILE),
        sanitizer=Sanitizer(rt.service, confidential_context),
        log=OutboundLog(run_dir / "outbound.jsonl"),
        run_ledger=RunLedger(credit_cap),
        month_ledger=MonthLedger(
            settings.data_dir / TAVILY_LEDGER_FILE, settings.tavily_monthly_limit
        ),
        events=rt.events,
        internal_domains=settings.internal_domains,
    )
