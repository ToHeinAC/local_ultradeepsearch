"""Approved research runs (PRD M4 AC1): a run exists only for a brief whose hash was approved."""

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, get_args

from app.brief.errors import NotFound, StaleBrief, WrongState
from app.store.db import Database
from app.store.sessions import SessionRow, session_from_row

# `created` is a run the M3 vault made without an approval; M6 adds `cancelled`.
RunStatus = Literal["queued", "running", "awaiting_plan_approval", "done", "blocked", "failed"]


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
    report_language: str | None
    response_format: str | None
    template_id: str | None
    approved_at: str | None
    origin: str  # "session" (approved in Phase 1) or "external" (`udr run --brief`)
    status_reason: str


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
        report_language=row["report_language"],
        response_format=row["response_format"],
        template_id=row["template_id"],
        approved_at=row["approved_at"],
        origin=row["origin"],
        status_reason=row["status_reason"],
    )


class RunStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get_run(self, run_id: str) -> RunRow | None:
        return self._fetch("run_id = ?", run_id)

    def run_for_session(self, session_id: str) -> RunRow | None:
        return self._fetch("session_id = ?", session_id)

    def list_runs(self) -> list[RunRow]:
        """All runs, newest first."""
        with self._db.lock:
            rows = self._db.conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
        return [_run(r) for r in rows]

    def set_status(self, run_id: str, status: RunStatus, reason: str = "") -> None:
        if status not in get_args(RunStatus):
            raise ValueError(f"unknown run status {status!r}")
        with self._db.tx():
            done = self._db.conn.execute(
                "UPDATE runs SET status = ?, status_reason = ? WHERE run_id = ?",
                (status, reason, run_id),
            )
            if done.rowcount == 0:
                raise NotFound(run_id)

    def create_external_run(
        self,
        *,
        sha256: str,
        brief_path: str,
        tier: str,
        template_id: str,
        response_format: str,
        report_language: str,
        approved_at: datetime,
    ) -> RunRow:
        """A queued run of a brief supplied from outside Phase 1 (`udr run --brief`)."""
        with self._db.tx():
            run_id = self._new_id()
            self._db.conn.execute(
                "INSERT INTO runs (run_id, created_at, label, brief_sha256, brief_path, tier, "
                "status, report_language, response_format, template_id, approved_at, origin) "
                "VALUES (?, ?, '', ?, ?, ?, 'queued', ?, ?, ?, ?, 'external')",
                (
                    run_id,
                    self._db.stamp(),
                    sha256,
                    brief_path,
                    tier,
                    report_language,
                    response_format,
                    template_id,
                    approved_at.isoformat(),
                ),
            )
            return self._by_id(run_id)

    def _new_id(self) -> str:
        return f"r-{self._db.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"

    def _by_id(self, run_id: str) -> RunRow:
        return _run(
            self._db.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
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
            return self._create(session, sha256, brief_path, tier, summarize_model)

    def _existing(self, session: SessionRow, sha256: str) -> RunRow | None:
        if session.run_id is None:
            return None
        if session.approved_sha256 != sha256:
            raise StaleBrief("the session was approved with a different brief")
        return self._by_id(session.run_id)

    def _create(
        self,
        session: SessionRow,
        sha256: str,
        brief_path: str,
        tier: str,
        summarize_model: str | None,
    ) -> RunRow:
        """The run copies the session's settings; `approved_at` is its creation time."""
        now = self._db.now()
        run_id = self._new_id()
        session_id = session.session_id
        self._db.conn.execute(
            "INSERT INTO runs (run_id, created_at, label, session_id, brief_sha256, brief_path, "
            "tier, summarize_model, status, report_language, response_format, template_id, "
            "approved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
            (
                run_id,
                now.isoformat(),
                session_id,
                session_id,
                sha256,
                brief_path,
                tier,
                summarize_model,
                session.report_language,
                session.response_format,
                session.template_id,
                now.isoformat(),
            ),
        )
        self._db.conn.execute(
            "UPDATE sessions SET status = 'approved', approved_sha256 = ?, archive_path = ?, "
            "run_id = ?, updated_at = ? WHERE session_id = ?",
            (sha256, brief_path, run_id, now.isoformat(), session_id),
        )
        return self._by_id(run_id)
