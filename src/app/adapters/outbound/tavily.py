"""Tavily Search and Extract over plain REST (one attempt per call)."""

from collections.abc import Sequence

from app.adapters.outbound.http_util import as_dict, as_list, json_object, send
from app.adapters.outbound.types import ExtractedPage, HttpFactory, SearchHit, default_http

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
PLAN_LIMIT_CODES = (432, 433)  # plan limit, pay-as-you-go limit
PROVIDER = "tavily"


class TavilyApi:
    def __init__(self, api_key: str, http: HttpFactory = default_http, *, timeout_s: float) -> None:
        self._headers = {"authorization": f"Bearer {api_key}"}
        self._http = http
        self._timeout_s = timeout_s

    def _post(self, url: str, payload: dict[str, object]) -> dict[str, object]:
        response = send(
            self._http,
            self._timeout_s,
            PROVIDER,
            "POST",
            url,
            json=payload,
            headers=self._headers,
            plan_limit_codes=PLAN_LIMIT_CODES,
        )
        return json_object(PROVIDER, response)

    def search(
        self, query: str, max_results: int = 10, include_domains: Sequence[str] = ()
    ) -> list[SearchHit]:
        payload: dict[str, object] = {
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "include_domains": list(include_domains),
            "include_answer": False,
            "include_raw_content": False,
        }
        results = (
            as_dict(r) for r in as_list(self._post(TAVILY_SEARCH_URL, payload).get("results"))
        )
        return [
            SearchHit(
                str(r.get("title") or ""), str(r["url"]), str(r.get("content") or ""), PROVIDER
            )
            for r in results
            if r.get("url")
        ]

    def extract(self, urls: Sequence[str]) -> tuple[list[ExtractedPage], list[str]]:
        """(extracted pages, URLs Tavily could not extract). Failed URLs cost nothing."""
        payload: dict[str, object] = {
            "urls": list(urls),
            "extract_depth": "basic",
            "format": "markdown",
        }
        body = self._post(TAVILY_EXTRACT_URL, payload)
        pages = [
            ExtractedPage(str(r["url"]), str(r.get("raw_content") or ""))
            for r in map(as_dict, as_list(body.get("results")))
            if r.get("url")
        ]
        failed = [
            str(r["url"]) for r in map(as_dict, as_list(body.get("failed_results"))) if r.get("url")
        ]
        return pages, failed
