"""Full-text search over a run's notes (FTS5, diacritics folded)."""

import re
import sqlite3

from app.store.models import SearchResult

MAX_TERMS = 32
_WORD = re.compile(r"[^\W_]+")

# bm25 weights follow the FTS5 column order: run_id, note_id (unindexed), title, summary, body.
_SQL = """
SELECT n.note_id, n.kind, n.title, n.derivative_of,
       -bm25(notes_fts, 0.0, 0.0, 10.0, 5.0, 1.0) AS score
FROM notes_fts
JOIN notes n ON n.rowid = notes_fts.rowid
WHERE notes_fts MATCH ? AND n.run_id = ?
ORDER BY score DESC, n.rowid
LIMIT ?
"""


def fts_query(text: str) -> str | None:
    """Words of ``text`` as an OR query of quoted terms; None if there is nothing to search."""
    seen: dict[str, None] = {}
    for word in _WORD.findall(text.lower()):
        seen.setdefault(word)
    terms = list(seen)[:MAX_TERMS]
    return " OR ".join(f'"{t}"' for t in terms) if terms else None


def search(conn: sqlite3.Connection, run_id: str, text: str, limit: int = 20) -> list[SearchResult]:
    query = fts_query(text)
    if query is None:
        return []
    rows = conn.execute(_SQL, (query, run_id, limit)).fetchall()
    return [
        SearchResult(r["note_id"], r["kind"], r["title"], float(r["score"]), r["derivative_of"])
        for r in rows
    ]
