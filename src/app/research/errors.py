"""Errors of Phase 2. `NotFound`, `WrongState` and `InvalidInput` are reused from
`app.brief.errors`; the service layer (M6) maps them to HTTP 404, 409 and 422."""


class ResearchError(Exception):
    """A run cannot continue for a reason the owner can fix; the run is marked `failed`."""


class StalePlan(Exception):
    """The plan hash is not the current plan's (HTTP 409)."""


class PlanBlocked(Exception):
    """The plan holds a blocked query, or nothing to search, and cannot be approved (HTTP 409)."""
