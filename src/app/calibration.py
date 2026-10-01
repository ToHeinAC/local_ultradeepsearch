"""Find the largest `reason` context window that still loads 100 % into VRAM (PRD §3.1).

Ollama silently spills layers to the CPU when the KV cache for `num_ctx` does not fit, which makes
a model several times slower. So for each candidate we load the model with that context, ask
`/api/ps` how much of it sits in VRAM, and keep the largest candidate where all of it does.
Nothing here opens a connection itself: the transport and the `/api/ps` probe are injected.
"""

import contextlib
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from app.adapters.ollama_transport import LoadedModel, normalize_tag
from app.artifacts import write_json
from app.config import Settings
from app.events import EventSink
from app.llm.errors import LLMError
from app.llm.types import ChatRequest, RoleSpec, Transport
from app.prompts.llm import CALIBRATION_PROBE_PROMPT

CANDIDATES = (32768, 24576, 16384, 12288)


class CalibrationError(Exception):
    """The measurement itself could not be made (as opposed to "nothing fits")."""


class PsProbe(Protocol):
    def ps(self, base_url: str) -> list[LoadedModel] | None: ...


class CtxMeasurement(BaseModel):
    model_config = ConfigDict(frozen=True)

    num_ctx: int
    size: int
    size_vram: int

    @property
    def fully_in_vram(self) -> bool:
        return self.size > 0 and self.size_vram >= self.size


class Calibration(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str
    gpu: int
    reason_num_ctx: int
    measured_at: str
    measurements: list[CtxMeasurement]


def pick_num_ctx(measurements: Sequence[CtxMeasurement]) -> int | None:
    """The largest measured context whose model sits entirely in VRAM, if any."""
    return max((m.num_ctx for m in measurements if m.fully_in_vram), default=None)


def load_calibration(path: Path) -> Calibration | None:
    """The stored calibration, or None if the file is missing or unreadable."""
    try:
        return Calibration.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError):
        return None


def save_calibration(path: Path, calibration: Calibration) -> None:
    write_json(path, calibration)


def calibration_for(calibration: Calibration | None, settings: Settings) -> int | None:
    """The calibrated context, but only if it was measured for this model on this GPU."""
    if calibration is None:
        return None
    same_model = calibration.model == settings.model_reason
    return (
        calibration.reason_num_ctx
        if same_model and calibration.gpu == settings.own_ollama_gpu
        else None
    )


def _probe_request(spec: RoleSpec, num_ctx: int) -> ChatRequest:
    return ChatRequest(
        model=spec.model,
        messages=({"role": "user", "content": CALIBRATION_PROBE_PROMPT},),
        schema=None,
        think=False,
        num_ctx=num_ctx,
        num_predict=1,
        temperature=0.0,
        keep_alive=spec.keep_alive,
    )


def _unload(spec: RoleSpec, url: str, transport: Transport, timeout_s: float) -> None:
    """Free the VRAM again; an empty chat with keep_alive 0 unloads. Best effort."""
    request = ChatRequest(
        model=spec.model,
        messages=(),
        schema=None,
        think=False,
        num_ctx=spec.num_ctx,
        num_predict=1,
        temperature=0.0,
        keep_alive="0s",
    )
    with contextlib.suppress(LLMError):
        transport.chat(url, request, timeout_s)


def _measure_loaded(model: str, num_ctx: int, probe: PsProbe, url: str) -> CtxMeasurement:
    loaded = probe.ps(url)
    if loaded is None:
        raise CalibrationError(f"cannot read /api/ps on {url}")
    wanted = normalize_tag(model)
    for entry in loaded:
        if normalize_tag(entry.name) == wanted:
            return CtxMeasurement(num_ctx=num_ctx, size=entry.size, size_vram=entry.size_vram)
    raise CalibrationError(f"{model} not listed by /api/ps after loading it")


def measure_reason_ctx(
    spec: RoleSpec,
    url: str,
    transport: Transport,
    probe: PsProbe,
    events: EventSink,
    *,
    timeout_s: float,
    candidates: Sequence[int] = CANDIDATES,
) -> list[CtxMeasurement]:
    """Load ``spec.model`` at each candidate context, largest first, until one fits in VRAM."""
    measurements: list[CtxMeasurement] = []
    try:
        for num_ctx in candidates:
            transport.chat(url, _probe_request(spec, num_ctx), timeout_s)
            measured = _measure_loaded(spec.model, num_ctx, probe, url)
            events.emit(
                "calibration_step",
                num_ctx=num_ctx,
                size=measured.size,
                size_vram=measured.size_vram,
                fits=measured.fully_in_vram,
            )
            measurements.append(measured)
            if measured.fully_in_vram:
                break
    finally:
        _unload(spec, url, transport, timeout_s)
    return measurements


def run_calibration(
    spec: RoleSpec,
    url: str,
    gpu: int,
    transport: Transport,
    probe: PsProbe,
    events: EventSink,
    *,
    timeout_s: float,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Calibration | None:
    """Measure and package the result; None when even the smallest candidate spills to CPU."""
    measurements = measure_reason_ctx(spec, url, transport, probe, events, timeout_s=timeout_s)
    chosen = pick_num_ctx(measurements)
    if chosen is None:
        return None
    return Calibration(
        model=spec.model,
        gpu=gpu,
        reason_num_ctx=chosen,
        measured_at=now().isoformat(),
        measurements=measurements,
    )
