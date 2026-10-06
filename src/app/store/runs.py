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
    created_by: str | None = None  # id of the API key that created it; None for the CLI
    cancel_requested: bool = False
    pending_plan_sha256: str | None = None  # a plan approved and waiting for the worker


RUN_STATUSES = (
    "awaiting_brief_approval",
    "queued",
    "running",
    "awaiting_plan_approval",
    "done",
    "blocked",
    "failed",
    "cancelled",
)
# `created` is what a vault gives a run that no brief approved (tests and tools).
_ALLOWED_STATUS: dict[str, set[str]] = {
    "awaiting_brief_approval": {"queued", "cancelled"},
    "created": {"queued", "running", "failed"},
    "queued": {"running", "failed", "cancelled"},
    "running": {"awaiting_plan_approval", "done", "blocked", "failed", "cancelled", "queued"},
    "awaiting_plan_approval": {"running", "queued", "failed", "cancelled"},
    "failed": {"running", "queued"},
    "cancelled": {"queued"},
    "done": set(),
    "blocked": set(),
}
# Cancelling a run that does not execute takes effect at once; a running one sets a flag.
_CANCEL_AT_ONCE = ("queued", "awaiting_plan_approval", "awaiting_brief_approval")


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
        created_by=row["created_by"],
        cancel_requested=bool(row["cancel_requested"]),
        pending_plan_sha256=row["pending_plan_sha256"],
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

    def create_external(
        self,
        *,
        brief_sha256: str,
        brief_path: str,
        tier: str,
        settings_json: str,
        status: str = "queued",
        created_by: str | None = None,
    ) -> RunRow:
        """A run for a brief that did not come from a session (an owner's file, an API caller):
        its brief is archived already, its settings are given. It starts `queued`, or
        `awaiting_brief_approval` when the creator may not approve. Every call is a new run."""
        if status not in ("queued", "awaiting_brief_approval"):
            raise WrongState(f"a new run cannot start {status}")
        now = self._db.now()
        run_id = f"r-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        with self._db.tx():
            self._db.conn.execute(
                "INSERT INTO runs (run_id, created_at, label, session_id, brief_sha256, "
                "brief_path, tier, summarize_model, status, settings_json, created_by) "
                "VALUES (?, ?, ?, NULL, ?, ?, ?, NULL, ?, ?, ?)",
                (
                    run_id,
                    now.isoformat(),
                    "external",
                    brief_sha256,
                    brief_path,
                    tier,
                    status,
                    settings_json,
                    created_by,
                ),
            )
        row = self.get_run(run_id)
        assert row is not None
        return row

    def approve_external(self, run_id: str, brief_sha256: str) -> RunRow:
        """Queue a run that waits for its brief to be approved; the hash must be its brief's."""
        with self._db.tx():
            row = self._locked(run_id)
            if row["status"] != "awaiting_brief_approval":
                raise WrongState(f"run is {row['status']}, not awaiting a brief approval")
            if row["brief_sha256"] != brief_sha256:
                raise StaleBrief("the hash does not belong to the run's brief")
            self._db.conn.execute("UPDATE runs SET status = 'queued' WHERE run_id = ?", (run_id,))
        return self._get(run_id)

    def _locked(self, run_id: str) -> sqlite3.Row:
        """The run's row, inside a write transaction; `NotFound` when there is none."""
        row = self._db.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFound(run_id)
        return row

    def _get(self, run_id: str) -> RunRow:
        row = self.get_run(run_id)
        assert row is not None
        return row

    # ---- queue, cancel and deletion (M6) ---------------------------------------------------

    def request_cancel(self, run_id: str) -> str:
        """Cancel a run: at once when it does not execute, else set the flag the worker polls.
        Returns the run's status afterwards. A finished or cancelled run raises `WrongState`."""
        with self._db.tx():
            status = self._locked(run_id)["status"]
            if status in _CANCEL_AT_ONCE:
                self._db.conn.execute(
                    "UPDATE runs SET status = 'cancelled', pending_plan_sha256 = NULL "
                    "WHERE run_id = ?",
                    (run_id,),
                )
                return "cancelled"
            if status != "running":
                raise WrongState(f"run is {status}")
            self._db.conn.execute(
                "UPDATE runs SET cancel_requested = 1 WHERE run_id = ?", (run_id,)
            )
            return status

    def cancel_requested(self, run_id: str) -> bool:
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT cancel_requested FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def clear_cancel(self, run_id: str) -> None:
        with self._db.tx():
            self._db.conn.execute(
                "UPDATE runs SET cancel_requested = 0 WHERE run_id = ?", (run_id,)
            )

    def set_pending_plan(self, run_id: str, sha256: str) -> None:
        """Record an approved plan and queue the run, in one conditional statement: of two
        concurrent approvals only the first changes a row, the second gets `WrongState`."""
        with self._db.tx():
            changed = self._db.conn.execute(
                "UPDATE runs SET pending_plan_sha256 = ?, status = 'queued' WHERE run_id = ? "
                "AND status = 'awaiting_plan_approval' AND pending_plan_sha256 IS NULL",
                (sha256, run_id),
            ).rowcount
            if changed == 0:
                status = self._locked(run_id)["status"]
                raise WrongState(f"run is {status}, its plan cannot be approved")

    def take_pending_plan(self, run_id: str) -> str | None:
        """The approved plan hash waiting for the worker, read and cleared in one transaction."""
        with self._db.tx():
            sha = self._locked(run_id)["pending_plan_sha256"]
            if sha is not None:
                self._db.conn.execute(
                    "UPDATE runs SET pending_plan_sha256 = NULL WHERE run_id = ?", (run_id,)
                )
        return sha

    def next_runnable(self) -> RunRow | None:
        """The run a worker takes next: an orphan left `running` by a dead worker first, else the
        oldest `queued` one (FIFO)."""
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT * FROM runs WHERE status IN ('running', 'queued') "
                "ORDER BY status = 'running' DESC, created_at, rowid LIMIT 1"
            ).fetchone()
        return _run(row) if row else None

    def list_runs(self, limit: int = 100) -> list[RunRow]:
        with self._db.lock:
            rows = self._db.conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_run(r) for r in rows]

    def delete_run(self, run_id: str) -> RunRow:
        """Remove the run row, its vault rows (foreign keys cascade) and the upload rows of its
        session. A running run is refused. Returns the deleted row, for the files to follow."""
        with self._db.tx():
            row = _run(self._locked(run_id))
            if row.status == "running":
                raise WrongState("run is running")
            if row.session_id is not None:
                self._db.conn.execute("DELETE FROM uploads WHERE session_id = ?", (row.session_id,))
            self._db.conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        return row

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
