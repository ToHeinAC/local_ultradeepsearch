import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fixtures_corpus import FOCUS, FakeFetcher
from support import make_settings

from app import bootstrap
from app.adapters.ollama_instance import InstanceState
from app.adapters.ollama_transport import OllamaAdmin
from app.adapters.outbound.denylist import Denylist
from app.adapters.outbound.errors import DenylistBlocked
from app.adapters.outbound.gateway import OutboundGateway, PreparedQuery
from app.adapters.outbound.log import OutboundLog, OutboundRecord
from app.adapters.outbound.tavily import TavilyApi
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


# ---- M2: gateway composition ----------------------------------------------------------------


def test_providers_follow_the_settings(tmp_path: Path) -> None:
    without = bootstrap.build_providers(settings(tmp_path))
    assert without.tavily is None
    with_key = bootstrap.build_providers(settings(tmp_path, tavily_api_key="tvly-x"))
    assert isinstance(with_key.tavily, TavilyApi)


def test_the_gateway_is_wired_to_the_run_and_the_data_dir(tmp_path: Path) -> None:
    data = tmp_path / "data"
    Denylist(["Projekt Kranich"]).save(data / "denylist.txt")
    seen: list[httpx.Request] = []

    def net(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"results": [{"title": "T", "url": "https://h.org", "content": ""}]}
        )

    s = settings(data, tavily_api_key="tvly-x", openalex_mailto="me@example.org")
    rt, _ = runtime(
        data, script=[reply('{"sanitized_query": "reactor costs", "removed_terms": ["X"]}')]
    )
    rt = replace(rt, settings=s)

    def http(timeout: float) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(net), timeout=timeout)

    gateway = bootstrap.build_gateway(
        rt,
        tmp_path / "runs" / "r1",
        credit_cap=60,
        confidential_context="Client X",
        providers=bootstrap.build_providers(s, http=http),
    )
    prepared = gateway.prepare_query("X reactor costs", step="2.1")
    assert prepared.sent == "reactor costs"
    gateway.search_web(prepared, step="2")
    line = json.loads((tmp_path / "runs" / "r1" / "outbound.jsonl").read_text(encoding="utf-8"))
    assert (line["provider"], line["credits"]) == ("tavily_search", 1)
    assert json.loads((data / "tavily-ledger.json").read_text(encoding="utf-8"))["credits"] == 1
    with pytest.raises(DenylistBlocked):
        gateway.search_web(PreparedQuery("x", "projekt kranich", ()), step="2")
    assert len(seen) == 1


def test_a_resumed_run_keeps_the_credits_it_already_spent(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "r1"
    log = OutboundLog(run_dir / "outbound.jsonl")
    log.write(OutboundRecord(step="2", provider="tavily_search", status="200", credits=5))
    log.write(OutboundRecord(step="2", provider="tavily_extract", status="200", credits=3))
    rt, _ = runtime(tmp_path)
    resumed = bootstrap.build_gateway(rt, run_dir, credit_cap=60, confidential_context="x")
    assert resumed.credits_used == 8
    fresh = bootstrap.build_gateway(
        rt, tmp_path / "runs" / "r2", credit_cap=60, confidential_context="x"
    )
    assert fresh.credits_used == 0


# ---- run directory, vault and pipeline composition (M3) --------------------------------------


def test_a_run_lives_in_its_own_directory_under_data_runs(tmp_path: Path) -> None:
    s = settings(tmp_path)
    assert bootstrap.run_dir(s, "2026-10-01-abc") == s.data_dir / "runs" / "2026-10-01-abc"


@pytest.mark.parametrize("bad", ["", "..", "../x", "a/b", "a\\b", ".hidden", "x" * 65, "a b"])
def test_a_run_id_cannot_escape_the_runs_directory(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError, match="run id"):
        bootstrap.run_dir(settings(tmp_path), bad)


def test_the_vault_database_is_shared_and_runs_are_isolated(tmp_path: Path) -> None:
    s = settings(tmp_path)
    first = bootstrap.open_vault(s, "run-a", label="first")
    second = bootstrap.open_vault(s, "run-b")
    assert (s.data_dir / bootstrap.VAULT_FILE).exists()
    first.reject("https://x.org/", "https://x.org", "too_short")
    assert first.rejections() != []
    assert second.rejections() == []
    # reopening the same run in a "new process" sees what was stored
    assert bootstrap.open_vault(s, "run-a").rejections() != []


def test_build_pipeline_wires_the_profile_the_vault_and_a_fetcher(tmp_path: Path) -> None:
    rt, _ = runtime(tmp_path)
    fetcher = FakeFetcher({})
    pipeline = bootstrap.build_pipeline(rt, "run-a", tier="light", focus=FOCUS, fetcher=fetcher)
    assert pipeline.resume() == 0
    assert (rt.settings.data_dir / bootstrap.VAULT_FILE).exists()


def test_build_pipeline_uses_the_gateway_with_the_profiles_credit_cap_by_default(
    tmp_path: Path,
) -> None:
    rt, _ = runtime(tmp_path)
    pipeline = bootstrap.build_pipeline(rt, "run-a", tier="full", focus=FOCUS)
    assert isinstance(pipeline.fetcher, OutboundGateway)
    assert pipeline.fetcher.credit_cap == 300  # config/profiles.toml [full]


def test_an_unknown_tier_is_an_error(tmp_path: Path) -> None:
    rt, _ = runtime(tmp_path)
    with pytest.raises(ValueError, match="tier"):
        bootstrap.build_pipeline(rt, "run-a", tier="turbo", focus=FOCUS)
