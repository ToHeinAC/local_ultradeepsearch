"""What the models are asked to return in Phase 2 (validated by Pydantic, enforced via Ollama)."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.pipeline.strategies import DomainName

Text = Annotated[str, StringConstraints(strip_whitespace=True)]
Line = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Register = Literal["teach", "survey", "analyze", "advocate"]
InferenceDepth = Literal["surface", "standard", "deep"]


class Entity(BaseModel):
    name: Line
    type: Text = ""
    required_fields: list[Text] = []


class TimePeriod(BaseModel):
    """A backward-looking reporting period with a primary source (drives lens D searches)."""

    period: Line
    type: Text = ""
    primary_source: Text = ""
    issuer: Text = ""


class Levers(BaseModel):
    """Run posture (upstream levers); rendered into the shim files."""

    model_config = ConfigDict(
        validate_by_name=True, validate_by_alias=True, serialize_by_alias=True
    )

    register_: Register = Field(
        default="analyze", alias="register"
    )  # `register` is a BaseModel name
    register_confidence: Literal["high", "low"] = "low"
    domain_notes: Text = ""
    inference_depth: InferenceDepth = "standard"


class Decomposition(BaseModel):
    sub_questions: Annotated[list[Line], Field(min_length=1)]
    entities: list[Entity] = []
    required_formats: list[Text] = []
    required_sections: list[Text] = []
    time_horizons: list[Text] = []
    time_periods: list[TimePeriod] = []
    scope_conditions: list[Text] = []
    domains: list[DomainName] = []
    section_headings: list[Text] = []
    section_weights: list[float] = []
    tier_recommendation: Literal["light", "full"]
    tier_rationale: Text = ""
    modality: Literal["collect", "synthesize", "compare", "forecast"]
    levers: Levers = Levers()


class CoverageRow(BaseModel):
    phrase: Line  # verbatim from the brief
    item_ids: list[Text] = []
    scope_ok: bool
    gap: bool
    note: Text = ""


class CoverageMatrix(BaseModel):
    rows: list[CoverageRow]


class Headings(BaseModel):
    headings: list[Text]
