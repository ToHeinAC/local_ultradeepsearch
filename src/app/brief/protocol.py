"""What a caller sends to a waiting brief session (the resume values of its interrupts).

The service validates these before it resumes the graph: an invalid value must never be stored as
a checkpoint's pending write, or the session could not continue.
"""

from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from app.brief.models import AnswerKind
from app.pipeline.profiles import ResponseFormatName

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class AnswerInput(BaseModel):
    kind: AnswerKind
    text: str = ""

    @model_validator(mode="after")
    def _text_needs_text(self) -> "AnswerInput":
        if self.kind == "text" and not self.text.strip():
            raise ValueError("an answer of kind 'text' needs a text")
        return self


class AskReply(BaseModel):
    """The owner's reply to a round: one answer per question, an optional note, or `genug`."""

    answers: list[AnswerInput]
    note: str = ""
    genug: bool = False


class OfferReply(BaseModel):
    """The reply to a pasted finished prompt: strengthen it once, or install it as it is."""

    strengthen: bool


class Approve(BaseModel):
    action: Literal["approve"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tier: Literal["light", "full"]
    summarize_model: str | None = None
    tavily_cap: int | None = Field(default=None, ge=0)
    at: AwareDatetime  # chosen by the service, so a resumed `finalize` archives under the same name


class Revise(BaseModel):
    action: Literal["revise"]
    feedback: NonBlank


class Edit(BaseModel):
    action: Literal["edit"]
    text: str

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("the brief text must not be empty")
        return value


class SettingsChange(BaseModel):
    action: Literal["settings"]
    report_language: str | None = Field(default=None, pattern=r"^[a-z]{2}$")
    response_format: ResponseFormatName | None = None
    template_id: str | None = None

    @model_validator(mode="after")
    def _something_changes(self) -> "SettingsChange":
        if (
            self.report_language is None
            and self.response_format is None
            and self.template_id is None
        ):
            raise ValueError("a settings change needs at least one setting")
        return self


class Save(BaseModel):
    action: Literal["save"]


Decision = Annotated[Approve | Revise | Edit | SettingsChange | Save, Field(discriminator="action")]
_DECISIONS: TypeAdapter[Decision] = TypeAdapter(Decision)


def parse_decision(value: object) -> Decision:
    """The decision in ``value``; raises `pydantic.ValidationError` if it is not a valid one."""
    return _DECISIONS.validate_python(value)
