"""What the models are asked to return for a source (validated by Pydantic, enforced via Ollama)."""

from typing import Literal

from pydantic import BaseModel, Field

Stance = Literal["supports", "refutes", "neutral"]
EvidenceType = Literal[
    "empirical", "theoretical", "anecdotal", "expert-opinion", "statistical", "legal", "historical"
]
Confidence = Literal["high", "medium", "low"]


class ClaimDraft(BaseModel):
    claim: str = Field(min_length=1)
    stance: Stance
    stance_target: str
    evidence_type: EvidenceType
    scope_conditions: str = ""
    quoted_support: str = Field(min_length=1)
    numbers: list[str] = []
    entities: list[str] = []
    time_period: str | None = None
    region: str | None = None
    confidence: Confidence


class ChunkExtraction(BaseModel):
    summary: str
    claims: list[ClaimDraft]


class MergedSummary(BaseModel):
    summary: str


Relevance = Literal["load-bearing", "useful", "tangential", "not-relevant"]


class PartialAnalysis(BaseModel):
    """What one part of a long source says (the map step, and merged parts in the reduce step)."""

    key_points: list[str]
    numbers: list[str] = []
    quotes: list[str] = []


class SourceAnalysis(BaseModel):
    thesis: str
    methodology: str = ""
    key_findings: list[str]
    load_bearing_citations: list[str] = []
    caveats: str = ""
    relevance_to_query: str
    quotes: list[str] = []
    relevance: Relevance
