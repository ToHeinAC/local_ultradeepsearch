"""The search plan of a run (PRD M5, step 2.1 and step 2): one row per query, wave 1 and wave 2.

A row moves `draft → planned | blocked` when it is sanitized and `planned → done | failed` when it
is searched; an edit returns it to `draft`, a delete marks it `deleted`. Query ids (`q001`, ...)
are allocated inside the write transaction, so two writers never get the same id.
"""

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.brief.errors import NotFound
from app.store.db import Database

Lens = Literal["breadth", "depth", "adversarial", "period"]
Channel = Literal["web", "scholarly"]
QueryState = Literal["draft", "planned", "blocked", "deleted", "done", "failed"]


@dataclass(frozen=True)
class QueryDraft:
    item_id: str
    lens: Lens
    channel: Channel
    original: str  # as drafted or typed; stays local, only `sent` leaves the machine


@dataclass(frozen=True)
class QueryRow:
    run_id: str
    query_id: str
    wave: int
    item_id: str
    lens: str
    channel: str
    original: str
    sent: str
    removed: tuple[str, ...]
    state: str
    reason: str  # why blocked or failed
    hits: tuple[dict[str, Any], ...]


def _query(row: sqlite3.Row) -> QueryRow:
    return QueryRow(
        run_id=row["run_id"],
        query_id=row["query_id"],
        wave=row["wave"],
        item_id=row["item_id"],
        lens=row["lens"],
        channel=row["channel"],
        original=row["original"],
        sent=row["sent"],
        removed=tuple(json.loads(row["removed_json"])),
        state=row["state"],
        reason=row["reason"],
        hits=tuple(json.loads(row["hits_json"])),
    )


class ResearchStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _insert(self, run_id: str, wave: int, drafts: Sequence[QueryDraft]) -> list[str]:
        """Insert inside an open transaction; returns the new ids."""
        first = self._db.conn.execute(
            "SELECT COALESCE(MAX(CAST(SUBSTR(query_id, 2) AS INTEGER)), 0) + 1 "
            "FROM plan_queries WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
        ids = [f"q{first + n:03d}" for n in range(len(drafts))]
        self._db.conn.executemany(
            "INSERT INTO plan_queries (run_id, query_id, wave, item_id, lens, channel, original, "
            "state) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft')",
            [
                (run_id, qid, wave, d.item_id, d.lens, d.channel, d.original)
                for qid, d in zip(ids, drafts, strict=True)
            ],
        )
        return ids

    def insert_drafts(self, run_id: str, wave: int, drafts: Sequence[QueryDraft]) -> list[QueryRow]:
        """Insert a batch of drafts in one transaction."""
        with self._db.tx():
            ids = self._insert(run_id, wave, drafts)
        return [self.get(run_id, qid) for qid in ids]

    def add(
        self, run_id: str, item_id: str, lens: Lens, channel: Channel, original: str
    ) -> QueryRow:
        """One more wave-1 draft (the owner added a query to the plan)."""
        return self.insert_drafts(run_id, 1, [QueryDraft(item_id, lens, channel, original)])[0]

    def rows(self, run_id: str, *, wave: int | None = None) -> list[QueryRow]:
        sql = "SELECT * FROM plan_queries WHERE run_id = ?"
        params: tuple[object, ...] = (run_id,)
        if wave is not None:
            sql += " AND wave = ?"
            params = (run_id, wave)
        with self._db.lock:
            rows = self._db.conn.execute(f"{sql} ORDER BY query_id", params).fetchall()
        return [_query(r) for r in rows]

    def get(self, run_id: str, query_id: str) -> QueryRow:
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT * FROM plan_queries WHERE run_id = ? AND query_id = ?", (run_id, query_id)
            ).fetchone()
        if row is None:
            raise NotFound(f"{run_id}/{query_id}")
        return _query(row)

    def _set(self, run_id: str, query_id: str, assignments: str, params: Sequence[object]) -> None:
        with self._db.tx():
            done = self._db.conn.execute(
                f"UPDATE plan_queries SET {assignments} WHERE run_id = ? AND query_id = ?",
                (*params, run_id, query_id),
            )
            if done.rowcount == 0:
                raise NotFound(f"{run_id}/{query_id}")

    def set_sanitized(
        self,
        run_id: str,
        query_id: str,
        sent: str,
        removed: Sequence[str],
        state: Literal["planned", "blocked"],
        reason: str = "",
    ) -> None:
        self._set(
            run_id,
            query_id,
            "sent = ?, removed_json = ?, state = ?, reason = ?",
            (sent, json.dumps(list(removed), ensure_ascii=False), state, reason),
        )

    def set_result(
        self,
        run_id: str,
        query_id: str,
        state: Literal["done", "failed"],
        hits: Sequence[dict[str, Any]],
        reason: str = "",
    ) -> None:
        """The state and the hits of a search, in one statement (AD10: never searched twice)."""
        self._set(
            run_id,
            query_id,
            "state = ?, hits_json = ?, reason = ?",
            (state, json.dumps(list(hits), ensure_ascii=False), reason),
        )

    def edit(self, run_id: str, query_id: str, original: str) -> QueryRow:
        """New text for a query: back to `draft`, to be sanitized again."""
        self._set(
            run_id,
            query_id,
            "original = ?, sent = '', removed_json = '[]', state = 'draft', reason = ''",
            (original,),
        )
        return self.get(run_id, query_id)

    def delete(self, run_id: str, query_id: str) -> None:
        self._set(run_id, query_id, "state = 'deleted'", ())
