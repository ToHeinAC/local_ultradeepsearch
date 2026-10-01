"""The vault of one run: notes, claims and rejected sources in SQLite.

Every method is scoped to the run id given at construction, so a vault cannot read another run.
Writes are single `BEGIN IMMEDIATE` transactions; a stage change and the rows it belongs to are
committed together, which is what makes a crash resumable (PRD AD10).
"""

import json
import sqlite3
import threading
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.store import search as fts
from app.store.db import connect, migrate
from app.store.models import (
    ClaimRecord,
    Kind,
    NewClaim,
    NewSource,
    Note,
    Rejection,
    RunStats,
    SearchResult,
    SourceMeta,
    Stage,
    normalize_doi,
)

_INSERT_NOTE = """
INSERT INTO notes (run_id, note_id, kind, stage, url, final_url, canonical_url, doi, title,
  content_type, via, body, pages_json, word_count, summary, meta_json, source_tier, derivative_of,
  analysis_of, minhash, links_json, created_at)
VALUES (:run_id, :note_id, :kind, :stage, :url, :final_url, :canonical_url, :doi, :title,
  :content_type, :via, :body, :pages_json, :word_count, '', :meta_json, :source_tier,
  :derivative_of, :analysis_of, :minhash, :links_json, :created_at)
"""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _json_tuple(text: str) -> tuple[str, ...]:
    return tuple(json.loads(text))


def _note(row: sqlite3.Row) -> Note:
    return Note(
        run_id=row["run_id"],
        note_id=row["note_id"],
        kind=row["kind"],
        stage=row["stage"],
        url=row["url"],
        final_url=row["final_url"],
        canonical_url=row["canonical_url"],
        doi=row["doi"],
        title=row["title"],
        content_type=row["content_type"],
        via=row["via"],
        body=row["body"],
        pages=_json_tuple(row["pages_json"]),
        word_count=row["word_count"],
        summary=row["summary"],
        meta=json.loads(row["meta_json"]),
        source_tier=row["source_tier"],
        utility=row["utility"],
        derivative_of=row["derivative_of"],
        analysis_of=row["analysis_of"],
        links=_json_tuple(row["links_json"]),
        extract_failed=bool(row["extract_failed"]),
        claims_kept=row["claims_kept"],
        claims_dropped=row["claims_dropped"],
        created_at=row["created_at"],
    )


def _claim(row: sqlite3.Row) -> ClaimRecord:
    return ClaimRecord(
        claim_id=row["claim_id"],
        note_id=row["note_id"],
        claim=row["claim"],
        stance=row["stance"],
        stance_target=row["stance_target"],
        evidence_type=row["evidence_type"],
        scope_conditions=row["scope_conditions"],
        quoted_support=row["quoted_support"],
        numbers=_json_tuple(row["numbers_json"]),
        entities=_json_tuple(row["entities_json"]),
        time_period=row["time_period"],
        region=row["region"],
        confidence=row["confidence"],
    )


def _rejection(row: sqlite3.Row) -> Rejection:
    return Rejection(
        canonical_url=row["canonical_url"],
        url=row["url"],
        reason=row["reason"],
        detail=row["detail"],
        attempts=row["attempts"],
        retryable=bool(row["retryable"]),
        created_at=row["created_at"],
    )


class Vault:
    def __init__(
        self,
        path: Path | str,
        run_id: str,
        *,
        label: str = "",
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.run_id = run_id
        self._now = now
        self._lock = threading.RLock()
        self._conn = connect(path)
        migrate(self._conn)
        with self._tx():
            self._conn.execute(
                "INSERT OR IGNORE INTO runs (run_id, created_at, label) VALUES (?, ?, ?)",
                (run_id, self._stamp(), label),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- plumbing -------------------------------------------------------------------------

    def _stamp(self) -> str:
        return self._now().isoformat()

    @contextmanager
    def _tx(self) -> Generator[None]:
        """One write transaction; rolled back on any exception, including a crash signal."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _next_id(self, table: str, column: str, prefix: str, width: int) -> str:
        sql = (
            f"SELECT COALESCE(MAX(CAST(SUBSTR({column}, 2) AS INTEGER)), 0) + 1 "
            f"FROM {table} WHERE run_id = ?"
        )
        number = self._conn.execute(sql, (self.run_id,)).fetchone()[0]
        return f"{prefix}{number:0{width}d}"

    def _one(self, where: str, params: Sequence[Any]) -> Note | None:
        sql = f"SELECT * FROM notes WHERE run_id = ? AND {where} ORDER BY rowid LIMIT 1"
        with self._lock:
            row = self._conn.execute(sql, (self.run_id, *params)).fetchone()
        return _note(row) if row else None

    # ---- reading notes --------------------------------------------------------------------

    def get_note(self, note_id: str) -> Note | None:
        return self._one("note_id = ?", (note_id,))

    def find_by_canonical(self, canonical_url: str) -> Note | None:
        return self._one("kind = 'source' AND canonical_url = ?", (canonical_url,))

    def find_by_doi(self, doi: str | None) -> Note | None:
        normalized = normalize_doi(doi)
        return self._one("kind = 'source' AND doi = ?", (normalized,)) if normalized else None

    def notes(self, *, kind: Kind | None = None, stage: Stage | None = None) -> list[Note]:
        where, params = ["run_id = ?"], [self.run_id]
        for column, value in (("kind", kind), ("stage", stage)):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        sql = f"SELECT * FROM notes WHERE {' AND '.join(where)} ORDER BY rowid"
        with self._lock:
            return [_note(r) for r in self._conn.execute(sql, params).fetchall()]

    def pending_notes(self) -> list[Note]:
        """Source notes whose extraction or analysis is not finished, oldest first."""
        sql = (
            "SELECT * FROM notes WHERE run_id = ? AND kind = 'source' "
            "AND stage != 'complete' ORDER BY rowid"
        )
        with self._lock:
            return [_note(r) for r in self._conn.execute(sql, (self.run_id,)).fetchall()]

    def minhashes(self) -> list[tuple[str, bytes]]:
        """(note_id, signature) of the original sources, for rebuilding the near-duplicate index."""
        sql = (
            "SELECT note_id, minhash FROM notes WHERE run_id = ? AND kind = 'source' "
            "AND derivative_of IS NULL AND minhash IS NOT NULL ORDER BY rowid"
        )
        with self._lock:
            return [
                (r["note_id"], bytes(r["minhash"])) for r in self._conn.execute(sql, (self.run_id,))
            ]

    # ---- writing notes --------------------------------------------------------------------

    def add_source_note(self, source: NewSource) -> tuple[Note, bool]:
        """Store a fetched source at stage `fetched`; returns (note, created)."""
        with self._tx():
            existing = self.find_by_canonical(source.canonical_url)
            if existing is not None:
                return existing, False
            note_id = self._next_id("notes", "note_id", "n", 4)
            params: dict[str, Any] = {
                "run_id": self.run_id,
                "note_id": note_id,
                "kind": "source",
                "stage": "fetched",
                "url": source.url,
                "final_url": source.final_url,
                "canonical_url": source.canonical_url,
                "doi": normalize_doi(source.doi),
                "title": source.title,
                "content_type": source.content_type,
                "via": source.via,
                "body": source.body,
                "pages_json": json.dumps(list(source.pages), ensure_ascii=False),
                "word_count": source.word_count,
                "meta_json": json.dumps(asdict(source.meta), ensure_ascii=False),
                "source_tier": source.source_tier,
                "derivative_of": source.derivative_of,
                "analysis_of": None,
                "minhash": source.minhash,
                "links_json": json.dumps(list(source.links), ensure_ascii=False),
                "created_at": self._stamp(),
            }
            self._conn.execute(_INSERT_NOTE, params)
            created = self.get_note(note_id)
        assert created is not None
        return created, True

    def save_extraction(
        self,
        note_id: str,
        summary: str,
        claims: Sequence[NewClaim],
        *,
        dropped: int,
        failed: bool,
    ) -> bool:
        """Store summary and claims and move `fetched` → `extracted` in one transaction.

        Returns False (and changes nothing) if the note is no longer at stage `fetched`.
        """
        with self._tx():
            row = self._conn.execute(
                "SELECT stage FROM notes WHERE run_id = ? AND note_id = ? AND kind = 'source'",
                (self.run_id, note_id),
            ).fetchone()
            if row is None:
                raise KeyError(note_id)
            if row["stage"] != "fetched":
                return False
            for claim in claims:
                self._insert_claim(note_id, claim)
            self._conn.execute(
                "UPDATE notes SET summary = ?, stage = 'extracted', extract_failed = ?, "
                "claims_kept = ?, claims_dropped = ? WHERE run_id = ? AND note_id = ?",
                (summary, int(failed), len(claims), dropped, self.run_id, note_id),
            )
        return True

    def _insert_claim(self, note_id: str, claim: NewClaim) -> None:
        claim_id = self._next_id("claims", "claim_id", "c", 5)
        self._conn.execute(
            "INSERT INTO claims (run_id, claim_id, note_id, claim, stance, stance_target, "
            "evidence_type, scope_conditions, quoted_support, numbers_json, entities_json, "
            "time_period, region, confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.run_id,
                claim_id,
                note_id,
                claim.claim,
                claim.stance,
                claim.stance_target,
                claim.evidence_type,
                claim.scope_conditions,
                claim.quoted_support,
                json.dumps(list(claim.numbers), ensure_ascii=False),
                json.dumps(list(claim.entities), ensure_ascii=False),
                claim.time_period,
                claim.region,
                claim.confidence,
            ),
        )

    def mark_complete(self, note_id: str) -> None:
        """`extracted` → `complete` when no analysis is needed. Other stages are left alone."""
        with self._tx():
            self._conn.execute(
                "UPDATE notes SET stage = 'complete' WHERE run_id = ? AND note_id = ? "
                "AND kind = 'source' AND stage = 'extracted'",
                (self.run_id, note_id),
            )

    def add_analysis_note(self, source_note_id: str, title: str, body: str) -> Note:
        """Store the analysis of an extracted source and complete the source, atomically."""
        with self._tx():
            source = self.get_note(source_note_id)
            if source is None:
                raise KeyError(source_note_id)
            result = self._one("kind = 'source_analysis' AND analysis_of = ?", (source_note_id,))
            if result is None:
                if source.stage != "extracted":
                    raise ValueError(
                        f"source {source_note_id} must be extracted, is {source.stage}"
                    )
                note_id = self._next_id("notes", "note_id", "n", 4)
                params = self._analysis_params(note_id, source, title, body)
                self._conn.execute(_INSERT_NOTE, params)
                result = self.get_note(note_id)
            self._conn.execute(
                "UPDATE notes SET stage = 'complete' "
                "WHERE run_id = ? AND note_id = ? AND stage = 'extracted'",
                (self.run_id, source_note_id),
            )
        assert result is not None
        return result

    def _analysis_params(self, note_id: str, source: Note, title: str, body: str) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "note_id": note_id,
            "kind": "source_analysis",
            "stage": "complete",
            "url": source.url,
            "final_url": source.final_url,
            "canonical_url": source.canonical_url,
            "doi": None,
            "title": title,
            "content_type": "text/markdown",
            "via": "analysis",
            "body": body,
            "pages_json": "[]",
            "word_count": len(body.split()),
            "meta_json": json.dumps(asdict(SourceMeta())),
            "source_tier": source.source_tier,
            "derivative_of": None,
            "analysis_of": source.note_id,
            "minhash": None,
            "links_json": "[]",
            "created_at": self._stamp(),
        }

    # ---- claims, rejections, search, stats ------------------------------------------------

    def claims(self, note_id: str | None = None) -> list[ClaimRecord]:
        sql, params = "SELECT * FROM claims WHERE run_id = ?", [self.run_id]
        if note_id is not None:
            sql, params = sql + " AND note_id = ?", [*params, note_id]
        with self._lock:
            return [_claim(r) for r in self._conn.execute(sql + " ORDER BY rowid", params)]

    def reject(
        self,
        url: str,
        canonical_url: str,
        reason: str,
        detail: str = "",
        *,
        retryable: bool = False,
    ) -> Rejection:
        """Record a rejected source; repeated rejections of one URL count their attempts."""
        with self._tx():
            updated = self._conn.execute(
                "UPDATE rejected_sources SET reason = ?, detail = ?, retryable = ?, "
                "attempts = attempts + 1, url = ? WHERE run_id = ? AND canonical_url = ?",
                (reason, detail, int(retryable), url, self.run_id, canonical_url),
            ).rowcount
            if updated == 0:
                self._conn.execute(
                    "INSERT INTO rejected_sources (run_id, canonical_url, url, reason, detail, "
                    "attempts, retryable, created_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                    (
                        self.run_id,
                        canonical_url,
                        url,
                        reason,
                        detail,
                        int(retryable),
                        self._stamp(),
                    ),
                )
        stored = self.get_rejection(canonical_url)
        assert stored is not None
        return stored

    def get_rejection(self, canonical_url: str) -> Rejection | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM rejected_sources WHERE run_id = ? AND canonical_url = ?",
                (self.run_id, canonical_url),
            ).fetchone()
        return _rejection(row) if row else None

    def rejections(self) -> list[Rejection]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM rejected_sources WHERE run_id = ? ORDER BY rowid", (self.run_id,)
            ).fetchall()
        return [_rejection(r) for r in rows]

    def clear_rejection(self, canonical_url: str) -> None:
        with self._tx():
            self._conn.execute(
                "DELETE FROM rejected_sources WHERE run_id = ? AND canonical_url = ?",
                (self.run_id, canonical_url),
            )

    def search(self, text: str, limit: int = 20) -> list[SearchResult]:
        with self._lock:
            return fts.search(self._conn, self.run_id, text, limit)

    def stats(self) -> RunStats:
        with self._lock:
            kinds = dict(
                self._conn.execute(
                    "SELECT kind, COUNT(*) FROM notes WHERE run_id = ? GROUP BY kind",
                    (self.run_id,),
                ).fetchall()
            )
            rejected = dict(
                self._conn.execute(
                    "SELECT reason, COUNT(*) FROM rejected_sources "
                    "WHERE run_id = ? GROUP BY reason",
                    (self.run_id,),
                ).fetchall()
            )
            sums = self._conn.execute(
                "SELECT COALESCE(SUM(claims_kept), 0), COALESCE(SUM(claims_dropped), 0), "
                "COALESCE(SUM(extract_failed), 0), COALESCE(SUM(derivative_of IS NOT NULL), 0) "
                "FROM notes WHERE run_id = ? AND kind = 'source'",
                (self.run_id,),
            ).fetchone()
        kept, dropped, failed, derivatives = sums
        seen = kept + dropped
        return RunStats(
            notes_by_kind=kinds,
            derivatives=derivatives,
            rejected_by_reason=rejected,
            claims_kept=kept,
            claims_dropped=dropped,
            claims_drop_rate=dropped / seen if seen else 0.0,
            extract_failed=failed,
            source_analyses=kinds.get("source_analysis", 0),
        )

    def delete_run(self) -> None:
        """Remove this run's rows (notes, claims, rejections, search index)."""
        with self._tx():
            self._conn.execute("DELETE FROM runs WHERE run_id = ?", (self.run_id,))
