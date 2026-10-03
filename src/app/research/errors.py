"""Errors of Phase 2 that callers (CLI, later REST and MCP) tell apart."""


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
