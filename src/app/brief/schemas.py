"""What the models are asked to return in Phase 1 (validated by Pydantic, enforced via Ollama)."""

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

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
