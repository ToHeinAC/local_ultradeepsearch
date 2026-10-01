"""Data returned by the outbound providers and the gateway."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import httpx

HttpFactory = Callable[[float], httpx.Client]  # timeout -> client; tests pass a MockTransport


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    provider: str  # "tavily" or "ddgs"


@dataclass(frozen=True)
class ScholarlyRecord:
    source: str  # "openalex", "crossref" or "arxiv"
    title: str
    url: str
    doi: str | None = None
    year: int | None = None
    authors: tuple[str, ...] = ()
    venue: str | None = None
    cited_by_count: int | None = None
    is_retracted: bool | None = None  # only OpenAlex knows; None means unknown
    oa_url: str | None = None
    abstract: str | None = None
    kind: str | None = None


@dataclass(frozen=True)
class ExtractedPage:
    url: str
    content: str


@dataclass(frozen=True)
class RawResponse:
    """One HTTP response, redirects not followed. ``too_large``: body cut at the size cap."""

    url: str
    status: int
    headers: Mapping[str, str]
    body: bytes
    too_large: bool = False

    @property
    def content_type(self) -> str | None:
        return self.headers.get("content-type")

    @property
    def location(self) -> str | None:
        return self.headers.get("location")


def default_http(timeout_s: float) -> httpx.Client:
    return httpx.Client(timeout=timeout_s, follow_redirects=False)
