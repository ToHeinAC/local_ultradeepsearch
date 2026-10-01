from pathlib import Path

import httpx
from support import make_settings

from app import bootstrap
from app.adapters.ollama_instance import InstanceState
from app.adapters.ollama_transport import OllamaAdmin
from app.adapters.system_probe import Gpu
from app.calibration import Calibration, CtxMeasurement, save_calibration
from app.config import Settings
from app.events import MemoryEventSink
from app.llm.fakes import ScriptedTransport, reply
from app.llm.types import ChatReply, Endpoint, Role

GIB = 1024**3
OWN_PORT, SHARED_PORT = 11436, 11434
ALL_TAGS = ["qwen3.8-27b:latest", "gemma4:e4b"]


def settings(tmp_path: Path, **overrides: object) -> Settings:
    return make_settings(data_dir=tmp_path, **overrides)


class FakeHost:
    """InstanceProbe + the two host questions the doctor asks."""

    def __init__(self, *, serving: bool = True, binary: str | None = "/usr/bin/ollama") -> None:
        self._serving = serving
        self._binary = binary

    def serving(self, base_url: str) -> bool:
        return self._serving

    def gpu_indices(self) -> list[int] | None:
        return [0, 1]

    def find_binary(self, name: str) -> str | None:
        return self._binary

    def find_models_dir(self, preferred: Path | None) -> Path | None:
        return preferred or Path("/models")

    def gpus(self) -> list[Gpu] | None:
        return [Gpu(0, "RTX 4090", 24564, 83), Gpu(1, "RTX 4090", 24564, 90)]

    def free_disk_bytes(self, path: Path) -> int:
        return 77 * GIB


def admin(*, own_loaded: tuple[int, int] = (0, 0), own_up: bool = True) -> OllamaAdmin:
    def handler(request: httpx.Request) -> httpx.Response:
        on_own = request.url.port == OWN_PORT
        if on_own and not own_up:
            raise httpx.ConnectError("refused")
        if request.url.path == "/api/tags":
            names = ALL_TAGS if on_own else ["gemma4:e4b"]
            return httpx.Response(200, json={"models": [{"name": n} for n in names]})
        if request.url.path == "/api/ps":
            size, vram = own_loaded
            models = (
                [{"name": "qwen3.8-27b:latest", "size": size, "size_vram": vram}] if size else []
            )
            return httpx.Response(200, json={"models": models})
        return httpx.Response(200, json={"version": "0.31.1"})

    return OllamaAdmin(lambda t: httpx.Client(transport=httpx.MockTransport(handler), timeout=t))


def runtime(
    tmp_path: Path,
    *,
    probe: FakeHost | None = None,
    http: OllamaAdmin | None = None,
    script: list[ChatReply | Exception] | None = None,
) -> tuple[bootstrap.Runtime, ScriptedTransport]:
    transport = ScriptedTransport(script or [reply("hello")])
    rt = bootstrap.build_runtime(
        settings(tmp_path),
        MemoryEventSink(),
        probe=probe or FakeHost(),
        admin=http or admin(),
        transport=transport,
        env={},
    )
    return rt, transport


def calibration(model: str = "qwen3.8-27b:latest", gpu: int = 1, ctx: int = 24576) -> Calibration:
    ms = [CtxMeasurement(num_ctx=ctx, size=1, size_vram=1)]
    return Calibration(model=model, gpu=gpu, reason_num_ctx=ctx, measured_at="t", measurements=ms)


def test_a_running_own_instance_is_adopted_and_used_for_its_roles(tmp_path: Path) -> None:
    rt, _ = runtime(tmp_path)
    assert rt.status.state is InstanceState.ADOPTED
    assert rt.urls == {
        Endpoint.OWN: "http://127.0.0.1:11436",
        Endpoint.SHARED: "http://127.0.0.1:11434",
    }
    assert rt.registry[Role.REASON].num_ctx == 16384  # uncalibrated fallback


def test_a_stored_calibration_sets_the_reason_context(tmp_path: Path) -> None:
    save_calibration(tmp_path / "calibration.json", calibration(ctx=24576))
    assert runtime(tmp_path)[0].registry[Role.REASON].num_ctx == 24576


def test_a_calibration_for_another_model_or_gpu_is_ignored(tmp_path: Path) -> None:
    save_calibration(tmp_path / "calibration.json", calibration(model="other:7b"))
    assert runtime(tmp_path)[0].registry[Role.REASON].num_ctx == 16384
    save_calibration(tmp_path / "calibration.json", calibration(gpu=0))
    assert runtime(tmp_path)[0].registry[Role.REASON].num_ctx == 16384


def test_the_service_is_wired_to_the_transport_and_urls(tmp_path: Path) -> None:
    rt, transport = runtime(tmp_path)
    assert rt.service.text(Role.SUMMARIZE, [{"role": "user", "content": "hi"}]) == "hello"
    assert transport.calls[0][0] == "http://127.0.0.1:11434"  # summarize runs on the shared daemon


def test_without_a_daemon_or_binary_both_endpoints_fall_back_to_shared(tmp_path: Path) -> None:
    rt, _ = runtime(tmp_path, probe=FakeHost(serving=False, binary=None))
    assert rt.status.state is InstanceState.FAIL_OPEN
    assert rt.urls[Endpoint.OWN] == rt.urls[Endpoint.SHARED]


def test_the_snapshot_collects_what_the_doctor_needs(tmp_path: Path) -> None:
    gib = 20 * GIB
    save_calibration(tmp_path / "calibration.json", calibration(ctx=24576))
    rt, _ = runtime(tmp_path, http=admin(own_loaded=(gib, gib)))
    snap = bootstrap.collect_snapshot(rt)
    assert snap.tags[Endpoint.OWN] == frozenset({"qwen3.8-27b:latest", "gemma4:e4b"})
    assert snap.tags[Endpoint.SHARED] == frozenset({"gemma4:e4b"})
    assert snap.own_loaded_vram_bytes == gib
    assert snap.free_disk_bytes == 77 * GIB
    assert [g.index for g in snap.gpus or []] == [0, 1]
    assert snap.calibration_ctx == 24576
    assert snap.instance.state is InstanceState.ADOPTED


def test_an_unreachable_own_instance_reports_none_not_empty(tmp_path: Path) -> None:
    rt, _ = runtime(tmp_path, probe=FakeHost(serving=False, binary=None), http=admin(own_up=False))
    snap = bootstrap.collect_snapshot(rt)
    assert snap.own_loaded_vram_bytes == 0
    assert snap.tags[Endpoint.OWN] == snap.tags[Endpoint.SHARED]  # both point at the shared daemon


def test_calibrate_measures_the_reason_model_on_the_own_endpoint(tmp_path: Path) -> None:
    gib = 20 * GIB
    rt, transport = runtime(
        tmp_path, http=admin(own_loaded=(gib, gib)), script=[reply("."), reply("")]
    )
    result = bootstrap.calibrate(rt)
    assert result is not None
    assert (result.model, result.gpu, result.reason_num_ctx) == ("qwen3.8-27b:latest", 1, 32768)
    assert {call[0] for call in transport.calls} == {"http://127.0.0.1:11436"}
