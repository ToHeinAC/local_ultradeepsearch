"""Plain types of a research run, shared by the steps, the service and the graph."""

from dataclasses import dataclass

LIGHT_STEPS = ("0", "1", "2.1", "2", "10", "15", "16", "G", "X")  # PRD §3.6, in order


@dataclass(frozen=True)
class RunSpec:
    """What a run was approved with; fixed for its whole life."""

    run_id: str
    tier: str
    brief_sha256: str
    brief_path: str
    template_id: str
    response_format: str
    report_language: str
    summarize_model: str | None


@dataclass(frozen=True)
class Item:
    """One searchable atomic item of step 1: a sub-question, an entity or a time period."""

    item_id: str  # i01, i02, ... in that order
    kind: str  # "sub_question", "entity" or "period"
    text: str
