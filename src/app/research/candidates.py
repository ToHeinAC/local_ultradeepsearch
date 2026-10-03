"""From stored search results to the pages a run fetches (PRD M5 step 2).

Pure functions. The stored results of every query are the single source of truth, so a resumed
run derives the same candidates, the same first wave and the same coverage again (AD10).
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.adapters.outbound.types import ScholarlyRecord, SearchHit
from app.pipeline.strategies import SourceStrategies, tier_for
from app.pipeline.urls import dedup_key
from app.research.models import PlannedQuery
from app.store.models import Note, SourceMeta, normalize_doi
from app.store.research import SearchRow


@dataclass(frozen=True)
class Hit:
    url: str  # what to fetch
    title: str
    meta: SourceMeta


@dataclass(frozen=True)
class Candidate:
    url: str
    key: str  # the canonical URL: the vault's dedup key
    items: tuple[str, ...]  # the atomic items whose queries found it
    meta: SourceMeta
    weight: float  # source-tier weight (PRD §3.3)
    rank: int  # best position in any result list, 0 first
    wave: int
    order: int  # discovery order, the last tie-breaker


def encode_results(hits: Sequence[SearchHit | ScholarlyRecord]) -> str:
    """The results of one search as JSON for `searches.results_json`. A scholarly record is
    fetched from its open-access copy when it has one."""
    rows: list[dict[str, Any]] = []
    for hit in hits:
        if isinstance(hit, ScholarlyRecord):
            rows.append(
                {
                    "url": hit.oa_url or hit.url,
                    "title": hit.title,
                    "scholarly": True,
                    "doi": hit.doi,
                    "year": hit.year,
                    "authors": list(hit.authors),
                    "venue": hit.venue,
                    "cited_by_count": hit.cited_by_count,
                    "is_retracted": hit.is_retracted,
                    "oa_url": hit.oa_url,
                }
            )
        else:
            rows.append({"url": hit.url, "title": hit.title, "scholarly": False})
    return json.dumps(rows, ensure_ascii=False)


def decode_results(text: str) -> list[Hit]:
    hits: list[Hit] = []
    for row in json.loads(text):
        meta = SourceMeta()
        if row.get("scholarly"):
            meta = SourceMeta(
                doi=row.get("doi"),
                scholarly=True,
                year=row.get("year"),
                authors=tuple(row.get("authors") or ()),
                venue=row.get("venue"),
                cited_by_count=row.get("cited_by_count"),
                is_retracted=row.get("is_retracted"),
                oa_url=row.get("oa_url"),
            )
        hits.append(Hit(row["url"], row["title"], meta))
    return hits


def _weight(url: str, meta: SourceMeta, strategies: SourceStrategies) -> float:
    tier = tier_for(url, strategies, has_doi=bool(meta.doi), scholarly=meta.scholarly)
    return strategies.tier_weights[tier]


def _merged(
    old: Candidate, hit: Hit, item: str, rank: int, strategies: SourceStrategies
) -> Candidate:
    meta = hit.meta if hit.meta.scholarly and not old.meta.scholarly else old.meta
    items = old.items if item in old.items else (*old.items, item)
    return Candidate(
        old.url,
        old.key,
        items,
        meta,
        _weight(old.url, meta, strategies),
        min(old.rank, rank),
        old.wave,
        old.order,
    )


def collect_candidates(
    queries: Mapping[str, PlannedQuery], rows: Sequence[SearchRow], strategies: SourceStrategies
) -> list[Candidate]:
    """Every page the stored searches found, once, with the items it serves, in discovery order.
    Rows of queries that are not in ``queries`` are ignored."""
    found: dict[str, Candidate] = {}
    for row in rows:
        query = queries.get(row.query_id)
        if query is None:
            continue
        for rank, hit in enumerate(decode_results(row.results_json)):
            key = dedup_key(hit.url)
            if key in found:
                found[key] = _merged(found[key], hit, query.item, rank, strategies)
            else:
                weight = _weight(hit.url, hit.meta, strategies)
                found[key] = Candidate(
                    hit.url, key, (query.item,), hit.meta, weight, rank, row.wave, len(found)
                )
    return list(found.values())


def _best_first(pool: Sequence[Candidate], item: str) -> list[Candidate]:
    served = [c for c in pool if item in c.items]
    return sorted(served, key=lambda c: (-c.weight, c.rank, c.order))


def select_wave(pool: Sequence[Candidate], item_ids: Sequence[str], cap: int) -> list[Candidate]:
    """Up to ``cap`` pages, taking turns between the items, each item's best page first, so no
    item is starved by another with many hits. A page serving several items is taken once."""
    ranked = {item: _best_first(pool, item) for item in item_ids}
    cursor = dict.fromkeys(item_ids, 0)
    chosen: dict[str, Candidate] = {}
    while len(chosen) < cap:
        before = len(chosen)
        for item in item_ids:
            while cursor[item] < len(ranked[item]) and ranked[item][cursor[item]].key in chosen:
                cursor[item] += 1
            if cursor[item] < len(ranked[item]) and len(chosen) < cap:
                page = ranked[item][cursor[item]]
                chosen[page.key] = page
        if len(chosen) == before:
            break
    return list(chosen.values())


def select_for_items(
    pool: Sequence[Candidate], item_ids: Sequence[str], per_item: int, exclude: set[str]
) -> list[Candidate]:
    """The best ``per_item`` pages of each item that are not in ``exclude`` (known by key)."""
    chosen: dict[str, Candidate] = {}
    for item in item_ids:
        fresh = [c for c in _best_first(pool, item) if c.key not in exclude and c.key not in chosen]
        chosen.update((c.key, c) for c in fresh[:per_item])
    return list(chosen.values())


def provenance(
    notes: Sequence[Note], candidates: Sequence[Candidate]
) -> dict[str, tuple[str, ...]]:
    """Per substantive note (complete, original, successfully read), the items whose queries
    found it. A note is matched to its candidate by canonical URL, or by DOI when another URL got
    there first."""
    by_key = {c.key: c for c in candidates}
    by_doi = {normalize_doi(c.meta.doi): c for c in candidates if c.meta.doi}
    found: dict[str, tuple[str, ...]] = {}
    for note in notes:
        if (
            note.kind != "source"
            or note.stage != "complete"
            or note.derivative_of is not None
            or note.extract_failed
        ):
            continue
        candidate = by_key.get(note.canonical_url) or by_doi.get(note.doi)
        found[note.note_id] = candidate.items if candidate else ()
    return found


def coverage_counts(
    notes: Sequence[Note], candidates: Sequence[Candidate], item_ids: Sequence[str]
) -> dict[str, int]:
    """Per item, the substantive sources its queries found (D6)."""
    counts = dict.fromkeys(item_ids, 0)
    for items in provenance(notes, candidates).values():
        for item in items:
            if item in counts:
                counts[item] += 1
    return counts
