"""Shared fakes of the Phase-2 tests: a gateway with a denylist and call counts."""

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.adapters.outbound.errors import DenylistBlocked, OutboundBlocked, SearchUnavailable
from app.adapters.outbound.gateway import (
    Document,
    FetchFailure,
    PreparedQuery,
    ScholarlySource,
)
from app.adapters.outbound.types import ScholarlyRecord, SearchHit

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
    prepared: list[str] = field(default_factory=lambda: [])
    web_calls: list[tuple[str, tuple[str, ...], int]] = field(default_factory=lambda: [])
    scholarly_calls: list[tuple[str, str]] = field(default_factory=lambda: [])
    credits: int = 0

    def _check(self, text: str) -> None:
        if any(term in text.casefold() for term in self.denied):
            raise DenylistBlocked("denylist")

    def prepare_query(self, query: str, *, step: str) -> PreparedQuery:
        self.prepared.append(query)
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
