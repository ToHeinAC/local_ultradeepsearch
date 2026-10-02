"""Shared rig of the Phase-1 tests: scripted models and the wired parts of a brief session."""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from support import make_pdf, make_settings

from app.brief.interview import Interviewer
from app.brief.uploads import UploadFile, UploadIngestor
from app.events import MemoryEventSink
from app.graphs.brief import BriefDeps, build_brief_graph
from app.llm.fakes import CallbackTransport, reply
from app.llm.roles import build_registry
from app.llm.service import LLMService
from app.llm.types import ChatReply, ChatRequest, Endpoint
from app.pipeline.profiles import Phase1Limits, load_phase1, load_response_formats
from app.prompts import brief as prompts
from app.store.db import Database
from app.store.runs import RunStore
from app.store.sessions import SessionStore
from app.templates import load_templates

SETTINGS = make_settings()
LIMITS = load_phase1(SETTINGS.config_dir)
FORMATS = load_response_formats(SETTINGS.config_dir)
TEMPLATES = load_templates([SETTINGS.templates_dir])
URLS = {Endpoint.OWN: "http://own", Endpoint.SHARED: "http://shared"}
REGISTRY = build_registry(SETTINGS)
NOW = datetime(2026, 10, 2, 9, 30, 15, tzinfo=UTC)
QUESTION = "Wie lange dauert der Rückbau eines Forschungsreaktors?"
ITEMS = ("question", "context", "goal", "audience", "scope", "output", "depth")


class Crash(BaseException):
    """A crash that no `except Exception` may swallow."""


def checklist(**statuses: str) -> list[dict[str, str]]:
    return [{"item": i, "status": statuses.get(i, "clear"), "note": ""} for i in ITEMS]


def q(item: str, text: str, candidate: str = "Vorschlag") -> dict[str, str]:
    return {"item": item, "question": text, "candidate": candidate}


ROUND_ONE = {
    "checklist": checklist(audience="missing", scope="assumed", goal="missing"),
    "questions": [q("audience", "Wer liest den Bericht?", "Ingenieure"), q("scope", "Was nicht?")],
    "finished_prompt": False,
}
DONE = {"checklist": checklist(), "questions": [], "finished_prompt": False}
DRAFT = {
    "question": QUESTION,
    "audience": "Ingenieure",
    "research_questions": ["Wie lange dauert der Rückbau?"],
    "tone": "Fachlich",
}
TIER = {"tier": "full", "response_format": "structured", "rationale": "Strittige Datenlage."}
FINISHED = {"checklist": checklist(), "questions": [], "finished_prompt": True}
PASTED = "# Wie teuer ist der Rückbau?\n\nBitte prüfe:\n1. Kosten je Anlage\n2. Dauer\n"


@dataclass
class Models:
    """Scripted answers by prompt, and a count of every kind of call."""

    assessments: list[dict[str, Any]] = field(default_factory=lambda: [ROUND_ONE, DONE])
    draft: dict[str, Any] = field(default_factory=lambda: DRAFT)
    revised: dict[str, Any] = field(default_factory=lambda: {**DRAFT, "goal": "Überarbeitet."})
    tier: dict[str, Any] = field(default_factory=lambda: TIER)
    crash_on: dict[str, int] = field(default_factory=lambda: {})
    errors: dict[str, Exception] = field(default_factory=lambda: {})  # kind -> error to return
    calls: dict[str, int] = field(default_factory=lambda: {})
    prompts_seen: dict[str, list[str]] = field(default_factory=lambda: {})

    def count(self, kind: str) -> int:
        return self.calls.get(kind, 0)

    def total(self) -> int:
        return sum(self.calls.values())

    def __call__(self, request: ChatRequest) -> ChatReply | Exception:
        kind = self._kind(request)
        self.calls[kind] = self.calls.get(kind, 0) + 1
        self.prompts_seen.setdefault(kind, []).append(
            "\n".join(m["content"] for m in request.messages)
        )
        if self.crash_on.get(kind) == self.calls[kind]:
            raise Crash(kind)
        if kind in self.errors:
            return self.errors[kind]
        if kind == "ocr":
            return reply("Gescannter Text, der lang genug ist, um eine ganze Seite zu sein.")
        return reply(json.dumps(self._answer(kind)))

    @staticmethod
    def _kind(request: ChatRequest) -> str:
        if request.schema is None:  # only the ocr role sends a schema-less request in Phase 1
            return "ocr"
        system = request.messages[0]["content"]
        for name, constant in (
            ("revise", prompts.REVISE_SYSTEM),
            ("strengthen", prompts.STRENGTHEN_SYSTEM),
        ):
            if system == constant:
                return name
        return {
            "Assessment": "assess",
            "BriefDraft": "draft",
            "TierRecommendation": "tier",
            "UploadFacts": "facts",
            "UploadDigest": "digest",
        }[str(request.schema["title"])]

    def _answer(self, kind: str) -> dict[str, Any]:
        number = self.count(kind)
        if kind == "assess":
            return self.assessments[min(number, len(self.assessments)) - 1]
        if kind == "facts":
            return {"facts": [{"fact": "Ein Fakt aus der Datei.", "page": 1}]}
        if kind == "digest":
            return {"items": [{"fact": "Ein Fakt aus der Datei.", "file": "a.pdf", "page": 1}]}
        return {
            "draft": self.draft,
            "strengthen": self.draft,
            "revise": self.revised,
            "tier": self.tier,
        }[kind]


def answers(*kinds: str, genug: bool = False, note: str = "") -> dict[str, Any]:
    return {
        "answers": [{"kind": k, "text": "Mein Text" if k == "text" else ""} for k in kinds],
        "genug": genug,
        "note": note,
    }


def a_pdf(name: str = "a.pdf") -> UploadFile:
    return UploadFile(name, make_pdf(["Diese Seite hat genug Text, damit keine OCR noetig ist."]))


@dataclass
class Parts:
    """The wired objects of one 'process': stores, ingestor, interviewer and the compiled graph."""

    db: Database
    sessions: SessionStore
    runs: RunStore
    ingestor: UploadIngestor
    interviewer: Interviewer
    deps: BriefDeps
    graph: Any
    llm: LLMService
    events: MemoryEventSink


def build_parts(tmp: Path, models: Models, limits: Phase1Limits = LIMITS) -> Parts:
    events = MemoryEventSink()
    llm = LLMService(
        REGISTRY, URLS, CallbackTransport(models), events, timeout_s=5, sleep=lambda _s: None
    )
    db = Database(tmp / "data" / "udr.sqlite", now=lambda: NOW)
    sessions, runs = SessionStore(db), RunStore(db)
    ingestor = UploadIngestor(sessions, llm, limits, tmp / "uploads", events)
    interviewer = Interviewer(llm, limits, FORMATS)
    deps = BriefDeps(
        store=sessions,
        runs=runs,
        ingestor=ingestor,
        interviewer=interviewer,
        templates=TEMPLATES,
        formats=FORMATS,
        limits=limits,
        briefs_dir=tmp / "data" / "briefs",
        drafts_dir=tmp / "data" / "briefs" / "drafts",
    )
    saver = SqliteSaver(sqlite3.connect(tmp / "checkpoints.sqlite", check_same_thread=False))
    graph = build_brief_graph(deps, saver)
    return Parts(db, sessions, runs, ingestor, interviewer, deps, graph, llm, events)
