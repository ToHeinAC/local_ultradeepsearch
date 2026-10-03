"""Searches a run has already made (PRD AD10): stored before their results are used, so a resumed
run never sends a query twice and never spends a credit twice."""

from dataclasses import dataclass

from app.store.db import Database


@dataclass(frozen=True)
class SearchRow:
    query_id: str
    source: str  # "web", "openalex", "crossref" or "arxiv"
    wave: int
    results_json: str


class SearchStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, run_id: str, query_id: str, source: str, wave: int, results_json: str) -> bool:
        """Store the results of one query at one source; False if it was stored before (the first
        answer stands)."""
        with self._db.tx():
            cursor = self._db.conn.execute(
                "INSERT OR IGNORE INTO searches (run_id, query_id, source, wave, results_json, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, query_id, source, wave, results_json, self._db.stamp()),
            )
            return cursor.rowcount == 1

    def has(self, run_id: str, query_id: str, source: str) -> bool:
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT 1 FROM searches WHERE run_id = ? AND query_id = ? AND source = ?",
                (run_id, query_id, source),
            ).fetchone()
        return row is not None

    def all(self, run_id: str) -> list[SearchRow]:
        with self._db.lock:
            rows = self._db.conn.execute(
                "SELECT query_id, source, wave, results_json FROM searches WHERE run_id = ? "
                "ORDER BY rowid",
                (run_id,),
            ).fetchall()
        return [SearchRow(r["query_id"], r["source"], r["wave"], r["results_json"]) for r in rows]
