"""API keys (PRD M6): a key is shown once; the database holds only its sha256."""

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass

from app.brief.errors import NotFound
from app.store.db import Database

KEY_PREFIX = "udr_"


@dataclass(frozen=True)
class ApiKey:
    key_id: str
    name: str
    self_approve: bool
    created_at: str
    revoked_at: str | None = None


def _digest(key_text: str) -> str:
    return hashlib.sha256(key_text.encode()).hexdigest()


class KeyStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, name: str, *, self_approve: bool) -> tuple[ApiKey, str]:
        """A new key and its text; the text is returned here and never again."""
        text = KEY_PREFIX + secrets.token_urlsafe(32)
        key_id = f"k-{secrets.token_hex(4)}"
        created = self._db.stamp()
        with self._db.tx():
            self._db.conn.execute(
                "INSERT INTO api_keys (key_id, name, key_sha256, self_approve, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (key_id, name, _digest(text), int(self_approve), created),
            )
        return ApiKey(key_id, name, self_approve, created), text

    def verify(self, key_text: str) -> ApiKey | None:
        """The key behind ``key_text``; `None` for an unknown or revoked one."""
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT * FROM api_keys WHERE key_sha256 = ? AND revoked_at IS NULL",
                (_digest(key_text),),
            ).fetchone()
        return _key(row) if row else None

    def list(self) -> list[ApiKey]:
        with self._db.lock:
            rows = self._db.conn.execute("SELECT * FROM api_keys ORDER BY created_at, rowid")
            return [_key(r) for r in rows.fetchall()]

    def revoke(self, key_id: str) -> None:
        with self._db.tx():
            changed = self._db.conn.execute(
                "UPDATE api_keys SET revoked_at = COALESCE(revoked_at, ?) WHERE key_id = ?",
                (self._db.stamp(), key_id),
            ).rowcount
        if changed == 0:
            raise NotFound(key_id)


def _key(row: sqlite3.Row) -> ApiKey:
    return ApiKey(
        key_id=row["key_id"],
        name=row["name"],
        self_approve=bool(row["self_approve"]),
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
    )
