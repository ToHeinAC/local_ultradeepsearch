import json
from dataclasses import replace

import pytest
from support import make_settings

from app.adapters.ollama_instance import InstanceState, InstanceStatus
from app.adapters.system_probe import Gpu
from app.config import Settings
from app.doctor import (
    Check,
    DoctorSnapshot,
    Level,
    evaluate,
    exit_code,
    render,
    render_json,
)
from app.llm.roles import build_registry
from app.llm.types import Endpoint

GIB = 1024**3
OWN = "http://127.0.0.1:11436"
SHARED = "http://127.0.0.1:11434"
ALL_MODELS = frozenset(
    {
        "qwen3.8-27b:latest",
        "LiquidAI/lfm2.5-1.2b-instruct:latest",
        "gemma4:e4b",
        "deepseek-ocr:3b",
        "other:1b",
    }
)


def settings(**overrides: object) -> Settings:
    return make_settings(**overrides)


def green(**overrides: object) -> DoctorSnapshot:
    base = DoctorSnapshot(
        settings=settings(),
        instance=InstanceStatus(InstanceState.STARTED),
        urls={Endpoint.OWN: OWN, Endpoint.SHARED: SHARED},
        tags={Endpoint.OWN: ALL_MODELS, Endpoint.SHARED: ALL_MODELS},
        free_disk_bytes=100 * GIB,
        gpus=[Gpu(0, "RTX 4090", 24564, 83), Gpu(1, "RTX 4090", 24564, 15)],
        own_loaded_vram_bytes=0,
        calibration_ctx=32768,
    )
    return replace(base, **overrides)


def run(snapshot: DoctorSnapshot) -> dict[str, Check]:
    registry = build_registry(snapshot.settings)
    return {c.name: c for c in evaluate(snapshot, registry)}


def test_a_healthy_machine_has_no_errors_or_warnings() -> None:
    checks = run(green())
    assert {c.level for c in checks.values()} == {Level.OK}
    assert exit_code(list(checks.values())) == 0


def test_every_role_gets_its_own_model_check() -> None:
    assert {"model:reason", "model:extract", "model:summarize", "model:ocr"} <= set(run(green()))


def test_every_missing_model_is_named_with_its_endpoint() -> None:
    snap = green(
        tags={
            Endpoint.OWN: ALL_MODELS - {"qwen3.8-27b:latest"},
            Endpoint.SHARED: ALL_MODELS - {"gemma4:e4b", "deepseek-ocr:3b"},
        }
    )
    checks = run(snap)
    assert checks["model:reason"].level is Level.ERROR
    assert "qwen3.8-27b:latest" in checks["model:reason"].detail
    assert OWN in checks["model:reason"].detail
    assert checks["model:summarize"].level is Level.ERROR
    assert "gemma4:e4b" in checks["model:summarize"].detail
    assert SHARED in checks["model:summarize"].detail
    assert checks["model:ocr"].level is Level.ERROR
    assert checks["model:extract"].level is Level.ERROR  # extract shares the summarize model
    assert SHARED in checks["model:extract"].detail
    assert exit_code(list(checks.values())) == 1


def test_model_tags_are_compared_after_normalisation() -> None:
    snap = green(settings=settings(model_reason="qwen3.8-27b"))
    assert run(snap)["model:reason"].level is Level.OK


def test_an_unreachable_shared_daemon_is_an_error_and_blocks_its_roles() -> None:
    snap = green(tags={Endpoint.OWN: ALL_MODELS, Endpoint.SHARED: None})
    checks = run(snap)
    assert checks["shared_endpoint"].level is Level.ERROR
    assert SHARED in checks["shared_endpoint"].detail
    assert checks["model:summarize"].level is Level.ERROR
    assert "unreachable" in checks["model:summarize"].detail


def test_a_failed_own_instance_is_an_error_with_its_reason() -> None:
    snap = green(
        instance=InstanceStatus(InstanceState.FAIL_OPEN, "startup timeout after 30s"),
        urls={Endpoint.OWN: SHARED, Endpoint.SHARED: SHARED},
    )
    checks = run(snap)
    assert checks["own_instance"].level is Level.ERROR
    assert "startup timeout" in checks["own_instance"].detail


@pytest.mark.parametrize("state", [InstanceState.ADOPTED, InstanceState.STARTED])
def test_a_running_own_instance_is_ok(state: InstanceState) -> None:
    assert run(green(instance=InstanceStatus(state)))["own_instance"].level is Level.OK


def test_a_deliberately_disabled_own_instance_is_not_an_error() -> None:
    snap = green(
        settings=settings(own_ollama_enabled=False),
        instance=InstanceStatus(InstanceState.DISABLED, "disabled"),
        urls={Endpoint.OWN: SHARED, Endpoint.SHARED: SHARED},
    )
    checks = run(snap)
    assert checks["own_instance"].level is Level.OK
    assert "disabled" in checks["own_instance"].detail
    assert checks["gpu"].level is Level.OK  # nothing is pinned, so nothing to validate


def test_low_disk_is_an_error_and_exactly_enough_is_fine() -> None:
    assert run(green(free_disk_bytes=19 * GIB))["disk"].level is Level.ERROR
    assert run(green(free_disk_bytes=20 * GIB))["disk"].level is Level.OK
    assert "19" in run(green(free_disk_bytes=19 * GIB))["disk"].detail


def test_an_invalid_gpu_index_is_an_error() -> None:
    checks = run(green(settings=settings(own_ollama_gpu=5)))
    assert checks["gpu"].level is Level.ERROR
    assert "5" in checks["gpu"].detail


def test_missing_nvidia_smi_is_an_error_when_pinning_is_wanted() -> None:
    checks = run(green(gpus=None))
    assert checks["gpu"].level is Level.ERROR
    assert "nvidia-smi" in checks["gpu"].detail


def test_missing_calibration_is_only_a_warning() -> None:
    checks = run(green(calibration_ctx=None))
    assert checks["calibration"].level is Level.WARNING
    assert "--calibrate" in checks["calibration"].detail
    assert exit_code(list(checks.values())) == 0


def test_a_small_calibrated_context_warns_about_risk_r7() -> None:
    checks = run(green(calibration_ctx=12288))
    assert checks["calibration"].level is Level.WARNING
    assert "12288" in checks["calibration"].detail
    assert run(green(calibration_ctx=16384))["calibration"].level is Level.OK


def test_foreign_vram_on_the_pinned_gpu_warns() -> None:
    busy = [Gpu(0, "RTX 4090", 24564, 83), Gpu(1, "RTX 4090", 24564, 12 * 1024)]
    checks = run(green(gpus=busy, own_loaded_vram_bytes=0))
    assert checks["gpu_sharing"].level is Level.WARNING
    assert "GPU 1" in checks["gpu_sharing"].detail


def test_vram_used_by_our_own_models_is_not_foreign() -> None:
    busy = [Gpu(0, "RTX 4090", 24564, 83), Gpu(1, "RTX 4090", 24564, 18 * 1024)]
    checks = run(green(gpus=busy, own_loaded_vram_bytes=18 * GIB))
    assert checks["gpu_sharing"].level is Level.OK


def test_gpu_sharing_is_not_judged_when_our_instance_is_not_running() -> None:
    busy = [Gpu(1, "RTX 4090", 24564, 12 * 1024)]
    snap = green(gpus=busy, instance=InstanceStatus(InstanceState.FAIL_OPEN, "x"))
    assert run(snap)["gpu_sharing"].level is Level.OK


def test_render_has_one_line_per_check_and_marks_the_level() -> None:
    checks = [
        Check("a", Level.OK, "fine"),
        Check("b", Level.WARNING, "hm"),
        Check("c", Level.ERROR, "bad"),
    ]
    lines = render(checks).splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("OK")
    assert lines[1].startswith("WARN")
    assert lines[2].startswith("ERROR")
    assert "bad" in lines[2]


def test_render_json_is_machine_readable() -> None:
    out = json.loads(render_json([Check("a", Level.WARNING, "ä hm")]))
    assert out == [{"name": "a", "level": "warning", "detail": "ä hm"}]


def test_exit_code_is_one_only_for_errors() -> None:
    assert exit_code([Check("a", Level.WARNING, "")]) == 0
    assert exit_code([Check("a", Level.OK, ""), Check("b", Level.ERROR, "")]) == 1
    assert exit_code([]) == 0
