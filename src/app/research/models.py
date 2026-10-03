"""What the models return in Phase 2, and the artifacts built from it (validated by Pydantic,
enforced via Ollama's `format`)."""

from collections.abc import Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.pipeline.profiles import ResponseFormatName
from app.pipeline.strategies import DomainName

Text = Annotated[str, StringConstraints(strip_whitespace=True)]
Voice = Literal["teach", "survey", "analyze", "advocate"]
InferenceDepth = Literal["surface", "standard", "deep"]
Modality = Literal["collect", "synthesize", "compare", "forecast"]
Tier = Literal["light", "full"]


class Entity(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    type: Text = ""
    required_fields: list[Text] = []


class TimePeriod(BaseModel):
    period: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    type: Text = ""
    primary_source: Text = ""
    issuer: Text = ""


class Levers(BaseModel):
    voice: Voice = "analyze"
    voice_confidence: Literal["high", "low"] = "low"
    domain_notes: Text = ""
    inference_depth: InferenceDepth = "standard"


class DecompositionDraft(BaseModel):
    """What step 1 asks the model for; code adds the questions, tier and headings."""

    entities: list[Entity] = []
    required_formats: list[Text] = []
    required_sections: list[Text] = []
    required_section_headings: list[Text] = []
    time_horizons: list[Text] = []
    time_periods: list[TimePeriod] = []
    scope_conditions: list[Text] = []
    domains: list[DomainName] = []
    tier_recommendation: Tier = "full"
    tier_rationale: Text = ""
    modality: Modality = "synthesize"
    levers: Levers = Levers()


class Decomposition(BaseModel):
    """`prompt-decomposition.json`: the brief broken into the items the run must cover."""

    model_config = ConfigDict(frozen=True)

    sub_questions: list[str]  # the brief's numbered research questions, verbatim
    entities: list[Entity]
    required_formats: list[str]
    required_sections: list[str]
    required_section_headings: list[str]  # plain text, in order; the template's or derived
    time_horizons: list[str]
    time_periods: list[TimePeriod]
    scope_conditions: list[str]
    domains: list[DomainName]
    pipeline_tier: Tier  # what the owner chose; never changed by the recommendation
    tier_recommendation: Tier
    tier_rationale: str
    response_format: ResponseFormatName
    modality: Modality
    levers: Levers


class AtomicItem(BaseModel):
    """Something the report must cover and the search plan must search for."""

    model_config = ConfigDict(frozen=True)

    id: str  # Q1 (research question), E1 (entity) or P1 (time period)
    kind: Literal["question", "entity", "period"]
    text: str


class MatrixRow(BaseModel):
    phrase: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    items: list[Text] = []
    scope_ok: bool = True
    note: Text = ""


class CoverageMatrix(BaseModel):
    rows: list[MatrixRow]


def atomic_items(decomposition: Decomposition) -> list[AtomicItem]:
    """Questions, entities and time periods as items with stable ids, in that order."""
    items = [
        AtomicItem(id=f"Q{n}", kind="question", text=q)
        for n, q in enumerate(decomposition.sub_questions, 1)
    ]
    items += [
        AtomicItem(id=f"E{n}", kind="entity", text=e.name)
        for n, e in enumerate(decomposition.entities, 1)
    ]
    items += [
        AtomicItem(id=f"P{n}", kind="period", text=_period_text(p))
        for n, p in enumerate(decomposition.time_periods, 1)
    ]
    return items


def render_items(items: Sequence[AtomicItem]) -> str:
    """One line per item, as the prompts show them: `Q1: question: text`."""
    return "\n".join(f"{item.id}: {item.kind}: {item.text}" for item in items)


def _period_text(period: TimePeriod) -> str:
    parts = [period.period, period.primary_source, period.issuer]
    return ", ".join(p for p in parts if p)


Lens = Literal["A", "B", "C", "D"]  # breadth, scholarly, adversarial, period-pinned
QueryKind = Literal["web", "scholarly"]


class PlanQueryDraft(BaseModel):
    item: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    lens: Lens
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class PlanDraft(BaseModel):
    queries: list[PlanQueryDraft]


class PlannedQuery(BaseModel):
    """One line of the search plan. ``sent`` is exactly what would leave the machine."""

    model_config = ConfigDict(frozen=True)

    query_id: str
    item: str  # atomic item id
    lens: Lens
    kind: QueryKind  # lens B goes to scholarly sources, every other lens to the web
    original: str
    sent: str  # empty while ``blocked``
    removed_terms: list[str] = []
    blocked: str | None = None  # why it cannot be sent: "denylist", "sanitizer_failed", ...


class SearchPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    queries: tuple[PlannedQuery, ...]


class CondensedLine(BaseModel):
    key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class CondensedEvidence(BaseModel):
    """Evidence that did not fit a section's prompt, condensed by the `summarize` role."""

    lines: list[CondensedLine]
