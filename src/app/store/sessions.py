"""Brief sessions, their uploads and the facts distilled from them (PRD M4).

The conversation itself lives in the LangGraph checkpoint; this is the queryable summary. It also
holds the current brief text and its hash, so an approval can be checked against them in one
transaction (see `runs.RunStore.approve`).
"""

import json
import secrets
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.brief.errors import NotFound, WrongState
from app.brief.render import brief_sha256, canonical_text
from app.store.db import Database

SessionStatus = Literal["interviewing", "awaiting_decision", "saved", "approved"]
UploadKind = Literal["pdf", "docx", "md", "txt"]
UploadStage = Literal["stored", "extracted", "distilled"]
Fact = tuple[str, int]  # (fact, page)

# `approved` is reached only through RunStore.approve, never through set_status.
_ALLOWED: dict[str, set[str]] = {
    "interviewing": {"awaiting_decision"},
    "awaiting_decision": {"saved"},
    "saved": {"awaiting_decision"},
    "approved": set(),
}


class InvalidTransition(WrongState):
    """The session cannot move from its current status to the requested one."""


@dataclass(frozen=True)
class SessionRow:
    session_id: str
    created_at: str
    updated_at: str
    status: SessionStatus
    interview_language: str
    report_language: str | None
    response_format: str | None
    template_id: str | None
    upload_digest: str
    digest_notice: str
    brief_text: str | None
    brief_sha256: str | None
    approved_sha256: str | None
    archive_path: str | None
    run_id: str | None
    created_by: str | None = None  # id of the API key that started it; None for the CLI


@dataclass(frozen=True)
class NewUpload:
    name: str
    kind: UploadKind
    size: int
    pages: int
    sha256: str


@dataclass(frozen=True)
class UploadRow:
    session_id: str
    file_id: str
    name: str
    kind: UploadKind
    size: int
    pages: int
    sha256: str
    stage: UploadStage
    page_texts: tuple[str, ...]
    warnings: tuple[str, ...]
    created_at: str


def session_from_row(row: sqlite3.Row) -> SessionRow:
    return SessionRow(**{key: row[key] for key in row.keys()})  # noqa: SIM118 - sqlite3.Row


def _upload(row: sqlite3.Row) -> UploadRow:
    return UploadRow(
        session_id=row["session_id"],
        file_id=row["file_id"],
        name=row["name"],
        kind=row["kind"],
        size=row["size"],
        pages=row["pages"],
        sha256=row["sha256"],
        stage=row["stage"],
        page_texts=tuple(json.loads(row["pages_json"])),
        warnings=tuple(json.loads(row["warnings_json"])),
        created_at=row["created_at"],
    )


class SessionStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ---- sessions -------------------------------------------------------------------------

    def create(self, interview_language: str, created_by: str | None = None) -> SessionRow:
        session_id = f"s{secrets.token_hex(6)}"
        stamp = self._db.stamp()
        with self._db.tx():
            self._db.conn.execute(
                "INSERT INTO sessions (session_id, created_at, updated_at, status, "
                "interview_language, created_by) VALUES (?, ?, ?, 'interviewing', ?, ?)",
                (session_id, stamp, stamp, interview_language, created_by),
            )
        session = self.get(session_id)
        assert session is not None
        return session

    def get(self, session_id: str) -> SessionRow | None:
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return session_from_row(row) if row else None

    def list_sessions(self) -> list[SessionRow]:
        """All sessions, newest first."""
        with self._db.lock:
            rows = self._db.conn.execute(
                "SELECT * FROM sessions ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
        return [session_from_row(r) for r in rows]

    def delete_session(self, session_id: str) -> bool:
        """Remove a session with its uploads and parts (for one that never got going). An approved
        session belongs to a run and is never deleted. Returns whether a session was removed."""
        with self._db.tx():
            row = self._db.conn.execute(
                "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return False
            if row["status"] == "approved":
                raise WrongState("an approved session belongs to a run and cannot be deleted")
            self._db.conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            return True

    def _update(self, session_id: str, assignments: str, params: Sequence[object]) -> None:
        with self._db.tx():
            done = self._db.conn.execute(
                f"UPDATE sessions SET {assignments}, updated_at = ? WHERE session_id = ?",
                (*params, self._db.stamp(), session_id),
            )
            if done.rowcount == 0:
                raise NotFound(session_id)

    def set_status(self, session_id: str, status: SessionStatus) -> None:
        with self._db.tx():
            row = self._db.conn.execute(
                "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise NotFound(session_id)
            if status not in _ALLOWED[row["status"]]:
                raise InvalidTransition(f"{row['status']} -> {status}")
            self._db.conn.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE session_id = ?",
                (status, self._db.stamp(), session_id),
            )

    def set_settings(
        self, session_id: str, report_language: str, response_format: str, template_id: str
    ) -> None:
        self._update(
            session_id,
            "report_language = ?, response_format = ?, template_id = ?",
            (report_language, response_format, template_id),
        )

    def set_digest(
        self, session_id: str, digest: str, notice: str, distilled: Sequence[str] = ()
    ) -> None:
        """Store the digest and, in the same transaction, move the files it now covers
        (``distilled``, file ids) `extracted` → `distilled`."""
        with self._db.tx():
            done = self._db.conn.execute(
                "UPDATE sessions SET upload_digest = ?, digest_notice = ?, updated_at = ? "
                "WHERE session_id = ?",
                (digest, notice, self._db.stamp(), session_id),
            )
            if done.rowcount == 0:
                raise NotFound(session_id)
            self._db.conn.executemany(
                "UPDATE uploads SET stage = 'distilled' "
                "WHERE session_id = ? AND file_id = ? AND stage = 'extracted'",
                [(session_id, file_id) for file_id in distilled],
            )

    def set_brief(self, session_id: str, text: str) -> str:
        """Store the current brief in canonical form; returns its approval hash."""
        canonical = canonical_text(text)
        sha = brief_sha256(canonical)
        self._update(session_id, "brief_text = ?, brief_sha256 = ?", (canonical, sha))
        return sha

    # ---- uploads --------------------------------------------------------------------------

    def add_uploads(self, session_id: str, files: Sequence[NewUpload]) -> list[UploadRow]:
        """Record a batch of uploads; all rows or none."""
        stamp = self._db.stamp()
        with self._db.tx():
            if (
                self._db.conn.execute(
                    "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
                ).fetchone()
                is None
            ):
                raise NotFound(session_id)
            first = self._db.conn.execute(
                "SELECT COALESCE(MAX(CAST(SUBSTR(file_id, 2) AS INTEGER)), 0) + 1 "
                "FROM uploads WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            ids = [f"f{first + n:02d}" for n in range(len(files))]
            for file_id, new in zip(ids, files, strict=True):
                self._db.conn.execute(
                    "INSERT INTO uploads (session_id, file_id, name, kind, size, pages, sha256, "
                    "stage, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'stored', ?)",
                    (
                        session_id,
                        file_id,
                        new.name,
                        new.kind,
                        new.size,
                        new.pages,
                        new.sha256,
                        stamp,
                    ),
                )
        by_id = {row.file_id: row for row in self.uploads(session_id)}
        return [by_id[file_id] for file_id in ids]

    def uploads(self, session_id: str) -> list[UploadRow]:
        with self._db.lock:
            rows = self._db.conn.execute(
                "SELECT * FROM uploads WHERE session_id = ? ORDER BY file_id", (session_id,)
            ).fetchall()
        return [_upload(r) for r in rows]

    def _advance(self, sql: str, params: Sequence[object]) -> bool:
        with self._db.tx():
            return self._db.conn.execute(sql, params).rowcount > 0

    def save_pages(
        self, session_id: str, file_id: str, pages: Sequence[str], warnings: Sequence[str]
    ) -> bool:
        """Store the extracted text and move `stored` → `extracted`. False if it already moved."""
        return self._advance(
            "UPDATE uploads SET pages_json = ?, warnings_json = ?, stage = 'extracted' "
            "WHERE session_id = ? AND file_id = ? AND stage = 'stored'",
            (json.dumps(list(pages)), json.dumps(list(warnings)), session_id, file_id),
        )

    def add_part(self, session_id: str, file_id: str, part: int, facts: Sequence[Fact]) -> None:
        """Commit one distilled part. The first result stays; a re-run changes nothing."""
        with self._db.tx():
            self._db.conn.execute(
                "INSERT OR IGNORE INTO upload_parts (session_id, file_id, part, facts_json) "
                "VALUES (?, ?, ?, ?)",
                (session_id, file_id, part, json.dumps([list(f) for f in facts])),
            )

    def parts(self, session_id: str, file_id: str) -> dict[int, list[Fact]]:
        with self._db.lock:
            rows = self._db.conn.execute(
                "SELECT part, facts_json FROM upload_parts "
                "WHERE session_id = ? AND file_id = ? ORDER BY part",
                (session_id, file_id),
            ).fetchall()
        return {r["part"]: [(f[0], f[1]) for f in json.loads(r["facts_json"])] for r in rows}
