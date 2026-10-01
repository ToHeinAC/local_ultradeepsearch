"""Live checks of the outbound gateway against the real services. Run with `pytest -m live`.

Queries are neutral. Tavily runs only if TAVILY_API_KEY is in this project's `.env` (it spends one
search credit). The sanitizer check uses the real `reason` model on our own Ollama instance.
"""

import json
from pathlib import Path

import pytest

from app import bootstrap
from app.adapters.outbound.gateway import Document, FetchFailure, OutboundGateway, PreparedQuery
from app.config import Settings
from app.events import MemoryEventSink

pytestmark = pytest.mark.live

QUERY = "research reactor decommissioning"
CONTEXT = "Client: Mueller-Werke GmbH. Internal project name: Kranich. Contact: Dr. Anna Schmidt."


def dotenv_settings(data_dir: Path) -> Settings:
    """Settings that also read this project's `.env` (conftest scrubs the process env)."""
    return Settings(_env_file=Path(__file__).parents[2] / ".env", data_dir=data_dir)  # pyright: ignore[reportCallIssue]


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory) -> tuple[OutboundGateway, Path, Settings]:
    data = tmp_path_factory.mktemp("udr-outbound")
    settings = dotenv_settings(data)
    rt = bootstrap.build_runtime(settings, MemoryEventSink())
    gateway = bootstrap.build_gateway(rt, data / "run", credit_cap=5, confidential_context=CONTEXT)
    return gateway, data / "run" / "outbound.jsonl", settings


def q(text: str) -> PreparedQuery:
    return PreparedQuery(text, text, ())


@pytest.mark.parametrize("source", ["openalex", "crossref", "arxiv"])
def test_scholarly_sources_answer(
    live: tuple[OutboundGateway, Path, Settings], source: str
) -> None:
    gateway, _, _ = live
    records = gateway.search_scholarly(q(QUERY), step="live", source=source)  # type: ignore[arg-type]
    assert records
    assert all(r.title for r in records)


def test_web_search_answers(live: tuple[OutboundGateway, Path, Settings]) -> None:
    gateway, _, _ = live
    hits = gateway.search_web(q(QUERY), step="live", max_results=5)
    assert hits
    assert all(h.url.startswith("http") for h in hits)


def test_fetch_html(live: tuple[OutboundGateway, Path, Settings]) -> None:
    gateway, _, _ = live
    doc = gateway.fetch("https://en.wikipedia.org/wiki/Nuclear_decommissioning", step="live")
    assert isinstance(doc, Document), doc
    assert len(doc.text) > 2000


def test_fetch_pdf(live: tuple[OutboundGateway, Path, Settings]) -> None:
    gateway, _, _ = live
    doc = gateway.fetch("https://arxiv.org/pdf/1706.03762", step="live")
    assert isinstance(doc, Document), doc
    assert len(doc.pages) > 5
    assert "attention" in doc.text.lower()


def test_private_addresses_are_refused(live: tuple[OutboundGateway, Path, Settings]) -> None:
    gateway, _, _ = live
    result = gateway.fetch("http://172.16.4.112:8540/", step="live")
    assert result == FetchFailure("http://172.16.4.112:8540/", "blocked_private")


def test_sanitizer_removes_the_client(live: tuple[OutboundGateway, Path, Settings]) -> None:
    gateway, _, _ = live
    prepared = gateway.prepare_query(
        "Mueller-Werke Kranich decommissioning cost estimate methods", step="live"
    )
    sent = prepared.sent.lower()
    assert "mueller" not in sent
    assert "kranich" not in sent
    assert "decommissioning" in sent


def test_every_request_was_logged(live: tuple[OutboundGateway, Path, Settings]) -> None:
    _, log_path, settings = live
    lines = [json.loads(x) for x in log_path.read_text(encoding="utf-8").splitlines()]
    providers = {line["provider"] for line in lines}
    assert {"openalex", "crossref", "arxiv", "http_get"} <= providers
    assert providers & {"tavily_search", "ddgs_search"}
    if settings.tavily_api_key is None:
        assert "tavily_search" not in providers
