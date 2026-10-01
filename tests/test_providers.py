import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

from app.adapters.outbound.ddgs_search import DdgsSearch
from app.adapters.outbound.errors import (
    PlanLimitError,
    ProviderAuthError,
    ProviderError,
    TransientProviderError,
)
from app.adapters.outbound.http_get import HttpGetter
from app.adapters.outbound.tavily import TAVILY_EXTRACT_URL, TAVILY_SEARCH_URL, TavilyApi
from app.adapters.outbound.types import ExtractedPage, SearchHit

Handler = Callable[[httpx.Request], httpx.Response]


def factory(handler: Handler) -> Callable[[float], httpx.Client]:
    return lambda timeout: httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)


# ---- Tavily ---------------------------------------------------------------------------------


def tavily(handler: Handler) -> TavilyApi:
    return TavilyApi("tvly-secret", factory(handler), timeout_s=30)


def test_tavily_search_request_and_hits() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "T1", "url": "https://a.org/1", "content": "snippet 1", "score": 0.9},
                    {"title": "T2", "url": "https://b.org/2", "content": "snippet 2", "score": 0.5},
                ]
            },
        )

    hits = tavily(handler).search(
        "reactor decommissioning", max_results=7, include_domains=("iaea.org",)
    )
    assert hits == [
        SearchHit("T1", "https://a.org/1", "snippet 1", "tavily"),
        SearchHit("T2", "https://b.org/2", "snippet 2", "tavily"),
    ]
    (request,) = seen
    assert str(request.url) == TAVILY_SEARCH_URL
    assert request.method == "POST"
    assert request.headers["authorization"] == "Bearer tvly-secret"
    body = json.loads(request.content)
    assert body == {
        "query": "reactor decommissioning",
        "search_depth": "basic",
        "max_results": 7,
        "include_domains": ["iaea.org"],
        "include_answer": False,
        "include_raw_content": False,
    }


def test_tavily_extract_returns_pages_and_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == TAVILY_EXTRACT_URL
        assert json.loads(request.content) == {
            "urls": ["https://a.org/1", "https://b.org/2"],
            "extract_depth": "basic",
            "format": "markdown",
        }
        return httpx.Response(
            200,
            json={
                "results": [{"url": "https://a.org/1", "raw_content": "# Page one"}],
                "failed_results": [{"url": "https://b.org/2", "error": "blocked"}],
            },
        )

    pages, failed = tavily(handler).extract(["https://a.org/1", "https://b.org/2"])
    assert pages == [ExtractedPage("https://a.org/1", "# Page one")]
    assert failed == ["https://b.org/2"]


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (432, PlanLimitError),
        (433, PlanLimitError),
        (429, TransientProviderError),
        (500, TransientProviderError),
        (503, TransientProviderError),
    ],
)
def test_tavily_status_codes(status: int, error: type[Exception]) -> None:
    body = {"detail": {"error": "nope"}}
    api = tavily(lambda r: httpx.Response(status, json=body))
    with pytest.raises(error) as exc:
        api.search("q")
    assert getattr(exc.value, "status", None) == status
    assert "nope" in str(exc.value)


def test_tavily_bad_request_is_not_transient() -> None:
    api = tavily(lambda r: httpx.Response(400, json={"detail": {"error": "max 20 urls"}}))
    with pytest.raises(ProviderError) as exc:
        api.search("q")
    assert not isinstance(exc.value, TransientProviderError | PlanLimitError | ProviderAuthError)


@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout("slow"), httpx.ConnectError("down"), httpx.RemoteProtocolError("x")]
)
def test_tavily_network_problems_are_transient(error: Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    with pytest.raises(TransientProviderError):
        tavily(handler).search("q")


def test_tavily_garbage_body_is_transient() -> None:
    with pytest.raises(TransientProviderError):
        tavily(lambda r: httpx.Response(200, text="<html>oops")).search("q")


# ---- ddgs -----------------------------------------------------------------------------------


def test_ddgs_maps_results() -> None:
    calls: list[tuple[str, int]] = []

    def search(query: str, max_results: int) -> list[dict[str, Any]]:
        calls.append((query, max_results))
        return [{"title": "T", "href": "https://c.org/x", "body": "b"}, {"title": "no url"}]

    assert DdgsSearch(search).search("q", max_results=5) == [
        SearchHit("T", "https://c.org/x", "b", "ddgs")
    ]
    assert calls == [("q", 5)]


def test_ddgs_no_results_is_an_empty_list() -> None:
    def search(query: str, max_results: int) -> list[dict[str, Any]]:
        raise DDGSException("No results found.")

    assert DdgsSearch(search).search("q") == []


@pytest.mark.parametrize(
    "error",
    [RatelimitException("slow down"), TimeoutException("timed out"), DDGSException("engine broke")],
)
def test_ddgs_failures_are_transient(error: Exception) -> None:
    def search(query: str, max_results: int) -> list[dict[str, Any]]:
        raise error

    with pytest.raises(TransientProviderError):
        DdgsSearch(search).search("q")


# ---- HTTP GET -------------------------------------------------------------------------------

MIB = 1024 * 1024


def getter(handler: Handler, html_cap: int = 10 * MIB, pdf_cap: int = 25 * MIB) -> HttpGetter:
    return HttpGetter(
        factory(handler), timeout_s=30, max_html_bytes=html_cap, max_pdf_bytes=pdf_cap
    )


def test_get_returns_status_headers_and_body_and_sends_our_user_agent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>hi</p>")

    response = getter(handler).get("https://a.org/x")
    assert (response.status, response.body, response.content_type) == (
        200,
        b"<p>hi</p>",
        "text/html",
    )
    assert response.too_large is False
    agent = seen[0].headers["user-agent"]
    assert agent.startswith("local-ultradeepsearch/")
    assert "http" not in agent  # no URL in the UA (SEC lesson) and no e-mail
    assert "@" not in agent


def test_get_does_not_follow_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://10.0.0.1/"})

    response = getter(handler).get("https://a.org/x")
    assert (response.status, response.location) == (302, "http://10.0.0.1/")


def test_stream_cap_for_html_cuts_and_flags() -> None:
    body = b"x" * 5000
    response = getter(
        lambda r: httpx.Response(200, headers={"content-type": "text/html"}, content=body),
        html_cap=1000,
    ).get("https://a.org/x")
    assert response.too_large is True
    assert len(response.body) <= 1000 + 65536  # stops at the first chunk beyond the cap


def test_stream_cap_for_pdf_uses_the_larger_limit() -> None:
    body = b"%PDF" + b"x" * 5000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=body)

    assert getter(handler, html_cap=1000, pdf_cap=10_000).get("https://a.org/x").too_large is False
    assert getter(handler, html_cap=1000, pdf_cap=2000).get("https://a.org/x").too_large is True


def test_a_declared_length_over_the_cap_is_refused_without_reading() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html", "content-length": "999999"}, content=b"x"
        )

    response = getter(handler, html_cap=1000).get("https://a.org/x")
    assert response.too_large is True
    assert response.body == b""


@pytest.mark.parametrize("error", [httpx.ReadTimeout("slow"), httpx.ConnectError("down")])
def test_get_network_problems_are_transient(error: Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    with pytest.raises(TransientProviderError):
        getter(handler).get("https://a.org/x")
