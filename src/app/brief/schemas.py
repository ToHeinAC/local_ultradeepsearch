"""What the models are asked to return in Phase 1 (validated by Pydantic, enforced via Ollama)."""

from typing import Annotated

from pydantic import BaseModel, StringConstraints

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
