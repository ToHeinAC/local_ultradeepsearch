"""Small shared types of the brief: the checklist and the session settings."""

from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from app.pipeline.profiles import ResponseFormatName

ChecklistItem = Literal["question", "context", "goal", "audience", "scope", "output", "depth"]
CHECKLIST_ORDER: tuple[ChecklistItem, ...] = get_args(ChecklistItem)
# Items only the owner can supply. Output and depth have defaults and a recommendation instead.
CONTENT_ITEMS: tuple[ChecklistItem, ...] = ("context", "goal", "audience", "scope")


class SessionSettings(BaseModel):
    """What the Output section of the brief is rendered from (never written by a model)."""

    model_config = ConfigDict(frozen=True)

    report_language: str = Field(pattern=r"^[a-z]{2}$")
    response_format: ResponseFormatName
    template_id: str = Field(min_length=1)
