"""The SearXNG client (PRD §3.3): our own metasearch on loopback, first in the web chain."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.adapters.outbound.errors import ProviderError, TransientProviderError
from app.adapters.outbound.searxng import SearxngApi
from app.adapters.outbound.types import SearchHit

BASE = "http://127.0.0.1:8888"
Handler = Callable[[httpx.Request], httpx.Response]


def api(handler: Handler) -> SearxngApi:
    def factory(timeout: float) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)

    return SearxngApi(BASE, factory, timeout_s=5.0)


def answer(body: Any, status: int = 200) -> tuple[Handler, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)

    return handler, seen


RESULTS = {
    "results": [
        {"title": "A", "url": "https://a.example/x", "content": "about a"},
        {"title": "B", "url": "https://b.example/y"},
        {"title": "no url", "content": "dropped"},
    ]
}


def test_hits_are_parsed_and_the_request_asks_for_json() -> None:
    handler, seen = answer(RESULTS)
    hits = api(handler).search("heat pumps", 10)
    assert hits == [
        SearchHit("A", "https://a.example/x", "about a", "searxng"),
        SearchHit("B", "https://b.example/y", "", "searxng"),
    ]
    request = seen[0]
    assert request.method == "GET"
    assert str(request.url).startswith(f"{BASE}/search?")
    assert request.url.params["q"] == "heat pumps"
    assert request.url.params["format"] == "json"


def test_max_results_caps_the_hits() -> None:
    handler, _ = answer({"results": [{"title": str(i), "url": f"https://x/{i}"} for i in range(8)]})
    assert len(api(handler).search("q", 3)) == 3


def test_no_results_is_an_empty_list() -> None:
    handler, _ = answer({"results": []})
    assert api(handler).search("q") == []


@pytest.mark.parametrize("status", [429, 500, 503])
def test_an_outage_is_transient(status: int) -> None:
    handler, _ = answer({"error": "x"}, status)
    with pytest.raises(TransientProviderError):
        api(handler).search("q")


def test_a_refused_format_is_a_provider_error() -> None:
    handler, _ = answer({}, 403)  # json format not enabled in settings.yml
    with pytest.raises(ProviderError):
        api(handler).search("q")


def test_a_connection_error_is_transient() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(TransientProviderError):
        api(refuse).search("q")


def test_a_body_that_is_not_json_is_transient() -> None:
    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>")

    with pytest.raises(TransientProviderError):
        api(html).search("q")


def test_healthy_means_a_json_answer_with_a_results_list() -> None:
    handler, _ = answer({"results": []})
    assert api(handler).healthy() is True


@pytest.mark.parametrize("status", [403, 500])
def test_unhealthy_on_an_error_status(status: int) -> None:
    handler, _ = answer({}, status)
    assert api(handler).healthy() is False


def test_unhealthy_when_nothing_answers() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert api(refuse).healthy() is False
