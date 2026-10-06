"""M6 A6: every domain error has one HTTP status and one body shape."""

import pytest

from app.api.errors import Forbidden, to_http
from app.brief.errors import InvalidInput, NotFound, StaleBrief, UploadRejected, WrongState
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


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (NotFound("r-1"), 404),
        (StaleBrief("old"), 409),
        (StalePlan("old"), 409),
        (WrongState("not now"), 409),
        (PlanBlocked("denylist"), 409),
        (ReportNotReady("running"), 409),
        (InvalidInput("empty"), 422),
        (UploadRejected("too big"), 422),
        (BriefParseError("no title"), 422),
        (TierNotAvailable("Full-Tier ab M8"), 422),
        (TemplateError("bad"), 422),
        (EmptyPlan("none"), 422),
        (InvalidEdit("line"), 422),
        (Forbidden("self_approve needed"), 403),
    ],
)
def test_the_status_of_each_error(error: Exception, status: int) -> None:
    mapped = to_http(error)
    assert mapped is not None
    assert mapped[0] == status
    assert mapped[1]["detail"] == str(error)


def test_a_report_that_is_not_ready_names_the_status_of_the_run() -> None:
    mapped = to_http(ReportNotReady("awaiting_plan_approval"))
    assert mapped is not None
    assert mapped[1] == {
        "detail": "the run is awaiting_plan_approval",
        "status": "awaiting_plan_approval",
    }


def test_an_error_without_a_status_says_none() -> None:
    mapped = to_http(NotFound("r-1"))
    assert mapped is not None
    assert mapped[1]["status"] is None


def test_an_unknown_error_is_not_mapped() -> None:
    assert to_http(RuntimeError("bug")) is None
