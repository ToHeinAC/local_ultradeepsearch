import dataclasses

import pytest

from app.config import Settings
from app.llm.errors import (
    LLMError,
    LLMModelMissingError,
    LLMOutputError,
    LLMTruncatedError,
    LLMUnavailableError,
    PromptTooLargeError,
)
from app.llm.roles import FALLBACK_REASON_NUM_CTX, build_registry
from app.llm.types import Endpoint, Role


def make(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # pyright: ignore[reportCallIssue]


def test_defaults_match_the_prd_table() -> None:
    reg = build_registry(make())
    assert set(reg) == set(Role)
    expected = {
        Role.REASON: ("qwen3.8-27b:latest", Endpoint.OWN, 16384, 8192, 0.2, "30m", 1),
        Role.EXTRACT: (
            "LiquidAI/lfm2.5-1.2b-instruct:latest",
            Endpoint.OWN,
            8192,
            2048,
            0.0,
            "30m",
            2,
        ),
        Role.SUMMARIZE: ("gemma4:e4b", Endpoint.SHARED, 16384, 4096, 0.1, "10m", 2),
        Role.OCR: ("deepseek-ocr:3b", Endpoint.SHARED, 8192, 4096, 0.0, "5m", 1),
    }
    for role, values in expected.items():
        spec = reg[role]
        assert (
            spec.model,
            spec.endpoint,
            spec.num_ctx,
            spec.num_predict,
            spec.temperature,
            spec.keep_alive,
            spec.max_concurrency,
        ) == values
        assert spec.role is role


def test_model_overrides_win() -> None:
    reg = build_registry(make(model_reason="x:1b", model_summarize="gemma4:e2b"))
    assert reg[Role.REASON].model == "x:1b"
    assert reg[Role.SUMMARIZE].model == "gemma4:e2b"


def test_context_overrides_come_from_settings() -> None:
    reg = build_registry(make(num_ctx_extract=4096, num_ctx_summarize=8192, num_ctx_ocr=2048))
    assert reg[Role.EXTRACT].num_ctx == 4096
    assert reg[Role.SUMMARIZE].num_ctx == 8192
    assert reg[Role.OCR].num_ctx == 2048


def test_calibration_sets_reason_context() -> None:
    assert build_registry(make(), reason_num_ctx=32768)[Role.REASON].num_ctx == 32768
    assert build_registry(make())[Role.REASON].num_ctx == FALLBACK_REASON_NUM_CTX


def test_output_budget_always_fits_inside_the_context() -> None:
    for spec in build_registry(make(num_ctx_extract=1024), reason_num_ctx=12288).values():
        assert 0 < spec.num_predict < spec.num_ctx


def test_role_specs_are_immutable() -> None:
    spec = build_registry(make())[Role.REASON]
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.model = "other"  # type: ignore[misc]  # pyright: ignore[reportAttributeAccessIssue]


def test_error_hierarchy_and_payloads() -> None:
    for cls in (LLMUnavailableError, LLMModelMissingError, LLMOutputError, LLMTruncatedError):
        assert issubclass(cls, LLMError)
    assert issubclass(PromptTooLargeError, LLMError)
    assert LLMOutputError("boom", raw='{"x":').raw == '{"x":'
    assert LLMTruncatedError("cut", raw="abc").raw == "abc"
    too_big = PromptTooLargeError(estimate=900, limit=800)
    assert (too_big.estimate, too_big.limit) == (900, 800)
    assert "900" in str(too_big)
