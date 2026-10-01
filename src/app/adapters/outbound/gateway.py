"""The run-scoped outbound gateway (PRD §3.2, §3.3): the single door to the internet.

Every call checks the exact outgoing payload against the denylist, every URL passes the
private-URL guard (again on each redirect hop), every attempt is written to the outbound log, and
Tavily credits are counted against the run cap and the month ledger. Search falls back to ddgs
instead of failing. Providers make one attempt each; retries and logging live here.
"""

import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar
from urllib.parse import urljoin

from app.adapters.outbound.ddgs_search import DdgsSearch
from app.adapters.outbound.denylist import Denylist
from app.adapters.outbound.errors import (
    DenylistBlocked,
    OutboundBlocked,
    PlanLimitError,
    ProviderAuthError,
    ProviderError,
    SearchUnavailable,
    TransientProviderError,
)
from app.adapters.outbound.extract import (
    Extracted,
    ExtractionError,
    decode_html,
    detect_kind,
    html_to_text,
    pdf_to_text,
    plain_text,
)
from app.adapters.outbound.guard import Resolver, check_url, system_resolver
from app.adapters.outbound.http_get import HttpGetter
from app.adapters.outbound.ledger import MonthLedger, RunLedger, extract_credits, search_credits
from app.adapters.outbound.log import OutboundLog, OutboundRecord
from app.adapters.outbound.sanitizer import SanitizedQuery, SanitizerError
from app.adapters.outbound.tavily import TAVILY_EXTRACT_URL, TAVILY_SEARCH_URL, TavilyApi
from app.adapters.outbound.throttle import HostThrottle
from app.adapters.outbound.types import RawResponse, ScholarlyRecord, SearchHit
from app.events import EventSink

T = TypeVar("T")
ScholarlySource = Literal["openalex", "crossref", "arxiv"]

REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 5
TAVILY_RETRY_S = (1.0, 2.0)  # PRD: Tavily timeout -> two retries
SCHOLARLY_RETRY_S = (1.0, 2.0)
DDGS_RETRY_S = (2.0, 5.0)  # PRD: ddgs -> three tries in total
MIN_HTML_CHARS = 300
DDGS_PACE_URL = "https://duckduckgo.com/"  # pacing key only; ddgs picks its engines itself


class QuerySanitizer(Protocol):
    def sanitize(self, query: str) -> SanitizedQuery: ...


class ScholarlyApi(Protocol):
    def request_url(self, query: str, max_results: int = 10) -> str: ...
    def search(self, query: str, max_results: int = 10) -> list[ScholarlyRecord]: ...


@dataclass(frozen=True)
class PreparedQuery:
    """A query cleared for sending. `sent` is exactly what leaves the machine."""

    original: str
    sent: str
    removed_terms: tuple[str, ...]


@dataclass(frozen=True)
class Document:
    url: str
    final_url: str
    content_type: str | None
    title: str | None
    text: str
    pages: tuple[str, ...]  # PDF pages; empty otherwise
    html: str | None  # decoded HTML for link extraction in M3; None for PDFs and Tavily extracts
    via: Literal["local", "tavily_extract"]


@dataclass(frozen=True)
class FetchFailure:
    url: str
    reason: str


@dataclass(frozen=True)
class Providers:
    tavily: TavilyApi | None  # None when no TAVILY_API_KEY is configured
    ddgs: DdgsSearch
    openalex: ScholarlyApi
    crossref: ScholarlyApi
    arxiv: ScholarlyApi
    http: HttpGetter


def _status(exc: ProviderError) -> str:
    if exc.status:
        return str(exc.status)
    return f"error:{exc.kind}" if isinstance(exc, TransientProviderError) else "error:provider"


def _fallback_allowed(reason: str) -> bool:
    """Local failures worth one Tavily Extract attempt; never oversize or unsupported files."""
    return reason in ("timeout", "network", "empty_text") or reason.startswith("http_")


class OutboundGateway:
    def __init__(
        self,
        *,
        providers: Providers,
        denylist: Denylist,
        sanitizer: QuerySanitizer,
        log: OutboundLog,
        run_ledger: RunLedger,
        month_ledger: MonthLedger,
        events: EventSink,
        internal_domains: Sequence[str] = (),
        resolve: Resolver = system_resolver,
        throttle: HostThrottle | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._providers = providers
        self._scholarly: dict[str, ScholarlyApi] = {
            "openalex": providers.openalex,
            "crossref": providers.crossref,
            "arxiv": providers.arxiv,
        }
        self._deny, self._sanitizer, self._out = denylist, sanitizer, log
        self._run, self._month, self._events = run_ledger, month_ledger, events
        self._internal, self._resolve = tuple(internal_domains), resolve
        self._throttle = throttle or HostThrottle()
        self._sleep = sleep
        self._switched = False
        self._lock = threading.Lock()

    @property
    def credit_cap(self) -> int:
        return self._run.cap

    @property
    def credits_used(self) -> int:
        """Tavily credits this run has spent, including those spent before a restart."""
        return self._run.used

    # ---- bookkeeping ----------------------------------------------------------------------

    def _log(
        self,
        step: str,
        provider: str,
        status: str,
        *,
        query: PreparedQuery | None = None,
        url: str | None = None,
        credits: int = 0,
    ) -> None:
        self._out.write(
            OutboundRecord(
                step=step,
                provider=provider,
                status=status,
                original_query=query.original if query else None,
                sent_query=query.sent if query else None,
                removed_terms=query.removed_terms if query else (),
                url=url,
                credits=credits,
            )
        )

    def _require_clean(
        self,
        texts: Iterable[str],
        *,
        step: str,
        provider: str,
        query: PreparedQuery | None = None,
        url: str | None = None,
    ) -> None:
        hits = sorted({hit for text in texts for hit in self._deny.find(text)})
        if hits:
            self._log(step, provider, "blocked:denylist", query=query, url=url)
            raise DenylistBlocked("denylisted: " + ", ".join(hits))

    def _charge(self, credits: int) -> None:
        self._run.charge(credits)
        result = self._month.charge(credits)
        if result.crossed_warning:
            self._events.emit(
                "tavily_month_warning", level="warning", used=result.total, limit=self._month.limit
            )

    def _attempts(
        self,
        call: Callable[[], T],
        *,
        provider: str,
        step: str,
        delays: Sequence[float],
        pace_url: str,
        log_url: str | None,
        query: PreparedQuery | None = None,
        credits_of: Callable[[T], int] | None = None,
    ) -> T:
        """Run ``call`` with retries on transient errors; log every attempt; charge credits."""
        pending = list(delays)
        while True:
            self._throttle.wait(pace_url)
            try:
                result = call()
            except TransientProviderError as exc:
                self._log(step, provider, _status(exc), query=query, url=log_url)
                if not pending:
                    raise SearchUnavailable(str(exc)) from exc
                self._sleep(pending.pop(0))
                continue
            except ProviderError as exc:
                self._log(step, provider, _status(exc), query=query, url=log_url)
                raise
            credits = credits_of(result) if credits_of else 0
            if credits:
                self._charge(credits)
            ok = "200" if log_url else "ok"
            self._log(step, provider, ok, query=query, url=log_url, credits=credits)
            return result

    # ---- provider switching ---------------------------------------------------------------

    @property
    def provider(self) -> str:
        return "ddgs" if self._switched or self._providers.tavily is None else "tavily"

    def _tavily_block_reason(self) -> str | None:
        if self._providers.tavily is None:
            return "no_api_key"
        if self._switched:
            return "switched"
        if self._month.at_limit():
            return "month_limit"
        if not self._run.can_afford(search_credits()):
            return "run_cap"
        return None

    def _switch(self, reason: str) -> None:
        with self._lock:
            if self._switched:
                return
            self._switched = True
        details = {"from": "tavily", "to": "ddgs", "reason": reason}
        self._events.emit("provider_switched", level="warning", **details)

    # ---- queries --------------------------------------------------------------------------

    def prepare_query(self, query: str, *, step: str) -> PreparedQuery:
        """Denylist, sanitizer, denylist again. Nothing leaves the machine here."""
        text = " ".join(query.split())
        unsanitized = PreparedQuery(text, text, ())
        self._require_clean([text], step=step, provider="sanitizer", query=unsanitized)
        try:
            result = self._sanitizer.sanitize(text)
        except SanitizerError as exc:
            self._log(step, "sanitizer", "blocked:sanitizer_failed", query=unsanitized)
            raise OutboundBlocked("sanitizer_failed") from exc
        prepared = PreparedQuery(text, result.sanitized_query, tuple(result.removed_terms))
        if not prepared.sent:
            self._log(step, "sanitizer", "blocked:empty_after_sanitizing", query=prepared)
            raise DenylistBlocked("empty after sanitizing")
        self._require_clean([prepared.sent], step=step, provider="sanitizer", query=prepared)
        return prepared

    def search_web(
        self,
        prepared: PreparedQuery,
        *,
        step: str,
        include_domains: Sequence[str] = (),
        max_results: int = 10,
    ) -> list[SearchHit]:
        """Tavily while it is usable, otherwise ddgs. Raises `SearchUnavailable` if both fail."""
        texts = [prepared.sent, *include_domains]
        self._require_clean(texts, step=step, provider="web_search", query=prepared)
        reason = self._tavily_block_reason()
        if reason is not None:
            self._switch(reason)
        else:
            hits = self._try_tavily(prepared, step, include_domains, max_results)
            if hits is not None:
                return hits
        return self._attempts(
            lambda: self._providers.ddgs.search(prepared.sent, max_results),
            provider="ddgs_search",
            step=step,
            delays=DDGS_RETRY_S,
            pace_url=DDGS_PACE_URL,
            log_url=None,
            query=prepared,
        )

    def _try_tavily(
        self, prepared: PreparedQuery, step: str, domains: Sequence[str], max_results: int
    ) -> list[SearchHit] | None:
        """Tavily hits, or None when this query has to go to ddgs instead."""
        tavily = self._providers.tavily
        if tavily is None:  # pragma: no cover - excluded by _tavily_block_reason
            return None
        try:
            return self._attempts(
                lambda: tavily.search(prepared.sent, max_results, domains),
                provider="tavily_search",
                step=step,
                delays=TAVILY_RETRY_S,
                pace_url=TAVILY_SEARCH_URL,
                log_url=TAVILY_SEARCH_URL,
                query=prepared,
                credits_of=lambda _hits: search_credits(),
            )
        except PlanLimitError:
            self._switch("plan_limit")
        except ProviderAuthError:
            self._switch("auth")
        except (SearchUnavailable, ProviderError):
            pass  # an outage or a bad request: ddgs answers this one query
        return None

    def search_scholarly(
        self, prepared: PreparedQuery, *, step: str, source: ScholarlySource, max_results: int = 10
    ) -> list[ScholarlyRecord]:
        """One scholarly source. Raises `SearchUnavailable` when it cannot answer."""
        self._require_clean([prepared.sent], step=step, provider=source, query=prepared)
        api = self._scholarly[source]
        url = api.request_url(prepared.sent, max_results)
        try:
            return self._attempts(
                lambda: api.search(prepared.sent, max_results),
                provider=source,
                step=step,
                delays=SCHOLARLY_RETRY_S,
                pace_url=url,
                log_url=url,
                query=prepared,
            )
        except ProviderError as exc:
            raise SearchUnavailable(str(exc)) from exc

    # ---- fetching -------------------------------------------------------------------------

    def fetch(self, url: str, *, step: str) -> Document | FetchFailure:
        """Local fetch first; Tavily Extract once for network/HTTP failures or near-empty HTML."""
        reason = self._fetch_block_reason(url, step)
        if reason:
            return FetchFailure(url, reason)
        local = self._fetch_local(url, step)
        if isinstance(local, Document) or not _fallback_allowed(local.reason):
            return local
        return self._extract_via_tavily(url, step) or local

    def _fetch_block_reason(self, url: str, step: str) -> str | None:
        try:
            self._require_clean([url], step=step, provider="http_get", url=url)
        except DenylistBlocked:
            return "blocked_denylist"
        guard = check_url(url, internal_domains=self._internal, resolve=self._resolve)
        if guard is None:
            return None
        self._log(step, "http_get", f"blocked:{guard}", url=url)
        return "blocked_private"

    def _fetch_local(self, url: str, step: str) -> Document | FetchFailure:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            response = self._get_once(current, step)
            if isinstance(response, FetchFailure):
                return FetchFailure(url, response.reason)
            if response.status not in REDIRECT_CODES or not response.location:
                return self._to_document(url, current, response)
            current = urljoin(current, response.location)
            reason = self._fetch_block_reason(current, step)
            if reason:
                return FetchFailure(url, reason)
        return FetchFailure(url, "too_many_redirects")

    def _get_once(self, url: str, step: str) -> RawResponse | FetchFailure:
        self._throttle.wait(url)
        try:
            response = self._providers.http.get(url)
        except TransientProviderError as exc:
            self._log(step, "http_get", _status(exc), url=url)
            return FetchFailure(url, exc.kind)
        self._log(step, "http_get", str(response.status), url=url)
        return response

    def _to_document(self, url: str, final: str, response: RawResponse) -> Document | FetchFailure:
        if response.too_large:
            return FetchFailure(url, "too_large")
        if response.status != 200:
            return FetchFailure(url, f"http_{response.status}")
        kind = detect_kind(response.content_type, final, response.body[:1024])
        html = decode_html(response.body, response.content_type) if kind == "html" else None
        try:
            extracted = self._extract(kind, response, final, html)
        except ExtractionError:
            return FetchFailure(url, "extract_failed")
        if extracted is None:
            return FetchFailure(url, "unsupported_type")
        if kind == "html" and len(extracted.text) < MIN_HTML_CHARS:
            return FetchFailure(url, "empty_text")
        return Document(
            url,
            final,
            response.content_type,
            extracted.title,
            extracted.text,
            extracted.pages,
            html,
            "local",
        )

    @staticmethod
    def _extract(
        kind: str, response: RawResponse, final: str, html: str | None
    ) -> Extracted | None:
        if kind == "pdf":
            return pdf_to_text(response.body)
        if html is not None:
            return html_to_text(html, final)
        if kind == "text":
            return plain_text(response.body, response.content_type)
        return None

    def _extract_via_tavily(self, url: str, step: str) -> Document | None:
        tavily = self._providers.tavily
        if tavily is None or self._tavily_block_reason() is not None:
            return None
        try:
            pages, _failed = self._attempts(
                lambda: tavily.extract([url]),
                provider="tavily_extract",
                step=step,
                delays=TAVILY_RETRY_S,
                pace_url=TAVILY_EXTRACT_URL,
                log_url=url,
                credits_of=lambda result: extract_credits(len(result[0])),
            )
        except PlanLimitError:
            self._switch("plan_limit")
            return None
        except ProviderAuthError:
            self._switch("auth")
            return None
        except (SearchUnavailable, ProviderError):
            return None
        page = next((p for p in pages if p.content.strip()), None)
        if page is None:
            return None
        text = page.content.strip()
        return Document(url, page.url, "text/markdown", None, text, (), None, "tavily_extract")
