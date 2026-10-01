"""Scholarly search: OpenAlex, Crossref and arXiv (one attempt per call)."""

import re
from typing import Any
from urllib.parse import urlencode
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from app.adapters.outbound.errors import ProviderError, TransientProviderError
from app.adapters.outbound.http_util import as_dict, as_list, json_object, send
from app.adapters.outbound.types import HttpFactory, ScholarlyRecord, default_http

OPENALEX_URL = "https://api.openalex.org/works"
CROSSREF_URL = "https://api.crossref.org/works"
ARXIV_URL = "https://export.arxiv.org/api/query"
OPENALEX_FIELDS = (
    "id,doi,display_name,publication_year,authorships,primary_location,cited_by_count,"
    "is_retracted,open_access,type,abstract_inverted_index"
)
_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"
_TAG = re.compile(r"<[^>]+>")
_WORD = re.compile(r"[^\W_]+")


def _clean(text: object) -> str:
    return " ".join(_TAG.sub(" ", str(text or "")).split())


def _doi(value: object) -> str | None:
    text = str(value or "").strip().lower()
    return text.removeprefix("https://doi.org/").removeprefix("http://doi.org/") or None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _inverted_abstract(index: object) -> str | None:
    positions = [(pos, word) for word, places in as_dict(index).items() for pos in as_list(places)]
    return " ".join(word for _, word in sorted(positions)) or None


class OpenAlexApi:
    def __init__(
        self,
        http: HttpFactory = default_http,
        *,
        mailto: str | None = None,
        api_key: str | None = None,
        timeout_s: float,
    ) -> None:
        self._http, self._mailto, self._timeout_s = http, mailto, timeout_s
        self._headers = {"authorization": f"Bearer {api_key}"} if api_key else {}

    def _params(self, query: str, max_results: int) -> dict[str, str]:
        params = {"search": query, "per_page": str(max_results), "select": OPENALEX_FIELDS}
        return params | ({"mailto": self._mailto} if self._mailto else {})

    def request_url(self, query: str, max_results: int = 10) -> str:
        return f"{OPENALEX_URL}?{urlencode(self._params(query, max_results))}"

    def search(self, query: str, max_results: int = 10) -> list[ScholarlyRecord]:
        response = send(
            self._http,
            self._timeout_s,
            "openalex",
            "GET",
            OPENALEX_URL,
            params=self._params(query, max_results),
            headers=self._headers,
        )
        return [
            _openalex_record(as_dict(w))
            for w in as_list(json_object("openalex", response).get("results"))
        ]


def _openalex_record(work: dict[str, Any]) -> ScholarlyRecord:
    location = as_dict(work.get("primary_location"))
    authors = (
        as_dict(a.get("author")).get("display_name")
        for a in map(as_dict, as_list(work.get("authorships")))
    )
    retracted = work.get("is_retracted")
    return ScholarlyRecord(
        source="openalex",
        title=_clean(work.get("display_name")),
        url=str(location.get("landing_page_url") or work.get("doi") or work.get("id") or ""),
        doi=_doi(work.get("doi")),
        year=_int(work.get("publication_year")),
        authors=tuple(str(a) for a in authors if a),
        venue=as_dict(location.get("source")).get("display_name"),
        cited_by_count=_int(work.get("cited_by_count")),
        is_retracted=retracted if isinstance(retracted, bool) else None,
        oa_url=as_dict(work.get("open_access")).get("oa_url"),
        abstract=_inverted_abstract(work.get("abstract_inverted_index")),
        kind=work.get("type"),
    )


class CrossrefApi:
    def __init__(
        self, http: HttpFactory = default_http, *, mailto: str | None = None, timeout_s: float
    ) -> None:
        self._http, self._mailto, self._timeout_s = http, mailto, timeout_s

    def _params(self, query: str, max_results: int) -> dict[str, str]:
        params = {"query": query, "rows": str(max_results)}
        return params | ({"mailto": self._mailto} if self._mailto else {})

    def request_url(self, query: str, max_results: int = 10) -> str:
        return f"{CROSSREF_URL}?{urlencode(self._params(query, max_results))}"

    def search(self, query: str, max_results: int = 10) -> list[ScholarlyRecord]:
        response = send(
            self._http,
            self._timeout_s,
            "crossref",
            "GET",
            CROSSREF_URL,
            params=self._params(query, max_results),
        )
        items = as_list(as_dict(json_object("crossref", response).get("message")).get("items"))
        return [_crossref_record(as_dict(item)) for item in items]


def _crossref_author(author: dict[str, Any]) -> str:
    full = " ".join(str(author[k]) for k in ("given", "family") if author.get(k))
    return full or str(author.get("name") or "")


def _crossref_record(item: dict[str, Any]) -> ScholarlyRecord:
    doi = _doi(item.get("DOI"))
    parts = as_list(as_dict(item.get("issued")).get("date-parts"))
    first = as_list(parts[0]) if parts else []
    titles, venues = as_list(item.get("title")), as_list(item.get("container-title"))
    authors = (_crossref_author(as_dict(a)) for a in as_list(item.get("author")))
    return ScholarlyRecord(
        source="crossref",
        title=_clean(titles[0]) if titles else "",
        url=str(item.get("URL") or (f"https://doi.org/{doi}" if doi else "")),
        doi=doi,
        year=_int(first[0]) if first else None,
        authors=tuple(a for a in authors if a),
        venue=str(venues[0]) if venues else None,
        cited_by_count=_int(item.get("is-referenced-by-count")),
        abstract=_clean(item.get("abstract")) or None,
        kind=item.get("type"),
    )


class ArxivApi:
    def __init__(self, http: HttpFactory = default_http, *, timeout_s: float) -> None:
        self._http, self._timeout_s = http, timeout_s

    @staticmethod
    def _search_query(query: str) -> str:
        return " AND ".join(f"all:{w}" for w in _WORD.findall(query.lower())[:8])

    def _params(self, query: str, max_results: int) -> dict[str, str]:
        return {
            "search_query": self._search_query(query),
            "start": "0",
            "max_results": str(max_results),
        }

    def request_url(self, query: str, max_results: int = 10) -> str:
        return f"{ARXIV_URL}?{urlencode(self._params(query, max_results))}"

    def search(self, query: str, max_results: int = 10) -> list[ScholarlyRecord]:
        if not self._search_query(query):
            return []
        response = send(
            self._http,
            self._timeout_s,
            "arxiv",
            "GET",
            ARXIV_URL,
            params=self._params(query, max_results),
        )
        try:
            feed = fromstring(response.text)
        except DefusedXmlException as exc:
            raise ProviderError("arxiv", f"refused hostile XML: {type(exc).__name__}") from exc
        except ParseError as exc:
            raise TransientProviderError("arxiv", f"unreadable XML: {exc}") from exc
        return [_arxiv_record(entry) for entry in feed.iter(f"{_ATOM}entry")]


def _https(url: str) -> str:
    return url.replace("http://arxiv.org/", "https://arxiv.org/", 1)


def _arxiv_record(entry: Element) -> ScholarlyRecord:
    def text(tag: str) -> str:
        return " ".join((entry.findtext(tag) or "").split())

    pdf = next(
        (link.get("href") for link in entry.iter(f"{_ATOM}link") if link.get("title") == "pdf"),
        None,
    )
    published = text(f"{_ATOM}published")
    return ScholarlyRecord(
        source="arxiv",
        title=text(f"{_ATOM}title"),
        url=_https(text(f"{_ATOM}id")),
        doi=_doi(text(f"{_ARXIV}doi")),
        year=int(published[:4]) if published[:4].isdigit() else None,
        authors=tuple(
            " ".join((a.findtext(f"{_ATOM}name") or "").split())
            for a in entry.iter(f"{_ATOM}author")
        ),
        venue=text(f"{_ARXIV}journal_ref") or None,
        oa_url=_https(pdf) if pdf else None,
        abstract=text(f"{_ATOM}summary") or None,
        kind="preprint",
    )
