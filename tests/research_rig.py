"""Shared fakes of the Phase-2 tests: a gateway with a denylist and call counts."""

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fixtures_corpus import Built, FakeModels, SimulatedCrash, article, build, doc, site
from langgraph.checkpoint.sqlite import SqliteSaver
from support import make_settings

from app.adapters.outbound.denylist import Denylist
from app.adapters.outbound.errors import DenylistBlocked, OutboundBlocked, SearchUnavailable
from app.adapters.outbound.gateway import (
    Document,
    FetchFailure,
    PreparedQuery,
    ScholarlySource,
)
from app.adapters.outbound.types import ScholarlyRecord, SearchHit
from app.events import MemoryEventSink
from app.graphs.research import ResearchRunner, build_research_graph
from app.llm.fakes import CallbackTransport, reply
from app.llm.roles import build_registry
from app.llm.service import LLMService
from app.llm.types import ChatReply, ChatRequest, Endpoint
from app.pipeline.profiles import load_response_formats
from app.prompts import research as prompts
from app.research.context import ContextBuilders, ContextFactory
from app.research.service import ResearchDeps, ResearchService
from app.research.steps import LightSteps
from app.store.db import Database
from app.store.research import ResearchStore
from app.store.runs import RunStore
from app.templates import load_templates

SANITIZER_FAILS = "SANITIZER-FAILS"  # a query containing this makes the fake sanitizer fail
UNAVAILABLE = "UNAVAILABLE"  # a sent query containing this finds no provider


@dataclass
class FakeGateway:
    """Sanitizes by removing ``secret`` terms, blocks ``denied`` terms, answers searches from
    ``web_hits`` / ``scholarly_hits`` by sent query, and counts every call."""

    denied: tuple[str, ...] = ("geheimprojekt",)
    secret: tuple[str, ...] = ("Firma X",)
    web_hits: dict[str, list[SearchHit]] = field(default_factory=lambda: {})
    scholarly_hits: dict[str, list[ScholarlyRecord]] = field(default_factory=lambda: {})
    crash_prepare_on: int | None = None  # the n-th `prepare_query` call raises `SimulatedCrash`
    crash_search_on: int | None = None  # the n-th `search_web` call raises, before it is charged
    prepared: list[str] = field(default_factory=lambda: [])
    web_calls: list[tuple[str, tuple[str, ...], int]] = field(default_factory=lambda: [])
    scholarly_calls: list[tuple[str, str]] = field(default_factory=lambda: [])
    credits: int = 0

    def _check(self, text: str) -> None:
        if any(term in text.casefold() for term in self.denied):
            raise DenylistBlocked("denylist")

    def prepare_query(self, query: str, *, step: str) -> PreparedQuery:
        self.prepared.append(query)
        if self.crash_prepare_on == len(self.prepared):
            raise SimulatedCrash("crash while sanitizing")
        self._check(query)
        if SANITIZER_FAILS in query:
            raise OutboundBlocked("sanitizer_failed")
        sent, removed = query, []
        for term in self.secret:
            if term in sent:
                sent = " ".join(sent.replace(term, " ").split())
                removed.append(term)
        if not sent:
            raise DenylistBlocked("empty after sanitizing")
        return PreparedQuery(query, sent, tuple(removed))

    def search_web(
        self,
        prepared: PreparedQuery,
        *,
        step: str,
        include_domains: Sequence[str] = (),
        max_results: int = 10,
    ) -> list[SearchHit]:
        self._check(prepared.sent)
        self.web_calls.append((prepared.sent, tuple(include_domains), max_results))
        if self.crash_search_on == len(self.web_calls):
            raise SimulatedCrash("crash while searching")
        if UNAVAILABLE in prepared.sent:
            raise SearchUnavailable("both providers failed")
        self.credits += 1
        return list(self.web_hits.get(prepared.sent, []))[:max_results]

    def search_scholarly(
        self, prepared: PreparedQuery, *, step: str, source: ScholarlySource, max_results: int = 10
    ) -> list[ScholarlyRecord]:
        self._check(prepared.sent)
        self.scholarly_calls.append((source, prepared.sent))
        return [r for r in self.scholarly_hits.get(prepared.sent, []) if r.source == source]

    def fetch(self, url: str, *, step: str) -> Document | FetchFailure:  # pragma: no cover
        return FetchFailure(url, "not used by these tests")

    @property
    def credits_used(self) -> int:
        return self.credits


# ---- the wired Phase-2 service ---------------------------------------------------------------


SETTINGS = make_settings()
NOW = datetime(2026, 10, 4, 9, 0, 0, tzinfo=UTC)
BRIEF = (
    "# Wie teuer ist der Rückbau von Kernkraftwerken?\n\n## Forschungsfragen\n\n"
    "1. Kosten je Anlage\n2. Dauer\n"
)
DECOMPOSITION: dict[str, Any] = {
    "sub_questions": ["Was kostet der Rückbau je Anlage?", "Wie lange dauert er?"],
    "entities": [{"name": "Kernkraftwerk Obrigheim", "type": "plant", "required_fields": []}],
    "time_periods": [{"period": "2005-2025", "type": "range", "primary_source": "", "issuer": ""}],
    "domains": ["regulation_de_eu"],
    "section_headings": ["## Kosten", "## Dauer", "## Einordnung"],
    "section_weights": [2.0, 1.0, 1.0],
    "tier_recommendation": "light",
    "tier_rationale": "Begrenzte Frage.",
    "modality": "synthesize",
    "levers": {"register": "analyze", "inference_depth": "standard", "domain_notes": "Behörden."},
}
MATRIX = {"rows": [{"phrase": "Rückbau", "item_ids": ["i01"], "scope_ok": True, "gap": False}]}
PLAN = [
    {"item_id": "i01", "lens": "breadth", "query": "Rückbau Kosten Kernkraftwerk"},
    {"item_id": "i02", "lens": "breadth", "query": "Rückbau Dauer Kernkraftwerk"},
    {"item_id": "i03", "lens": "breadth", "query": "Obrigheim Stilllegung"},
    {"item_id": "i04", "lens": "period", "query": "Rückbaukosten 2005 bis 2025 Bericht"},
    *[
        {"item_id": "i01", "lens": "adversarial", "query": f"Kritik Rückbaukosten {n}"}
        for n in range(5)
    ],
]


def hit(url: str) -> SearchHit:
    return SearchHit(title=f"T {url}", url=url, snippet="s", provider="tavily")


def corpus_hits() -> dict[str, list[SearchHit]]:
    """Every planned query finds two distinct corpus pages, so every item ends up covered."""
    return {q["query"]: [hit(site(2 * k + 1)), hit(site(2 * k + 2))] for k, q in enumerate(PLAN)}


def corpus_pages() -> dict[str, Any]:
    return {site(n): doc(site(n), article(n)) for n in range(1, 2 * len(PLAN) + 1)}


@dataclass
class ResearchModels:
    """Answers each Phase-2 `reason` call by its system prompt; ``errors`` raise instead."""

    plan: list[dict[str, str]] = field(default_factory=lambda: list(PLAN))
    errors: dict[str, Exception] = field(default_factory=lambda: {})
    crash_on: dict[str, int] = field(default_factory=lambda: {})
    calls: list[str] = field(default_factory=lambda: [])

    def kind(self, request: ChatRequest) -> str:
        first = request.messages[0]["content"].split("\n", 1)[0]
        for name in ("DECOMPOSE", "COVERAGE", "REVISE_DECOMPOSITION", "HEADINGS", "PLAN", "WAVE2"):
            if getattr(prompts, f"{name}_SYSTEM").split("\n", 1)[0] == first:
                return name.lower()
        raise AssertionError(f"unknown prompt: {first!r}")

    def count(self, kind: str) -> int:
        return self.calls.count(kind)

    def __call__(self, request: ChatRequest) -> ChatReply | Exception:
        kind = self.kind(request)
        self.calls.append(kind)
        if self.crash_on.get(kind) == self.count(kind):
            raise SimulatedCrash(f"crash during {kind}")
        if kind in self.errors:
            return self.errors[kind]
        answers = {
            "decompose": DECOMPOSITION,
            "revise_decomposition": DECOMPOSITION,
            "coverage": MATRIX,
            "headings": {"headings": ["Kosten", "Dauer"]},
            "plan": {"queries": self.plan},
            "wave2": {"queries": []},
        }
        return reply(json.dumps(answers[kind]))


@dataclass
class ResearchRig:
    service: ResearchService
    gateway: FakeGateway
    models: ResearchModels
    builts: dict[str, Built]
    denylist: Denylist
    runs: RunStore
    store: ResearchStore
    base: Path
    events: MemoryEventSink
    steps: LightSteps

    def new_run(self, text: str = BRIEF, template_id: str = "auto") -> str:
        row = self.service.create_external_run(
            text,
            tier="light",
            template_id=template_id,
            response_format="structured",
            report_language="de",
        )
        return row.run_id


def _llm(models: ResearchModels, events: MemoryEventSink) -> LLMService:
    return LLMService(
        build_registry(SETTINGS),
        {Endpoint.OWN: "http://own", Endpoint.SHARED: "http://shared"},
        CallbackTransport(models),
        events,
        timeout_s=5,
        sleep=lambda _s: None,
    )


def _builders(
    base: Path, llm: LLMService, gateway: FakeGateway, events: MemoryEventSink
) -> tuple[ContextBuilders, dict[str, Built]]:
    builts: dict[str, Built] = {}

    def built(run_id: str) -> Built:
        if run_id not in builts:
            builts[run_id] = build(base, corpus_pages(), models=FakeModels(), run_id=run_id)
        return builts[run_id]

    return ContextBuilders(
        run_dir=lambda run_id: base / "runs" / run_id,
        events=lambda _d: events,
        llm=lambda _spec: llm,
        gateway=lambda *_a: gateway,
        vault=lambda run_id: built(run_id).vault,
        pipeline=lambda run_id, *_a: built(run_id).pipeline,
    ), builts


def build_research_rig(
    base: Path,
    *,
    models: ResearchModels | None = None,
    gateway: FakeGateway | None = None,
    denylist: Denylist | None = None,
) -> ResearchRig:
    """One 'process' of the Phase-2 service; calling it again on ``base`` is a restart."""
    models = models or ResearchModels()
    gateway = gateway or FakeGateway(web_hits=corpus_hits())
    denylist = denylist or Denylist([])
    events = MemoryEventSink()
    db = Database(base / "udr.sqlite", now=lambda: NOW)
    runs, store = RunStore(db), ResearchStore(db)
    templates = load_templates([SETTINGS.templates_dir])
    formats = load_response_formats(SETTINGS.config_dir)
    builders, builts = _builders(base, _llm(models, events), gateway, events)
    contexts = ContextFactory(
        runs=runs,
        store=store,
        templates=templates,
        formats=formats,
        config_dir=SETTINGS.config_dir,
        build=builders,
    )
    steps = LightSteps(contexts)
    saver = SqliteSaver(sqlite3.connect(base / "checkpoints.sqlite", check_same_thread=False))
    runner = ResearchRunner(build_research_graph(steps, saver))
    service = ResearchService(
        ResearchDeps(
            runs=runs,
            store=store,
            runner=runner,
            steps=steps,
            templates=templates,
            formats=formats,
            briefs_dir=base / "briefs",
            run_dir=lambda run_id: base / "runs" / run_id,
            credits=lambda _run_id: gateway.credits_used,
            load_denylist=lambda: denylist,
            now=lambda: NOW,
        )
    )
    return ResearchRig(service, gateway, models, builts, denylist, runs, store, base, events, steps)
