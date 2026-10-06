"""The HTTP face of the domain errors (PRD M6 A6): one status and one body shape for each."""

from typing import Any

from app.brief.errors import InvalidInput, NotFound, StaleBrief, WrongState
from app.brief.parse import BriefParseError
from app.research.errors import (
    EmptyPlan,
    InvalidEdit,
    PlanBlocked,
    ReportNotReady,
    StalePlan,
)
from app.research.service import TierNotAvailable
from app.templates import TemplateError


class Forbidden(Exception):
    """The API key may not do this (an approval without `self_approve`): HTTP 403."""


_STATUS: tuple[tuple[tuple[type[Exception], ...], int], ...] = (
    ((NotFound,), 404),
    ((StaleBrief, StalePlan, WrongState, PlanBlocked, ReportNotReady), 409),
    ((InvalidInput, BriefParseError, TierNotAvailable, TemplateError, EmptyPlan, InvalidEdit), 422),
    ((Forbidden,), 403),
)


def to_http(exc: Exception) -> tuple[int, dict[str, Any]] | None:
    """The status and body for ``exc``, or `None` for an error that is a bug (HTTP 500)."""
    for types, status in _STATUS:
        if isinstance(exc, types):
            return status, {"detail": str(exc), "status": getattr(exc, "status", None)}
    return None


HANDLED: tuple[type[Exception], ...] = tuple(t for types, _ in _STATUS for t in types)
