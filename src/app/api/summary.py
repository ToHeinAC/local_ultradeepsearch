"""What the GUI shows about runs and sessions (PRD M7): plain values read from the files and rows
behind them. The pure helpers are tested alone; `Facade` gathers the inputs."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class StepSpan:
    step: str
    status: str  # "running" or "done"
    started_at: str
    ended_at: str | None


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    status: str
    tier: str
    waiting_for: str
    title: str | None
    brief_sha256: str | None
    created_at: str
    created_by: str | None  # the name of the API key; None for the CLI
    started_at: str | None  # the first step's start
    ended_at: str | None  # the last step's stamp, once the run is over
    elapsed_s: float | None  # only when it ended; the GUI counts a running run itself
    step: str | None  # the step running now, or the one that failed
    steps: tuple[StepSpan, ...]
    credits_run: int
    credit_cap: int
    credits_month: int
    month_limit: int
    sources: int | None  # None until the first notes are counted
    warnings: int  # events at level warning or error


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    status: str
    title: str | None
    created_at: str
    updated_at: str
    created_by: str | None
    run_id: str | None


def step_spans(steps: Sequence[Mapping[str, str]]) -> tuple[StepSpan, ...]:
    """One span per step, in the order the steps began. A step that began again after a crash
    keeps its first start; its end is when it finally finished."""
    started: dict[str, str] = {}
    ended: dict[str, str] = {}
    for record in steps:
        step = record["step"]
        if record["status"] == "done":
            ended.setdefault(step, record["ts"])
        else:
            started.setdefault(step, record["ts"])
    return tuple(
        StepSpan(step, "done" if step in ended else "running", at, ended.get(step))
        for step, at in started.items()
    )


def elapsed_seconds(start: str | None, end: str | None) -> float | None:
    if start is None or end is None:
        return None
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def title_of(brief: str | None) -> str | None:
    """The first level-1 heading of a brief."""
    for line in (brief or "").splitlines():
        if line.startswith("# "):
            return line[2:].strip() or None
    return None
