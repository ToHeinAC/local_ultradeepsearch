"""Helpers shared by test modules."""

from typing import Any

from app.config import Settings


def make_settings(**overrides: Any) -> Settings:
    """Settings that never read a developer's `.env` (init kwargs and `UDR_*` env still apply)."""
    return Settings(_env_file=None, **overrides)  # pyright: ignore[reportCallIssue]
