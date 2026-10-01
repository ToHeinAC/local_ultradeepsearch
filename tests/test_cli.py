import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from support import make_settings
from typer.testing import CliRunner

from app import bootstrap, cli
from app.adapters.ollama_instance import InstanceState, InstanceStatus
from app.adapters.system_probe import Gpu
from app.calibration import Calibration, CalibrationError, CtxMeasurement
from app.config import Settings
from app.doctor import DoctorSnapshot
from app.events import EventSink, JsonlEventSink
from app.llm.roles import build_registry
from app.llm.types import Endpoint

GIB = 1024**3
OWN, SHARED = "http://127.0.0.1:11436", "http://127.0.0.1:11434"
ALL = frozenset(
    {"qwen3.8-27b:latest", "LiquidAI/lfm2.5-1.2b-instruct:latest", "gemma4:e4b", "deepseek-ocr:3b"}
)
runner = CliRunner()


def settings(tmp_path: Path) -> Settings:
    return make_settings(data_dir=tmp_path)


def snapshot(tmp_path: Path, **overrides: Any) -> DoctorSnapshot:
    fields: dict[str, Any] = {
        "settings": settings(tmp_path),
        "instance": InstanceStatus(InstanceState.STARTED),
        "urls": {Endpoint.OWN: OWN, Endpoint.SHARED: SHARED},
        "tags": {Endpoint.OWN: ALL, Endpoint.SHARED: ALL},
        "free_disk_bytes": 100 * GIB,
        "gpus": [Gpu(0, "RTX 4090", 24564, 83), Gpu(1, "RTX 4090", 24564, 15)],
        "own_loaded_vram_bytes": 0,
        "calibration_ctx": 32768,
    }
    return DoctorSnapshot(**{**fields, **overrides})


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Patch the composition root so the CLI runs against canned data."""
    monkeypatch.setenv("UDR_DATA_DIR", str(tmp_path))
    state = SimpleNamespace(
        snapshot=snapshot(tmp_path),
        status=InstanceStatus(InstanceState.STARTED),
        calibrate_calls=0,
        calibrate_result=None,
        calibrate_error=None,
        events=None,
    )

    def build_runtime(s: Settings, events: EventSink, **_: object) -> SimpleNamespace:
        state.events = events
        return SimpleNamespace(settings=s, status=state.status, registry=build_registry(s))

    def calibrate(_rt: object) -> Calibration | None:
        state.calibrate_calls += 1
        if state.calibrate_error:
            raise state.calibrate_error
        return state.calibrate_result

    monkeypatch.setattr(bootstrap, "build_runtime", build_runtime)
    monkeypatch.setattr(bootstrap, "collect_snapshot", lambda _rt: state.snapshot)
    monkeypatch.setattr(bootstrap, "calibrate", calibrate)
    return state


def a_calibration(ctx: int = 24576) -> Calibration:
    ms = [CtxMeasurement(num_ctx=ctx, size=1, size_vram=1)]
    return Calibration(
        model="qwen3.8-27b:latest", gpu=1, reason_num_ctx=ctx, measured_at="t", measurements=ms
    )


def test_a_healthy_doctor_exits_zero(wired: SimpleNamespace) -> None:
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 0
    assert "ERROR" not in result.stdout
    assert "model:reason" in result.stdout


def test_missing_models_exit_one_and_are_named(wired: SimpleNamespace, tmp_path: Path) -> None:
    wired.snapshot = snapshot(
        tmp_path, tags={Endpoint.OWN: ALL - {"qwen3.8-27b:latest"}, Endpoint.SHARED: ALL}
    )
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 1
    assert "qwen3.8-27b:latest" in result.stdout


def test_json_output_is_valid_on_stdout(wired: SimpleNamespace) -> None:
    result = runner.invoke(cli.app, ["doctor", "--json"])
    rows = json.loads(result.stdout)
    assert result.exit_code == 0
    assert {"name", "level", "detail"} == set(rows[0])
    assert any(r["name"] == "disk" for r in rows)


def test_calibrate_writes_the_file_and_reports_it(wired: SimpleNamespace, tmp_path: Path) -> None:
    wired.calibrate_result = a_calibration(24576)
    result = runner.invoke(cli.app, ["doctor", "--calibrate"])
    assert result.exit_code == 0
    stored = json.loads((tmp_path / "calibration.json").read_text(encoding="utf-8"))
    assert stored["reason_num_ctx"] == 24576
    assert "24576" in result.output


def test_calibrate_json_keeps_stdout_machine_readable(wired: SimpleNamespace) -> None:
    wired.calibrate_result = a_calibration()
    result = runner.invoke(cli.app, ["doctor", "--calibrate", "--json"])
    assert isinstance(json.loads(result.stdout), list)


def test_calibrate_when_nothing_fits_exits_one(wired: SimpleNamespace, tmp_path: Path) -> None:
    wired.calibrate_result = None
    result = runner.invoke(cli.app, ["doctor", "--calibrate"])
    assert result.exit_code == 1
    assert "does not fit" in result.output
    assert not (tmp_path / "calibration.json").exists()


def test_calibrate_needs_a_running_own_instance(wired: SimpleNamespace) -> None:
    wired.status = InstanceStatus(InstanceState.FAIL_OPEN, "startup timeout after 30s")
    result = runner.invoke(cli.app, ["doctor", "--calibrate"])
    assert result.exit_code == 1
    assert "startup timeout" in result.output
    assert wired.calibrate_calls == 0


def test_a_failed_measurement_exits_one_with_the_reason(wired: SimpleNamespace) -> None:
    wired.calibrate_error = CalibrationError("qwen3.8-27b:latest not listed by /api/ps")
    result = runner.invoke(cli.app, ["doctor", "--calibrate"])
    assert result.exit_code == 1
    assert "not listed" in result.output


def test_doctor_events_go_to_the_data_dir(wired: SimpleNamespace, tmp_path: Path) -> None:
    runner.invoke(cli.app, ["doctor"])
    assert isinstance(wired.events, JsonlEventSink)
    wired.events.emit("probe")
    assert json.loads((tmp_path / "events.jsonl").read_text(encoding="utf-8"))["type"] == "probe"


def test_main_runs_the_typer_app(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(cli, "app", lambda: called.append("ran"))
    cli.main()
    assert called == ["ran"]


# ---- udr denylist ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("UDR_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bootstrap, "load_settings", lambda: settings(tmp_path))
    return tmp_path


def test_denylist_add_list_remove(data_dir: Path) -> None:
    assert runner.invoke(cli.app, ["denylist", "list"]).stdout == ""
    added = runner.invoke(cli.app, ["denylist", "add", "Müller-Werke", "Projekt Kranich"])
    assert added.exit_code == 0
    assert "added: Müller-Werke" in added.output
    again = runner.invoke(cli.app, ["denylist", "add", "MUELLER WERKE"])
    assert again.exit_code == 0
    assert "already covered" in again.output
    listed = runner.invoke(cli.app, ["denylist", "list"])
    assert listed.stdout.splitlines() == ["Müller-Werke", "Projekt Kranich"]
    removed = runner.invoke(cli.app, ["denylist", "remove", "projekt kranich", "nope"])
    assert removed.exit_code == 1  # one term was not on the list
    assert "removed: projekt kranich" in removed.output
    assert "not found: nope" in removed.output
    assert (data_dir / "denylist.txt").read_text(encoding="utf-8").splitlines()[1:] == [
        "Müller-Werke"
    ]


def test_denylist_rejects_terms_without_letters(data_dir: Path) -> None:
    result = runner.invoke(cli.app, ["denylist", "add", "--", "---"])
    assert result.exit_code == 2
    assert "letters or digits" in result.output
    assert not (data_dir / "denylist.txt").exists()
