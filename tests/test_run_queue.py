"""Migration 4: the run queue, the cancel flag, the pending plan hash and run deletion (M6)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.brief.errors import NotFound, StaleBrief, WrongState
from app.store.db import Database
from app.store.runs import RunStore

START = datetime(2026, 10, 6, 8, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "udr.sqlite", now=Clock())


@pytest.fixture
def runs(db: Database) -> RunStore:
    return RunStore(db)


def new_run(runs: RunStore, status: str = "queued", created_by: str | None = None) -> str:
    """An external run; a status a creator cannot choose is forced in the table directly."""
    creatable = status if status in ("queued", "awaiting_brief_approval") else "queued"
    row = runs.create_external(
        brief_sha256="a" * 64,
        brief_path="b.md",
        tier="light",
        settings_json="{}",
        status=creatable,
        created_by=created_by,
    )
    if creatable != status:
        with runs._db.tx():  # pyright: ignore[reportPrivateUsage]
            runs._db.conn.execute(  # pyright: ignore[reportPrivateUsage]
                "UPDATE runs SET status = ? WHERE run_id = ?", (status, row.run_id)
            )
    return row.run_id


def status_of(runs: RunStore, run_id: str) -> str:
    row = runs.get_run(run_id)
    assert row is not None
    return row.status


def test_an_external_run_remembers_who_created_it(runs: RunStore) -> None:
    run_id = new_run(runs, created_by="k-1")
    row = runs.get_run(run_id)
    assert row is not None
    assert (row.created_by, row.cancel_requested, row.pending_plan_sha256) == ("k-1", False, None)


def test_a_run_without_self_approval_waits_for_its_brief_to_be_approved(runs: RunStore) -> None:
    run_id = new_run(runs, "awaiting_brief_approval")
    with pytest.raises(StaleBrief):
        runs.approve_external(run_id, "b" * 64)
    assert status_of(runs, run_id) == "awaiting_brief_approval"
    assert runs.approve_external(run_id, "a" * 64).status == "queued"
    with pytest.raises(WrongState):
        runs.approve_external(run_id, "a" * 64)
    with pytest.raises(NotFound):
        runs.approve_external("nope", "a" * 64)


@pytest.mark.parametrize("status", ["queued", "awaiting_plan_approval", "awaiting_brief_approval"])
def test_cancel_acts_at_once_on_a_run_that_does_not_execute(runs: RunStore, status: str) -> None:
    run_id = new_run(runs, status)
    assert runs.request_cancel(run_id) == "cancelled"
    assert status_of(runs, run_id) == "cancelled"


def test_cancel_of_a_running_run_only_sets_the_flag(runs: RunStore) -> None:
    run_id = new_run(runs)
    runs.set_status(run_id, "running")
    assert runs.request_cancel(run_id) == "running"
    assert status_of(runs, run_id) == "running"
    assert runs.cancel_requested(run_id)
    runs.clear_cancel(run_id)
    assert not runs.cancel_requested(run_id)


@pytest.mark.parametrize("final", ["done", "blocked", "failed"])
def test_cancel_of_a_finished_run_is_refused(runs: RunStore, final: str) -> None:
    run_id = new_run(runs)
    runs.set_status(run_id, "running")
    runs.set_status(run_id, final)
    with pytest.raises(WrongState):
        runs.request_cancel(run_id)


def test_cancel_of_an_unknown_run(runs: RunStore) -> None:
    with pytest.raises(NotFound):
        runs.request_cancel("nope")


def test_a_cancelled_run_can_be_queued_again_and_nothing_else(runs: RunStore) -> None:
    run_id = new_run(runs)
    runs.request_cancel(run_id)
    with pytest.raises(WrongState):
        runs.set_status(run_id, "running")
    runs.set_status(run_id, "queued")


def test_an_approved_plan_queues_the_run_once(runs: RunStore) -> None:
    run_id = new_run(runs, "awaiting_plan_approval")
    runs.set_pending_plan(run_id, "p" * 64)
    assert status_of(runs, run_id) == "queued"
    with pytest.raises(WrongState):  # the second of two concurrent approvals
        runs.set_pending_plan(run_id, "q" * 64)
    assert runs.pending_plan(run_id) == "p" * 64
    assert runs.pending_plan(run_id) == "p" * 64  # reading does not clear it
    runs.clear_pending_plan(run_id)
    assert runs.pending_plan(run_id) is None


def test_a_plan_can_only_be_approved_while_it_awaits_approval(runs: RunStore) -> None:
    run_id = new_run(runs)
    with pytest.raises(WrongState):
        runs.set_pending_plan(run_id, "p" * 64)
    with pytest.raises(NotFound):
        runs.set_pending_plan("nope", "p" * 64)


def test_cancelling_a_queued_run_drops_its_pending_plan(runs: RunStore) -> None:
    run_id = new_run(runs, "awaiting_plan_approval")
    runs.set_pending_plan(run_id, "p" * 64)
    runs.request_cancel(run_id)
    assert runs.pending_plan(run_id) is None


def test_the_next_runnable_run_is_an_orphan_first_then_the_oldest_queued(runs: RunStore) -> None:
    assert runs.next_runnable() is None
    first, second, third = new_run(runs), new_run(runs), new_run(runs)
    runs.set_status(second, "queued")
    next_run = runs.next_runnable()
    assert next_run is not None
    assert next_run.run_id == first
    runs.set_status(third, "running")
    next_run = runs.next_runnable()
    assert next_run is not None
    assert next_run.run_id == third
    runs.set_status(third, "awaiting_plan_approval")
    runs.set_status(first, "running")
    runs.set_status(first, "done")
    next_run = runs.next_runnable()
    assert next_run is not None
    assert next_run.run_id == second


def test_runs_awaiting_approval_are_not_runnable(runs: RunStore) -> None:
    new_run(runs, "awaiting_brief_approval")
    new_run(runs, "awaiting_plan_approval")
    assert runs.next_runnable() is None


def test_runs_are_listed_newest_first(runs: RunStore) -> None:
    ids = [new_run(runs) for _ in range(3)]
    assert [r.run_id for r in runs.list_runs()] == ids[::-1]
    assert [r.run_id for r in runs.list_runs(limit=2)] == ids[:0:-1]


def test_a_second_connection_sees_a_status_written_by_the_first(
    db: Database, runs: RunStore
) -> None:
    run_id = new_run(runs)
    other = RunStore(Database(db.path))
    runs.set_status(run_id, "running")
    row = other.get_run(run_id)
    assert row is not None
    assert row.status == "running"


def test_a_running_run_cannot_be_deleted(runs: RunStore) -> None:
    run_id = new_run(runs)
    runs.set_status(run_id, "running")
    with pytest.raises(WrongState):
        runs.delete_run(run_id)
    with pytest.raises(NotFound):
        runs.delete_run("nope")


def test_deleting_a_run_removes_its_vault_rows_and_the_uploads_of_its_session(
    db: Database, runs: RunStore
) -> None:
    run_id = new_run(runs)
    with db.tx():
        db.conn.execute(
            "INSERT INTO notes (run_id, note_id, kind, stage, url, canonical_url, title, body, "
            "word_count, source_tier, created_at) VALUES (?, 'n1', 'source', 'complete', 'u', "
            "'u', 't', 'b', 1, 'x', 'now')",
            (run_id,),
        )
        db.conn.execute("INSERT INTO searches VALUES (?, 'q1', 'web', 1, '[]', 'now')", (run_id,))
        db.conn.execute(
            "INSERT INTO sessions (session_id, created_at, updated_at, status, "
            "interview_language, run_id) VALUES ('s-1', 'n', 'n', 'approved', 'de', ?)",
            (run_id,),
        )
        db.conn.execute("UPDATE runs SET session_id = 's-1' WHERE run_id = ?", (run_id,))
        db.conn.execute(
            "INSERT INTO uploads VALUES ('s-1', 'f1', 'a.txt', 'txt', 1, 1, 'h', 'stored', "
            "'[]', '[]', 'now')"
        )
        db.conn.execute("INSERT INTO upload_parts VALUES ('s-1', 'f1', 0, '[]')")
    assert runs.delete_run(run_id).session_id == "s-1"
    for table in ("runs", "notes", "searches", "uploads", "upload_parts", "notes_fts"):
        assert db.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
