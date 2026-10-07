"""`udr doctor`: judge a snapshot of the machine against what the PRD needs. Pure, no I/O."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.adapters.ollama_instance import InstanceState, InstanceStatus
from app.adapters.ollama_transport import normalize_tag
from app.adapters.system_probe import Gpu
from app.config import Settings
from app.llm.roles import FALLBACK_REASON_NUM_CTX
from app.llm.types import Endpoint, Role, RoleSpec

GIB = 1024**3
MIB = 1024**2
FOREIGN_VRAM_WARN_BYTES = 2 * GIB
MIN_COMFORTABLE_CTX = 16384  # below this, PRD risk R7 applies

_TAGS = {"ok": "OK", "warning": "WARN", "error": "ERROR"}


class Level(StrEnum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class Check:
    name: str
    level: Level
    detail: str


@dataclass(frozen=True)
class DoctorSnapshot:
    settings: Settings
    instance: InstanceStatus
    urls: Mapping[Endpoint, str]  # effective URLs: our own falls back to the shared one
    tags: Mapping[Endpoint, frozenset[str] | None]  # installed models; None = unreachable
    free_disk_bytes: int
    gpus: Sequence[Gpu] | None
    own_loaded_vram_bytes: int  # VRAM used by models loaded in our own instance
    calibration_ctx: int | None
    searxng_ok: bool | None = None  # None: not configured; else whether it answered a search


def _instance_is_up(snapshot: DoctorSnapshot) -> bool:
    return snapshot.instance.state in (InstanceState.ADOPTED, InstanceState.STARTED)


def _check_shared(s: DoctorSnapshot) -> Check:
    url = s.urls[Endpoint.SHARED]
    tags = s.tags[Endpoint.SHARED]
    if tags is None:
        return Check("shared_endpoint", Level.ERROR, f"no answer from {url}")
    return Check("shared_endpoint", Level.OK, f"reachable at {url} ({len(tags)} models)")


def _check_own(s: DoctorSnapshot) -> Check:
    state = s.instance.state
    if state is InstanceState.DISABLED:
        return Check(
            "own_instance", Level.OK, "disabled by configuration; roles use the shared daemon"
        )
    if state is InstanceState.FAIL_OPEN:
        reason = s.instance.reason
        return Check(
            "own_instance", Level.ERROR, f"not running: {reason}; falling back to the shared daemon"
        )
    return Check("own_instance", Level.OK, f"{state.value} at {s.urls[Endpoint.OWN]}")


def _check_model(s: DoctorSnapshot, spec: RoleSpec) -> Check:
    name = f"model:{spec.role.value}"
    url = s.urls[spec.endpoint]
    tags = s.tags[spec.endpoint]
    if tags is None:
        return Check(name, Level.ERROR, f"{spec.model}: cannot verify, {url} is unreachable")
    if normalize_tag(spec.model) not in tags:
        return Check(name, Level.ERROR, f"{spec.model} is not installed on {url}")
    return Check(name, Level.OK, f"{spec.model} on {url}")


def _check_disk(s: DoctorSnapshot) -> Check:
    need = s.settings.min_free_disk_gb
    free = s.free_disk_bytes / GIB
    if free < need:
        return Check("disk", Level.ERROR, f"{free:.0f} GiB free, need at least {need:g} GiB")
    return Check("disk", Level.OK, f"{free:.0f} GiB free")


def _check_gpu(s: DoctorSnapshot) -> Check:
    wanted = s.settings.own_ollama_gpu
    if not s.settings.own_ollama_enabled:
        return Check("gpu", Level.OK, "no GPU pinning (own instance disabled)")
    if s.gpus is None:
        return Check("gpu", Level.ERROR, f"nvidia-smi unavailable; cannot pin GPU {wanted}")
    found = {g.index: g for g in s.gpus}
    if wanted not in found:
        return Check("gpu", Level.ERROR, f"GPU {wanted} does not exist; available: {sorted(found)}")
    gpu = found[wanted]
    return Check("gpu", Level.OK, f"GPU {wanted}: {gpu.name}, {gpu.total_mib / 1024:.0f} GiB")


def _check_calibration(s: DoctorSnapshot) -> Check:
    ctx = s.calibration_ctx
    if ctx is None:
        detail = f"not calibrated, using {FALLBACK_REASON_NUM_CTX}; run `udr doctor --calibrate`"
        return Check("calibration", Level.WARNING, detail)
    if ctx < MIN_COMFORTABLE_CTX:
        detail = f"reason context is only {ctx} (< {MIN_COMFORTABLE_CTX}); see PRD risk R7"
        return Check("calibration", Level.WARNING, detail)
    return Check("calibration", Level.OK, f"reason num_ctx {ctx}")


def _check_sharing(s: DoctorSnapshot) -> Check:
    gpu = next((g for g in s.gpus or [] if g.index == s.settings.own_ollama_gpu), None)
    if gpu is None or not _instance_is_up(s):
        return Check("gpu_sharing", Level.OK, "not applicable")
    foreign = gpu.used_mib * MIB - s.own_loaded_vram_bytes
    if foreign > FOREIGN_VRAM_WARN_BYTES:
        detail = (
            f"about {foreign / GIB:.0f} GiB on GPU {gpu.index} are used by something other than "
            "our instance (another Ollama or app); see PRD risk R6"
        )
        return Check("gpu_sharing", Level.WARNING, detail)
    return Check("gpu_sharing", Level.OK, f"GPU {gpu.index} is free of foreign load")


def _check_searxng(s: DoctorSnapshot) -> Check:
    if s.searxng_ok is None:
        return Check("searxng", Level.OK, "not configured; web search starts with Tavily")
    url = s.settings.searxng_url
    if s.searxng_ok:
        return Check("searxng", Level.OK, f"answers at {url}")
    return Check("searxng", Level.WARNING, f"no JSON answer from {url}; searches go on to Tavily")


def evaluate(snapshot: DoctorSnapshot, registry: Mapping[Role, RoleSpec]) -> list[Check]:
    """All checks, in display order."""
    return [
        _check_shared(snapshot),
        _check_own(snapshot),
        *(_check_model(snapshot, spec) for spec in registry.values()),
        _check_disk(snapshot),
        _check_gpu(snapshot),
        _check_calibration(snapshot),
        _check_sharing(snapshot),
        _check_searxng(snapshot),
    ]


def exit_code(checks: Sequence[Check]) -> int:
    """1 if any check is an error, else 0 (warnings never fail the doctor)."""
    return 1 if any(c.level is Level.ERROR for c in checks) else 0


def render(checks: Sequence[Check]) -> str:
    return "\n".join(f"{_TAGS[c.level.value]:<6}{c.name:<20}{c.detail}" for c in checks)


def render_json(checks: Sequence[Check]) -> str:
    rows = [{"name": c.name, "level": c.level.value, "detail": c.detail} for c in checks]
    return json.dumps(rows, ensure_ascii=False, indent=2)
