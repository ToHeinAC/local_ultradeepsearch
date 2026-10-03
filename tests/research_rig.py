"""Shared rig of the Phase-2 tests: a scripted model that answers by schema or prompt, and the
parts of a research run wired on fakes."""

import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from support import make_settings

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
        "domain_notes": "Behördliche Quellen zuerst; Genehmigungsrecht mit Stand der letzten Jahre.",
        "inference_depth": "standard",
    },
}
CLEAN_MATRIX: dict[str, Any] = {
    "rows": [
        {"phrase": "Rückbau", "items": ["Q1"], "scope_ok": True},
        {"phrase": "Forschungsreaktor", "items": ["E1"], "scope_ok": True},
    ]
}


@dataclass
class ResearchModels:
    """Scripted answers by schema (or prompt for free text), and a record of every call."""

    drafts: list[dict[str, Any]] = field(default_factory=lambda: [DRAFT])
    matrices: list[dict[str, Any]] = field(default_factory=lambda: [CLEAN_MATRIX])
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
