"""Approved research runs (PRD M4 AC1): a run exists only for a brief whose hash was approved."""

import secrets
import sqlite3
from dataclasses import dataclass

from app.brief.errors import NotFound, StaleBrief, WrongState
from app.store.db import Database
from app.store.sessions import SessionRow, session_from_row


@dataclass(frozen=True)
class RunRow:
    run_id: str
    session_id: str | None
    brief_sha256: str | None
    brief_path: str | None
    tier: str | None
    summarize_model: str | None
    status: str
    created_at: str
    settings_json: str | None  # report settings of a run without a session (external brief)


RUN_STATUSES = ("queued", "running", "awaiting_plan_approval", "done", "blocked", "failed")
# `created` is what a vault gives a run that no brief approved (tests and tools).
_ALLOWED_STATUS: dict[str, set[str]] = {
    "created": {"queued", "running", "failed"},
    "queued": {"running", "failed"},
    "running": {"awaiting_plan_approval", "done", "blocked", "failed"},
    "awaiting_plan_approval": {"running", "failed"},
    "failed": {"running"},
    "done": set(),
    "blocked": set(),
}


def _run(row: sqlite3.Row) -> RunRow:
    return RunRow(
        run_id=row["run_id"],
        session_id=row["session_id"],
        brief_sha256=row["brief_sha256"],
        brief_path=row["brief_path"],
        tier=row["tier"],
        summarize_model=row["summarize_model"],
        status=row["status"],
        created_at=row["created_at"],
        settings_json=row["settings_json"],
    )


class RunStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get_run(self, run_id: str) -> RunRow | None:
        return self._fetch("run_id = ?", run_id)

    def run_for_session(self, session_id: str) -> RunRow | None:
        return self._fetch("session_id = ?", session_id)

    def set_status(self, run_id: str, status: str) -> None:
        """Move a run to ``status``. Setting the current status again is a no-op; a finished run
        (`done`, `blocked`) never moves again, a `failed` one may run again."""
        if status not in RUN_STATUSES:
            raise WrongState(f"unknown status {status!r}")
        with self._db.tx():
            row = self._db.conn.execute(
                "SELECT status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise NotFound(run_id)
            current = row["status"]
            if current == status:
                return
            if status not in _ALLOWED_STATUS.get(current, set()):
                raise WrongState(f"{current} -> {status}")
            self._db.conn.execute("UPDATE runs SET status = ? WHERE run_id = ?", (status, run_id))

    def set_settings(self, run_id: str, settings_json: str) -> None:
        with self._db.tx():
            self._db.conn.execute(
                "UPDATE runs SET settings_json = ? WHERE run_id = ?", (settings_json, run_id)
            )

    def _fetch(self, where: str, value: str) -> RunRow | None:
        with self._db.lock:
            row = self._db.conn.execute(f"SELECT * FROM runs WHERE {where}", (value,)).fetchone()
        return _run(row) if row else None

    def approve(
        self,
        session_id: str,
        *,
        sha256: str,
        brief_path: str,
        tier: str,
        summarize_model: str | None,
    ) -> RunRow:
        """Create the run of an approved brief, and mark the session approved, in one transaction.

        ``sha256`` must be the hash of the session's current brief and the session must wait at the
        decision point. Approving again with the same hash returns the existing run, so a
        `finalize` that runs twice after a restart makes one run, never two."""
        with self._db.tx():
            row = self._db.conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise NotFound(session_id)
            session = session_from_row(row)
            existing = self._existing(session, sha256)
            if existing is not None:
                return existing
            if session.status != "awaiting_decision":
                raise WrongState(f"session is {session.status}, not awaiting a decision")
            if session.brief_sha256 != sha256:
                raise StaleBrief("the hash does not belong to the current brief")
            return self._create(session_id, sha256, brief_path, tier, summarize_model)

    def _existing(self, session: SessionRow, sha256: str) -> RunRow | None:
        if session.run_id is None:
            return None
        if session.approved_sha256 != sha256:
            raise StaleBrief("the session was approved with a different brief")
        row = self._db.conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (session.run_id,)
        ).fetchone()
        return _run(row)

    def _create(
        self, session_id: str, sha256: str, brief_path: str, tier: str, summarize_model: str | None
    ) -> RunRow:
        now = self._db.now()
        run_id = f"r-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        self._db.conn.execute(
            "INSERT INTO runs (run_id, created_at, label, session_id, brief_sha256, brief_path, "
            "tier, summarize_model, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued')",
            (
                run_id,
                now.isoformat(),
                session_id,
                session_id,
                sha256,
                brief_path,
                tier,
                summarize_model,
            ),
        )
        self._db.conn.execute(
            "UPDATE sessions SET status = 'approved', approved_sha256 = ?, archive_path = ?, "
            "run_id = ?, updated_at = ? WHERE session_id = ?",
            (sha256, brief_path, run_id, now.isoformat(), session_id),
        )
        row = self._db.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return _run(row)
