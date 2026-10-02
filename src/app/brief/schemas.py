"""What the models are asked to return in Phase 1 (validated by Pydantic, enforced via Ollama)."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from app.brief.models import ChecklistItem, ChecklistStatus
from app.pipeline.profiles import ResponseFormatName

Text = Annotated[str, StringConstraints(strip_whitespace=True)]


class BriefDraft(BaseModel):
    """The content of a brief. Code renders it; the Output section never comes from here."""

    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    audience: Text = ""
    decision: Text = ""
    background: Text = ""
    goal: Text = ""
    research_questions: list[Text] = []
    in_scope: list[Text] = []
    out_of_scope: list[Text] = []
    non_negotiables: list[Text] = []
    assumptions: list[Text] = []
    good_answer: Text = ""
    tone: Text = ""  # register: who reads it and how technical it should be


class UploadFact(BaseModel):
    fact: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    page: int = Field(ge=1)


class UploadFacts(BaseModel):
    """Facts the summarize role read in one part of an uploaded file."""

    facts: list[UploadFact]


class DigestItem(BaseModel):
    fact: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    file: str
    page: int = Field(ge=1)


class UploadDigest(BaseModel):
    """The facts worth keeping from all uploads, most relevant first; code renders the lines."""

    items: list[DigestItem]


class ChecklistEntry(BaseModel):
    item: ChecklistItem
    status: ChecklistStatus
    note: Text = ""  # what is known or assumed about the item


class Question(BaseModel):
    item: ChecklistItem
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    candidate: Text = ""  # the model's proposal; the owner accepts, edits or replaces it


class Assessment(BaseModel):
    """Where the interview stands: the checklist and the next questions."""

    checklist: list[ChecklistEntry]
    questions: list[Question] = []
    finished_prompt: bool = False  # the first message is already a complete research prompt


class TierRecommendation(BaseModel):
    tier: Literal["light", "full"]
    response_format: ResponseFormatName
    rationale: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
