"""Step 2 (PRD M5): search execution, URL queue, ingestion, coverage and wave 2."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fixtures_corpus import PROFILE, Built, article, build, doc, failure
from research_rig import UNAVAILABLE, FakeGateway
from support import make_settings

from app.adapters.outbound.types import ScholarlyRecord, SearchHit
from app.events import MemoryEventSink
from app.llm.errors import LLMModelMissingError
from app.llm.fakes import CallbackTransport, reply
from app.llm.roles import build_registry
from app.llm.service import LLMService
from app.llm.types import ChatReply, ChatRequest, Endpoint
from app.pipeline.strategies import load_strategies
from app.prompts import research as prompts
from app.research.models import Item
from app.research.plan import PlanScope
from app.research.sweep import (
    SweepScope,
    build_queue,
    coverage,
    run_queries,
    run_sweep,
)
from app.store.db import Database
from app.store.research import QueryDraft, QueryRow, ResearchStore

SETTINGS = make_settings()
STRATEGIES = load_strategies(SETTINGS.config_dir)
URLS = {Endpoint.OWN: "http://own", Endpoint.SHARED: "http://shared"}
ITEMS = [Item("i01", "sub_question", "Kosten"), Item("i02", "entity", "Obrigheim")]
SHIM = "## Run directives\n"


def url(i: int, host: str = "example.org") -> str:
    return f"https://s{i}.{host}/a{i}"


def hit(u: str, provider: str = "tavily") -> SearchHit:
    return SearchHit(title=f"T {u}", url=u, snippet="s", provider=provider)


@dataclass
class Wave2Models:
    queries: list[dict[str, str]] = field(default_factory=lambda: [])
    error: Exception | None = None
    calls: list[ChatRequest] = field(default_factory=lambda: [])

    def __call__(self, request: ChatRequest) -> ChatReply | Exception:
        first = request.messages[0]["content"].split("\n", 1)[0]
        assert first == prompts.WAVE2_SYSTEM.split("\n", 1)[0]
        self.calls.append(request)
        return self.error or reply(json.dumps({"queries": self.queries}))


@dataclass
class Rig:
    built: Built
    store: ResearchStore
    gateway: FakeGateway
    wave2: Wave2Models
    scope: SweepScope
    events: MemoryEventSink

    @property
    def run_id(self) -> str:
        return self.scope.plan.run_id

    def plan(self, *queries: tuple[str, str, str, str]) -> list[QueryRow]:
        """(item, lens, channel, sent) rows, already sanitized and planned."""
        drafts = [QueryDraft(i, lens, ch, sent) for i, lens, ch, sent in queries]  # type: ignore[arg-type]
        rows = self.store.insert_drafts(self.run_id, 1, drafts)
        for row in rows:
            self.store.set_sanitized(self.run_id, row.query_id, row.original, [], "planned")
        return self.store.rows(self.run_id)


def rig(
    tmp_path: Path,
    outcomes: dict[str, Any] | None = None,
    *,
    gateway: FakeGateway | None = None,
    wave2: Wave2Models | None = None,
    domains: tuple[str, ...] = (),
    profile: Any = PROFILE,
) -> Rig:
    built = build(tmp_path, outcomes or {}, profile=profile)
    store = ResearchStore(Database(tmp_path / "udr.sqlite"))
    gateway = gateway or FakeGateway()
    wave2 = wave2 or Wave2Models()
    events = MemoryEventSink()
    llm = LLMService(
        build_registry(SETTINGS),
        URLS,
        CallbackTransport(wave2),
        events,
        timeout_s=5,
        sleep=lambda _s: None,
    )
    scholarly = any(STRATEGIES.domains[d].scholarly_first for d in domains)  # type: ignore[index]
    plan = PlanScope(built.vault.run_id, built.run_dir, store, gateway, ITEMS, scholarly)
    scope = SweepScope(
        plan=plan,
        pipeline=built.pipeline,
        vault=built.vault,
        profile=profile,
        strategies=STRATEGIES,
        domains=domains,  # type: ignore[arg-type]
        events=events,
        llm=llm,
        brief="# Kosten\n\n1. Kosten\n",
        shim=SHIM,
    )
    return Rig(built, store, gateway, wave2, scope, events)


def pages(*numbers: int) -> dict[str, Any]:
    return {url(n): doc(url(n), article(n)) for n in numbers}


# ---- searching ------------------------------------------------------------------------------


def test_hits_are_stored_with_the_row_and_a_done_row_is_never_searched_again(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway(web_hits={"Kosten": [hit(url(1)), hit(url(2))]})
    r = rig(tmp_path, gateway=gateway)
    r.plan(("i01", "breadth", "web", "Kosten"))
    run_queries(r.scope, 1)
    (row,) = r.store.rows(r.run_id)
    assert row.state == "done"
    assert [h["url"] for h in row.hits] == [url(1), url(2)]
    assert row.hits[0]["provider"] == "tavily"
    run_queries(r.scope, 1)
    assert len(gateway.web_calls) == 1


def test_include_domains_only_for_depth_and_period_queries(tmp_path: Path) -> None:
    r = rig(tmp_path, domains=("regulation_de_eu",))
    r.plan(
        ("i01", "breadth", "web", "a"),
        ("i01", "depth", "web", "b"),
        ("i01", "adversarial", "web", "c"),
        ("i02", "period", "web", "d"),
    )
    run_queries(r.scope, 1)
    hints = {sent: domains for sent, domains, _ in r.gateway.web_calls}
    expected = STRATEGIES.domains["regulation_de_eu"].include_domains
    assert hints == {"a": (), "b": expected, "c": (), "d": expected}
    assert {n for _, _, n in r.gateway.web_calls} == {PROFILE.results_per_query}


def test_scholarly_queries_use_openalex_and_crossref_and_arxiv_only_for_stem(
    tmp_path: Path,
) -> None:
    record = ScholarlyRecord(
        source="openalex",
        title="Paper",
        url="https://doi.org/10.1/x",
        doi="10.1/x",
        year=2024,
        oa_url="https://repo.example.org/x.pdf",
    )
    gateway = FakeGateway(scholarly_hits={"rückbau": [record]})
    r = rig(tmp_path, gateway=gateway, domains=("regulation_de_eu", "science_medicine"))
    r.plan(("i01", "depth", "scholarly", "rückbau"))
    run_queries(r.scope, 1)
    assert [s for s, _ in gateway.scholarly_calls] == ["openalex", "crossref", "arxiv"]
    (row,) = r.store.rows(r.run_id)
    assert row.hits[0]["url"] == "https://repo.example.org/x.pdf"  # the open-access copy
    assert (row.hits[0]["doi"], row.hits[0]["provider"]) == ("10.1/x", "openalex")

    other = rig(tmp_path / "b", gateway=FakeGateway(), domains=("regulation_de_eu",))
    other.plan(("i01", "depth", "scholarly", "rückbau"))
    run_queries(other.scope, 1)
    assert [s for s, _ in other.gateway.scholarly_calls] == ["openalex", "crossref"]


def test_an_unavailable_search_marks_the_row_failed(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.plan(("i01", "breadth", "web", f"x {UNAVAILABLE}"))
    run_queries(r.scope, 1)
    (row,) = r.store.rows(r.run_id)
    assert (row.state, row.reason) == ("failed", "unavailable")


def test_only_planned_rows_are_searched(tmp_path: Path) -> None:
    r = rig(tmp_path)
    rows = r.plan(("i01", "breadth", "web", "a"), ("i01", "breadth", "web", "b"))
    r.store.delete(r.run_id, rows[0].query_id)
    run_queries(r.scope, 1)
    assert [sent for sent, _, _ in r.gateway.web_calls] == ["b"]


# ---- the queue (pure) -----------------------------------------------------------------------


def done_row(qid: str, item: str, urls: list[str], wave: int = 1) -> QueryRow:
    hits = tuple({"url": u, "title": "t", "snippet": "", "provider": "tavily"} for u in urls)
    return QueryRow("r", qid, wave, item, "breadth", "web", "q", "q", (), "done", "", hits)


def test_the_queue_deduplicates_and_keeps_every_item_id() -> None:
    rows = [
        done_row("q001", "i01", [url(1), url(2)]),
        done_row("q002", "i02", [url(1) + "?utm_source=x", url(3)]),
    ]
    queue = build_queue(rows, set(), candidates=40, cap=30, strategies=STRATEGIES)
    by_url = {e.url: e.item_ids for e in queue}
    assert by_url == {url(1): ("i01", "i02"), url(2): ("i01",), url(3): ("i02",)}


def test_the_queue_skips_known_urls_and_respects_the_caps() -> None:
    rows = [done_row("q001", "i01", [url(n) for n in range(1, 11)])]
    known = {url(1)}
    queue = build_queue(rows, known, candidates=6, cap=4, strategies=STRATEGIES)
    assert [e.url for e in queue] == [url(2), url(3), url(4), url(5)]
    assert len(build_queue(rows, known, candidates=3, cap=10, strategies=STRATEGIES)) == 2


def test_the_queue_takes_items_round_robin_and_prefers_strong_hosts() -> None:
    rows = [
        done_row("q001", "i01", [url(1), url(2), url(3)]),
        done_row("q002", "i02", [url(4), "https://www.bund.de/x", url(5)]),
    ]
    queue = build_queue(rows, set(), candidates=40, cap=4, strategies=STRATEGIES)
    assert [e.url for e in queue] == [url(1), "https://www.bund.de/x", url(2), url(4)]


# ---- ingestion and coverage -----------------------------------------------------------------


def test_the_sweep_fetches_the_queue_and_reports_coverage(tmp_path: Path) -> None:
    gateway = FakeGateway(web_hits={"a": [hit(url(n)) for n in (1, 2, 3, 4)], "b": [hit(url(5))]})
    r = rig(tmp_path, pages(1, 2, 3, 4, 5), gateway=gateway)
    r.plan(("i01", "breadth", "web", "a"), ("i02", "breadth", "web", "b"))
    result = run_sweep(r.scope)
    assert sorted(r.built.fetcher.calls) == sorted(url(n) for n in (1, 2, 3, 4, 5))
    status = {c.item_id: (c.sources, c.status) for c in result.coverage}
    assert status == {"i01": (4, "well"), "i02": (1, "thin")}
    gaps = (r.built.run_dir / "temp" / "coverage-gaps.md").read_text(encoding="utf-8")
    assert "| i01 | Kosten | well | 4 |" in gaps
    assert "| i02 | Obrigheim | thin | 1 |" in gaps


def test_coverage_counts_only_complete_original_non_wikipedia_sources(tmp_path: Path) -> None:
    wiki = "https://de.wikipedia.org/wiki/Obrigheim"
    outcomes = {**pages(1, 2), wiki: doc(wiki, article(9)), url(3): failure(url(3), "http_404")}
    gateway = FakeGateway(web_hits={"a": [hit(url(1)), hit(url(2)), hit(wiki), hit(url(3))]})
    r = rig(tmp_path, outcomes, gateway=gateway)
    r.plan(("i01", "breadth", "web", "a"))
    run_queries(r.scope, 1)
    r.built.pipeline.ingest_many([(u, None) for u in (url(1), url(2), wiki, url(3))])
    result = coverage(ITEMS, r.store.rows(r.run_id), r.built.vault, PROFILE.thin_sources)
    assert [(c.item_id, c.sources, c.status) for c in result] == [
        ("i01", 2, "adequate"),
        ("i02", 0, "uncovered"),
    ]


def test_a_failed_query_and_a_short_corpus_are_listed(tmp_path: Path) -> None:
    gateway = FakeGateway(web_hits={"a": [hit(url(1))]})
    r = rig(tmp_path, pages(1), gateway=gateway)
    r.plan(("i01", "breadth", "web", "a"), ("i02", "breadth", "web", f"b {UNAVAILABLE}"))
    run_sweep(r.scope)
    gaps = (r.built.run_dir / "temp" / "coverage-gaps.md").read_text(encoding="utf-8")
    assert f"b {UNAVAILABLE}" in gaps
    assert "unavailable" in gaps
    assert f"1 of at least {PROFILE.sources_min}" in gaps


# ---- wave 2 ---------------------------------------------------------------------------------


def wave2_rig(tmp_path: Path, queries: list[dict[str, str]], **kw: Any) -> Rig:
    hits = {"a": [hit(url(n)) for n in (1, 2, 3, 4)]}
    hits["Obrigheim Rückbau"] = [hit(url(n)) for n in range(10, 30)]
    gateway = FakeGateway(web_hits=hits)
    outcomes = pages(1, 2, 3, 4, *range(10, 30))
    r = rig(tmp_path, outcomes, gateway=gateway, wave2=Wave2Models(queries=queries), **kw)
    r.plan(("i01", "breadth", "web", "a"))
    return r


def test_wave_two_targets_only_thin_items_and_is_sanitized_and_capped(tmp_path: Path) -> None:
    queries = [
        {"item_id": "i02", "query": "Obrigheim Rückbau"},
        {"item_id": "i02", "query": "Firma X Obrigheim Kosten"},
        {"item_id": "i02", "query": "Geheimprojekt Obrigheim"},
        {"item_id": "i02", "query": "Obrigheim Zeitplan"},
        {"item_id": "i01", "query": "Kosten noch einmal"},  # i01 is well covered
    ]
    r = wave2_rig(tmp_path, queries)
    result = run_sweep(r.scope)
    assert len(r.wave2.calls) == 1
    assert "i02" in r.wave2.calls[0].messages[-1]["content"]
    assert r.wave2.calls[0].messages[0]["content"].endswith(SHIM)
    assert not r.wave2.calls[0].think
    wave2 = r.store.rows(r.run_id, wave=2)
    assert [row.original for row in wave2] == queries_for("i02", queries)[
        : PROFILE.wave2_queries_per_item
    ]
    assert {row.lens for row in wave2} == {"breadth"}
    states = {row.original: (row.state, row.sent) for row in wave2}
    assert states["Firma X Obrigheim Kosten"] == ("done", "Obrigheim Kosten")
    assert states["Geheimprojekt Obrigheim"][0] == "blocked"
    fetched_in_wave2 = [u for u in r.built.fetcher.calls if u not in pages(1, 2, 3, 4)]
    assert len(fetched_in_wave2) == PROFILE.wave2_urls
    assert dict((c.item_id, c.status) for c in result.coverage)["i02"] == "well"


def queries_for(item: str, queries: list[dict[str, str]]) -> list[str]:
    return [q["query"] for q in queries if q["item_id"] == item]


def test_wave_two_is_not_redrafted_on_resume(tmp_path: Path) -> None:
    r = wave2_rig(tmp_path, [{"item_id": "i02", "query": "Obrigheim Rückbau"}])
    run_sweep(r.scope)
    run_sweep(r.scope)
    assert len(r.wave2.calls) == 1
    assert len(r.store.rows(r.run_id, wave=2)) == 1


def test_a_wave_two_drafting_error_is_not_fatal(tmp_path: Path) -> None:
    r = wave2_rig(tmp_path, [])
    r.wave2.error = LLMModelMissingError("gone")
    result = run_sweep(r.scope)
    assert r.store.rows(r.run_id, wave=2) == []
    assert len(r.events.of_type("wave2_failed")) == 1
    assert dict((c.item_id, c.status) for c in result.coverage)["i02"] == "uncovered"


def test_no_wave_two_when_every_item_is_covered(tmp_path: Path) -> None:
    gateway = FakeGateway(
        web_hits={"a": [hit(url(n)) for n in (1, 2)], "b": [hit(url(3)), hit(url(4))]}
    )
    r = rig(tmp_path, pages(1, 2, 3, 4), gateway=gateway)
    r.plan(("i01", "breadth", "web", "a"), ("i02", "breadth", "web", "b"))
    run_sweep(r.scope)
    assert r.wave2.calls == []


def test_the_wave_one_queue_is_kept_for_a_resume(tmp_path: Path) -> None:
    hits = {f"q{k}": [hit(url(n)) for n in range(k * 10 + 1, k * 10 + 11)] for k in range(4)}
    gateway = FakeGateway(web_hits=hits)
    profile = PROFILE.model_copy(update={"fetch_waves": 1})
    r = rig(tmp_path, pages(*range(1, 41)), gateway=gateway, profile=profile)
    r.plan(*[("i01", "breadth", "web", f"q{k}") for k in range(4)])
    run_sweep(r.scope)
    first = list(r.built.fetcher.calls)
    assert len(first) == PROFILE.deduped_urls[1]
    run_sweep(r.scope)  # stored sources are reused; no new URLs fill the cap
    assert r.built.fetcher.calls == first
