"""Numeric budgets from `config/profiles.toml` (PRD AD3: numbers are data)."""

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ResponseFormatName = Literal["short", "structured", "argumentative"]


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    credit_cap: int = Field(gt=0)
    source_analysis_cap: int = Field(ge=0)
    long_source_words: int = Field(gt=0)


class Phase1Limits(BaseModel):
    """Interview and upload limits of the brief (PRD M4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_rounds: int = Field(gt=0)
    max_questions_per_round: int = Field(gt=0)
    max_files: int = Field(gt=0)
    max_file_mb: int = Field(gt=0)
    max_total_pages: int = Field(gt=0)
    ocr_min_chars: int = Field(ge=0)
    ocr_dpi: int = Field(ge=72, le=600)
    upload_digest_words: int = Field(gt=0)
    pseudo_page_chars: int = Field(gt=0)
    max_facts_per_part: int = Field(gt=0)
    language_min_chars: int = Field(gt=0)
    language_min_probability: float = Field(gt=0, le=1)


class FormatRange(BaseModel):
    """Target body words and citation count of one response format, as (low, high)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    words: tuple[int, int]
    citations: tuple[int, int]

    @field_validator("words", "citations")
    @classmethod
    def _ordered_and_positive(cls, value: tuple[int, int]) -> tuple[int, int]:
        low, high = value
        if not 0 < low <= high:
            raise ValueError(f"expected 0 < low <= high, got {low}..{high}")
        return value


class ResponseFormats(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    short: FormatRange
    structured: FormatRange
    argumentative: FormatRange

    def named(self, name: str) -> FormatRange:
        if name not in ("short", "structured", "argumentative"):
            raise ValueError(f"unknown response format {name!r}")
        return getattr(self, name)


class Profiles(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    light: Profile
    full: Profile
    phase1: Phase1Limits
    response_formats: ResponseFormats


def _load(config_dir: Path) -> Profiles:
    raw = tomllib.loads((config_dir / "profiles.toml").read_text(encoding="utf-8"))
    return Profiles.model_validate(raw)


def load_profile(name: str, config_dir: Path) -> Profile:
    """The profile called ``name`` ("light" or "full") from ``<config_dir>/profiles.toml``."""
    profiles = _load(config_dir)
    if name not in ("light", "full"):
        raise ValueError(f"unknown profile {name!r}")
    return profiles.light if name == "light" else profiles.full


def load_phase1(config_dir: Path) -> Phase1Limits:
    return _load(config_dir).phase1


def load_response_formats(config_dir: Path) -> ResponseFormats:
    return _load(config_dir).response_formats
