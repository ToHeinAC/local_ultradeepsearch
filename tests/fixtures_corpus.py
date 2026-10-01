"""Deterministic synthetic sources, a fake fetcher and a fake model for pipeline tests.

Everything here is offline and reproducible: articles are generated from seeded random words, so
two different topics share (almost) no word 3-grams, while an edited copy of one topic does.
"""

import json
import random
import re
import threading
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from support import make_settings

from app.adapters.outbound.gateway import Document, FetchFailure
from app.events import EventSink, MemoryEventSink
from app.llm.fakes import CallbackTransport, reply
from app.llm.roles import build_registry
from app.llm.service import LLMService
from app.llm.types import ChatReply, ChatRequest, Endpoint
from app.pipeline.analysis import SourceAnalyzer
from app.pipeline.extraction import Focus, NoteExtractor
from app.pipeline.fetch import FetchPipeline
from app.pipeline.profiles import Profile
from app.pipeline.strategies import SourceStrategies, load_strategies
from app.store.models import SourceMeta
from app.store.vault import Vault

VOCABULARY = [
    f"{stem}{n}"
    for stem in ("reactor", "fuel", "waste", "permit", "survey", "dose", "shield", "crane")
    for n in range(30)
]
HANG_SECONDS = 120
GERMAN = "Der Rückbau kerntechnischer Anlagen erfordert eine Genehmigung nach dem Atomgesetz. "


class SimulatedCrash(BaseException):
    """A crash that no `except Exception` may swallow, like a power cut or a SIGKILL."""


def article(topic: int, words: int = 260) -> str:
    """A unique, junk-free article whose first sentence is `Article <topic> describes ...`."""
    rng = random.Random(topic)
    paragraphs: list[str] = [
        f"Article {topic} describes the decommissioning programme of facility {topic}."
    ]
    count = 0
    while count < words:
        sentence = " ".join(rng.choice(VOCABULARY) for _ in range(12)) + "."
        paragraphs.append(sentence)
        count += 12
    return "\n\n".join(" ".join(paragraphs[i : i + 5]) for i in range(0, len(paragraphs), 5))


def edited(text: str, every: int) -> str:
    parts = text.split(" ")
    for i in range(0, len(parts), every):
        parts[i] = f"changed{i}"
    return " ".join(parts)


def prose(chars: int) -> str:
    sentence = "The commission reviewed the application for the research reactor in detail. "
    return (sentence * (chars // len(sentence) + 1))[:chars]


def doc(
    url: str,
    text: str,
    *,
    title: str | None = "A page",
    html: str | None = None,
    pages: tuple[str, ...] = (),
    content_type: str = "text/html",
    via: str = "local",
) -> Document:
    return Document(url, url, content_type, title, text, pages, html, via)  # type: ignore[arg-type]


def failure(url: str, reason: str) -> FetchFailure:
    return FetchFailure(url, reason)


Outcome = Document | FetchFailure


@dataclass
class FakeFetcher:
    """Serves ``outcomes`` by URL. A sequence of outcomes is played in order (the last repeats),
    so a URL can fail first and succeed later. Every call is recorded."""

    outcomes: Mapping[str, Outcome | Sequence[Outcome]]
    delay: float = 0.0
    calls: list[str] = field(default_factory=list)
    _seen: Counter[str] = field(default_factory=Counter)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def fetch(self, url: str, *, step: str) -> Outcome:
        with self._lock:
            self.calls.append(url)
            attempt = self._seen[url]
            self._seen[url] += 1
        if self.delay:
            time.sleep(self.delay)
        outcome = self.outcomes[url]
        if isinstance(outcome, Document | FetchFailure):
            return outcome
        return outcome[min(attempt, len(outcome) - 1)]

    def count(self, url: str) -> int:
        return self.calls.count(url)


_FENCE = re.compile(
    r'<untrusted-source url="(?P<url>[^"]*)">\n(?P<text>.*)\n</untrusted-source>', re.DOTALL
)
_FIRST_SENTENCE = re.compile(r"^(.+?\.)(?:\s|$)", re.DOTALL)


@dataclass
class FakeModels:
    """Answers every model call the pipeline makes, chosen by the requested schema.

    Each extraction returns one verbatim claim (the source's first sentence) and, with
    ``fabricate``, one claim whose quote does not occur in the source.
    """

    fabricate: bool = True
    crash_on_extraction: int | None = None
    crash_on_schema: tuple[str, int] | None = None  # (schema title, n-th call of that schema)
    hang_on_extraction: int | None = None  # sleep "forever" in the n-th extraction (SIGKILL test)
    fail_extraction: Exception | None = None
    delay: float = 0.0
    analysis_delay: float = 0.0  # opens a race window for the analysis cap
    extracted_urls: list[str] = field(default_factory=list)
    schemas: Counter[str] = field(default_factory=Counter)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __call__(self, request: ChatRequest) -> ChatReply | Exception:
        assert request.schema is not None
        title = str(request.schema["title"])
        with self._lock:
            self.schemas[title] += 1
            nth = self.schemas[title]
        if self.crash_on_schema == (title, nth):
            raise SimulatedCrash(f"crash during {title} #{nth}")
        if title == "ChunkExtraction":
            return self._extraction(request)
        if self.analysis_delay and title in ("PartialAnalysis", "SourceAnalysis"):
            time.sleep(self.analysis_delay)
        if title == "MergedSummary":
            return reply(json.dumps({"summary": "Merged summary."}))
        if title == "PartialAnalysis":
            return reply(json.dumps({"key_points": ["A point."], "numbers": [], "quotes": []}))
        return reply(json.dumps(_analysis()))

    def _extraction(self, request: ChatRequest) -> ChatReply | Exception:
        if self.delay:
            time.sleep(self.delay)
        match = _FENCE.search(request.messages[-1]["content"])
        assert match, "extraction prompt without a fenced source"
        with self._lock:
            self.extracted_urls.append(match["url"])
            number = len(self.extracted_urls)
        if self.crash_on_extraction == number:
            raise SimulatedCrash(f"crash during extraction #{number}")
        if self.hang_on_extraction == number:
            time.sleep(HANG_SECONDS)
        if self.fail_extraction is not None:
            return self.fail_extraction
        sentence = _FIRST_SENTENCE.match(match["text"])
        quote = sentence.group(1) if sentence else match["text"][:60]
        claims = [_claim("A verbatim claim.", quote)]
        if self.fabricate:
            claims.append(_claim("An invented claim.", "this sentence does not occur anywhere"))
        return reply(json.dumps({"summary": f"Summary: {quote[:40]}", "claims": claims}))


def _claim(text: str, quote: str) -> dict[str, object]:
    return {
        "claim": text,
        "stance": "supports",
        "stance_target": "scope",
        "evidence_type": "empirical",
        "scope_conditions": "",
        "quoted_support": quote,
        "numbers": [],
        "entities": [],
        "time_period": None,
        "region": None,
        "confidence": "medium",
    }


def _analysis() -> dict[str, object]:
    return {
        "thesis": "A thesis.",
        "methodology": "",
        "key_findings": ["A finding."],
        "load_bearing_citations": [],
        "caveats": "",
        "relevance_to_query": "Relevant.",
        "quotes": [],
        "relevance": "useful",
    }


# ---- a wired pipeline -----------------------------------------------------------------------

FOCUS = Focus(
    title="Decommissioning of research reactors",
    questions=("How long does dismantling take?", "What does it cost?"),
)
PROFILE = Profile(credit_cap=60, source_analysis_cap=6, long_source_words=5000)
URLS = {Endpoint.OWN: "http://own", Endpoint.SHARED: "http://shared"}


@dataclass
class Built:
    pipeline: FetchPipeline
    vault: Vault
    fetcher: FakeFetcher
    models: FakeModels
    events: EventSink
    run_dir: Path


def build(
    base_dir: Path,
    outcomes: Mapping[str, Outcome | Sequence[Outcome]],
    *,
    models: FakeModels | None = None,
    events: EventSink | None = None,
    profile: Profile = PROFILE,
    run_id: str = "run-a",
    fetch_delay: float = 0.0,
    strategies: SourceStrategies | None = None,
) -> Built:
    """Everything of one 'process': vault, fetcher, fake models and the pipeline on top.

    Calling it again with the same ``base_dir`` and ``run_id`` is a restart: new objects, same
    files.
    """
    settings = make_settings()
    models = models or FakeModels()
    sink: EventSink = events or MemoryEventSink()
    service = LLMService(
        build_registry(settings),
        URLS,
        CallbackTransport(models),
        sink,
        timeout_s=5,
        sleep=lambda _s: None,
    )
    vault = Vault(base_dir / "udr.sqlite", run_id)
    fetcher = FakeFetcher(outcomes, delay=fetch_delay)
    run_dir = base_dir / "runs" / run_id
    pipeline = FetchPipeline(
        vault=vault,
        fetcher=fetcher,
        extractor=NoteExtractor(service, sink),
        analyzer=SourceAnalyzer(service, sink),
        strategies=strategies or load_strategies(settings.config_dir),
        profile=profile,
        focus=FOCUS,
        run_dir=run_dir,
        events=sink,
    )
    return Built(pipeline, vault, fetcher, models, sink, run_dir)


# ---- the 20-page corpus of M3 AC1 -----------------------------------------------------------

Item = tuple[str, SourceMeta | None]


def site(i: int) -> str:
    return f"https://site{i}.example.org/a{i}"


NEAR_DUP = "https://mirror.example.org/copy-of-a2"
DOI_MIRROR = "https://doi-mirror.example.org/paper"
VARIANT_1 = "https://site1.example.org/a1?utm_source=newsletter"
VARIANT_2 = "https://www.site1.example.org/a1/#top"
LOGIN_WALL = "https://walls.example.org/login"
COOKIE_WALL = "https://walls.example.org/consent"
TOO_SHORT = "https://walls.example.org/stub"
GARBAGE = "https://walls.example.org/binary"
PDF_URL = "https://files.example.org/report.pdf"
GONE = "https://gone.example.org/missing"
PDF_PAGES = (article(21, words=130), article(22, words=130))
DOI = "10.1234/abc.3"


def ac1_corpus() -> tuple[dict[str, Outcome], list[Item]]:
    """20 URLs: 10 articles, 2 URL variants of the first, a near-duplicate of the second, a DOI
    mirror of the third, 4 junk pages, 1 PDF and 1 dead link. Returns (what the web serves,
    the order in which a run asks for them)."""
    served: dict[str, Outcome] = {
        site(i): doc(site(i), article(i), title=f"Article {i}", html=f'<a href="/more{i}">more</a>')
        for i in range(1, 11)
    }
    served[VARIANT_1] = doc(VARIANT_1, article(1))
    served[VARIANT_2] = doc(VARIANT_2, article(1))
    served[NEAR_DUP] = doc(NEAR_DUP, edited(article(2), every=20), title="A copy")
    served[DOI_MIRROR] = doc(DOI_MIRROR, article(3))
    served[LOGIN_WALL] = doc(LOGIN_WALL, "Sign in to continue reading. " + prose(600))
    served[COOKIE_WALL] = doc(COOKIE_WALL, prose(900) + " We use cookies. Accept all.")
    served[TOO_SHORT] = doc(TOO_SHORT, "Page not found.")
    served[GARBAGE] = doc(GARBAGE, "\ufffd" * 150 + prose(1900))
    served[PDF_URL] = doc(
        PDF_URL, "\n\n".join(PDF_PAGES), title=None, pages=PDF_PAGES, content_type="application/pdf"
    )
    served[GONE] = failure(GONE, "http_404")
    order: list[Item] = [
        (site(i), SourceMeta(doi=DOI, scholarly=True) if i == 3 else None) for i in range(1, 11)
    ]
    order += [
        (VARIANT_1, None),
        (VARIANT_2, None),
        (NEAR_DUP, None),
        (DOI_MIRROR, SourceMeta(doi="https://doi.org/10.1234/ABC.3")),
        (LOGIN_WALL, None),
        (COOKIE_WALL, None),
        (TOO_SHORT, None),
        (GARBAGE, None),
        (PDF_URL, None),
        (GONE, None),
    ]
    assert len(order) == 20
    return served, order


def simple_corpus(count: int) -> tuple[dict[str, Outcome | Sequence[Outcome]], list[str]]:
    """``count`` distinct one-page articles and their URLs, in order."""
    urls = [site(i) for i in range(1, count + 1)]
    return {u: doc(u, article(i)) for i, u in enumerate(urls, start=1)}, urls
