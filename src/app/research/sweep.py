"""Step 2 (PRD M5): run the planned searches, fetch what they found, check coverage per item and
search again once for thin items (wave 2). Everything is stored as it happens (AD10): a search is
`done` together with its hits and is never repeated; each wave's URL queue is kept, so a resumed
sweep fetches the same URLs and spends no credit twice."""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from app.adapters.outbound.errors import OutboundBlocked, SearchUnavailable
from app.adapters.outbound.gateway import PreparedQuery, ScholarlySource
from app.adapters.outbound.types import ScholarlyRecord, SearchHit
from app.artifacts import write_text
from app.events import EventSink
from app.llm.errors import LLMError
from app.llm.service import LLMService
from app.llm.types import Message, Role
from app.pipeline.fetch import MAX_ATTEMPTS, FetchPipeline
from app.pipeline.profiles import Profile
from app.pipeline.strategies import DomainName, SourceStrategies, tier_for
from app.pipeline.urls import dedup_key
from app.prompts import research as prompts
from app.research.journal import Journal
from app.research.models import Item
from app.research.plan import PlanScope, sanitize_pending, write_plan_file
from app.research.schemas import Wave2Draft
from app.store.models import SourceMeta
from app.store.research import QueryDraft, QueryRow
from app.store.vault import Vault

STEM_DOMAINS: tuple[DomainName, ...] = ("tech_standards", "science_medicine")
Status = Literal["uncovered", "thin", "adequate", "well"]
WIKIPEDIA = "wikipedia.org"


@dataclass(frozen=True)
class SweepScope:
    """Everything the sweep of one run touches."""

    plan: PlanScope
    pipeline: FetchPipeline
    vault: Vault
    profile: Profile
    strategies: SourceStrategies
    domains: Sequence[DomainName]
    events: EventSink
    llm: LLMService
    brief: str
    shim: str


@dataclass(frozen=True)
class QueueEntry:
    url: str
    meta: SourceMeta
    item_ids: tuple[str, ...]


@dataclass(frozen=True)
class ItemCoverage:
    item_id: str
    sources: int
    status: Status


@dataclass(frozen=True)
class SweepResult:
    coverage: list[ItemCoverage]
    sources_total: int
    failed_queries: list[QueryRow]


# ---- searching ------------------------------------------------------------------------------


def _record_hit(record: ScholarlyRecord) -> dict[str, Any]:
    return {
        "url": record.oa_url or record.url,
        "title": record.title,
        "snippet": record.abstract or "",
        "provider": record.source,
        "doi": record.doi,
        "year": record.year,
        "authors": list(record.authors),
        "venue": record.venue,
        "cited_by_count": record.cited_by_count,
        "is_retracted": record.is_retracted,
        "oa_url": record.oa_url,
    }


def _web_hit(hit: SearchHit) -> dict[str, Any]:
    return {"url": hit.url, "title": hit.title, "snippet": hit.snippet, "provider": hit.provider}


def _include_domains(scope: SweepScope, lens: str) -> tuple[str, ...]:
    if lens not in ("depth", "period"):
        return ()
    found: list[str] = []
    for name in scope.domains:
        for host in scope.strategies.domains[name].include_domains:
            if host not in found:
                found.append(host)
    return tuple(found)


def _scholarly_sources(scope: SweepScope) -> tuple[ScholarlySource, ...]:
    stem = any(d in STEM_DOMAINS for d in scope.domains)
    return ("openalex", "crossref", "arxiv") if stem else ("openalex", "crossref")


def _search_scholarly(scope: SweepScope, prepared: PreparedQuery) -> list[dict[str, Any]]:
    gateway, failed = scope.plan.gateway, 0
    hits: list[dict[str, Any]] = []
    sources = _scholarly_sources(scope)
    for source in sources:
        try:
            records = gateway.search_scholarly(prepared, step="2", source=source)
        except SearchUnavailable:
            failed += 1
            continue
        hits += [_record_hit(r) for r in records]
    if failed == len(sources):
        raise SearchUnavailable("no scholarly source answered")
    return hits


def _search_one(scope: SweepScope, row: QueryRow) -> list[dict[str, Any]]:
    prepared = PreparedQuery(row.original, row.sent, row.removed)
    if row.channel == "scholarly":
        return _search_scholarly(scope, prepared)
    found = scope.plan.gateway.search_web(
        prepared,
        step="2",
        include_domains=_include_domains(scope, row.lens),
        max_results=scope.profile.results_per_query,
    )
    return [_web_hit(h) for h in found]


def run_queries(scope: SweepScope, wave: int) -> None:
    """Search every `planned` row of ``wave`` in id order; a `done` row is never searched again."""
    store, run_id = scope.plan.store, scope.plan.run_id
    for row in store.rows(run_id, wave=wave):
        if row.state != "planned":
            continue
        try:
            hits = _search_one(scope, row)
        except SearchUnavailable:
            store.set_result(run_id, row.query_id, "failed", [], "unavailable")
        except OutboundBlocked as exc:
            store.set_result(run_id, row.query_id, "failed", [], exc.reason)
        else:
            store.set_result(run_id, row.query_id, "done", hits)


# ---- the URL queue (pure) -------------------------------------------------------------------


def _meta(hit: dict[str, Any]) -> SourceMeta:
    scholarly = hit.get("provider") in ("openalex", "crossref", "arxiv")
    return SourceMeta(
        doi=hit.get("doi"),
        scholarly=scholarly,
        year=hit.get("year"),
        authors=tuple(hit.get("authors") or ()),
        venue=hit.get("venue"),
        cited_by_count=hit.get("cited_by_count"),
        is_retracted=hit.get("is_retracted"),
        oa_url=hit.get("oa_url"),
    )


def _collect(
    rows: Sequence[QueryRow], known_keys: set[str], candidates: int
) -> dict[str, tuple[dict[str, Any], list[str]]]:
    """The first ``candidates`` hits by dedup key, unknown ones only, with the items that found
    each (the hit keeps its rank)."""
    hits = [(row.item_id, h) for row in rows for h in row.hits][:candidates]
    entries: dict[str, tuple[dict[str, Any], list[str]]] = {}
    for rank, (item_id, hit) in enumerate(hits):
        key = dedup_key(hit["url"])
        if key in known_keys:
            continue
        found = entries.setdefault(key, ({**hit, "_rank": rank}, []))
        if item_id not in found[1]:
            found[1].append(item_id)
    return entries


def _round_robin(queues: list[list[str]], cap: int) -> list[str]:
    chosen: list[str] = []
    while len(chosen) < cap and any(queues):
        for queue in queues:
            while queue and queue[0] in chosen:
                queue.pop(0)
            if queue and len(chosen) < cap:
                chosen.append(queue.pop(0))
    return chosen


def build_queue(
    rows: Sequence[QueryRow],
    known_keys: set[str],
    *,
    candidates: int,
    cap: int,
    strategies: SourceStrategies,
) -> list[QueueEntry]:
    """The URLs to fetch: the first ``candidates`` hits in row order, deduplicated, without known
    sources; per item the strongest host tier first, then taken round-robin over the items."""
    entries = _collect(rows, known_keys, candidates)
    by_item: dict[str, list[str]] = defaultdict(list)
    for key, (_, item_ids) in entries.items():
        for item_id in item_ids:
            by_item[item_id].append(key)

    def weight(key: str) -> tuple[float, int]:
        hit = entries[key][0]
        meta = _meta(hit)
        tier = tier_for(hit["url"], strategies, has_doi=bool(meta.doi), scholarly=meta.scholarly)
        return (-strategies.tier_weights[tier], hit["_rank"])

    queues = [sorted(by_item[i], key=weight) for i in sorted(by_item)]
    chosen = _round_robin(queues, cap)
    return [
        QueueEntry(entries[k][0]["url"], _meta(entries[k][0]), tuple(entries[k][1])) for k in chosen
    ]


# ---- coverage -------------------------------------------------------------------------------


def _status(sources: int, thin: int) -> Status:
    if sources == 0:
        return "uncovered"
    if sources <= thin:
        return "thin"
    return "adequate" if sources <= 3 else "well"


def _is_source(vault: Vault, key: str, doi: str | None) -> str | None:
    """The note id of a complete, original, citable source behind a hit, if there is one."""
    note = vault.find_by_canonical(key) or vault.find_by_doi(doi)
    if note is None or note.kind != "source" or note.stage != "complete":
        return None
    if note.derivative_of is not None or WIKIPEDIA in urlsplit(note.canonical_url).netloc:
        return None
    return note.note_id


def coverage(
    items: Sequence[Item], rows: Sequence[QueryRow], vault: Vault, thin_sources: int
) -> list[ItemCoverage]:
    """Per item: distinct complete, non-derivative sources found by that item's queries."""
    found: dict[str, set[str]] = {i.item_id: set() for i in items}
    for row in rows:
        for hit in row.hits:
            note_id = _is_source(vault, dedup_key(hit["url"]), hit.get("doi"))
            if note_id is not None and row.item_id in found:
                found[row.item_id].add(note_id)
    return [
        ItemCoverage(i.item_id, len(found[i.item_id]), _status(len(found[i.item_id]), thin_sources))
        for i in items
    ]


def _sources_total(vault: Vault) -> int:
    return sum(1 for n in vault.notes(kind="source") if _is_source(vault, n.canonical_url, n.doi))


# ---- fetching -------------------------------------------------------------------------------


def _known_keys(vault: Vault) -> set[str]:
    known = {n.canonical_url for n in vault.notes()}
    known |= {
        r.canonical_url for r in vault.rejections() if not r.retryable or r.attempts >= MAX_ATTEMPTS
    }
    return known


def _queue(scope: SweepScope, journal: Journal, wave: int, cap: int) -> list[QueueEntry]:
    """The wave's queue, built once and kept, so a resume fetches the same URLs."""
    key = f"queue{wave}"
    kept = journal.get(key)
    if kept is None:
        rows = scope.plan.store.rows(scope.plan.run_id)
        built = build_queue(
            rows,
            _known_keys(scope.vault),
            candidates=scope.profile.candidate_urls[1],
            cap=cap,
            strategies=scope.strategies,
        )
        kept = [
            {"url": e.url, "meta": e.meta.__dict__, "item_ids": list(e.item_ids)} for e in built
        ]
        journal.put(key, kept)
    return [_entry(k) for k in cast("list[dict[str, Any]]", kept)]


def _entry(kept: dict[str, Any]) -> QueueEntry:
    meta = {**cast("dict[str, Any]", kept["meta"])}
    meta["authors"] = tuple(meta.get("authors") or ())
    return QueueEntry(str(kept["url"]), SourceMeta(**meta), tuple(kept["item_ids"]))


def _fetch_wave(scope: SweepScope, journal: Journal, wave: int, cap: int) -> None:
    entries = _queue(scope, journal, wave, cap)
    scope.pipeline.resume()
    scope.pipeline.ingest_many([(e.url, e.meta) for e in entries])


# ---- wave 2 ---------------------------------------------------------------------------------


def _draft_wave2(scope: SweepScope, thin: Sequence[Item]) -> list[QueryDraft]:
    store, run_id = scope.plan.store, scope.plan.run_id
    used = [r.sent or r.original for r in store.rows(run_id) if r.state != "deleted"]
    per_item = scope.profile.wave2_queries_per_item
    system = prompts.WAVE2_SYSTEM.format(per_item=per_item) + f"\n\n{scope.shim}"
    user = prompts.WAVE2_USER.format(
        items="\n".join(f"{i.item_id}: {i.text}" for i in thin),
        used="\n".join(f"- {q}" for q in used),
        brief=scope.brief,
    )
    messages: list[Message] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    draft = scope.llm.structured(Role.REASON, messages, Wave2Draft, think=False)
    allowed = {i.item_id for i in thin}
    counts: dict[str, int] = defaultdict(int)
    drafts: list[QueryDraft] = []
    for q in draft.queries:
        if q.item_id in allowed and q.query and counts[q.item_id] < per_item:
            counts[q.item_id] += 1
            drafts.append(QueryDraft(q.item_id, "breadth", "web", q.query))
    return drafts


def _wave_two(scope: SweepScope, journal: Journal, result: list[ItemCoverage]) -> bool:
    """Draft, sanitize, search and fetch wave 2 once; True if it ran."""
    store, run_id = scope.plan.store, scope.plan.run_id
    if scope.profile.fetch_waves < 2:
        return False
    if not store.rows(run_id, wave=2):
        thin_ids = {c.item_id for c in result if c.status in ("thin", "uncovered")}
        thin = [i for i in scope.plan.items if i.item_id in thin_ids]
        if not thin:
            return False
        try:
            drafts = _draft_wave2(scope, thin)
        except LLMError as exc:
            scope.events.emit("wave2_failed", level="warning", error=type(exc).__name__)
            return False
        if drafts:
            store.insert_drafts(run_id, 2, drafts)
    sanitize_pending(scope.plan, wave=2, step="2")
    run_queries(scope, 2)
    _fetch_wave(scope, journal, 2, scope.profile.wave2_urls)
    return True


# ---- the step -------------------------------------------------------------------------------


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def write_gaps(scope: SweepScope, result: SweepResult) -> None:
    """`temp/coverage-gaps.md`: every item with status and count, failed queries, source total."""
    text = {i.item_id: i.text for i in scope.plan.items}
    lines = [
        "## Coverage gaps",
        "",
        "| Item | Text | Status | Sources |",
        "|---|---|---|---|",
    ]
    for c in result.coverage:
        lines.append(f"| {c.item_id} | {_cell(text[c.item_id])} | {c.status} | {c.sources} |")
    uncovered = [c.item_id for c in result.coverage if c.status == "uncovered"]
    if uncovered:
        lines += ["", f"**Genuine gaps (no source): {', '.join(uncovered)}**"]
    if result.failed_queries:
        lines += ["", "## Failed queries", ""]
        lines += [f"- {_cell(q.sent or q.original)}: {q.reason}" for q in result.failed_queries]
    target = scope.profile.sources_min
    lines += ["", f"Sources found: {result.sources_total} of at least {target}."]
    write_text(scope.plan.run_dir / "temp" / "coverage-gaps.md", "\n".join(lines) + "\n")


def run_sweep(scope: SweepScope) -> SweepResult:
    """Step 2 from the approved plan to the coverage report. Safe to call again after a crash."""
    plan, profile = scope.plan, scope.profile
    journal = Journal(plan.run_dir / "temp" / "sweep.json")
    run_queries(scope, 1)
    _fetch_wave(scope, journal, 1, profile.deduped_urls[1])
    first = coverage(plan.items, plan.store.rows(plan.run_id), scope.vault, profile.thin_sources)
    if _wave_two(scope, journal, first):
        write_plan_file(plan)
    rows = plan.store.rows(plan.run_id)
    result = SweepResult(
        coverage(plan.items, rows, scope.vault, profile.thin_sources),
        _sources_total(scope.vault),
        [r for r in rows if r.state == "failed"],
    )
    write_gaps(scope, result)
    return result
