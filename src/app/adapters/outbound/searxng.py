"""SearXNG search over its JSON API: our own metasearch instance on loopback (PRD §3.3).

It is an external service we run (`deploy/searxng/`), reached over HTTP like Ollama, so its base
URL is loopback by configuration. One attempt per call; the gateway decides what happens next.
"""

from app.adapters.outbound.http_util import as_dict, as_list, json_object, send
from app.adapters.outbound.types import HttpFactory, SearchHit, default_http

PROVIDER = "searxng"


class SearxngApi:
    def __init__(
        self, base_url: str, http: HttpFactory = default_http, *, timeout_s: float
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/search"
        self._http = http
        self._timeout_s = timeout_s

    @property
    def base_url(self) -> str:
        return self._url.removesuffix("/search")

    def search(self, query: str, max_results: int = 10) -> list[SearchHit]:
        response = send(
            self._http,
            self._timeout_s,
            PROVIDER,
            "GET",
            self._url,
            params={"q": query, "format": "json", "safesearch": "0"},
        )
        rows = (as_dict(r) for r in as_list(json_object(PROVIDER, response).get("results")))
        hits = [
            SearchHit(
                str(r.get("title") or ""), str(r["url"]), str(r.get("content") or ""), PROVIDER
            )
            for r in rows
            if r.get("url")
        ]
        return hits[:max_results]
