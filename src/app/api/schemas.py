"""Request and response models of the REST API (they make the OpenAPI schema).

The session and run views are the domain's own dataclasses (`SessionView`, `RunView`).
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.brief.protocol import AnswerInput
from app.research.models import SearchPlan

Tier = Literal["light", "full", "auto"]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MessageIn(Body):
    """Answers to the open questions (one per question), or the answer to an offer."""

    answers: list[AnswerInput] = Field(default_factory=lambda: [])
    note: str = ""
    genug: bool = False
    offer: Literal["strengthen", "install"] | None = None


class FeedbackIn(Body):
    feedback: str


class TextIn(Body):
    text: str


class SettingsIn(Body):
    report_language: str | None = None
    response_format: str | None = None
    template_id: str | None = None


class SessionApproveIn(Body):
    brief_sha256: str
    tier: Tier
    summarize_model: str | None = None


class ApprovedOut(BaseModel):
    run_id: str


class RunCreateIn(Body):
    brief: str
    tier: Tier
    template_id: str
    language: str | None = None
    response_format: str | None = None


class RunApproveIn(Body):
    brief_sha256: str


class PlanApproveIn(Body):
    plan_sha256: str


class PlanOut(BaseModel):
    plan: SearchPlan | None
    plan_sha256: str | None
    text: str


class EventsOut(BaseModel):
    events: list[dict[str, Any]]
    next: int


class OutboundOut(BaseModel):
    lines: list[dict[str, Any]]


class TemplateOut(BaseModel):
    id: str
    name: str
    description: str
    language: str
    default_response_format: str
    sections: list[str]


class DenylistIO(Body):
    terms: list[str]


class HealthOut(BaseModel):
    api: Literal["ok"]
    worker_lock: Literal["free", "held"]
    queued: int
