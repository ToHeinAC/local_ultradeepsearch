"""Plain types of a research run, shared by the steps, the service and the graph."""

from dataclasses import dataclass

from app.store.research import QueryRow

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


@dataclass(frozen=True)
class StepInfo:
    step_id: str
    status: str  # "running" or "done"


@dataclass(frozen=True)
class PlanView:
    plan_sha256: str
    items: tuple[Item, ...]
    rows: tuple[QueryRow, ...]  # wave 1, deleted ones included
    approvable: bool  # no blocked row and at least one planned row


@dataclass(frozen=True)
class RunView:
    run_id: str
    status: str
    reason: str
    tier: str | None
    template_id: str | None
    steps: tuple[StepInfo, ...]
    waiting_for: str  # "plan", "work" (stopped between steps) or "nothing"
    credits: int
    run_dir: str
    plan: PlanView | None  # while waiting for the plan approval
