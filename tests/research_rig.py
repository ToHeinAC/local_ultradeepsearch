"""Shared rig of the Phase-2 tests: a scripted model that answers by schema or prompt, and the
parts of a research run wired on fakes."""

import json
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from support import make_settings

from app.adapters.outbound.gateway import PreparedQuery
from app.adapters.outbound.types import ScholarlyRecord, SearchHit
from app.events import MemoryEventSink
from app.llm.fakes import CallbackTransport, reply
from app.llm.roles import build_registry
from app.llm.service import LLMService
from app.llm.types import ChatReply, ChatRequest, Endpoint
from app.pipeline.profiles import load_research_budget, load_response_formats, load_run_rules
from app.research.manifest import RunSettings
from app.templates import load_templates

SETTINGS = make_settings()
RULES = load_run_rules(SETTINGS.config_dir)
LIGHT = load_research_budget("light", SETTINGS.config_dir)
FORMATS = load_response_formats(SETTINGS.config_dir)
TEMPLATES = load_templates([SETTINGS.templates_dir])
URLS = {Endpoint.OWN: "http://own", Endpoint.SHARED: "http://shared"}
REGISTRY = build_registry(SETTINGS)
NOW = datetime(2026, 10, 3, 9, 0, 0, tzinfo=UTC)

QUESTION = "Wie lange dauert der Rückbau eines Forschungsreaktors?"
BRIEF = (
    f"# {QUESTION}\n\n"
    "Method: 1 Klärungsrunde\n\n"
    "## Forschungsfragen\n\n"
    "1. Wie lange dauert der Rückbau?\n"
    "2. Welche Genehmigungen sind nötig?\n\n"
    "## Ausgabe\n\n"
    "- **Berichtssprache: Deutsch.** Der gesamte Bericht wird auf Deutsch verfasst.\n"
)
RUN_SETTINGS = RunSettings(
    report_language="de",
    response_format="structured",
    template_id="technische-stellungnahme",
    interview_language="de",
    tier="light",
    summarize_model=None,
)
AUTO_SETTINGS = RUN_SETTINGS.model_copy(update={"template_id": "auto"})

DRAFT: dict[str, Any] = {
    "entities": [{"name": "Forschungsreaktor", "type": "Anlage", "required_fields": ["Dauer"]}],
    "required_formats": [],
    "required_sections": [],
    "required_section_headings": [],
    "time_horizons": [],
    "time_periods": [],
    "scope_conditions": ["Deutschland"],
    "domains": ["regulation_de_eu"],
    "tier_recommendation": "light",
    "tier_rationale": "Eine begrenzte Frage mit klarer Antwort.",
    "modality": "collect",
    "levers": {
        "voice": "analyze",
        "voice_confidence": "low",
        "domain_notes": "Behördliche Quellen zuerst; Genehmigungsrecht der letzten Jahre.",
        "inference_depth": "standard",
    },
}
CLEAN_MATRIX: dict[str, Any] = {
    "rows": [
        {"phrase": "Rückbau", "items": ["Q1"], "scope_ok": True},
        {"phrase": "Forschungsreaktor", "items": ["E1"], "scope_ok": True},
    ]
}


def q(item: str, lens: str, text: str) -> dict[str, str]:
    return {"item": item, "lens": lens, "query": text}


PLAN: dict[str, Any] = {
    "queries": [
        q("Q1", "A", "Rückbau Forschungsreaktor Dauer"),
        q("Q1", "B", "decommissioning research reactor duration study"),
        q("Q1", "C", "Rückbau Forschungsreaktor Verzögerungen Kritik"),
        q("Q2", "A", "Genehmigung Rückbau Atomgesetz"),
        q("Q2", "B", "nuclear decommissioning licensing review"),
        q("Q2", "C", "Genehmigung Rückbau Probleme Klage"),
        q("E1", "A", "Forschungsreaktor Stilllegung Stand"),
        q("E1", "C", "Forschungsreaktor Rückbau gescheitert"),
        q("E1", "C", "Forschungsreaktor Stilllegung Mehrkosten"),
        q("E1", "C", "Forschungsreaktor Rückbau Gegenargumente"),
    ]
}


class FakePreparer:
    """Stands in for the gateway's `prepare_query`: nothing leaves, every call is recorded.

    ``rewrite`` maps a query to what the sanitizer would send; ``refuse`` maps a query to the
    error the gateway would raise."""

    def __init__(
        self,
        rewrite: dict[str, tuple[str, tuple[str, ...]]] | None = None,
        refuse: dict[str, Exception] | None = None,
    ) -> None:
        self.rewrite = rewrite or {}
        self.refuse = refuse or {}
        self.calls: list[tuple[str, str]] = []

    def prepare_query(self, query: str, *, step: str) -> PreparedQuery:
        self.calls.append((query, step))
        if query in self.refuse:
            raise self.refuse[query]
        sent, removed = self.rewrite.get(query, (query, ()))
        return PreparedQuery(query, sent, removed)


class SearchCrash(BaseException):
    """A crash that no `except Exception` may swallow, like a power cut."""


class FakeSearcher:
    """Stands in for the gateway's searches: answers by sent query and records every call as
    `(source, sent)`. ``fail`` maps `(source, sent)` to the error to raise; ``crash_at`` raises a
    `SearchCrash` on that (1-based) call."""

    def __init__(
        self,
        web: dict[str, list[SearchHit]] | None = None,
        scholarly: dict[tuple[str, str], list[ScholarlyRecord]] | None = None,
        fail: dict[tuple[str, str], Exception] | None = None,
        crash_at: int | None = None,
    ) -> None:
        self.web = web or {}
        self.scholarly = scholarly or {}
        self.fail = fail or {}
        self.crash_at = crash_at
        self.calls: list[tuple[str, str]] = []

    def _enter(self, source: str, sent: str) -> None:
        self.calls.append((source, sent))
        if self.crash_at == len(self.calls):
            raise SearchCrash(f"crash at search {len(self.calls)}")
        if (source, sent) in self.fail:
            raise self.fail[(source, sent)]

    def search_web(
        self,
        prepared: PreparedQuery,
        *,
        step: str,
        include_domains: Sequence[str] = (),
        max_results: int = 10,
    ) -> list[SearchHit]:
        self._enter("web", prepared.sent)
        return self.web.get(prepared.sent, [])

    def search_scholarly(
        self, prepared: PreparedQuery, *, step: str, source: str, max_results: int = 10
    ) -> list[ScholarlyRecord]:
        self._enter(source, prepared.sent)
        return self.scholarly.get((source, prepared.sent), [])


@dataclass
class ResearchModels:
    """Scripted answers by schema (or prompt for free text), and a record of every call."""

    drafts: list[dict[str, Any]] = field(default_factory=lambda: [DRAFT])
    matrices: list[dict[str, Any]] = field(default_factory=lambda: [CLEAN_MATRIX])
    plans: list[dict[str, Any]] = field(default_factory=lambda: [PLAN])
    errors: dict[str, Exception] = field(default_factory=lambda: {})
    calls: dict[str, int] = field(default_factory=lambda: {})
    prompts: dict[str, list[str]] = field(default_factory=lambda: {})
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def count(self, kind: str) -> int:
        return self.calls.get(kind, 0)

    def __call__(self, request: ChatRequest) -> ChatReply | Exception:
        kind = str(request.schema["title"]) if request.schema else "text"
        with self._lock:
            self.calls[kind] = self.calls.get(kind, 0) + 1
            number = self.calls[kind]
            self.prompts.setdefault(kind, []).append(
                "\n".join(m["content"] for m in request.messages)
            )
        if kind in self.errors:
            return self.errors[kind]
        return reply(json.dumps(self._answer(kind, number)))

    def _answer(self, kind: str, number: int) -> dict[str, Any]:
        if kind == "DecompositionDraft":
            return self.drafts[min(number, len(self.drafts)) - 1]
        if kind == "CoverageMatrix":
            return self.matrices[min(number, len(self.matrices)) - 1]
        if kind == "PlanDraft":
            return self.plans[min(number, len(self.plans)) - 1]
        raise AssertionError(f"no scripted answer for {kind}")


def llm(models: ResearchModels, events: MemoryEventSink | None = None) -> LLMService:
    return LLMService(
        REGISTRY,
        URLS,
        CallbackTransport(models),
        events or MemoryEventSink(),
        timeout_s=5,
        sleep=lambda _s: None,
    )
