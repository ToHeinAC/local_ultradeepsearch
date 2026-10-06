"""Numeric budgets from `config/profiles.toml` (PRD AD3: numbers are data)."""

import tomllib
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

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


def _positive_range(value: tuple[int, int]) -> tuple[int, int]:
    low, high = value
    if not 0 < low <= high:
        raise ValueError(f"expected 0 < low <= high, got {low}..{high}")
    return value


Range = Annotated[tuple[int, int], AfterValidator(_positive_range)]


class ResearchBudget(BaseModel):
    """What one tier of Phase 2 may spend on searching, fetching and reading (PRD §3.8)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sources_min: int = Field(gt=0)
    planned_searches: Range
    adversarial_min: int = Field(ge=0)
    deduped_urls: Range
    fetch_waves: Range
    must_read_notes: Range
    readability_cap: int = Field(gt=0)


class RunRules(BaseModel):
    """Thresholds shared by both tiers: coverage, evidence, edits and the ship gate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    coverage_matrix_max_iterations: int = Field(gt=0)
    thinking_num_predict: int = Field(gt=0)  # output budget of the long thinking calls of steps 1-2
    thin_item_sources: int = Field(gt=0)  # fewer sources than this and an item is thin
    well_covered_sources: int = Field(gt=0)  # this many or more and an item is well covered
    plan_supplement_rounds: int = Field(ge=0)  # extra model calls to fill what a plan lacks
    wave2_queries_per_item: Range
    wave2_urls_per_item: int = Field(gt=0)
    search_max_results: int = Field(gt=0)
    pack_context_fraction: float = Field(gt=0, le=1)  # share of reason's prompt budget for evidence
    condensed_share: float = Field(ge=0, lt=1)  # of that budget, kept free for condensed evidence
    pack_passages: int = Field(ge=0)  # passages from a full-text search added to the evidence
    passage_chars: int = Field(gt=0)  # the longest passage taken from one source
    hunk_max_chars: int = Field(gt=0)
    section_over_factor: float = Field(gt=1)  # a section above this many times its words is long
    section_under_factor: float = Field(gt=0, lt=1)  # ... and below this many, short
    gate_fix_rounds: int = Field(gt=0)
    citation_density_min: float = Field(gt=0)  # citations per 1000 body words (G4)
    quote_min_words: int = Field(gt=0)  # shorter quoted spans are not checked (G6)
    retraction_window_chars: int = Field(gt=0)  # G9
    language_samples: int = Field(gt=0)  # G12
    length_tolerance: tuple[float, float]  # G3: body words within low * a .. high * b

    @field_validator("length_tolerance")
    @classmethod
    def _tolerance(cls, value: tuple[float, float]) -> tuple[float, float]:
        below, above = value
        if not 0 < below <= 1 <= above:
            raise ValueError(f"expected 0 < below <= 1 <= above, got {below}, {above}")
        return value


class ResearchProfiles(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    light: ResearchBudget
    full: ResearchBudget
    rules: RunRules


class ServiceLimits(BaseModel):
    """Numbers of the API and the worker (PRD M6)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_poll_s: float = Field(gt=0)  # how often an idle worker looks at the queue
    sse_poll_s: float = Field(gt=0)  # how often the event stream looks at the run's file
    session_threads: int = Field(ge=1)  # Phase-1 jobs that run at the same time
    summarize_models: tuple[str, ...] = Field(min_length=1)  # what `summarize_model` accepts


class Profiles(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    light: Profile
    full: Profile
    phase1: Phase1Limits
    response_formats: ResponseFormats


def _raw(config_dir: Path) -> dict[str, Any]:
    return tomllib.loads((config_dir / "profiles.toml").read_text(encoding="utf-8"))


def _load(config_dir: Path) -> Profiles:
    raw = _raw(config_dir)
    raw.pop("research", None)  # validated on its own: Phase 1 does not need it
    raw.pop("service", None)
    return Profiles.model_validate(raw)


def _load_research(config_dir: Path) -> ResearchProfiles:
    return ResearchProfiles.model_validate(_raw(config_dir).get("research", {}))


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


def load_research_budget(name: str, config_dir: Path) -> ResearchBudget:
    """The Phase-2 budget of tier ``name`` ("light" or "full")."""
    research = _load_research(config_dir)
    if name not in ("light", "full"):
        raise ValueError(f"unknown profile {name!r}")
    return research.light if name == "light" else research.full


def load_run_rules(config_dir: Path) -> RunRules:
    return _load_research(config_dir).rules


def load_service_limits(config_dir: Path) -> ServiceLimits:
    return ServiceLimits.model_validate(_raw(config_dir).get("service", {}))
