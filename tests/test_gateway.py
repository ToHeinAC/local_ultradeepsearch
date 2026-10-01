import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from ddgs.exceptions import DDGSException
from support import make_pdf

from app.adapters.outbound.ddgs_search import DdgsSearch
from app.adapters.outbound.denylist import Denylist
from app.adapters.outbound.errors import DenylistBlocked, OutboundBlocked, SearchUnavailable
from app.adapters.outbound.gateway import (
    Document,
    FetchFailure,
    OutboundGateway,
    PreparedQuery,
    Providers,
)
from app.adapters.outbound.http_get import HttpGetter
from app.adapters.outbound.ledger import MonthLedger, RunLedger
from app.adapters.outbound.log import OutboundLog
from app.adapters.outbound.sanitizer import SanitizedQuery, SanitizerError
from app.adapters.outbound.scholarly import ArxivApi, CrossrefApi, OpenAlexApi
from app.adapters.outbound.tavily import TavilyApi
from app.adapters.outbound.throttle import HostThrottle
from app.events import MemoryEventSink

TERM = "Müller-Werke"
NOW = datetime(2026, 10, 1, tzinfo=UTC)
ARTICLE = (
    "<html><body><article>"
    + "<p>Public research text about reactors.</p>" * 20
    + "</article></body></html>"
)
ATOM = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/1</id><title>T</title></entry></feed>'
Route = Callable[[httpx.Request], httpx.Response]


@dataclass
class Net:
    """One fake internet. Every request any real client makes is recorded here."""

    routes: dict[str, Route] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(f"{request.url.host}{request.url.path}") or self.routes.get(
            str(request.url.host)
        )
        if route is None:
            return httpx.Response(404, text="no route")
        return route(request)

    def wire(self) -> list[str]:
        """Everything that went out: URLs and bodies."""
        return [f"{r.url} {r.content.decode(errors='replace')}" for r in self.requests]

    def hosts(self) -> list[str]:
        return [str(r.url.host) for r in self.requests]


@dataclass
class FakeSanitizer:
    answers: dict[str, SanitizedQuery] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    fail: bool = False

    def sanitize(self, query: str) -> SanitizedQuery:
        self.calls.append(query)
        if self.fail:
            raise SanitizerError("no answer")
        return self.answers.get(query, SanitizedQuery(sanitized_query=query, removed_terms=[]))


def tavily_ok(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/extract":
        urls = json.loads(request.content)["urls"]
        return httpx.Response(
            200,
            json={
                "results": [{"url": u, "raw_content": "# Extracted\n" + "text " * 80} for u in urls]
            },
        )
    return httpx.Response(
        200, json={"results": [{"title": "T", "url": "https://hit.org/1", "content": "c"}]}
    )


def default_routes() -> dict[str, Route]:
    def openalex(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"id": "W1", "display_name": "Paper"}]})

    def crossref(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"items": [{"DOI": "10.1/x"}]}})

    def arxiv(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=ATOM)

    def page(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=ARTICLE.encode())

    return {
        "api.tavily.com": tavily_ok,
        "api.openalex.org": openalex,
        "api.crossref.org": crossref,
        "export.arxiv.org": arxiv,
        "public.org": page,
    }


class Rig:
    def __init__(
        self, tmp_path: Path, *, key: bool = True, cap: int = 300, month_limit: int = 1000
    ) -> None:
        self.net = Net(routes=default_routes())
        self.ddgs_calls: list[str] = []
        self.ddgs_error: Exception | None = None
        self.sanitizer = FakeSanitizer()
        self.events = MemoryEventSink()
        self.sleeps: list[float] = []
        self.log_path = tmp_path / "run" / "outbound.jsonl"
        self.month = MonthLedger(
            tmp_path / "tavily-ledger.json", limit=month_limit, now=lambda: NOW
        )
        self.run_ledger = RunLedger(cap)
        self.dns: dict[str, list[str]] = {}
        self.gateway = OutboundGateway(
            providers=self._providers(key),
            denylist=Denylist([TERM]),
            sanitizer=self.sanitizer,
            log=OutboundLog(self.log_path, now=lambda: NOW),
            run_ledger=self.run_ledger,
            month_ledger=self.month,
            events=self.events,
            internal_domains=("brenk.local",),
            resolve=lambda host: self.dns.get(host, ["93.184.215.14"]),
            throttle=HostThrottle(monotonic=lambda: 0.0, sleep=lambda s: None),
            sleep=self.sleeps.append,
        )

    def _http(self, timeout: float) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.net), timeout=timeout)

    def _providers(self, key: bool) -> Providers:
        return Providers(
            tavily=TavilyApi("tvly-test", self._http, timeout_s=5) if key else None,
            ddgs=DdgsSearch(self._ddgs),
            openalex=OpenAlexApi(self._http, timeout_s=5),
            crossref=CrossrefApi(self._http, timeout_s=5),
            arxiv=ArxivApi(self._http, timeout_s=5),
            http=HttpGetter(self._http, timeout_s=5, max_html_bytes=10_000, max_pdf_bytes=50_000),
        )

    def _ddgs(self, query: str, max_results: int) -> list[dict[str, Any]]:
        self.ddgs_calls.append(query)
        if self.ddgs_error:
            raise self.ddgs_error
        return [{"title": "D", "href": "https://ddg-hit.org/1", "body": "b"}]

    def log(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        return [json.loads(x) for x in self.log_path.read_text(encoding="utf-8").splitlines()]

    def switched(self) -> list[dict[str, Any]]:
        return [e.data for e in self.events.of_type("provider_switched")]


def q(text: str) -> PreparedQuery:
    return PreparedQuery(original=text, sent=text, removed_terms=())


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


# ---- AC1: denylisted terms never leave -------------------------------------------------------

VARIANTS = ["Müller-Werke", "MUELLER WERKE", "Muller_Werke", "muellerwerke", "M%C3%BCller-Werke"]


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("entry", ["web", "openalex", "crossref", "arxiv", "fetch"])
def test_no_provider_ever_sees_a_denylisted_term(rig: Rig, variant: str, entry: str) -> None:
    text = f"costs of {variant} dismantling"
    calls: dict[str, Callable[[], object]] = {
        "web": lambda: rig.gateway.search_web(q(text), step="2"),
        "openalex": lambda: rig.gateway.search_scholarly(q(text), step="2", source="openalex"),
        "crossref": lambda: rig.gateway.search_scholarly(q(text), step="2", source="crossref"),
        "arxiv": lambda: rig.gateway.search_scholarly(q(text), step="2", source="arxiv"),
    }
    if entry == "fetch":
        url = f"https://public.org/{variant}"
        assert rig.gateway.fetch(url, step="2") == FetchFailure(url, "blocked_denylist")
    else:
        with pytest.raises(DenylistBlocked):
            calls[entry]()
    assert rig.net.requests == []
    assert rig.ddgs_calls == []
    (line,) = rig.log()
    assert line["status"] == "blocked:denylist"


def test_a_redirect_to_a_denylisted_url_is_never_requested(rig: Rig) -> None:
    rig.net.routes["public.org/start"] = lambda r: httpx.Response(
        302, headers={"location": "/Mueller-Werke/report"}
    )
    result = rig.gateway.fetch("https://public.org/start", step="2")
    assert result == FetchFailure("https://public.org/start", "blocked_denylist")
    assert [str(r.url) for r in rig.net.requests] == ["https://public.org/start"]
    assert not any(Denylist([TERM]).find(w) for w in rig.net.wire())


def test_denylisted_include_domains_are_blocked(rig: Rig) -> None:
    with pytest.raises(DenylistBlocked):
        rig.gateway.search_web(q("reactor"), step="2", include_domains=("mueller-werke.de",))
    assert rig.net.requests == []


# ---- prepare_query and AC2 ------------------------------------------------------------------


def test_prepare_blocks_a_denylisted_query_before_the_sanitizer(rig: Rig) -> None:
    with pytest.raises(DenylistBlocked):
        rig.gateway.prepare_query("Müller-Werke reactor costs", step="2.1")
    assert rig.sanitizer.calls == []
    assert rig.log()[0]["status"] == "blocked:denylist"


def test_sanitizer_output_is_rechecked(rig: Rig) -> None:
    rig.sanitizer.answers["Projekt reactor costs"] = SanitizedQuery(
        sanitized_query="mueller werke reactor costs", removed_terms=["Projekt"]
    )
    with pytest.raises(DenylistBlocked):
        rig.gateway.prepare_query("Projekt reactor costs", step="2.1")
    assert rig.net.requests == []


def test_prepare_returns_the_sanitized_query(rig: Rig) -> None:
    # the gateway collapses whitespace before sanitizing, so the answer is keyed on that form
    rig.sanitizer.answers["Kranich reactor costs"] = SanitizedQuery(
        sanitized_query="reactor costs", removed_terms=["Kranich"]
    )
    prepared = rig.gateway.prepare_query("Kranich  reactor\ncosts", step="2.1")
    assert prepared == PreparedQuery("Kranich reactor costs", "reactor costs", ("Kranich",))
    assert rig.log() == []  # nothing left the machine yet


def test_an_empty_sanitizer_result_is_blocked(rig: Rig) -> None:
    rig.sanitizer.answers["Kranich"] = SanitizedQuery(sanitized_query="", removed_terms=["Kranich"])
    with pytest.raises(DenylistBlocked, match="empty"):
        rig.gateway.prepare_query("Kranich", step="2.1")


def test_a_failing_sanitizer_blocks_the_query(rig: Rig) -> None:
    rig.sanitizer.fail = True
    with pytest.raises(OutboundBlocked, match="sanitizer"):
        rig.gateway.prepare_query("reactor", step="2.1")
    assert rig.log()[0]["status"] == "blocked:sanitizer_failed"


# ---- web search, credits and AC5 switching -------------------------------------------------


def test_tavily_search_charges_one_credit_and_logs_the_query(rig: Rig) -> None:
    prepared = PreparedQuery("Kranich reactor", "reactor", ("Kranich",))
    hits = rig.gateway.search_web(prepared, step="2")
    assert [h.provider for h in hits] == ["tavily"]
    assert (rig.run_ledger.used, rig.month.used()) == (1, 1)
    (line,) = rig.log()
    assert line == {
        "ts": NOW.isoformat(),
        "step": "2",
        "provider": "tavily_search",
        "original_query": "Kranich reactor",
        "sent_query": "reactor",
        "removed_terms": ["Kranich"],
        "url": "https://api.tavily.com/search",
        "status": "200",
        "credits": 1,
    }
    assert json.loads(rig.net.requests[0].content)["query"] == "reactor"


def test_credits_are_charged_per_prd(rig: Rig) -> None:
    rig.net.routes["public.org"] = lambda r: httpx.Response(403, text="bot wall")
    for i in range(10):
        rig.gateway.search_web(q(f"query {i}"), step="2")
    for i in range(2):
        assert isinstance(rig.gateway.fetch(f"https://public.org/p{i}", step="2"), Document)
    assert rig.run_ledger.used == 12  # 10 searches + 2 single-URL extracts
    assert sum(line["credits"] for line in rig.log()) == 12


def test_switch_when_the_run_cap_is_reached(tmp_path: Path) -> None:
    rig = Rig(tmp_path, cap=2)
    providers = [rig.gateway.search_web(q(f"q{i}"), step="2")[0].provider for i in range(4)]
    assert providers == ["tavily", "tavily", "ddgs", "ddgs"]
    assert rig.switched() == [{"from": "tavily", "to": "ddgs", "reason": "run_cap"}]


@pytest.mark.parametrize("status", [432, 433])
def test_switch_on_tavily_plan_limit(rig: Rig, status: int) -> None:
    rig.net.routes["api.tavily.com"] = lambda r: httpx.Response(
        status, json={"detail": {"error": "limit"}}
    )
    assert rig.gateway.search_web(q("a"), step="2")[0].provider == "ddgs"
    assert rig.gateway.search_web(q("b"), step="2")[0].provider == "ddgs"
    assert rig.net.hosts() == ["api.tavily.com"]  # never asked again
    assert rig.switched() == [{"from": "tavily", "to": "ddgs", "reason": "plan_limit"}]
    assert rig.run_ledger.used == 0


def test_switch_on_invalid_key(rig: Rig) -> None:
    rig.net.routes["api.tavily.com"] = lambda r: httpx.Response(
        401, json={"detail": {"error": "bad key"}}
    )
    assert rig.gateway.search_web(q("a"), step="2")[0].provider == "ddgs"
    assert rig.switched()[0]["reason"] == "auth"


def test_switch_when_the_month_limit_is_already_reached(tmp_path: Path) -> None:
    rig = Rig(tmp_path, month_limit=5)
    rig.month.charge(5)
    assert rig.gateway.search_web(q("a"), step="2")[0].provider == "ddgs"
    assert rig.net.requests == []
    assert rig.switched() == [{"from": "tavily", "to": "ddgs", "reason": "month_limit"}]


def test_no_api_key_uses_ddgs_from_the_start(tmp_path: Path) -> None:
    rig = Rig(tmp_path, key=False)
    rig.gateway.search_web(q("a"), step="2")
    rig.gateway.search_web(q("b"), step="2")
    assert rig.ddgs_calls == ["a", "b"]
    assert rig.switched() == [{"from": "tavily", "to": "ddgs", "reason": "no_api_key"}]


def test_month_warning_is_emitted_once_at_eighty_percent(tmp_path: Path) -> None:
    rig = Rig(tmp_path, month_limit=10)
    rig.month.charge(7)
    for i in range(2):
        rig.gateway.search_web(q(f"q{i}"), step="2")
    (warning,) = rig.events.of_type("tavily_month_warning")
    assert warning.data == {"used": 8, "limit": 10}


def test_tavily_timeout_retries_twice(rig: Rig) -> None:
    attempts = iter([httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"), None])

    def flaky(request: httpx.Request) -> httpx.Response:
        error = next(attempts)
        if error:
            raise error
        return tavily_ok(request)

    rig.net.routes["api.tavily.com"] = flaky
    assert rig.gateway.search_web(q("a"), step="2")[0].provider == "tavily"
    assert rig.sleeps == [1.0, 2.0]
    assert [line["status"] for line in rig.log()] == ["error:timeout", "error:timeout", "200"]
    assert rig.run_ledger.used == 1


def test_a_tavily_outage_falls_back_per_query_without_switching(rig: Rig) -> None:
    rig.net.routes["api.tavily.com"] = lambda r: httpx.Response(503, text="down")
    assert rig.gateway.search_web(q("a"), step="2")[0].provider == "ddgs"
    assert rig.switched() == []
    rig.net.routes["api.tavily.com"] = tavily_ok
    assert rig.gateway.search_web(q("b"), step="2")[0].provider == "tavily"


def test_ddgs_gives_up_after_three_tries(tmp_path: Path) -> None:
    rig = Rig(tmp_path, key=False)
    rig.ddgs_error = DDGSException("blocked by engine")
    with pytest.raises(SearchUnavailable):
        rig.gateway.search_web(q("a"), step="2")
    assert len(rig.ddgs_calls) == 3
    assert [line["status"] for line in rig.log()] == ["error:transient"] * 3


# ---- scholarly ------------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["openalex", "crossref", "arxiv"])
def test_scholarly_search_is_logged_and_free(rig: Rig, source: str) -> None:
    records = rig.gateway.search_scholarly(q("reactor dismantling"), step="2", source=source)  # type: ignore[arg-type]
    assert [r.source for r in records] == [source]
    (line,) = rig.log()
    assert (line["provider"], line["status"], line["credits"]) == (source, "200", 0)
    assert line["url"] is not None
    assert rig.run_ledger.used == 0


def test_everything_down_returns_typed_failures(rig: Rig) -> None:
    down = lambda r: httpx.Response(503, text="down")  # noqa: E731
    for host in ("api.tavily.com", "api.openalex.org", "public.org"):
        rig.net.routes[host] = down
    rig.ddgs_error = DDGSException("all engines failed")
    with pytest.raises(SearchUnavailable):
        rig.gateway.search_web(q("a"), step="2")
    with pytest.raises(SearchUnavailable):
        rig.gateway.search_scholarly(q("a"), step="2", source="openalex")
    assert rig.gateway.fetch("https://public.org/x", step="2") == FetchFailure(
        "https://public.org/x", "http_503"
    )


# ---- fetch ----------------------------------------------------------------------------------


def test_fetch_html_locally(rig: Rig) -> None:
    doc = rig.gateway.fetch("https://public.org/article", step="2")
    assert isinstance(doc, Document)
    assert doc.via == "local"
    assert "Public research text" in doc.text
    assert doc.html is not None
    assert (doc.url, doc.final_url) == ("https://public.org/article", "https://public.org/article")
    (line,) = rig.log()
    assert (line["provider"], line["status"], line["url"]) == (
        "http_get",
        "200",
        "https://public.org/article",
    )


def test_fetch_pdf_locally(rig: Rig) -> None:
    pdf = make_pdf(["Page one", "Page two"])
    rig.net.routes["public.org/a.pdf"] = lambda r: httpx.Response(
        200, headers={"content-type": "application/pdf"}, content=pdf
    )
    doc = rig.gateway.fetch("https://public.org/a.pdf", step="2")
    assert isinstance(doc, Document)
    assert doc.pages == ("Page one", "Page two")
    assert doc.html is None


def test_redirects_are_followed_and_each_hop_is_logged(rig: Rig) -> None:
    rig.net.routes["public.org/old"] = lambda r: httpx.Response(
        301, headers={"location": "https://public.org/new"}
    )
    doc = rig.gateway.fetch("https://public.org/old", step="2")
    assert isinstance(doc, Document)
    assert doc.final_url == "https://public.org/new"
    assert [(line["status"], line["url"]) for line in rig.log()] == [
        ("301", "https://public.org/old"),
        ("200", "https://public.org/new"),
    ]


def test_redirect_into_private_space_is_never_requested(rig: Rig) -> None:
    rig.net.routes["public.org/go"] = lambda r: httpx.Response(
        302, headers={"location": "http://10.0.0.1/admin"}
    )
    result = rig.gateway.fetch("https://public.org/go", step="2")
    assert result == FetchFailure("https://public.org/go", "blocked_private")
    assert rig.net.hosts() == ["public.org"]
    assert rig.log()[-1]["status"] == "blocked:private_ip"


def test_private_and_internal_urls_are_never_requested_or_extracted(rig: Rig) -> None:
    rig.dns["wiki.example.com"] = ["10.4.4.4"]
    for url in (
        "http://172.16.4.112:8540/",
        "https://wiki.brenk.local/x",
        "https://wiki.example.com/x",
    ):
        assert rig.gateway.fetch(url, step="2") == FetchFailure(url, "blocked_private")
    assert rig.net.requests == []


def test_too_many_redirects(rig: Rig) -> None:
    rig.net.routes["public.org/loop"] = lambda r: httpx.Response(302, headers={"location": "/loop"})
    assert rig.gateway.fetch("https://public.org/loop", step="2") == FetchFailure(
        "https://public.org/loop", "too_many_redirects"
    )
    assert len(rig.net.requests) == 6


def test_too_large_is_not_retried_via_extract(rig: Rig) -> None:
    rig.net.routes["public.org/big"] = lambda r: httpx.Response(
        200, headers={"content-type": "text/html"}, content=b"x" * 20_000
    )
    assert rig.gateway.fetch("https://public.org/big", step="2") == FetchFailure(
        "https://public.org/big", "too_large"
    )
    assert "api.tavily.com" not in rig.net.hosts()


def test_unsupported_types_are_not_retried_via_extract(rig: Rig) -> None:
    rig.net.routes["public.org/img"] = lambda r: httpx.Response(
        200, headers={"content-type": "image/png"}, content=b"\x89PNG"
    )
    assert rig.gateway.fetch("https://public.org/img", step="2") == FetchFailure(
        "https://public.org/img", "unsupported_type"
    )
    assert "api.tavily.com" not in rig.net.hosts()


@pytest.mark.parametrize(
    ("route", "local_reason"),
    [
        (lambda r: httpx.Response(403, text="bot wall"), "http_403"),
        (
            lambda r: httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"<html><body>Login</body></html>",
            ),
            "empty_text",
        ),
    ],
)
def test_local_failures_fall_back_to_tavily_extract(
    rig: Rig, route: Route, local_reason: str
) -> None:
    rig.net.routes["public.org"] = route
    doc = rig.gateway.fetch("https://public.org/x", step="2")
    assert isinstance(doc, Document)
    assert doc.via == "tavily_extract"
    assert doc.text.startswith("# Extracted")
    extract_line = rig.log()[-1]
    assert (extract_line["provider"], extract_line["credits"]) == ("tavily_extract", 1)
    assert json.loads(rig.net.requests[-1].content)["urls"] == ["https://public.org/x"]
    del local_reason


def test_network_errors_fall_back_too(rig: Rig) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    rig.net.routes["public.org"] = refuse
    assert isinstance(rig.gateway.fetch("https://public.org/x", step="2"), Document)
    assert rig.log()[0]["status"] == "error:network"


def test_extract_failures_return_the_local_reason_and_cost_nothing(rig: Rig) -> None:
    rig.net.routes["public.org"] = lambda r: httpx.Response(404, text="gone")
    rig.net.routes["api.tavily.com/extract"] = lambda r: httpx.Response(
        200, json={"results": [], "failed_results": [{"url": "https://public.org/x", "error": "x"}]}
    )
    assert rig.gateway.fetch("https://public.org/x", step="2") == FetchFailure(
        "https://public.org/x", "http_404"
    )
    assert rig.run_ledger.used == 0


def test_no_extract_fallback_without_a_key_or_after_a_switch(tmp_path: Path) -> None:
    for rig in (Rig(tmp_path / "a", key=False), Rig(tmp_path / "b", cap=0)):
        rig.net.routes["public.org"] = lambda r: httpx.Response(403, text="bot wall")
        assert rig.gateway.fetch("https://public.org/x", step="2") == FetchFailure(
            "https://public.org/x", "http_403"
        )
        assert "api.tavily.com" not in rig.net.hosts()


# ---- AC7: one complete line per attempt -----------------------------------------------------

FIELDS = {
    "ts",
    "step",
    "provider",
    "original_query",
    "sent_query",
    "removed_terms",
    "url",
    "status",
    "credits",
}


def test_log_lines_are_complete_and_one_per_attempt(rig: Rig) -> None:
    rig.net.routes["public.org/old"] = lambda r: httpx.Response(
        301, headers={"location": "https://public.org/new"}
    )
    rig.gateway.search_web(q("a"), step="2")  # 1 attempt
    rig.gateway.search_scholarly(q("a"), step="2", source="crossref")  # 1 attempt
    rig.gateway.fetch("https://public.org/old", step="2")  # 2 hops
    rig.gateway.fetch("http://10.0.0.9/", step="2")  # 1 blocked
    lines = rig.log()
    assert len(lines) == 5
    assert all(set(line) == FIELDS for line in lines)
    assert len(rig.net.requests) == 4
