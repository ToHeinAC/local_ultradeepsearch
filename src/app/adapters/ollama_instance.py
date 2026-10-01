"""An Ollama daemon of our own on loopback, pinned to one GPU (PRD §3.1).

The shared system daemon on :11434 serves other apps and its environment needs root to change,
so we never touch it. Instead `ensure_own_instance` adopts a daemon already listening on our port
or starts a second `ollama serve` against the same model store, pinned to `UDR_OWN_OLLAMA_GPU`.

Fail-open: when it cannot be started, both endpoints use the shared daemon and a warning event is
emitted. The started daemon deliberately outlives this process and is adopted next time; the
systemd unit of M10 takes over its lifecycle. Prior art: `../local_summarizer/src/ollama_server.py`.
"""

import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from app.adapters.ollama_transport import OllamaAdmin
from app.adapters.system_probe import (
    Gpu,
    Runner,
    find_binary,
    find_models_dir,
    free_disk_bytes,
    list_gpus,
    run_command,
)
from app.config import Settings
from app.events import EventSink
from app.llm.types import Endpoint

POLL_INTERVAL_S = 0.5
STOP_GRACE_S = 5.0


class InstanceState(StrEnum):
    ADOPTED = "adopted"
    STARTED = "started"
    FAIL_OPEN = "fail_open"
    DISABLED = "disabled"


@dataclass(frozen=True)
class InstanceStatus:
    state: InstanceState
    reason: str | None = None


class InstanceProbe(Protocol):
    def serving(self, base_url: str) -> bool: ...
    def gpu_indices(self) -> list[int] | None: ...
    def find_binary(self, name: str) -> str | None: ...
    def find_models_dir(self, preferred: Path | None) -> Path | None: ...


class Process(Protocol):
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float) -> int: ...


class Spawner(Protocol):
    def __call__(self, argv: list[str], env: dict[str, str]) -> Process: ...


def instance_env(
    base: Mapping[str, str], *, port: int, gpu: int, models_dir: Path | None
) -> dict[str, str]:
    """Environment for our daemon: loopback port, one visible GPU, no Vulkan, one request."""
    env = dict(base)
    env.update(
        OLLAMA_HOST=f"127.0.0.1:{port}",
        # CUDA orders devices fastest-first; without PCI_BUS_ID index N may not be nvidia-smi's N.
        CUDA_VISIBLE_DEVICES=str(gpu),
        CUDA_DEVICE_ORDER="PCI_BUS_ID",
        # Without this a card hidden from CUDA reappears as a Vulkan device and models load there.
        OLLAMA_VULKAN="0",
        OLLAMA_NUM_PARALLEL="1",
        OLLAMA_MAX_LOADED_MODELS="2",
    )
    if models_dir is not None:
        env["OLLAMA_MODELS"] = str(models_dir)
    return env


def endpoint_urls(settings: Settings, status: InstanceStatus) -> dict[Endpoint, str]:
    """Where each endpoint lives; our own falls back to the shared daemon unless it is up."""
    own_is_up = status.state in (InstanceState.ADOPTED, InstanceState.STARTED)
    return {
        Endpoint.SHARED: settings.shared_ollama_url,
        Endpoint.OWN: settings.own_ollama_url if own_is_up else settings.shared_ollama_url,
    }


@dataclass(frozen=True)
class _SpawnPlan:
    argv: list[str]
    models_dir: Path | None


def _plan_spawn(settings: Settings, probe: InstanceProbe) -> _SpawnPlan | str:
    """What to start, or the reason it cannot be started."""
    binary = probe.find_binary(settings.ollama_binary)
    if binary is None:
        return f"ollama binary {settings.ollama_binary!r} not found"
    gpus = probe.gpu_indices()
    if gpus is None:
        return "no NVIDIA GPU visible (nvidia-smi unavailable)"
    if settings.own_ollama_gpu not in gpus:
        return f"GPU {settings.own_ollama_gpu} not found; available: {gpus}"
    models_dir = probe.find_models_dir(settings.ollama_models_dir)
    if models_dir is None:
        return "no Ollama model store found (set UDR_OLLAMA_MODELS_DIR)"
    return _SpawnPlan([binary, "serve"], models_dir)


def _fail_open(events: EventSink, reason: str) -> InstanceStatus:
    events.emit("ollama_fail_open", level="warning", reason=reason)
    return InstanceStatus(InstanceState.FAIL_OPEN, reason)


def _stop(proc: Process) -> None:
    proc.terminate()
    try:
        proc.wait(STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()


def _await_serving(
    proc: Process,
    url: str,
    settings: Settings,
    probe: InstanceProbe,
    events: EventSink,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> InstanceStatus:
    timeout_s = settings.own_ollama_startup_timeout_s
    deadline = monotonic() + timeout_s
    while monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            return _fail_open(events, f"ollama exited during startup (code {code})")
        if probe.serving(url):
            events.emit("ollama_own_started", url=url, gpu=settings.own_ollama_gpu)
            return InstanceStatus(InstanceState.STARTED)
        sleep(POLL_INTERVAL_S)
    _stop(proc)
    return _fail_open(events, f"startup timeout after {timeout_s:g}s")


def ensure_own_instance(
    settings: Settings,
    probe: InstanceProbe,
    spawner: Spawner,
    events: EventSink,
    *,
    base_env: Mapping[str, str],
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> InstanceStatus:
    """Adopt a daemon on our port, or start one pinned to the configured GPU, or fail open."""
    if not settings.own_ollama_enabled:
        events.emit("ollama_own_disabled")
        return InstanceStatus(InstanceState.DISABLED, "disabled by UDR_OWN_OLLAMA_ENABLED")
    url = settings.own_ollama_url
    if probe.serving(url):
        events.emit("ollama_own_adopted", url=url)
        return InstanceStatus(InstanceState.ADOPTED)
    plan = _plan_spawn(settings, probe)
    if isinstance(plan, str):
        return _fail_open(events, plan)
    env = instance_env(
        base_env,
        port=settings.own_ollama_port,
        gpu=settings.own_ollama_gpu,
        models_dir=plan.models_dir,
    )
    try:
        proc = spawner(plan.argv, env)
    except OSError as exc:
        return _fail_open(events, f"cannot start ollama: {exc}")
    return _await_serving(proc, url, settings, probe, events, monotonic, sleep)


def subprocess_spawner(argv: list[str], env: dict[str, str]) -> Process:
    """Start ``argv`` detached, so Ctrl-C aimed at our process group cannot kill the daemon."""
    return subprocess.Popen(
        argv,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


class LiveProbe:
    """`InstanceProbe` backed by Ollama's HTTP API, nvidia-smi and the filesystem."""

    def __init__(self, admin: OllamaAdmin, run: Runner = run_command) -> None:
        self._admin = admin
        self._run = run

    def serving(self, base_url: str) -> bool:
        return self._admin.version(base_url) is not None

    def gpus(self) -> list[Gpu] | None:
        return list_gpus(self._run)

    def gpu_indices(self) -> list[int] | None:
        gpus = self.gpus()
        return None if gpus is None else [g.index for g in gpus]

    def free_disk_bytes(self, path: Path) -> int:
        return free_disk_bytes(path)

    def find_binary(self, name: str) -> str | None:
        return find_binary(name)

    def find_models_dir(self, preferred: Path | None) -> Path | None:
        return find_models_dir(preferred)
