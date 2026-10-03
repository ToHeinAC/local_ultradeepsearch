"""The settings a run is approved with (PRD M5 D2): frozen at approval, read back in Phase 2."""

import json
from datetime import UTC, datetime

import pytest
from research_rig import TEMPLATES

from app.research.manifest import RunSettings
from app.research.settings import resolve_run_settings
from app.store.runs import RunRow
from app.store.sessions import SessionRow

STORED = {
    "report_language": "en",
    "response_format": "short",
    "template_id": "literaturuebersicht",
    "interview_language": "de",
    "tier": "light",
    "summarize_model": "gemma4:e2b",
}


def run_row(
    settings_json: str | None,
    tier: str | None = "light",
    session_id: str | None = "s1",
    summarize_model: str | None = None,
) -> RunRow:
    return RunRow(
        run_id="r-1",
        session_id=session_id,
        brief_sha256="a" * 64,
        brief_path="data/briefs/x.md",
        tier=tier,
        summarize_model=summarize_model,
        status="queued",
        created_at=datetime(2026, 10, 3, tzinfo=UTC).isoformat(),
        settings_json=settings_json,
    )


def session_row(**overrides: object) -> SessionRow:
    fields: dict[str, object] = {
        "session_id": "s1",
        "created_at": "x",
        "updated_at": "x",
        "status": "approved",
        "interview_language": "de",
        "report_language": None,
        "response_format": None,
        "template_id": None,
        "upload_digest": "",
        "digest_notice": "",
        "brief_text": "x",
        "brief_sha256": "a" * 64,
        "approved_sha256": "a" * 64,
        "archive_path": "x",
        "run_id": "r-1",
    }
    return SessionRow(**{**fields, **overrides})  # type: ignore[arg-type]


def test_the_stored_settings_are_what_the_run_uses() -> None:
    found = resolve_run_settings(run_row(json.dumps(STORED)), None, TEMPLATES)
    assert found == RunSettings.model_validate(STORED)


def test_a_run_approved_before_settings_were_frozen_falls_back_to_its_session() -> None:
    session = session_row(report_language="en", template_id="literaturuebersicht")
    row = run_row(None, tier="full", summarize_model="gemma4:e2b")
    found = resolve_run_settings(row, session, TEMPLATES)
    assert found.report_language == "en"
    assert found.template_id == "literaturuebersicht"
    assert found.response_format == "argumentative"  # the template's default
    assert (found.interview_language, found.tier, found.summarize_model) == (
        "de",
        "full",
        "gemma4:e2b",
    )


def test_the_fallback_uses_the_documented_defaults() -> None:
    found = resolve_run_settings(run_row(None), session_row(), TEMPLATES)
    assert (found.report_language, found.template_id, found.response_format) == (
        "de",
        "auto",
        "structured",
    )


def test_without_stored_settings_or_a_session_there_is_nothing_to_go_on() -> None:
    with pytest.raises(ValueError, match="no settings"):
        resolve_run_settings(run_row(None, session_id=None), None, TEMPLATES)
    with pytest.raises(ValueError, match="no settings"):
        resolve_run_settings(run_row(None), None, TEMPLATES)
    with pytest.raises(ValueError, match="no settings"):
        resolve_run_settings(run_row(None, tier=None), session_row(), TEMPLATES)


def test_damaged_stored_settings_are_an_error_not_a_guess() -> None:
    with pytest.raises(ValueError, match="report_language"):
        resolve_run_settings(run_row('{"report_language": "deutsch"}'), None, TEMPLATES)
