"""DuckDuckGo (and friends) via the `ddgs` package: the fallback when Tavily is unavailable.

`ddgs` talks through `primp` (Rust), which bypasses Python's socket module, so the offline test
suite cannot block it. Tests therefore always inject ``search``; only live tests reach the default.
"""

from collections.abc import Callable
from typing import Any, cast

from ddgs import DDGS  # pyright: ignore[reportUnknownVariableType]  # partially typed
from ddgs.exceptions import DDGSException, TimeoutException

from app.adapters.outbound.errors import TransientProviderError
from app.adapters.outbound.types import SearchHit

SearchFn = Callable[[str, int], list[dict[str, Any]]]
PROVIDER = "ddgs"
NO_RESULTS = "No results found."


def _default_search(query: str, max_results: int) -> list[dict[str, Any]]:
    client: Any = DDGS(timeout=10)
    return cast("list[dict[str, Any]]", client.text(query, max_results=max_results, region="wt-wt"))


class DdgsSearch:
    def __init__(self, search: SearchFn = _default_search) -> None:
        self._search = search

    def search(self, query: str, max_results: int = 10) -> list[SearchHit]:
        try:
            rows = self._search(query, max_results)
        except DDGSException as exc:  # also covers its rate-limit and timeout subclasses
            if str(exc) == NO_RESULTS:
                return []
            kind = "timeout" if isinstance(exc, TimeoutException) else "transient"
            raise TransientProviderError(
                PROVIDER, f"{type(exc).__name__}: {exc}", kind=kind
            ) from exc
        return [
            SearchHit(str(r.get("title") or ""), str(r["href"]), str(r.get("body") or ""), PROVIDER)
            for r in rows
            if r.get("href")
        ]
