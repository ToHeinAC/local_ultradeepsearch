"""Step 2 (PRD M5): search, fetch, coverage check and a second wave for thin items.

Every search is stored before its results are used, so a resumed run never sends a query twice and
derives the same candidates again (AD10). Fetching is the M3 pipeline, which resumes by itself.
A provider outage never fails the run: the search is stored empty and the gaps are documented.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.adapters.outbound.errors import OutboundBlocked, SearchUnavailable
from app.adapters.outbound.gateway import PreparedQuery, ScholarlySource
from app.adapters.outbound.types import ScholarlyRecord, SearchHit
from app.artifacts import write_json, write_text
from app.events import EventSink
from app.llm.service import LLMService
from app.llm.types import Message, Role
from app.pipeline.fetch import Ingested, IngestResult
from app.pipeline.profiles import ResearchBudget, RunRules
from app.pipeline.strategies import SourceStrategies
from app.prompts import research as prompts
from app.research.candidates import (
    Candidate,
    collect_candidates,
    coverage_counts,
    encode_results,
    select_for_items,
    select_wave,
)
from app.research.models import (
    AtomicItem,
    Decomposition,
    PlanDraft,
    PlannedQuery,
    PlanQueryDraft,
    SearchPlan,
    atomic_items,
    render_items,
)
from app.research.plan import QueryPreparer, normalized_queries, prepare
from app.store.models import Note, SourceMeta
from app.store.research import SearchStore
from app.store.vault import Vault
from app.text import normalize_for_match

STEP = "2"
ARXIV_DOMAINS = frozenset({"science_medicine", "tech_standards"})
WAVE2_FILE = "wave2-plan.json"


class Searcher(Protocol):
    """The outbound gateway's searches, or a fake in tests."""

    def search_web(
        self, prepared: PreparedQuery, *, step: str, max_results: int = 10
    ) -> list[SearchHit]: ...

    def search_scholarly(
        self, prepared: PreparedQuery, *, step: str, source: ScholarlySource, max_results: int = 10
    ) -> list[ScholarlyRecord]: ...


class Ingestor(Protocol):
    """The M3 fetch pipeline."""

    def ingest_many(
        self, items: Sequence[tuple[str, SourceMeta | None]], *, max_workers: int = 4
    ) -> list[IngestResult]: ...

    def resume(self) -> int: ...


@dataclass(frozen=True)
class SweepDeps:
    run_id: str
    run_dir: Path
    searches: SearchStore
    searcher: Searcher
    preparer: QueryPreparer
    ingestor: Ingestor
    vault: Vault
    service: LLMService
    strategies: SourceStrategies
    budget: ResearchBudget
    rules: RunRules
    events: EventSink


@dataclass(frozen=True)
class SweepResult:
    counts: dict[str, int]  # sources per atomic item
    sources: int  # substantive sources of the run
    wave2: bool


@dataclass
class _Context:
    brief: str
    items: list[AtomicItem]
    domains: tuple[str, ...]
    queries: dict[str, PlannedQuery]

    @property
    def item_ids(self) -> list[str]:
        return [item.id for item in self.items]


def sources_for(query: PlannedQuery, domains: Sequence[str]) -> list[str]:
    """Where a query is searched: the web, or OpenAlex and Crossref (and arXiv for science and
    technical topics)."""
    if query.kind == "web":
        return ["web"]
    sources = ["openalex", "crossref"]
    return [*sources, "arxiv"] if ARXIV_DOMAINS & set(domains) else sources


def _substantive(notes: Sequence[Note]) -> list[Note]:
    return [
        n
        for n in notes
        if n.kind == "source"
        and n.stage == "complete"
        and n.derivative_of is None
        and not n.extract_failed
    ]


class Sweeper:
    def __init__(self, deps: SweepDeps) -> None:
        self._d = deps

    def run(self, brief: str, plan: SearchPlan, decomposition: Decomposition) -> SweepResult:
        """Search the approved plan, fetch a first wave, and fetch a second one for thin items."""
        d = self._d
        d.ingestor.resume()
        ctx = _Context(
            brief,
            atomic_items(decomposition),
            tuple(decomposition.domains),
            {q.query_id: q for q in plan.queries},
        )
        self._search(ctx, plan.queries, wave=1)
        candidates = self._candidates(ctx)
        first = select_wave(candidates, ctx.item_ids, d.budget.deduped_urls[1])
        self._fetch(first, wave=1)
        counts = self._counts(ctx)
        thin = [i for i in ctx.item_ids if counts[i] < d.rules.thin_item_sources]
        wave2 = bool(thin) and d.budget.fetch_waves[1] >= 2
        if wave2:
            counts = self._second_wave(ctx, thin)
        notes = _substantive(d.vault.notes(kind="source"))
        self._write_coverage(ctx, counts, notes)
        return SweepResult(counts, len(notes), wave2)

    # ---- searching ------------------------------------------------------------------------

    def _search(self, ctx: _Context, queries: Sequence[PlannedQuery], wave: int) -> None:
        d = self._d
        for query in queries:
            if query.blocked:
                continue
            for source in sources_for(query, ctx.domains):
                if d.searches.has(d.run_id, query.query_id, source):
                    continue
                hits = self._ask(query, source)
                d.searches.add(d.run_id, query.query_id, source, wave, encode_results(hits))

    def _ask(self, query: PlannedQuery, source: str) -> list[SearchHit] | list[ScholarlyRecord]:
        d = self._d
        prepared = PreparedQuery(query.original, query.sent, tuple(query.removed_terms))
        size = d.rules.search_max_results
        try:
            if source == "web":
                return d.searcher.search_web(prepared, step=STEP, max_results=size)
            return d.searcher.search_scholarly(
                prepared,
                step=STEP,
                source=source,  # type: ignore[arg-type]  # one of the three scholarly sources
                max_results=size,
            )
        except SearchUnavailable as exc:
            d.events.emit(
                "search_failed",
                level="warning",
                query_id=query.query_id,
                source=source,
                reason=str(exc),
            )
        except OutboundBlocked as exc:
            d.events.emit(
                "search_blocked",
                level="warning",
                query_id=query.query_id,
                source=source,
                reason=exc.reason,
            )
        return []

    # ---- fetching and coverage ------------------------------------------------------------

    def _candidates(self, ctx: _Context) -> list[Candidate]:
        d = self._d
        return collect_candidates(ctx.queries, d.searches.all(d.run_id), d.strategies)

    def _fetch(self, chosen: Sequence[Candidate], wave: int) -> None:
        if not chosen:
            return
        results = self._d.ingestor.ingest_many([(c.url, c.meta) for c in chosen])
        stored = sum(1 for r in results if isinstance(r, Ingested) and not r.reused)
        self._d.events.emit("wave_done", wave=wave, requested=len(chosen), stored=stored)

    def _counts(self, ctx: _Context) -> dict[str, int]:
        notes = self._d.vault.notes(kind="source")
        return coverage_counts(notes, self._candidates(ctx), ctx.item_ids)

    # ---- the second wave ------------------------------------------------------------------

    def _second_wave(self, ctx: _Context, thin: list[str]) -> dict[str, int]:
        d = self._d
        extra = self._wave2_queries(ctx, thin)
        ctx.queries.update({q.query_id: q for q in extra})
        self._search(ctx, extra, wave=2)
        tried = {n.canonical_url for n in d.vault.notes()} | {
            r.canonical_url for r in d.vault.rejections()
        }
        chosen = select_for_items(
            self._candidates(ctx), thin, d.rules.wave2_urls_per_item, exclude=tried
        )
        self._fetch(chosen, wave=2)
        return self._counts(ctx)

    def _wave2_queries(self, ctx: _Context, thin: list[str]) -> list[PlannedQuery]:
        path = self._d.run_dir / "temp" / WAVE2_FILE
        if path.exists():
            stored = json.loads(path.read_text(encoding="utf-8"))
            return [PlannedQuery.model_validate(q) for q in stored["queries"]]
        drafts = self._ask_wave2(ctx, thin)
        queries = [
            prepare(self._d.preparer, f"w2-q{n:02d}", x.item, x.lens, x.query, step=STEP)
            for n, x in enumerate(drafts, 1)
        ]
        for query in queries:
            if query.blocked:
                self._d.events.emit(
                    "wave2_query_blocked",
                    level="warning",
                    query_id=query.query_id,
                    reason=query.blocked,
                )
        write_json(path, {"thin": thin, "queries": [q.model_dump(mode="json") for q in queries]})
        return queries

    def _ask_wave2(self, ctx: _Context, thin: list[str]) -> list[PlanQueryDraft]:
        d = self._d
        low, high = d.rules.wave2_queries_per_item
        tried = "\n".join(f"{q.item}: {q.original}" for q in ctx.queries.values() if q.item in thin)
        user = prompts.WAVE2_USER.format(
            brief=ctx.brief,
            items=render_items([i for i in ctx.items if i.id in thin]),
            tried=tried,
        )
        messages: list[Message] = [
            {
                "role": "system",
                "content": prompts.WAVE2_SYSTEM.format(min_queries=low, max_queries=high),
            },
            {"role": "user", "content": user},
        ]
        raw = d.service.structured(Role.REASON, messages, PlanDraft).queries
        seen = {normalize_for_match(q.original) for q in ctx.queries.values()}
        fresh = normalized_queries(raw, set(thin), seen)
        per_item: dict[str, int] = {}
        kept: list[PlanQueryDraft] = []
        for draft in fresh:
            per_item[draft.item] = per_item.get(draft.item, 0) + 1
            if per_item[draft.item] <= high:
                kept.append(draft)
        return kept

    # ---- the report -----------------------------------------------------------------------

    def _write_coverage(self, ctx: _Context, counts: dict[str, int], notes: Sequence[Note]) -> None:
        d = self._d
        if len(notes) < d.budget.sources_min:
            d.events.emit(
                "sources_below_minimum",
                level="warning",
                sources=len(notes),
                minimum=d.budget.sources_min,
            )
        text = render_coverage(ctx.items, counts, notes, d.budget.sources_min, d.rules)
        write_text(d.run_dir / "temp" / "coverage-gaps.md", text)


def status_of(count: int, rules: RunRules) -> str:
    if count == 0:
        return "**uncovered**"
    if count < rules.thin_item_sources:
        return "**thin**"
    return "well covered" if count >= rules.well_covered_sources else "adequate"


def render_coverage(
    items: Sequence[AtomicItem],
    counts: dict[str, int],
    notes: Sequence[Note],
    sources_min: int,
    rules: RunRules,
) -> str:
    """`coverage-gaps.md`: every item with its source count and status, the source total against
    the minimum, and any retracted source."""
    total = f"Sources: {len(notes)} substantive"
    total += (
        f", below the minimum of {sources_min}: proceeding with what was found."
        if len(notes) < sources_min
        else f" (minimum {sources_min})."
    )
    rows = [
        f"| {i.id} | {' '.join(i.text.split()).replace('|', '/')} | {counts[i.id]} | "
        f"{status_of(counts[i.id], rules)} |"
        for i in items
    ]
    parts = [
        "# Coverage gaps\n",
        total + "\n",
        "## Items\n",
        "| Item | Text | Sources | Status |\n|---|---|---|---|\n" + "\n".join(rows) + "\n",
    ]
    retracted = [n for n in notes if n.meta.get("is_retracted") is True]
    if retracted:
        listed = "\n".join(f"- {n.note_id} {n.url} (retracted)" for n in retracted)
        parts.append(f"## Retracted sources\n\n{listed}\n")
    return "\n".join(parts)
