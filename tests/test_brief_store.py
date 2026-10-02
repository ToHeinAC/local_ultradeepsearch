import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.brief.archive import archive_brief, write_draft
from app.brief.errors import NotFound, StaleBrief, WrongState
from app.brief.render import brief_sha256, canonical_text
from app.store.db import MIGRATIONS, Database, connect, migrate
from app.store.runs import RunStore
from app.store.sessions import InvalidTransition, NewUpload, SessionStore
from app.store.vault import Vault

NOW = datetime(2026, 10, 2, 9, 30, 15, tzinfo=UTC)
BRIEF = "# Wie teuer ist der Rückbau?\n\n## Forschungsfragen\n\n1. Kosten\n"


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "data" / "udr.sqlite", now=lambda: NOW)


@pytest.fixture
def sessions(db: Database) -> SessionStore:
    return SessionStore(db)


@pytest.fixture
def runs(db: Database) -> RunStore:
    return RunStore(db)


def upload(name: str = "a.pdf", **overrides: object) -> NewUpload:
    base: dict[str, object] = {
        "name": name,
        "kind": "pdf",
        "size": 1234,
        "pages": 3,
        "sha256": "ab" * 32,
    }
    return NewUpload(**{**base, **overrides})  # type: ignore[arg-type]


def ready(sessions: SessionStore, text: str = BRIEF) -> str:
    """A session that has a draft and waits for the owner's decision."""
    session = sessions.create("de")
    sessions.set_brief(session.session_id, text)
    sessions.set_status(session.session_id, "awaiting_decision")
    return session.session_id


# ---- migration 2 ----------------------------------------------------------------------------


def test_migration_2_keeps_the_rows_of_an_m3_database(tmp_path: Path) -> None:
    path = tmp_path / "udr.sqlite"
    conn = connect(path)
    migrate(conn, MIGRATIONS[:1])  # a database as M3 left it
    conn.execute("INSERT INTO runs (run_id, created_at, label) VALUES ('old', 'then', 'kept')")
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS) >= 2
    row = conn.execute("SELECT * FROM runs WHERE run_id = 'old'").fetchone()
    assert (row["label"], row["status"], row["session_id"], row["brief_sha256"]) == (
        "kept",
        "created",
        None,
        None,
    )
    vault = Vault(path, "old")  # the vault still works on the migrated database
    assert vault.notes() == []


def test_the_new_tables_exist(db: Database) -> None:
    names = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "uploads", "upload_parts"} <= names


# ---- sessions -------------------------------------------------------------------------------


def test_a_new_session_is_interviewing_with_nothing_chosen(sessions: SessionStore) -> None:
    session = sessions.create("fr")
    assert session.session_id.startswith("s")
    assert len(session.session_id) == 13
    assert (session.status, session.interview_language) == ("interviewing", "fr")
    assert (session.report_language, session.response_format, session.template_id) == (None,) * 3
    assert (session.brief_text, session.brief_sha256, session.run_id) == (None,) * 3
    assert session.created_at == session.updated_at == NOW.isoformat()
    assert sessions.get(session.session_id) == session


def test_session_ids_are_unique(sessions: SessionStore) -> None:
    assert len({sessions.create("de").session_id for _ in range(50)}) == 50


def test_unknown_sessions(sessions: SessionStore) -> None:
    assert sessions.get("s000000000000") is None
    with pytest.raises(NotFound):
        sessions.set_status("s000000000000", "awaiting_decision")


def test_sessions_are_listed_newest_first(db: Database) -> None:
    times = iter(datetime(2026, 10, 2, 9, minute, tzinfo=UTC) for minute in range(10))
    store = SessionStore(Database(db.path, now=lambda: next(times)))
    first, second = store.create("de"), store.create("en")
    assert [s.session_id for s in store.list_sessions()] == [second.session_id, first.session_id]


def walk(sessions: SessionStore, session_id: str, path: tuple[str, ...]) -> None:
    for status in path:
        sessions.set_status(session_id, status)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        (("awaiting_decision",), True),
        (("awaiting_decision", "saved", "awaiting_decision"), True),
        (("saved",), False),
        (("approved",), False),  # only RunStore.approve may set it
        (("awaiting_decision", "approved"), False),
        (("awaiting_decision", "interviewing"), False),
        (("awaiting_decision", "awaiting_decision"), False),
    ],
)
def test_status_transitions(sessions: SessionStore, path: tuple[str, ...], ok: bool) -> None:
    session_id = sessions.create("de").session_id
    if ok:
        walk(sessions, session_id, path)
        assert sessions.get(session_id).status == path[-1]  # type: ignore[union-attr]
    else:
        with pytest.raises(InvalidTransition):
            walk(sessions, session_id, path)


def test_an_approved_session_cannot_change_status(sessions: SessionStore, runs: RunStore) -> None:
    session_id = ready(sessions)
    approve(runs, session_id, brief_sha256(BRIEF))
    for status in ("interviewing", "awaiting_decision", "saved"):
        with pytest.raises(InvalidTransition):
            sessions.set_status(session_id, status)  # type: ignore[arg-type]


def test_settings_digest_and_brief_are_stored(sessions: SessionStore) -> None:
    session_id = sessions.create("de").session_id
    sessions.set_settings(session_id, "en", "short", "auto")
    sessions.set_digest(session_id, "- Fakt (a.pdf, S. 1)", "gekürzt")
    sha = sessions.set_brief(session_id, "# T\r\n\r\n1. Q\r\n")
    row = sessions.get(session_id)
    assert row is not None
    assert (row.report_language, row.response_format, row.template_id) == ("en", "short", "auto")
    assert (row.upload_digest, row.digest_notice) == ("- Fakt (a.pdf, S. 1)", "gekürzt")
    assert row.brief_text == "# T\n\n1. Q\n"  # canonical
    assert row.brief_sha256 == sha == brief_sha256("# T\n\n1. Q\n")


def test_updating_a_session_moves_updated_at(db: Database) -> None:
    clock = [datetime(2026, 10, 2, 9, 0, tzinfo=UTC)]
    store = SessionStore(Database(db.path, now=lambda: clock[0]))
    session_id = store.create("de").session_id
    clock[0] = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)
    store.set_digest(session_id, "x", "")
    row = store.get(session_id)
    assert row is not None
    assert (row.created_at, row.updated_at) == (
        "2026-10-02T09:00:00+00:00",
        "2026-10-02T10:00:00+00:00",
    )


# ---- uploads --------------------------------------------------------------------------------


def test_uploads_get_sequential_ids_and_start_stored(sessions: SessionStore) -> None:
    session_id = sessions.create("de").session_id
    rows = sessions.add_uploads(session_id, [upload("a.pdf"), upload("b.docx", kind="docx")])
    more = sessions.add_uploads(session_id, [upload("c.md", kind="md")])
    assert [r.file_id for r in rows + more] == ["f01", "f02", "f03"]
    assert {r.stage for r in rows + more} == {"stored"}
    assert [r.name for r in sessions.uploads(session_id)] == ["a.pdf", "b.docx", "c.md"]


def test_a_batch_of_uploads_is_all_or_nothing(sessions: SessionStore) -> None:
    session_id = sessions.create("de").session_id
    sessions.add_uploads(session_id, [upload("ok.pdf")])
    with pytest.raises(sqlite3.IntegrityError):
        sessions.add_uploads(session_id, [upload("fine.pdf"), upload("bad.exe", kind="exe")])
    assert [r.name for r in sessions.uploads(session_id)] == ["ok.pdf"]
    assert sessions.add_uploads(session_id, [upload("next.pdf")])[0].file_id == "f02"


def test_uploads_of_an_unknown_session_are_rejected(sessions: SessionStore) -> None:
    with pytest.raises(NotFound):
        sessions.add_uploads("s000000000000", [upload()])


def test_extraction_results_are_saved_once(sessions: SessionStore) -> None:
    session_id = sessions.create("de").session_id
    (row,) = sessions.add_uploads(session_id, [upload()])
    assert sessions.save_pages(session_id, row.file_id, ["Seite 1", "Seite 2"], ["ocr_unavailable"])
    assert not sessions.save_pages(session_id, row.file_id, ["anders"], [])  # already extracted
    (saved,) = sessions.uploads(session_id)
    assert (saved.stage, saved.page_texts, saved.warnings) == (
        "extracted",
        ("Seite 1", "Seite 2"),
        ("ocr_unavailable",),
    )


def test_parts_are_idempotent_and_ordered(sessions: SessionStore) -> None:
    session_id = sessions.create("de").session_id
    (row,) = sessions.add_uploads(session_id, [upload()])
    sessions.save_pages(session_id, row.file_id, ["x"], [])
    sessions.add_part(session_id, row.file_id, 1, [("Fakt B", 2)])
    sessions.add_part(session_id, row.file_id, 0, [("Fakt A", 1)])
    sessions.add_part(session_id, row.file_id, 0, [("anders", 9)])  # a re-run changes nothing
    parts = sessions.parts(session_id, row.file_id)
    assert parts == {0: [("Fakt A", 1)], 1: [("Fakt B", 2)]}
    assert list(parts) == [0, 1]  # in part order, whatever the insertion order


def test_a_file_is_distilled_only_after_extraction(sessions: SessionStore) -> None:
    session_id = sessions.create("de").session_id
    (row,) = sessions.add_uploads(session_id, [upload()])
    assert not sessions.mark_distilled(session_id, row.file_id)  # still `stored`
    sessions.save_pages(session_id, row.file_id, ["x"], [])
    assert sessions.mark_distilled(session_id, row.file_id)
    assert sessions.uploads(session_id)[0].stage == "distilled"
    assert not sessions.mark_distilled(session_id, row.file_id)


def test_delete_session_removes_it_with_everything_it_owns(
    db: Database, sessions: SessionStore
) -> None:
    keep = sessions.create("en").session_id
    gone = sessions.create("de").session_id
    (row,) = sessions.add_uploads(gone, [upload()])
    sessions.save_pages(gone, row.file_id, ["x"], [])
    sessions.add_part(gone, row.file_id, 0, [("Fakt", 1)])
    assert sessions.delete_session(gone) is True
    assert sessions.get(gone) is None
    assert sessions.get(keep) is not None
    assert db.conn.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM upload_parts").fetchone()[0] == 0
    assert sessions.delete_session(gone) is False  # already gone


def test_an_approved_session_cannot_be_deleted(sessions: SessionStore, runs: RunStore) -> None:
    session_id = ready(sessions)
    approve(runs, session_id, brief_sha256(BRIEF))
    with pytest.raises(WrongState):
        sessions.delete_session(session_id)
    assert sessions.get(session_id) is not None


def test_deleting_a_session_removes_its_uploads_and_parts(
    db: Database, sessions: SessionStore
) -> None:
    session_id = sessions.create("de").session_id
    (row,) = sessions.add_uploads(session_id, [upload()])
    sessions.save_pages(session_id, row.file_id, ["x"], [])
    sessions.add_part(session_id, row.file_id, 0, [("Fakt", 1)])
    db.conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
    assert db.conn.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM upload_parts").fetchone()[0] == 0


# ---- approval and runs (M4 AC1) -------------------------------------------------------------


def approve(runs: RunStore, session_id: str, sha: str, **overrides: object) -> object:
    params: dict[str, object] = {
        "sha256": sha,
        "brief_path": "data/briefs/2026-10-02T09-30-15Z.md",
        "tier": "light",
        "summarize_model": None,
    }
    return runs.approve(session_id, **{**params, **overrides})  # type: ignore[arg-type]


def test_approving_the_current_hash_creates_a_queued_run(
    sessions: SessionStore, runs: RunStore
) -> None:
    session_id = ready(sessions)
    run = runs.approve(
        session_id,
        sha256=brief_sha256(BRIEF),
        brief_path="data/briefs/x.md",
        tier="full",
        summarize_model="gemma4:e2b",
    )
    assert (run.session_id, run.brief_sha256, run.tier) == (session_id, brief_sha256(BRIEF), "full")
    assert (run.brief_path, run.summarize_model, run.status) == (
        "data/briefs/x.md",
        "gemma4:e2b",
        "queued",
    )
    assert runs.get_run(run.run_id) == run
    assert runs.run_for_session(session_id) == run
    row = sessions.get(session_id)
    assert row is not None
    assert (row.status, row.run_id, row.approved_sha256, row.archive_path) == (
        "approved",
        run.run_id,
        brief_sha256(BRIEF),
        "data/briefs/x.md",
    )


def test_run_ids_are_valid_directory_names(sessions: SessionStore, runs: RunStore) -> None:
    from app.bootstrap import RUN_ID

    run = approve(runs, ready(sessions), brief_sha256(BRIEF))
    assert RUN_ID.fullmatch(run.run_id)  # type: ignore[attr-defined]


def test_a_stale_hash_creates_no_run(sessions: SessionStore, runs: RunStore, db: Database) -> None:
    session_id = ready(sessions)
    with pytest.raises(StaleBrief):
        approve(runs, session_id, brief_sha256("# Etwas anderes\n\n1. Q\n"))
    with pytest.raises(StaleBrief):
        approve(runs, session_id, "0" * 64)
    assert db.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    row = sessions.get(session_id)
    assert row is not None
    assert (row.status, row.run_id) == ("awaiting_decision", None)


def test_an_edit_makes_the_old_hash_stale(sessions: SessionStore, runs: RunStore) -> None:
    session_id = ready(sessions)
    old = brief_sha256(BRIEF)
    new = sessions.set_brief(session_id, BRIEF + "2. Dauer\n")
    assert new != old
    with pytest.raises(StaleBrief):
        approve(runs, session_id, old)
    assert approve(runs, session_id, new)  # the new hash approves


def test_only_a_session_at_the_decision_point_can_be_approved(
    sessions: SessionStore, runs: RunStore
) -> None:
    interviewing = sessions.create("de")
    sessions.set_brief(interviewing.session_id, BRIEF)
    with pytest.raises(WrongState):
        approve(runs, interviewing.session_id, brief_sha256(BRIEF))
    parked = ready(sessions)
    sessions.set_status(parked, "saved")
    with pytest.raises(WrongState):
        approve(runs, parked, brief_sha256(BRIEF))


def test_an_unknown_session_cannot_be_approved(runs: RunStore) -> None:
    with pytest.raises(NotFound):
        approve(runs, "s000000000000", "0" * 64)


def test_approving_again_returns_the_same_run_after_a_crash(
    sessions: SessionStore, runs: RunStore, db: Database
) -> None:
    """`finalize` may run twice after a restart: one run, never two."""
    session_id = ready(sessions)
    first = approve(runs, session_id, brief_sha256(BRIEF))
    again = approve(runs, session_id, brief_sha256(BRIEF))
    assert again == first
    assert db.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_a_different_hash_after_approval_is_stale(sessions: SessionStore, runs: RunStore) -> None:
    session_id = ready(sessions)
    approve(runs, session_id, brief_sha256(BRIEF))
    with pytest.raises(StaleBrief):
        approve(runs, session_id, "f" * 64)


def test_an_unknown_run_is_none(runs: RunStore) -> None:
    assert runs.get_run("nope") is None
    assert runs.run_for_session("s000000000000") is None


def test_the_vault_can_use_an_approved_run(
    sessions: SessionStore, runs: RunStore, db: Database
) -> None:
    run = approve(runs, ready(sessions), brief_sha256(BRIEF))
    vault = Vault(db.path, run.run_id)  # type: ignore[attr-defined]
    assert vault.notes() == []
    assert runs.get_run(run.run_id).status == "queued"  # type: ignore[union-attr]


# ---- archive (M4 AC2) -----------------------------------------------------------------------


def test_the_archive_holds_exactly_the_approved_bytes(tmp_path: Path) -> None:
    text = "# Über <think>x</think>\r\n\r\n1. Ä ß 😀\r\n"
    path = archive_brief(tmp_path / "briefs", text, NOW)
    assert path.name == "2026-10-02T09-30-15Z.md"
    assert path.read_bytes() == canonical_text(text).encode("utf-8")
    assert b"<think>x</think>" in path.read_bytes()  # never scrubbed
    assert not path.read_bytes().startswith(b"---")  # no front matter


def test_the_archive_is_read_only(tmp_path: Path) -> None:
    path = archive_brief(tmp_path, BRIEF, NOW)
    assert stat.S_IMODE(path.stat().st_mode) == 0o444


def test_archiving_the_same_bytes_again_returns_the_same_file(tmp_path: Path) -> None:
    first = archive_brief(tmp_path, BRIEF, NOW)
    assert archive_brief(tmp_path, BRIEF, NOW) == first
    assert [p.name for p in tmp_path.iterdir()] == [first.name]


def test_a_different_brief_in_the_same_second_gets_a_suffix(tmp_path: Path) -> None:
    first = archive_brief(tmp_path, BRIEF, NOW)
    second = archive_brief(tmp_path, BRIEF + "2. Mehr\n", NOW)
    third = archive_brief(tmp_path, BRIEF + "3. Noch mehr\n", NOW)
    assert [p.name for p in (first, second, third)] == [
        "2026-10-02T09-30-15Z.md",
        "2026-10-02T09-30-15Z-2.md",
        "2026-10-02T09-30-15Z-3.md",
    ]
    assert first.read_bytes() == BRIEF.encode()  # the first is untouched
    assert archive_brief(tmp_path, BRIEF + "2. Mehr\n", NOW) == second  # found again by content


def test_a_failed_archive_leaves_no_file_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "link", broken)
    with pytest.raises(OSError, match="disk full"):
        archive_brief(tmp_path, BRIEF, NOW)
    assert list(tmp_path.iterdir()) == []  # neither a final file nor a half-written temp file


def test_the_archive_directory_is_created(tmp_path: Path) -> None:
    path = archive_brief(tmp_path / "a" / "b", BRIEF, NOW)
    assert path.parent == tmp_path / "a" / "b"


def test_a_draft_keeps_the_text_verbatim(tmp_path: Path) -> None:
    text = "# Über <think>bleibt</think>\r\n\r\n1. Q\r\n"
    path = write_draft(tmp_path, "s0123456789ab", text)
    assert path.read_bytes() == "# Über <think>bleibt</think>\n\n1. Q\n".encode()


def test_a_draft_is_written_per_session_and_overwritten(tmp_path: Path) -> None:
    path = write_draft(tmp_path / "drafts", "s0123456789ab", "# Erster\n\n1. Q\n")
    assert path == tmp_path / "drafts" / "s0123456789ab.md"
    write_draft(tmp_path / "drafts", "s0123456789ab", "# Zweiter\n\n1. Q\n")
    assert path.read_text(encoding="utf-8") == "# Zweiter\n\n1. Q\n"
    assert stat.S_IMODE(path.stat().st_mode) != 0o444  # drafts stay writable


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "s0123", ".hidden"])
def test_a_draft_name_cannot_escape_the_directory(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError, match="session id"):
        write_draft(tmp_path, bad, BRIEF)


# ---- transactions and concurrency -----------------------------------------------------------


class Crash(BaseException):
    """Like a KeyboardInterrupt or a simulated power cut: no `except Exception` may swallow it."""


def crash_inside_a_transaction(db: Database, session_id: str) -> None:
    with db.tx():
        db.conn.execute(
            "UPDATE sessions SET upload_digest = 'half' WHERE session_id = ?", (session_id,)
        )
        raise Crash


def test_a_crash_inside_a_transaction_rolls_everything_back(
    db: Database, sessions: SessionStore
) -> None:
    session_id = sessions.create("de").session_id
    with pytest.raises(Crash):
        crash_inside_a_transaction(db, session_id)
    row = sessions.get(session_id)
    assert row is not None
    assert row.upload_digest == ""
    sessions.set_digest(session_id, "after", "")  # the connection is usable again
    assert sessions.get(session_id).upload_digest == "after"  # type: ignore[union-attr]


def test_concurrent_approvals_of_one_session_make_exactly_one_run(
    sessions: SessionStore, runs: RunStore, db: Database
) -> None:
    import threading

    session_id = ready(sessions)
    sha = brief_sha256(BRIEF)
    barrier = threading.Barrier(8)
    results: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            results.append(approve(runs, session_id, sha).run_id)  # type: ignore[attr-defined]
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(set(results)) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
