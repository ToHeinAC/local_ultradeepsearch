"""Source ingestion (PRD M3): fetch, filter, deduplicate, store, extract, analyse.

Every source moves through the stages `fetched` → `extracted` → `complete`, and each move is one
database transaction (PRD AD10). So a crash at any point leaves a stored source at a known stage,
and `resume()` continues from there: nothing stored is fetched again, finished extractions and
analyses are not redone, and a claim is never stored twice.
"""

import re
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from app.adapters.outbound.gateway import Document, FetchFailure
from app.events import EventSink
from app.pipeline.analysis import SourceAnalyzer, needs_analysis, render_analysis
from app.pipeline.artifacts import export_note_files, write_note_file, write_run_stats
from app.pipeline.chunking import word_count
from app.pipeline.dedup import NearDupIndex, signature
from app.pipeline.extraction import Focus, NoteExtractor
from app.pipeline.junk import junk_reason
from app.pipeline.links import page_links
from app.pipeline.profiles import Profile
from app.pipeline.strategies import SourceStrategies, tier_for
from app.pipeline.urls import dedup_key
from app.store.models import NewSource, Note, Rejection, RunStats, SourceMeta
from app.store.vault import Vault

MAX_ATTEMPTS = 2  # a URL whose failure may be transient gets two attempts in total
QUALITY_MIN_CLAIMS = 20  # PRD risk R2: judge the extract model once this many claims were seen
QUALITY_MAX_DROP_RATE = 0.30
TITLE_CHARS = 120


class Fetcher(Protocol):
    """The outbound gateway, or a fake in tests."""

    def fetch(self, url: str, *, step: str) -> Document | FetchFailure: ...


@dataclass(frozen=True)
class Ingested:
    note: Note
    reused: bool  # True: the source was already in the vault, nothing was fetched


@dataclass(frozen=True)
class Rejected:
    rejection: Rejection
    reused: bool  # True: an earlier rejection stands, nothing was fetched


IngestResult = Ingested | Rejected


def _normalize_body(text: str) -> str:
    """Single spaces inside paragraphs, one blank line between them. The words are untouched."""
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    return "\n\n".join(p for p in (" ".join(b.split()) for b in blocks) if p)


def _enriched(meta: SourceMeta, doc: Document) -> SourceMeta:
    """``meta`` completed with what the page says about itself; what the search knew wins."""
    year = int(doc.published[:4]) if doc.published and doc.published[:4].isdigit() else None
    return replace(
        meta,
        authors=meta.authors or ((doc.author,) if doc.author else ()),
        year=meta.year if meta.year is not None else year,
        venue=meta.venue or doc.publisher,
    )


def _retryable(reason: str) -> bool:
    return reason in ("timeout", "network", "http_429") or reason.startswith("http_5")


class FetchPipeline:
    def __init__(
        self,
        *,
        vault: Vault,
        fetcher: Fetcher,
        extractor: NoteExtractor,
        analyzer: SourceAnalyzer,
        strategies: SourceStrategies,
        profile: Profile,
        focus: Focus,
        run_dir: Path,
        events: EventSink,
    ) -> None:
        self._vault = vault
        self._fetcher = fetcher
        self._extractor = extractor
        self._analyzer = analyzer
        self._strategies = strategies
        self._profile = profile
        self._focus = focus
        self._run_dir = run_dir
        self._events = events
        self._index = NearDupIndex(vault.minhashes())
        self._lock = threading.Lock()  # guards the index, the store step and the counters below
        self._key_locks: dict[str, threading.Lock] = {}
        self._note_locks: dict[str, threading.RLock] = {}
        self._analyses_running = 0
        self._quality_warned = False

    @property
    def fetcher(self) -> Fetcher:
        return self._fetcher

    # ---- public ---------------------------------------------------------------------------

    def ingest(self, url: str, *, meta: SourceMeta | None = None, step: str = "2") -> IngestResult:
        """Bring one source into the vault and finish its processing."""
        source_meta = meta or SourceMeta()
        key = dedup_key(url)
        with self._key_lock(key):
            known = self._known(key, source_meta)
            if known is not None:
                return known
            outcome = self._fetcher.fetch(url, step=step)
            if isinstance(outcome, FetchFailure):
                reason = outcome.reason
                return Rejected(self._reject(url, key, reason, retryable=_retryable(reason)), False)
            junk = junk_reason(outcome.text)
            if junk is not None:
                return Rejected(self._reject(url, key, junk, retryable=False), False)
            return self._store(url, key, outcome, source_meta)

    def ingest_many(
        self, items: Sequence[tuple[str, SourceMeta | None]], *, max_workers: int = 4
    ) -> list[IngestResult]:
        """`ingest` for many URLs in parallel; results come back in input order."""
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(self.ingest, url, meta=meta) for url, meta in items]
            return [future.result() for future in futures]

    def resume(self) -> int:
        """Continue everything a stopped run left unfinished; returns how many notes it advanced."""
        export_note_files(self._vault, self._run_dir)  # a crash may have lost files after commits
        pending = self._vault.pending_notes()
        for note in pending:
            self._continue(note)
        return len(pending)

    # ---- reuse and rejection --------------------------------------------------------------

    def _key_lock(self, key: str) -> threading.Lock:
        with self._lock:
            return self._key_locks.setdefault(key, threading.Lock())

    def _note_lock(self, note_id: str) -> threading.RLock:
        """One worker at a time advances a note. Two URLs of one DOI have different dedup keys, so
        the key lock alone would let both workers extract the same note."""
        with self._lock:
            return self._note_locks.setdefault(note_id, threading.RLock())

    def _known(self, key: str, meta: SourceMeta) -> IngestResult | None:
        note = self._vault.find_by_canonical(key) or self._vault.find_by_doi(meta.doi)
        if note is not None:
            return Ingested(self._continue(note), True)
        rejection = self._vault.get_rejection(key)
        if rejection and (not rejection.retryable or rejection.attempts >= MAX_ATTEMPTS):
            return Rejected(rejection, True)
        return None

    def _reject(self, url: str, key: str, reason: str, *, retryable: bool) -> Rejection:
        rejection = self._vault.reject(url, key, reason, retryable=retryable)
        self._events.emit("source_rejected", url=url, reason=reason, attempts=rejection.attempts)
        write_run_stats(self._run_dir, self._vault.stats())
        return rejection

    # ---- storing --------------------------------------------------------------------------

    def _store(self, url: str, key: str, doc: Document, meta: SourceMeta) -> IngestResult:
        body = _normalize_body(doc.text)
        sig = signature(body)
        with self._lock:
            existing = self._vault.find_by_doi(meta.doi)
            if existing is not None:
                note, created = existing, False
            else:
                original = self._index.find(sig) if sig else None
                note, created = self._vault.add_source_note(
                    self._new_source(url, key, doc, meta, body, sig, original)
                )
                if created and sig and original is None:
                    self._index.add(note.note_id, sig)
        if created:
            self._vault.clear_rejection(key)
            self._events.emit(
                "source_stored",
                note_id=note.note_id,
                url=url,
                derivative_of=note.derivative_of,
                source_tier=note.source_tier,
            )
        return Ingested(self._continue(note), not created)

    def _new_source(
        self,
        url: str,
        key: str,
        doc: Document,
        meta: SourceMeta,
        body: str,
        sig: bytes | None,
        original: str | None,
    ) -> NewSource:
        tier = tier_for(
            doc.final_url, self._strategies, has_doi=bool(meta.doi), scholarly=meta.scholarly
        )
        title = (doc.title or "").strip() or body.split("\n", 1)[0][:TITLE_CHARS].rstrip()
        return NewSource(
            url=url,
            final_url=doc.final_url,
            canonical_url=key,
            doi=meta.doi,
            title=title,
            content_type=doc.content_type,
            via=doc.via,
            body=body,
            pages=doc.pages,
            word_count=word_count(body),
            source_tier=tier,
            derivative_of=original,
            minhash=sig,
            links=page_links(doc.html, doc.final_url) if doc.html else (),
            meta=_enriched(meta, doc),
        )

    # ---- stages ---------------------------------------------------------------------------

    def _continue(self, note: Note) -> Note:
        """Finish whatever stage ``note`` is at. Safe to call again after any interruption."""
        with self._note_lock(note.note_id):
            return self._advance(self._fresh(note))  # a worker we waited for may have finished it

    def _advance(self, note: Note) -> Note:
        changed = False
        if note.stage == "fetched":
            result = self._extractor.extract(note, self._focus)
            self._vault.save_extraction(
                note.note_id,
                result.summary,
                result.claims,
                dropped=result.dropped,
                failed=result.failed,
            )
            note = self._fresh(note)
            changed = True
        if note.stage == "extracted":
            self._finish(note)
            note = self._fresh(note)
            changed = True
        if changed:
            self._publish(note)
        return note

    def _fresh(self, note: Note) -> Note:
        current = self._vault.get_note(note.note_id)
        assert current is not None
        return current

    def _finish(self, note: Note) -> None:
        """Run the long-source analysis if the profile allows it, then complete the note."""
        with self._lock:
            done = self._vault.stats().source_analyses
            wanted = needs_analysis(note, self._profile, done + self._analyses_running)
            if wanted:
                self._analyses_running += 1
        if not wanted:
            self._vault.mark_complete(note.note_id)
            return
        try:
            analysis = self._analyzer.analyze(note, self._focus)
            if analysis is None:
                self._vault.mark_complete(note.note_id)
            else:
                title, body = render_analysis(note, analysis)
                self._vault.add_analysis_note(note.note_id, title, body)
        finally:
            with self._lock:
                self._analyses_running -= 1

    def _publish(self, note: Note) -> None:
        write_note_file(self._run_dir, note, self._vault.claims(note.note_id))
        analysis = self._vault.find_analysis(note.note_id)
        if analysis is not None:
            write_note_file(self._run_dir, analysis, [])
        stats = self._vault.stats()
        write_run_stats(self._run_dir, stats)
        self._check_quality(stats)

    def _check_quality(self, stats: RunStats) -> None:
        seen = stats.claims_kept + stats.claims_dropped
        with self._lock:
            if self._quality_warned or seen < QUALITY_MIN_CLAIMS:
                return
            if stats.claims_drop_rate <= QUALITY_MAX_DROP_RATE:
                return
            self._quality_warned = True
        self._events.emit(
            "extract_quality_low",
            level="warning",
            claims_drop_rate=stats.claims_drop_rate,
            claims_seen=seen,
        )
