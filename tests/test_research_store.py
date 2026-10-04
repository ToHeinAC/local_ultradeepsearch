"""Run specs and search-plan queries (PRD M5): migration 3, `RunStore`, `ResearchStore`."""

import itertools
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.brief.errors import NotFound
from app.brief.render import brief_sha256
from app.store.db import MIGRATIONS, Database, connect, migrate
from app.store.research import QueryDraft, ResearchStore
from app.store.runs import RunStore
from app.store.sessions import SessionStore

NOW = datetime(2026, 10, 4, 8, 0, 0, tzinfo=UTC)
BRIEF = "# Wie teuer ist der Rückbau?\n\n## Forschungsfragen\n\n1. Kosten\n"
SHA = brief_sha256(BRIEF)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "data" / "udr.sqlite", now=lambda: NOW)


@pytest.fixture
def runs(db: Database) -> RunStore:
    return RunStore(db)


@pytest.fixture
def plan(db: Database) -> ResearchStore:
    return ResearchStore(db)


def approved_run(db: Database, *, settings: bool = True) -> str:
    sessions = SessionStore(db)
    session_id = sessions.create("de").session_id
    sessions.set_brief(session_id, BRIEF)
    if settings:
        sessions.set_settings(session_id, "en", "short", "auto")
    sessions.set_status(session_id, "awaiting_decision")
    run = RunStore(db).approve(
        session_id, sha256=SHA, brief_path="data/briefs/x.md", tier="light", summarize_model=None
    )
    return run.run_id


def external(runs: RunStore) -> str:
    return runs.create_external_run(
        sha256=SHA,
        brief_path="data/briefs/y.md",
        tier="light",
        template_id="auto",
        response_format="structured",
        report_language="de",
        approved_at=NOW,
    ).run_id


def drafts(*texts: str, item: str = "i01") -> list[QueryDraft]:
    return [QueryDraft(item, "breadth", "web", text) for text in texts]


# ---- migration 3 ----------------------------------------------------------------------------


def test_migration_3_keeps_an_m4_run_and_backfills_its_settings(tmp_path: Path) -> None:
    path = tmp_path / "udr.sqlite"
    conn = connect(path)
    migrate(conn, MIGRATIONS[:2])  # a database as M4 left it
    conn.execute(
        "INSERT INTO sessions (session_id, created_at, updated_at, status, interview_language, "
        "report_language, response_format, template_id) "
        "VALUES ('s1', 't', 't', 'approved', 'de', 'en', 'short', 'auto')"
    )
    conn.execute(
        "INSERT INTO runs (run_id, created_at, session_id, brief_sha256, tier, status) "
        "VALUES ('r1', '2026-10-02T09:00:00+00:00', 's1', 'abc', 'light', 'queued')"
    )
    conn.execute("INSERT INTO runs (run_id, created_at) VALUES ('m3', 'then')")
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS) >= 3
    r1 = conn.execute("SELECT * FROM runs WHERE run_id = 'r1'").fetchone()
    assert (r1["report_language"], r1["response_format"], r1["template_id"]) == (
        "en",
        "short",
        "auto",
    )
    assert (r1["approved_at"], r1["origin"], r1["status_reason"], r1["tier"]) == (
        "2026-10-02T09:00:00+00:00",
        "session",
        "",
        "light",
    )
    m3 = conn.execute("SELECT * FROM runs WHERE run_id = 'm3'").fetchone()
    assert (m3["approved_at"], m3["report_language"]) == (None, None)


def test_deleting_a_run_removes_its_queries(db: Database, plan: ResearchStore) -> None:
    run_id = approved_run(db)
    plan.insert_drafts(run_id, 1, drafts("a", "b"))
    db.conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
    assert db.conn.execute("SELECT COUNT(*) FROM plan_queries").fetchone()[0] == 0


# ---- runs -----------------------------------------------------------------------------------


def test_an_approved_run_carries_the_session_settings(db: Database, runs: RunStore) -> None:
    run = runs.get_run(approved_run(db))
    assert run is not None
    assert (run.report_language, run.response_format, run.template_id) == ("en", "short", "auto")
    assert (run.approved_at, run.origin, run.status_reason) == (NOW.isoformat(), "session", "")


def test_an_external_run_is_queued_without_a_session(runs: RunStore) -> None:
    run = runs.get_run(external(runs))
    assert run is not None
    assert (run.session_id, run.origin, run.status, run.tier) == (
        None,
        "external",
        "queued",
        "light",
    )
    assert (run.brief_sha256, run.brief_path) == (SHA, "data/briefs/y.md")
    assert (run.template_id, run.response_format, run.report_language) == (
        "auto",
        "structured",
        "de",
    )
    assert run.approved_at == NOW.isoformat()


def test_two_external_runs_of_the_same_brief_are_two_runs(runs: RunStore) -> None:
    assert external(runs) != external(runs)


def test_set_status_with_a_reason(runs: RunStore) -> None:
    run_id = external(runs)
    runs.set_status(run_id, "failed", "LLMTimeoutError: decompose")
    run = runs.get_run(run_id)
    assert run is not None
    assert (run.status, run.status_reason) == ("failed", "LLMTimeoutError: decompose")
    runs.set_status(run_id, "running")
    assert runs.get_run(run_id).status_reason == ""  # type: ignore[union-attr]


def test_set_status_rejects_unknown_runs_and_statuses(runs: RunStore) -> None:
    with pytest.raises(NotFound):
        runs.set_status("r-nope", "running")
    with pytest.raises(ValueError, match="unknown run status"):
        runs.set_status(external(runs), "exploded")  # type: ignore[arg-type]


def test_runs_are_listed_newest_first(db: Database) -> None:
    ticks = itertools.count()
    db.now = lambda: datetime(2026, 10, 4, 8, next(ticks), tzinfo=UTC)
    runs = RunStore(db)
    first, second = external(runs), external(runs)
    assert [r.run_id for r in runs.list_runs()] == [second, first]


# ---- search-plan queries --------------------------------------------------------------------


def test_drafts_get_sequential_ids_in_one_batch(db: Database, plan: ResearchStore) -> None:
    run_id = external(RunStore(db))
    rows = plan.insert_drafts(run_id, 1, drafts("a", "b", "c"))
    assert [r.query_id for r in rows] == ["q001", "q002", "q003"]
    assert all(r.state == "draft" and r.wave == 1 and r.sent == "" for r in rows)
    more = plan.insert_drafts(run_id, 2, drafts("d"))
    assert [(r.query_id, r.wave) for r in more] == [("q004", 2)]
    assert [r.query_id for r in plan.rows(run_id, wave=1)] == ["q001", "q002", "q003"]
    assert len(plan.rows(run_id)) == 4


def test_query_ids_stay_unique_under_two_threads(db: Database, plan: ResearchStore) -> None:
    run_id = external(RunStore(db))
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        for n in range(10):
            plan.add(run_id, "i01", "breadth", "web", f"q{n}")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = [r.query_id for r in plan.rows(run_id)]
    assert ids == [f"q{n:03d}" for n in range(1, 21)]


def test_sanitizing_and_results_are_stored(db: Database, plan: ResearchStore) -> None:
    run_id = external(RunStore(db))
    (row,) = plan.insert_drafts(run_id, 1, drafts("Kosten Rückbau Firma X"))
    plan.set_sanitized(run_id, row.query_id, "Kosten Rückbau", ["Firma X"], "planned")
    done = plan.get(run_id, row.query_id)
    assert (done.sent, done.removed, done.state, done.reason) == (
        "Kosten Rückbau",
        ("Firma X",),
        "planned",
        "",
    )
    hits = [{"url": "https://a.example/1", "title": "A", "snippet": "s", "provider": "tavily"}]
    plan.set_result(run_id, row.query_id, "done", hits)
    assert plan.get(run_id, row.query_id).hits == tuple(hits)
    plan.set_result(run_id, row.query_id, "failed", [], "unavailable")
    failed = plan.get(run_id, row.query_id)
    assert (failed.state, failed.reason, failed.hits) == ("failed", "unavailable", ())


def test_an_edit_returns_a_row_to_draft(db: Database, plan: ResearchStore) -> None:
    run_id = external(RunStore(db))
    (row,) = plan.insert_drafts(run_id, 1, drafts("alt"))
    plan.set_sanitized(run_id, row.query_id, "", [], "blocked", "denylist")
    edited = plan.edit(run_id, row.query_id, "neu")
    assert (edited.original, edited.sent, edited.removed, edited.state, edited.reason) == (
        "neu",
        "",
        (),
        "draft",
        "",
    )


def test_delete_and_add(db: Database, plan: ResearchStore) -> None:
    run_id = external(RunStore(db))
    (row,) = plan.insert_drafts(run_id, 1, drafts("a"))
    plan.delete(run_id, row.query_id)
    assert plan.get(run_id, row.query_id).state == "deleted"
    added = plan.add(run_id, "i02", "adversarial", "web", "Kritik")
    assert (added.query_id, added.item_id, added.lens, added.wave, added.state) == (
        "q002",
        "i02",
        "adversarial",
        1,
        "draft",
    )


def test_unknown_queries_are_not_found(db: Database, plan: ResearchStore) -> None:
    run_id = external(RunStore(db))
    with pytest.raises(NotFound):
        plan.get(run_id, "q001")
    with pytest.raises(NotFound):
        plan.edit(run_id, "q001", "x")
    with pytest.raises(NotFound):
        plan.delete(run_id, "q001")
    with pytest.raises(NotFound):
        plan.set_sanitized(run_id, "q001", "x", [], "planned")
    with pytest.raises(NotFound):
        plan.set_result(run_id, "q001", "done", [])
