from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.adapters.outbound.errors import ProviderError, TransientProviderError
from app.adapters.outbound.scholarly import ArxivApi, CrossrefApi, OpenAlexApi
from app.adapters.outbound.types import ScholarlyRecord

Handler = Callable[[httpx.Request], httpx.Response]


def factory(handler: Handler) -> Callable[[float], httpx.Client]:
    return lambda timeout: httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)


def recording(
    body: Any, status: int = 200, text: str | None = None
) -> tuple[Handler, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=body)

    return handler, seen


# ---- OpenAlex -------------------------------------------------------------------------------

OPENALEX_WORK = {
    "id": "https://openalex.org/W123",
    "doi": "https://doi.org/10.1000/xyz",
    "display_name": "Decommissioning of research reactors",
    "publication_year": 2021,
    "authorships": [
        {"author": {"display_name": "A. Author"}},
        {"author": {"display_name": "B. Author"}},
    ],
    "primary_location": {
        "landing_page_url": "https://journal.org/article/123",
        "source": {"display_name": "Nuclear Engineering"},
    },
    "cited_by_count": 42,
    "is_retracted": False,
    "open_access": {"is_oa": True, "oa_url": "https://repo.org/123.pdf"},
    "type": "article",
    "abstract_inverted_index": {"Reactors": [0], "are": [1], "dismantled.": [2]},
}


def test_openalex_request_and_mapping() -> None:
    handler, seen = recording({"results": [OPENALEX_WORK]})
    api = OpenAlexApi(factory(handler), mailto="me@example.org", api_key="oa-key", timeout_s=30)
    (record,) = api.search("research reactor decommissioning", max_results=5)
    assert record == ScholarlyRecord(
        source="openalex",
        title="Decommissioning of research reactors",
        url="https://journal.org/article/123",
        doi="10.1000/xyz",
        year=2021,
        authors=("A. Author", "B. Author"),
        venue="Nuclear Engineering",
        cited_by_count=42,
        is_retracted=False,
        oa_url="https://repo.org/123.pdf",
        abstract="Reactors are dismantled.",
        kind="article",
    )
    request = seen[0]
    assert request.url.host == "api.openalex.org"
    assert request.url.path == "/works"
    assert request.url.params["search"] == "research reactor decommissioning"
    assert request.url.params["per_page"] == "5"
    assert request.url.params["mailto"] == "me@example.org"
    assert "api_key" not in request.url.params  # the key travels in a header only
    assert request.headers["authorization"] == "Bearer oa-key"
    assert "is_retracted" in request.url.params["select"]
    assert api.request_url("q", 5).startswith("https://api.openalex.org/works?")
    assert "oa-key" not in api.request_url("q", 5)


def test_openalex_sparse_work() -> None:
    handler, _ = recording({"results": [{"id": "https://openalex.org/W9", "display_name": None}]})
    (record,) = OpenAlexApi(factory(handler), timeout_s=30).search("q")
    assert record.title == ""
    assert record.url == "https://openalex.org/W9"
    assert (record.doi, record.is_retracted, record.abstract, record.authors) == (
        None,
        None,
        None,
        (),
    )


def test_openalex_without_mailto_or_key_sends_neither() -> None:
    handler, seen = recording({"results": []})
    assert OpenAlexApi(factory(handler), timeout_s=30).search("q") == []
    assert "mailto" not in seen[0].url.params
    assert "authorization" not in seen[0].headers


# ---- Crossref -------------------------------------------------------------------------------

CROSSREF_ITEM = {
    "DOI": "10.2000/ABC",
    "title": ["Radiation protection in dismantling"],
    "author": [{"given": "Anna", "family": "Muster"}, {"name": "Consortium X"}],
    "issued": {"date-parts": [[2019, 5]]},
    "URL": "https://doi.org/10.2000/abc",
    "is-referenced-by-count": 7,
    "type": "journal-article",
    "container-title": ["Health Physics"],
    "abstract": "<jats:p>Dose <jats:italic>limits</jats:italic> matter.</jats:p>",
}


def test_crossref_request_and_mapping() -> None:
    handler, seen = recording({"message": {"items": [CROSSREF_ITEM]}})
    (record,) = CrossrefApi(factory(handler), mailto="me@example.org", timeout_s=30).search(
        "dose", 3
    )
    assert record == ScholarlyRecord(
        source="crossref",
        title="Radiation protection in dismantling",
        url="https://doi.org/10.2000/abc",
        doi="10.2000/abc",
        year=2019,
        authors=("Anna Muster", "Consortium X"),
        venue="Health Physics",
        cited_by_count=7,
        is_retracted=None,
        oa_url=None,
        abstract="Dose limits matter.",
        kind="journal-article",
    )
    params = seen[0].url.params
    assert (params["query"], params["rows"], params["mailto"]) == ("dose", "3", "me@example.org")


def test_crossref_tolerates_missing_fields() -> None:
    handler, _ = recording({"message": {"items": [{"DOI": "10.1/x"}]}})
    (record,) = CrossrefApi(factory(handler), timeout_s=30).search("q")
    assert (record.title, record.url, record.year, record.authors) == (
        "",
        "https://doi.org/10.1/x",
        None,
        (),
    )


# ---- arXiv ----------------------------------------------------------------------------------

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00001v2</id>
    <published>2024-01-02T00:00:00Z</published>
    <title>  Robotic   dismantling
      of reactors </title>
    <summary> We present a method. </summary>
    <author><name>C. Robot</name></author>
    <author><name>D. Arm</name></author>
    <arxiv:doi>10.3000/xyz</arxiv:doi>
    <arxiv:journal_ref>Robotics 12 (2024)</arxiv:journal_ref>
    <link href="http://arxiv.org/abs/2401.00001v2" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/2401.00001v2"
          rel="related" type="application/pdf"/>
  </entry>
</feed>"""


def test_arxiv_request_and_mapping() -> None:
    handler, seen = recording(None, text=ATOM)
    (record,) = ArxivApi(factory(handler), timeout_s=30).search("robotic reactor-dismantling", 4)
    assert record == ScholarlyRecord(
        source="arxiv",
        title="Robotic dismantling of reactors",
        url="https://arxiv.org/abs/2401.00001v2",
        doi="10.3000/xyz",
        year=2024,
        authors=("C. Robot", "D. Arm"),
        venue="Robotics 12 (2024)",
        cited_by_count=None,
        is_retracted=None,
        oa_url="https://arxiv.org/pdf/2401.00001v2",
        abstract="We present a method.",
        kind="preprint",
    )
    params = seen[0].url.params
    assert seen[0].url.host == "export.arxiv.org"
    assert params["search_query"] == "all:robotic AND all:reactor AND all:dismantling"
    assert params["max_results"] == "4"


def test_arxiv_rejects_hostile_xml() -> None:
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><feed>&a;</feed>'
    handler, _ = recording(None, text=bomb)
    with pytest.raises(ProviderError):
        ArxivApi(factory(handler), timeout_s=30).search("q")


def test_arxiv_query_without_words_returns_nothing_without_a_request() -> None:
    handler, seen = recording(None, text=ATOM)
    assert ArxivApi(factory(handler), timeout_s=30).search("!!! ???") == []
    assert seen == []


# ---- shared error mapping -------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_statuses(status: int) -> None:
    handler, _ = recording({}, status=status)
    for api in (
        OpenAlexApi(factory(handler), timeout_s=1),
        CrossrefApi(factory(handler), timeout_s=1),
        ArxivApi(factory(handler), timeout_s=1),
    ):
        with pytest.raises(TransientProviderError):
            api.search("q")


def test_client_errors_are_not_transient() -> None:
    handler, _ = recording({"error": "bad"}, status=400)
    with pytest.raises(ProviderError) as exc:
        OpenAlexApi(factory(handler), timeout_s=1).search("q")
    assert not isinstance(exc.value, TransientProviderError)


def test_network_errors_are_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(TransientProviderError):
        CrossrefApi(factory(handler), timeout_s=1).search("q")


def test_unexpected_json_shape_is_transient() -> None:
    handler, _ = recording(["not", "an", "object"])
    with pytest.raises(TransientProviderError):
        OpenAlexApi(factory(handler), timeout_s=1).search("q")
