"""The role → model registry (PRD §3.1). Everything tunable comes from `Settings`."""

from app.config import Settings
from app.llm.types import Endpoint, Role, RoleSpec

FALLBACK_REASON_NUM_CTX = 16384  # used until `udr doctor --calibrate` has measured a value


def _spec(
    role: Role,
    model: str,
    endpoint: Endpoint,
    num_ctx: int,
    *,
    num_predict: int,
    temperature: float,
    keep_alive: str,
    max_concurrency: int,
) -> RoleSpec:
    """Output budget is capped at half the context, so a prompt always has room."""
    budget = min(num_predict, num_ctx // 2)
    return RoleSpec(
        role, model, endpoint, num_ctx, budget, temperature, keep_alive, max_concurrency
    )


def build_registry(settings: Settings, reason_num_ctx: int | None = None) -> dict[Role, RoleSpec]:
    """Build the four role specs. `reason_num_ctx` is the calibrated context, if any."""
    reason_ctx = reason_num_ctx or FALLBACK_REASON_NUM_CTX
    return {
        Role.REASON: _spec(
            Role.REASON,
            settings.model_reason,
            Endpoint.OWN,
            reason_ctx,
            num_predict=8192,
            temperature=0.2,
            keep_alive="30m",
            max_concurrency=1,
        ),
        Role.EXTRACT: _spec(
            Role.EXTRACT,
            settings.model_extract,
            Endpoint.OWN,
            settings.num_ctx_extract,
            num_predict=2048,
            temperature=0.0,
            keep_alive="30m",
            max_concurrency=2,
        ),
        Role.SUMMARIZE: _spec(
            Role.SUMMARIZE,
            settings.model_summarize,
            Endpoint.SHARED,
            settings.num_ctx_summarize,
            num_predict=4096,
            temperature=0.1,
            keep_alive="10m",
            max_concurrency=2,
        ),
        Role.OCR: _spec(
            Role.OCR,
            settings.model_ocr,
            Endpoint.SHARED,
            settings.num_ctx_ocr,
            num_predict=4096,
            temperature=0.0,
            keep_alive="5m",
            max_concurrency=1,
        ),
    }
