"""Plain data types of the vault."""

import re
from dataclasses import dataclass, field
from typing import Any, Literal

Kind = Literal["source", "source_analysis"]
Stage = Literal["fetched", "extracted", "complete"]

_DOI = re.compile(r"(10\.\d{4,9}/\S+)", re.IGNORECASE)


def normalize_doi(raw: str | None) -> str | None:
    """`10.1000/ABC`, `https://doi.org/10.1000/abc`, `doi:10.1000/abc` → `10.1000/abc`."""
    match = _DOI.search((raw or "").strip())
    return match.group(1).lower().rstrip(".,;)") if match else None


@dataclass(frozen=True)
class SourceMeta:
    """What the caller already knows about a source before it is fetched (search metadata)."""

    doi: str | None = None
    scholarly: bool = False  # found through OpenAlex, Crossref or arXiv
    year: int | None = None
    authors: tuple[str, ...] = ()
    venue: str | None = None
    cited_by_count: int | None = None
    is_retracted: bool | None = None
    oa_url: str | None = None


@dataclass(frozen=True)
class NewSource:
    url: str
    final_url: str
    canonical_url: str
    doi: str | None
    title: str
    content_type: str | None
    via: str
    body: str
    pages: tuple[str, ...]
    word_count: int
    source_tier: str
    derivative_of: str | None
    minhash: bytes | None
    links: tuple[str, ...]
    meta: SourceMeta = field(default_factory=SourceMeta)


@dataclass(frozen=True)
class Note:
    run_id: str
    note_id: str
    kind: Kind
    stage: Stage
    url: str
    final_url: str | None
    canonical_url: str
    doi: str | None
    title: str
    content_type: str | None
    via: str | None
    body: str
    pages: tuple[str, ...]
    word_count: int
    summary: str
    meta: dict[str, Any]
    source_tier: str
    utility: float | None
    derivative_of: str | None
    analysis_of: str | None
    links: tuple[str, ...]
    extract_failed: bool
    claims_kept: int
    claims_dropped: int
    created_at: str


@dataclass(frozen=True)
class NewClaim:
    claim: str
    stance: str
    stance_target: str
    evidence_type: str
    scope_conditions: str
    quoted_support: str
    numbers: tuple[str, ...]
    entities: tuple[str, ...]
    time_period: str | None
    region: str | None
    confidence: str


@dataclass(frozen=True)
class ClaimRecord:
    claim_id: str
    note_id: str
    claim: str
    stance: str
    stance_target: str
    evidence_type: str
    scope_conditions: str
    quoted_support: str
    numbers: tuple[str, ...]
    entities: tuple[str, ...]
    time_period: str | None
    region: str | None
    confidence: str


@dataclass(frozen=True)
class Rejection:
    canonical_url: str
    url: str
    reason: str
    detail: str
    attempts: int
    retryable: bool
    created_at: str


@dataclass(frozen=True)
class SearchResult:
    note_id: str
    kind: str
    title: str
    score: float  # higher is better
    derivative_of: str | None


@dataclass(frozen=True)
class RunStats:
    notes_by_kind: dict[str, int]
    derivatives: int
    rejected_by_reason: dict[str, int]
    claims_kept: int
    claims_dropped: int
    claims_drop_rate: float
    extract_failed: int
    source_analyses: int
