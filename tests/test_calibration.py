import json
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.adapters.ollama_transport import LoadedModel
from app.calibration import (
    CANDIDATES,
    Calibration,
    CalibrationError,
    CtxMeasurement,
    calibration_for,
    load_calibration,
    measure_reason_ctx,
    pick_num_ctx,
    run_calibration,
    save_calibration,
)
from app.config import Settings
from app.events import MemoryEventSink
from app.llm.errors import LLMUnavailableError
from app.llm.fakes import ScriptedTransport, reply
from app.llm.roles import build_registry
from app.llm.types import ChatReply, Role

OWN = "http://127.0.0.1:11436"
MODEL = "qwen3.8-27b:latest"
GIB = 1024**3
NOW = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # pyright: ignore[reportCallIssue]


def loaded(size_vram_gib: float, size_gib: float = 20) -> list[LoadedModel]:
    return [LoadedModel(MODEL, int(size_gib * GIB), int(size_vram_gib * GIB), None)]


class FakeAdmin:
    def __init__(self, *states: list[LoadedModel] | None) -> None:
        self._states = deque(states)
        self.calls = 0

    def ps(self, base_url: str) -> list[LoadedModel] | None:
        self.calls += 1
        return self._states.popleft()


def spec():
    return build_registry(settings())[Role.REASON]


# ---- pick_num_ctx ---------------------------------------------------------------------------


def test_candidates_are_largest_first() -> None:
    assert CANDIDATES == (32768, 24576, 16384, 12288)


def test_pick_returns_the_largest_context_that_fits_in_vram() -> None:
    ms = [
        CtxMeasurement(num_ctx=32768, size=22, size_vram=18),
        CtxMeasurement(num_ctx=24576, size=21, size_vram=21),
        CtxMeasurement(num_ctx=16384, size=20, size_vram=20),
    ]
    assert pick_num_ctx(ms) == 24576
    assert pick_num_ctx(list(reversed(ms))) == 24576  # order does not matter


def test_pick_returns_none_when_nothing_fits() -> None:
    assert pick_num_ctx([CtxMeasurement(num_ctx=12288, size=20, size_vram=19)]) is None
    assert pick_num_ctx([]) is None


def test_a_zero_sized_model_never_counts_as_fitting() -> None:
    assert pick_num_ctx([CtxMeasurement(num_ctx=12288, size=0, size_vram=0)]) is None


# ---- file round trip ------------------------------------------------------------------------


def a_calibration(**overrides: object) -> Calibration:
    base: dict[str, object] = {
        "model": MODEL,
        "gpu": 1,
        "reason_num_ctx": 24576,
        "measured_at": NOW.isoformat(),
        "measurements": [CtxMeasurement(num_ctx=24576, size=5, size_vram=5)],
    }
    return Calibration(**{**base, **overrides})  # type: ignore[arg-type]


def test_calibration_roundtrips_through_the_file(tmp_path: Path) -> None:
    path = tmp_path / "data" / "calibration.json"
    cal = a_calibration()
    save_calibration(path, cal)
    assert load_calibration(path) == cal
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["reason_num_ctx"] == 24576
    assert on_disk["model"] == MODEL
    assert on_disk["gpu"] == 1


def test_a_missing_or_corrupt_file_loads_as_none(tmp_path: Path) -> None:
    assert load_calibration(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_calibration(bad) is None
    bad.write_text('{"model": "x"}', encoding="utf-8")
    assert load_calibration(bad) is None


def test_a_calibration_only_applies_to_the_model_and_gpu_it_was_measured_on() -> None:
    cal = a_calibration()
    assert calibration_for(cal, settings()) == 24576
    assert calibration_for(cal, settings(model_reason="other:7b")) is None
    assert calibration_for(cal, settings(own_ollama_gpu=0)) is None
    assert calibration_for(None, settings()) is None


# ---- measurement ----------------------------------------------------------------------------


def measure(admin: FakeAdmin, transport: ScriptedTransport, events: MemoryEventSink):
    return measure_reason_ctx(spec(), OWN, transport, admin, events, timeout_s=60)


def test_measurement_stops_at_the_first_context_that_fits() -> None:
    admin = FakeAdmin(loaded(18), loaded(20))  # 32768 spills to CPU, 24576 fits
    transport = ScriptedTransport([reply("."), reply("."), reply("")])  # last one is the unload
    events = MemoryEventSink()
    ms = measure(admin, transport, events)
    assert [m.num_ctx for m in ms] == [32768, 24576]
    assert pick_num_ctx(ms) == 24576
    requests = [c[1] for c in transport.calls]
    assert [r.num_ctx for r in requests[:2]] == [32768, 24576]
    assert all(r.num_predict == 1 and r.think is False for r in requests[:2])
    assert {c[0] for c in transport.calls} == {OWN}
    assert [e.data["num_ctx"] for e in events.of_type("calibration_step")] == [32768, 24576]


def test_the_model_is_unloaded_afterwards() -> None:
    transport = ScriptedTransport([reply("."), reply("")])
    measure(FakeAdmin(loaded(20)), transport, MemoryEventSink())
    unload = transport.calls[-1][1]
    assert unload.keep_alive == "0s"
    assert unload.messages == ()
    assert len(transport.calls) == 2


def test_nothing_fits_returns_all_measurements_and_still_unloads() -> None:
    admin = FakeAdmin(*[loaded(10) for _ in CANDIDATES])
    transport = ScriptedTransport([reply(".")] * len(CANDIDATES) + [reply("")])
    ms = measure(admin, transport, MemoryEventSink())
    assert [m.num_ctx for m in ms] == list(CANDIDATES)
    assert pick_num_ctx(ms) is None
    assert transport.calls[-1][1].keep_alive == "0s"


def test_a_model_missing_from_ps_is_an_error_but_still_unloads() -> None:
    transport = ScriptedTransport([reply("."), reply("")])
    with pytest.raises(CalibrationError, match="not listed"):
        measure(FakeAdmin([]), transport, MemoryEventSink())
    assert transport.calls[-1][1].keep_alive == "0s"


def test_an_unreachable_ps_is_an_error() -> None:
    transport = ScriptedTransport([reply("."), reply("")])
    with pytest.raises(CalibrationError, match="/api/ps"):
        measure(FakeAdmin(None), transport, MemoryEventSink())


def test_a_failing_unload_does_not_mask_the_result() -> None:
    script: list[ChatReply | Exception] = [reply("."), LLMUnavailableError("gone")]
    ms = measure(FakeAdmin(loaded(20)), ScriptedTransport(script), MemoryEventSink())
    assert pick_num_ctx(ms) == 32768


def test_the_model_name_in_ps_is_matched_after_tag_normalisation() -> None:
    bare = [LoadedModel("qwen3.8-27b", 20 * GIB, 20 * GIB, None)]  # no ":latest"
    ms = measure(FakeAdmin(bare), ScriptedTransport([reply("."), reply("")]), MemoryEventSink())
    assert pick_num_ctx(ms) == 32768


# ---- run_calibration ------------------------------------------------------------------------


def run(admin: FakeAdmin, transport: ScriptedTransport) -> Calibration | None:
    return run_calibration(
        spec(), OWN, 1, transport, admin, MemoryEventSink(), timeout_s=60, now=lambda: NOW
    )


def test_run_calibration_builds_the_result() -> None:
    cal = run(
        FakeAdmin(loaded(18), loaded(20)), ScriptedTransport([reply("."), reply("."), reply("")])
    )
    assert cal is not None
    assert (cal.model, cal.gpu, cal.reason_num_ctx) == (MODEL, 1, 24576)
    assert cal.measured_at == NOW.isoformat()
    assert [m.num_ctx for m in cal.measurements] == [32768, 24576]


def test_run_calibration_returns_none_when_nothing_fits() -> None:
    admin = FakeAdmin(*[loaded(10) for _ in CANDIDATES])
    assert run(admin, ScriptedTransport([reply(".")] * len(CANDIDATES) + [reply("")])) is None
