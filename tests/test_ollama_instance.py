import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from support import make_settings

from app.adapters.ollama_instance import (
    InstanceState,
    InstanceStatus,
    LiveProbe,
    endpoint_urls,
    ensure_own_instance,
    instance_env,
    subprocess_spawner,
)
from app.adapters.ollama_transport import OllamaAdmin
from app.adapters.system_probe import Gpu
from app.events import MemoryEventSink
from app.llm.types import Endpoint

OWN = "http://127.0.0.1:11436"
SHARED = "http://127.0.0.1:11434"
MODELS = Path("/usr/share/ollama/.ollama/models")


@dataclass
class FakeClock:
    now: float = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeProbe:
    def __init__(
        self,
        clock: FakeClock,
        *,
        up_after: float | None,
        gpus: list[int] | None = None,
        binary: str | None = "/usr/bin/ollama",
        models: Path | None = MODELS,
    ) -> None:
        self.clock, self.up_after = clock, up_after
        self.gpus = [0, 1] if gpus is None else gpus
        self.binary, self.models = binary, models
        self.gpu_lookup_fails = False

    def serving(self, base_url: str) -> bool:
        return self.up_after is not None and self.clock.now >= self.up_after

    def gpu_indices(self) -> list[int] | None:
        return None if self.gpu_lookup_fails else self.gpus

    def find_binary(self, name: str) -> str | None:
        return self.binary

    def find_models_dir(self, preferred: Path | None) -> Path | None:
        return preferred or self.models


@dataclass
class FakeProcess:
    returncode: int | None = None
    terminated: bool = False
    killed: bool = False
    stubborn: bool = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float) -> int:
        if self.stubborn and not self.killed:
            raise subprocess.TimeoutExpired("ollama", timeout)
        return -15


@dataclass
class FakeSpawner:
    process: FakeProcess = field(default_factory=FakeProcess)
    error: OSError | None = None
    calls: list[tuple[list[str], dict[str, str]]] = field(default_factory=list)

    def __call__(self, argv: list[str], env: dict[str, str]) -> FakeProcess:
        self.calls.append((argv, env))
        if self.error:
            raise self.error
        return self.process


class World:
    def __init__(
        self,
        *,
        up_after: float | None,
        gpus: list[int] | None = None,
        binary: str | None = "/usr/bin/ollama",
        models: Path | None = MODELS,
    ) -> None:
        self.clock = FakeClock()
        self.probe = FakeProbe(
            self.clock, up_after=up_after, gpus=gpus, binary=binary, models=models
        )
        self.spawner = FakeSpawner()
        self.events = MemoryEventSink()

    def run(self, **settings: object) -> InstanceStatus:
        return ensure_own_instance(
            make_settings(**settings),
            self.probe,
            self.spawner,
            self.events,
            base_env={"PATH": "/bin", "OLLAMA_HOST": "0.0.0.0:1"},
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
        )


def test_instance_env_sets_exactly_the_pinning_variables() -> None:
    base = {"PATH": "/bin", "OLLAMA_HOST": "0.0.0.0:1", "HOME": "/home/x"}
    env = instance_env(base, port=11436, gpu=1, models_dir=MODELS)
    assert env == {
        "PATH": "/bin",
        "HOME": "/home/x",
        "OLLAMA_HOST": "127.0.0.1:11436",
        "CUDA_VISIBLE_DEVICES": "1",
        "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
        "OLLAMA_VULKAN": "0",
        "OLLAMA_NUM_PARALLEL": "1",
        "OLLAMA_MAX_LOADED_MODELS": "2",
        "OLLAMA_MODELS": str(MODELS),
    }
    assert "OLLAMA_MODELS" not in instance_env({}, port=1, gpu=0, models_dir=None)
    assert base["OLLAMA_HOST"] == "0.0.0.0:1"  # input is not mutated


def test_disabled_never_spawns_and_uses_the_shared_daemon() -> None:
    w = World(up_after=None)
    status = w.run(own_ollama_enabled=False)
    assert status.state is InstanceState.DISABLED
    assert w.spawner.calls == []
    settings = make_settings(own_ollama_enabled=False)
    assert endpoint_urls(settings, status) == {Endpoint.OWN: SHARED, Endpoint.SHARED: SHARED}


def test_a_daemon_already_on_the_port_is_adopted() -> None:
    w = World(up_after=0)
    status = w.run()
    assert status.state is InstanceState.ADOPTED
    assert w.spawner.calls == []
    assert [e.type for e in w.events.events] == ["ollama_own_adopted"]
    assert endpoint_urls(make_settings(), status) == {Endpoint.OWN: OWN, Endpoint.SHARED: SHARED}


def test_a_missing_daemon_is_started_with_the_pinned_environment() -> None:
    w = World(up_after=3.0)
    status = w.run()
    assert status == InstanceStatus(InstanceState.STARTED, None)
    ((argv, env),) = w.spawner.calls
    assert argv == ["/usr/bin/ollama", "serve"]
    assert env["OLLAMA_HOST"] == "127.0.0.1:11436"
    assert env["CUDA_VISIBLE_DEVICES"] == "1"
    assert env["OLLAMA_MODELS"] == str(MODELS)
    assert not w.spawner.process.terminated
    assert [e.type for e in w.events.events] == ["ollama_own_started"]


def test_startup_timeout_fails_open_after_thirty_seconds_with_a_warning() -> None:
    w = World(up_after=None)
    status = w.run()
    assert status.state is InstanceState.FAIL_OPEN
    assert status.reason is not None
    assert "timeout" in status.reason
    assert 30 <= w.clock.now < 31
    assert w.spawner.process.terminated
    (event,) = w.events.of_type("ollama_fail_open")
    assert event.level == "warning"
    assert event.data["reason"] == status.reason
    assert endpoint_urls(make_settings(), status)[Endpoint.OWN] == SHARED


def test_a_stubborn_daemon_is_killed_after_the_timeout() -> None:
    w = World(up_after=None)
    w.spawner.process = FakeProcess(stubborn=True)
    w.run(own_ollama_startup_timeout_s=2)
    assert w.spawner.process.terminated
    assert w.spawner.process.killed


def test_a_daemon_that_exits_early_fails_open_immediately() -> None:
    w = World(up_after=None)
    w.spawner.process = FakeProcess(returncode=1)
    status = w.run()
    assert status.state is InstanceState.FAIL_OPEN
    assert status.reason is not None
    assert "exited" in status.reason
    assert w.clock.now == 0


def test_missing_binary_fails_open_without_spawning() -> None:
    w = World(up_after=None, binary=None)
    status = w.run()
    assert status.state is InstanceState.FAIL_OPEN
    assert status.reason is not None
    assert "not found" in status.reason
    assert w.spawner.calls == []


def test_invalid_gpu_index_fails_open_and_names_the_choices() -> None:
    w = World(up_after=None, gpus=[0])
    status = w.run(own_ollama_gpu=1)
    assert status.state is InstanceState.FAIL_OPEN
    assert status.reason is not None
    assert "GPU 1" in status.reason
    assert "[0]" in status.reason
    assert w.spawner.calls == []


def test_no_visible_nvidia_gpu_fails_open() -> None:
    w = World(up_after=None)
    w.probe.gpu_lookup_fails = True
    status = w.run()
    assert status.state is InstanceState.FAIL_OPEN
    assert status.reason is not None
    assert "nvidia" in status.reason.lower()


def test_missing_model_store_fails_open() -> None:
    w = World(up_after=None, models=None)
    status = w.run()
    assert status.state is InstanceState.FAIL_OPEN
    assert status.reason is not None
    assert "model store" in status.reason


def test_a_spawn_error_fails_open_with_its_reason() -> None:
    w = World(up_after=None)
    w.spawner.error = OSError("permission denied")
    status = w.run()
    assert status.state is InstanceState.FAIL_OPEN
    assert status.reason is not None
    assert "permission denied" in status.reason


def test_the_configured_models_dir_is_passed_through() -> None:
    w = World(up_after=1.0)
    w.run(ollama_models_dir=Path("/custom/store"))
    assert w.spawner.calls[0][1]["OLLAMA_MODELS"] == "/custom/store"


# ---- real adapters --------------------------------------------------------------------------


def test_subprocess_spawner_passes_env_and_detaches(tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    code = "import os, sys, pathlib; pathlib.Path(sys.argv[1]).write_text(os.environ['UDR_PROBE'])"
    proc = subprocess_spawner([sys.executable, "-c", code, str(out)], {"UDR_PROBE": "pinned"})
    assert proc.wait(timeout=10) == 0
    assert out.read_text() == "pinned"


def test_live_probe_uses_its_collaborators(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"version": "0.31.1"})

    admin = OllamaAdmin(lambda t: httpx.Client(transport=httpx.MockTransport(handler), timeout=t))
    probe = LiveProbe(admin, run=lambda argv: "0, GPU, 100, 1\n1, GPU, 100, 1\n")
    assert probe.serving(OWN) is True
    assert probe.gpu_indices() == [0, 1]
    assert probe.find_binary("sh") is not None
    assert probe.find_models_dir(None) is None or isinstance(probe.find_models_dir(None), Path)


def test_live_probe_reports_unreachable_as_not_serving() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    admin = OllamaAdmin(lambda t: httpx.Client(transport=httpx.MockTransport(refuse), timeout=t))
    probe = LiveProbe(admin, run=lambda argv: None)
    assert probe.serving(OWN) is False
    assert probe.gpu_indices() is None


def test_live_probe_answers_the_host_questions_the_doctor_asks(tmp_path: Path) -> None:
    admin = OllamaAdmin(
        lambda t: httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200)), timeout=t
        )
    )
    probe = LiveProbe(admin, run=lambda argv: "0, RTX 4090, 24564, 83\n")
    assert probe.gpus() == [Gpu(0, "RTX 4090", 24564, 83)]
    assert probe.free_disk_bytes(tmp_path) > 0
    assert LiveProbe(admin, run=lambda argv: None).gpus() is None
