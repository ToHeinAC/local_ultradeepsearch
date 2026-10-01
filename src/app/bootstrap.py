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
from app.adapters.system_probe import Gpu
from app.calibration import Calibration, calibration_for, load_calibration, run_calibration
from app.config import Settings
from app.doctor import DoctorSnapshot
from app.events import EventSink
from app.llm.roles import build_registry
from app.llm.service import LLMService
from app.llm.types import Endpoint, Role, RoleSpec, Transport

CALIBRATION_FILE = "calibration.json"


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
