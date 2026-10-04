"""Numeric budgets from `config/profiles.toml` (PRD AD3: numbers are data)."""

import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

ResponseFormatName = Literal["short", "structured", "argumentative"]


def _ordered_and_positive(value: tuple[float, float]) -> tuple[float, float]:
    low, high = value
    if not 0 < low <= high:
        raise ValueError(f"expected 0 < low <= high, got {low}..{high}")
    return value


Range = Annotated[tuple[int, int], AfterValidator(_ordered_and_positive)]
FloatRange = Annotated[tuple[float, float], AfterValidator(_ordered_and_positive)]


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    credit_cap: int = Field(gt=0)
    source_analysis_cap: int = Field(ge=0)
    long_source_words: int = Field(gt=0)
    planned_searches: Range  # wave-1 queries
    adversarial_min: int = Field(ge=0)
    results_per_query: int = Field(gt=0)
    candidate_urls: Range
    deduped_urls: Range
    wave2_urls: int = Field(ge=0)
    wave2_queries_per_item: int = Field(gt=0)
    fetch_waves: int = Field(gt=0)
    sources_min: int = Field(gt=0)
    sources_target: Range
    thin_sources: int = Field(ge=0)
    must_read_notes: Range
    readability_cap: int = Field(gt=0)


class ResearchConfig(BaseModel):
    """Numbers of the research steps that do not depend on the tier (PRD M5)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    coverage_iterations: int = Field(gt=0)
    section_weight_bounds: FloatRange
    section_min_words: int = Field(gt=0)
    passages_per_note: int = Field(gt=0)
    passage_chars: int = Field(gt=0)
    max_citations_per_bracket: int = Field(gt=0)
    hunk_max_old_chars: int = Field(gt=0)


class ReadabilityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    merge_max_chars: int = Field(gt=0)
    paragraph_target_chars: Range
    break_min_chars: int = Field(gt=0)
    split_min_chars: int = Field(gt=0)
    added_words_max: int = Field(ge=0)
    connector_words_max: int = Field(ge=0)


class GateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fix_rounds: int = Field(ge=0)
    length_tolerance: FloatRange
    citation_density_min: float = Field(gt=0)
    quote_min_words: int = Field(gt=0)
    retraction_window_chars: int = Field(gt=0)
    language_sample_chars: int = Field(gt=0)


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

    words: Range
    citations: Range


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
    research: ResearchConfig
    readability: ReadabilityConfig
    gate: GateConfig


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


def load_research_config(config_dir: Path) -> ResearchConfig:
    return _load(config_dir).research


def load_readability_config(config_dir: Path) -> ReadabilityConfig:
    return _load(config_dir).readability


def load_gate_config(config_dir: Path) -> GateConfig:
    return _load(config_dir).gate
