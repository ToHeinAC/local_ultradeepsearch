"""What Phase 2 needs from the outbound gateway; the real `OutboundGateway` satisfies it."""

from collections.abc import Sequence
from typing import Protocol

from app.adapters.outbound.gateway import (
    Document,
    FetchFailure,
    PreparedQuery,
    ScholarlySource,
)
from app.adapters.outbound.types import ScholarlyRecord, SearchHit


class ResearchGateway(Protocol):
    def prepare_query(self, query: str, *, step: str) -> PreparedQuery: ...

    def search_web(
        self,
        prepared: PreparedQuery,
        *,
        step: str,
        include_domains: Sequence[str] = (),
        max_results: int = 10,
    ) -> list[SearchHit]: ...

    def search_scholarly(
        self, prepared: PreparedQuery, *, step: str, source: ScholarlySource, max_results: int = 10
    ) -> list[ScholarlyRecord]: ...

    def fetch(self, url: str, *, step: str) -> Document | FetchFailure: ...

    @property
    def credits_used(self) -> int: ...
