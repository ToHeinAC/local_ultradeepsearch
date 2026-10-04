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
