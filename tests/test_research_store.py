"""Migration 3: stored searches and the settings and status of a run (PRD M5, AD10)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.brief.errors import NotFound, WrongState
from app.store.db import Database
from app.store.research import SearchStore
from app.store.runs import RunStore

NOW = datetime(2026, 10, 3, 8, 0, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "udr.sqlite", now=lambda: NOW)


def make_run(db: Database, run_id: str = "r-1") -> RunStore:
    with db.tx():
        db.conn.execute(
            "INSERT INTO runs (run_id, created_at, label, status) VALUES (?, ?, '', 'queued')",
            (run_id, NOW.isoformat()),
        )
    return RunStore(db)


def test_a_search_is_stored_once_per_query_and_source(db: Database) -> None:
    make_run(db)
    store = SearchStore(db)
    assert store.add("r-1", "q1", "web", 1, '[{"url": "https://a"}]')
    assert not store.add("r-1", "q1", "web", 1, '[{"url": "https://other"}]')  # first one stands
    assert store.add("r-1", "q1", "openalex", 1, "[]")
    rows = store.all("r-1")
    assert [(r.query_id, r.source, r.wave) for r in rows] == [
        ("q1", "web", 1),
        ("q1", "openalex", 1),
    ]
    assert rows[0].results_json == '[{"url": "https://a"}]'
    assert store.has("r-1", "q1", "web")
    assert not store.has("r-1", "q2", "web")


def test_searches_of_another_run_are_not_seen(db: Database) -> None:
    make_run(db, "r-1")
    make_run(db, "r-2")
    store = SearchStore(db)
    store.add("r-1", "q1", "web", 1, "[]")
    assert store.all("r-2") == []


def test_a_search_needs_its_run(db: Database) -> None:
    with pytest.raises(Exception, match="FOREIGN KEY"):
        SearchStore(db).add("missing", "q1", "web", 1, "[]")


def test_status_moves_through_the_run_life(db: Database) -> None:
    runs = make_run(db)
    for status in ("running", "awaiting_plan_approval", "running", "done"):
        runs.set_status("r-1", status)
        row = runs.get_run("r-1")
        assert row is not None
        assert row.status == status


def test_a_failed_run_can_run_again_but_a_finished_one_cannot(db: Database) -> None:
    runs = make_run(db)
    runs.set_status("r-1", "running")
    runs.set_status("r-1", "failed")
    runs.set_status("r-1", "running")
    runs.set_status("r-1", "blocked")
    with pytest.raises(WrongState, match="blocked -> running"):
        runs.set_status("r-1", "running")


def test_a_done_run_never_moves_again_and_the_same_status_is_a_no_op(db: Database) -> None:
    runs = make_run(db)
    runs.set_status("r-1", "running")
    runs.set_status("r-1", "running")  # a resumed node says it again
    runs.set_status("r-1", "done")
    with pytest.raises(WrongState, match="done -> running"):
        runs.set_status("r-1", "running")
    with pytest.raises(WrongState, match="done -> blocked"):
        runs.set_status("r-1", "blocked")


def test_an_unknown_status_or_run_is_refused(db: Database) -> None:
    runs = make_run(db)
    with pytest.raises(WrongState, match="unknown status"):
        runs.set_status("r-1", "sleeping")
    with pytest.raises(NotFound):
        runs.set_status("nope", "running")


def test_the_settings_of_an_external_run_are_stored_with_it(db: Database) -> None:
    runs = make_run(db)
    row = runs.get_run("r-1")
    assert row is not None
    assert row.settings_json is None
    runs.set_settings("r-1", '{"report_language": "de"}')
    again = runs.get_run("r-1")
    assert again is not None
    assert again.settings_json == '{"report_language": "de"}'
