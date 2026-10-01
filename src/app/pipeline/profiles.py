"""Per-tier numeric budgets from `config/profiles.toml` (PRD AD3: numbers are data)."""

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    credit_cap: int = Field(gt=0)
    source_analysis_cap: int = Field(ge=0)
    long_source_words: int = Field(gt=0)


class Profiles(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    light: Profile
    full: Profile


def load_profile(name: str, config_dir: Path) -> Profile:
    """The profile called ``name`` ("light" or "full") from ``<config_dir>/profiles.toml``."""
    raw = tomllib.loads((config_dir / "profiles.toml").read_text(encoding="utf-8"))
    profiles = Profiles.model_validate(raw)
    if name not in ("light", "full"):
        raise ValueError(f"unknown profile {name!r}")
    return profiles.light if name == "light" else profiles.full
