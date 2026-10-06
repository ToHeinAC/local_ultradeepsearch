"""Errors of Phase 2 that callers (CLI, REST and MCP) tell apart."""

from collections.abc import Callable


class ResearchError(Exception):
    """Base of the errors below."""


class StalePlan(ResearchError):
    """The hash does not belong to the current search plan."""


class PlanBlocked(ResearchError):
    """The plan has queries that cannot be sent (denylist, sanitizer): edit or delete them."""


class EmptyPlan(ResearchError):
    """A plan without queries cannot be approved."""


class InvalidEdit(ResearchError):
    """An edited plan line is malformed or names an unknown item."""


class WorkerBusy(ResearchError):
    """Another run holds the worker slot."""


class RunCancelled(ResearchError):
    """The owner asked to cancel; the run stops at the next safe point (never a failure)."""


class ReportNotReady(ResearchError):
    """The run has no report to hand out yet; ``status`` is where it stands (HTTP 409)."""

    def __init__(self, status: str) -> None:
        super().__init__(f"the run is {status}")
        self.status = status


def never_stop() -> bool:
    return False


def check_stop(stop: Callable[[], bool]) -> None:
    """Raise `RunCancelled` when ``stop`` says the run was cancelled."""
    if stop():
        raise RunCancelled
