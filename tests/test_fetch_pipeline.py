import json
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fixtures_corpus import (
    COOKIE_WALL,
    DOI,
    DOI_MIRROR,
    GARBAGE,
    GERMAN,
    GONE,
    LOGIN_WALL,
    PDF_PAGES,
    PROFILE,
    TOO_SHORT,
    VARIANT_1,
    VARIANT_2,
    Built,
    FakeModels,
    Outcome,
    SimulatedCrash,
    ac1_corpus,
    article,
    build,
    doc,
    edited,
    failure,
    prose,
    simple_corpus,
    site,
)

from app.llm.errors import LLMModelMissingError
from app.pipeline.artifacts import export_note_files
from app.pipeline.extraction import lead
from app.pipeline.fetch import Ingested, IngestResult, Rejected
from app.store.models import SourceMeta
from app.store.vault import Vault


def ingested(result: IngestResult) -> Ingested:
    assert isinstance(result, Ingested), result
    return result


def rejected(result: IngestResult) -> Rejected:
    assert isinstance(result, Rejected), result
    return result


def ingest_all(b: Built, urls: list[str]) -> None:
    for url in urls:
        b.pipeline.ingest(url)


def run_json(path: Path) -> dict[str, object]:
    return json.loads((path / "run.json").read_text(encoding="utf-8"))


# ---- AC1: the 20-page corpus ----------------------------------------------------------------


def test_the_twenty_page_corpus_keeps_exactly_the_expected_notes(tmp_path: Path) -> None:
    served, order = ac1_corpus()
    b = build(tmp_path, served)
    results = [b.pipeline.ingest(url, meta=meta) for url, meta in order]

    stored = b.vault.notes(kind="source")
    assert [n.note_id for n in stored] == [f"n{i:04d}" for i in range(1, 13)]
    assert all(n.stage == "complete" for n in stored)
    by_id = {n.note_id: n for n in stored}
    assert by_id["n0011"].canonical_url == "https://mirror.example.org/copy-of-a2"
    assert by_id["n0011"].derivative_of == "n0002"
    assert by_id["n0012"].pages == PDF_PAGES
    assert {n.note_id for n in stored if n.derivative_of} == {"n0011"}

    # URL variants and the DOI mirror resolve to notes that already exist, without a fetch
    outcome = {url: result for (url, _), result in zip(order, results, strict=True)}
    for url, note_id in ((VARIANT_1, "n0001"), (VARIANT_2, "n0001"), (DOI_MIRROR, "n0003")):
        result = outcome[url]
        assert isinstance(result, Ingested)
        assert (result.note.note_id, result.reused) == (note_id, True)
        assert b.fetcher.count(url) == 0
    assert len(b.fetcher.calls) == 17
    assert all(b.fetcher.calls.count(u) == 1 for u in b.fetcher.calls)

    # every rejection has a reason code
    rejections = {r.url: r.reason for r in b.vault.rejections()}
    assert rejections == {
        LOGIN_WALL: "login_wall",
        COOKIE_WALL: "cookie_wall",
        TOO_SHORT: "too_short",
        GARBAGE: "binary_garbage",
        GONE: "http_404",
    }
    for url in rejections:
        result = outcome[url]
        assert isinstance(result, Rejected)
        assert result.reused is False
    assert [len(b.vault.claims(n.note_id)) for n in stored] == [1] * 12


def test_the_files_on_disk_are_final_after_ingestion(tmp_path: Path) -> None:
    served, order = ac1_corpus()
    b = build(tmp_path, served)
    for url, meta in order:
        b.pipeline.ingest(url, meta=meta)
    assert sorted(p.name for p in (b.run_dir / "notes").iterdir()) == [
        f"n{i:04d}.md" for i in range(1, 13)
    ]
    assert (
        export_note_files(b.vault, b.run_dir) == 0
    )  # nothing is stale: the pipeline wrote the end state


# ---- AC2: claim verification and the drop rate ----------------------------------------------


def test_the_drop_rate_goes_to_run_json_and_warns_once(tmp_path: Path) -> None:
    served, order = ac1_corpus()
    b = build(tmp_path, served)
    for url, meta in order:
        b.pipeline.ingest(url, meta=meta)
    stats = run_json(b.run_dir)["stats"]
    assert isinstance(stats, dict)
    assert (stats["claims_kept"], stats["claims_dropped"]) == (
        12,
        12,
    )  # one invented claim per note
    assert stats["claims_drop_rate"] == pytest.approx(0.5)
    assert stats["rejected_by_reason"] == {
        "login_wall": 1,
        "cookie_wall": 1,
        "too_short": 1,
        "binary_garbage": 1,
        "http_404": 1,
    }
    warnings = [e for e in b.events.events if e.type == "extract_quality_low"]  # type: ignore[attr-defined]
    assert len(warnings) == 1
    assert warnings[0].data["claims_drop_rate"] == pytest.approx(0.5)


def test_no_warning_when_the_models_quotes_are_faithful(tmp_path: Path) -> None:
    served, order = ac1_corpus()
    b = build(tmp_path, served, models=FakeModels(fabricate=False))
    for url, meta in order:
        b.pipeline.ingest(url, meta=meta)
    stats = run_json(b.run_dir)["stats"]
    assert isinstance(stats, dict)
    assert stats["claims_drop_rate"] == 0.0
    assert not [e for e in b.events.events if e.type == "extract_quality_low"]  # type: ignore[attr-defined]


def test_a_failed_extraction_keeps_the_note_with_a_lead_summary(tmp_path: Path) -> None:
    b = build(
        tmp_path,
        {site(1): doc(site(1), article(1))},
        models=FakeModels(fail_extraction=LLMModelMissingError("gone")),
    )
    result = b.pipeline.ingest(site(1))
    assert isinstance(result, Ingested)
    note = result.note
    assert (note.extract_failed, note.stage) == (True, "complete")
    assert note.summary == lead(note.body)
    assert b.vault.claims() == []
    assert run_json(b.run_dir)["stats"]["extract_failed"] == 1  # type: ignore[index]
    assert b.vault.find_analysis(note.note_id) is None


def test_a_foreign_language_source_is_kept(tmp_path: Path) -> None:
    text = GERMAN * 30
    b = build(tmp_path, {site(1): doc(site(1), text)})
    result = b.pipeline.ingest(site(1))
    assert isinstance(result, Ingested)
    claims = b.vault.claims(result.note.note_id)
    assert [c.quoted_support for c in claims] == [GERMAN.strip()]


# ---- what gets stored -----------------------------------------------------------------------


def test_the_body_is_the_extractors_text_with_normalised_whitespace(tmp_path: Path) -> None:
    raw = "Alpha   beta\n gamma\n\n\n\nDelta\tepsilon \n\n" + prose(400)
    b = build(tmp_path, {site(1): doc(site(1), raw)})
    result = b.pipeline.ingest(site(1))
    assert isinstance(result, Ingested)
    assert result.note.body.startswith("Alpha beta gamma\n\nDelta epsilon\n\nThe commission")
    assert result.note.word_count == len(result.note.body.split())


def test_title_links_and_tier_are_recorded(tmp_path: Path) -> None:
    html = '<a href="/next">n</a><a href="https://other.org/x#y">o</a>'
    served = {
        "https://www.sec.gov/filing": doc(
            "https://www.sec.gov/filing", article(1), title="Annual report", html=html
        ),
        "https://blog.example.net/a": doc("https://blog.example.net/a", article(2), title=None),
        "https://journal.example.net/a": doc("https://journal.example.net/a", article(3)),
        "https://plain.example.net/a": doc("https://plain.example.net/a", article(4)),
    }
    b = build(tmp_path, served)
    sec = ingested(b.pipeline.ingest("https://www.sec.gov/filing"))
    blog = ingested(b.pipeline.ingest("https://blog.example.net/a"))
    scholarly = ingested(
        b.pipeline.ingest("https://journal.example.net/a", meta=SourceMeta(scholarly=True))
    )
    doi = ingested(b.pipeline.ingest("https://plain.example.net/a", meta=SourceMeta(doi=DOI)))
    assert (sec.note.title, sec.note.source_tier) == ("Annual report", "ground_truth")
    assert sec.note.links == ("https://www.sec.gov/next", "https://other.org/x")
    assert blog.note.title.startswith("Article 2 describes")  # no title: the first line of the text
    assert len(blog.note.title) <= 120
    assert blog.note.source_tier == "unknown"
    assert scholarly.note.source_tier == "institutional"
    assert (doi.note.doi, doi.note.source_tier) == (DOI, "institutional")


def test_the_search_index_covers_stored_sources(tmp_path: Path) -> None:
    served, order = ac1_corpus()
    b = build(tmp_path, served)
    for url, meta in order:
        b.pipeline.ingest(url, meta=meta)
    assert "n0004" in {r.note_id for r in b.vault.search("Article 4 describes facility 4")}


# ---- reuse and retries ----------------------------------------------------------------------


def test_a_known_url_is_not_fetched_or_extracted_again(tmp_path: Path) -> None:
    b = build(tmp_path, {site(1): doc(site(1), article(1))})
    first = ingested(b.pipeline.ingest(site(1)))
    second = ingested(b.pipeline.ingest(site(1) + "?utm_source=again"))
    assert (first.reused, second.reused) == (False, True)
    assert second.note.note_id == first.note.note_id
    assert b.fetcher.calls == [site(1)]
    assert b.models.schemas["ChunkExtraction"] == 1


FLAKY = "https://flaky.example.org/p"


def test_a_transient_failure_is_retried_once_and_then_final(tmp_path: Path) -> None:
    outcomes: dict[str, Outcome | list[Outcome]] = {
        FLAKY: [failure(FLAKY, "network"), failure(FLAKY, "network"), doc(FLAKY, article(5))]
    }
    b = build(tmp_path, outcomes)
    first, second, third = (rejected(b.pipeline.ingest(FLAKY)) for _ in range(3))
    assert (first.rejection.attempts, first.rejection.retryable, first.reused) == (1, True, False)
    assert (second.rejection.attempts, second.reused) == (2, False)
    assert third.reused is True  # two attempts are all a URL gets
    assert b.fetcher.count(FLAKY) == 2


def test_a_retry_that_succeeds_clears_the_rejection(tmp_path: Path) -> None:
    outcomes: dict[str, Outcome | list[Outcome]] = {
        FLAKY: [failure(FLAKY, "timeout"), doc(FLAKY, article(5))]
    }
    b = build(tmp_path, outcomes)
    assert isinstance(b.pipeline.ingest(FLAKY), Rejected)
    assert isinstance(b.pipeline.ingest(FLAKY), Ingested)
    assert b.vault.rejections() == []


@pytest.mark.parametrize(
    ("reason", "retryable"),
    [
        ("timeout", True),
        ("network", True),
        ("http_429", True),
        ("http_503", True),
        ("http_404", False),
        ("http_403", False),
        ("blocked_private", False),
        ("blocked_denylist", False),
        ("too_large", False),
        ("unsupported_type", False),
        ("empty_text", False),
        ("extract_failed", False),
    ],
)
def test_which_failures_are_worth_a_second_attempt(
    tmp_path: Path, reason: str, retryable: bool
) -> None:
    b = build(tmp_path, {FLAKY: failure(FLAKY, reason)})
    first = rejected(b.pipeline.ingest(FLAKY))
    again = rejected(b.pipeline.ingest(FLAKY))
    assert first.rejection.retryable is retryable
    assert again.reused is (not retryable)
    assert b.fetcher.count(FLAKY) == (2 if retryable else 1)


# ---- concurrency ----------------------------------------------------------------------------


def test_parallel_ingestion_never_duplicates_or_refetches(tmp_path: Path) -> None:
    urls = [site(i) for i in range(1, 11)]
    served: dict[str, Outcome | list[Outcome]] = {
        u: doc(u, article(i)) for i, u in enumerate(urls, start=1)
    }
    b = build(tmp_path, served, fetch_delay=0.01)
    items = [(u, None) for u in urls * 3]
    results = b.pipeline.ingest_many(items, max_workers=4)
    assert len(results) == 30
    assert all(isinstance(r, Ingested) for r in results)
    assert sum(1 for r in results if isinstance(r, Ingested) and not r.reused) == 10
    assert len(b.vault.notes(kind="source")) == 10
    assert all(b.fetcher.count(u) == 1 for u in urls)
    assert b.models.schemas["ChunkExtraction"] == 10
    assert len(b.vault.claims()) == 10
    assert [r.note.note_id for r in results if isinstance(r, Ingested)][
        :10
    ] != []  # input order kept


def test_two_urls_of_one_doi_in_flight_at_once_make_one_note(tmp_path: Path) -> None:
    first, second = "https://a.example.org/paper", "https://b.example.org/paper"
    served: dict[str, Outcome | list[Outcome]] = {
        first: doc(first, article(7)),
        second: doc(second, article(8)),  # a different text: only the DOI says it is the same paper
    }
    b = build(tmp_path, served, fetch_delay=0.1)
    meta = SourceMeta(doi=DOI)
    results = b.pipeline.ingest_many([(first, meta), (second, meta)], max_workers=2)
    assert sorted(b.fetcher.calls) == [first, second]  # both were in flight, neither could know
    assert len(b.vault.notes(kind="source")) == 1
    assert b.models.schemas["ChunkExtraction"] == 1  # the second worker must not extract it again
    assert all(isinstance(r, Ingested) for r in results)
    assert sorted(r.reused for r in results if isinstance(r, Ingested)) == [False, True]
    assert len(b.vault.claims()) == 1


def test_reusing_a_note_that_another_worker_is_extracting_waits_for_it(tmp_path: Path) -> None:
    first, second = "https://a.example.org/paper", "https://b.example.org/paper"
    served: dict[str, Outcome | list[Outcome]] = {
        first: doc(first, article(7)),
        second: doc(second, article(8)),
    }
    b = build(tmp_path, served, models=FakeModels(delay=0.4))
    worker = threading.Thread(
        target=b.pipeline.ingest, args=(first,), kwargs={"meta": SourceMeta(doi=DOI)}
    )
    worker.start()
    deadline = time.monotonic() + 5
    while b.vault.get_note("n0001") is None:  # stored, and now busy in the slow extraction
        assert time.monotonic() < deadline, "the first source was never stored"
        time.sleep(0.01)
    result = ingested(b.pipeline.ingest(second, meta=SourceMeta(doi=DOI)))
    worker.join()
    assert result.reused is True
    assert result.note.stage == "complete"  # it waited for the other worker's result
    assert b.models.schemas["ChunkExtraction"] == 1
    assert b.fetcher.calls == [first]


# ---- long sources and their analyses --------------------------------------------------------


def long_doc(url: str, topic: int) -> Outcome:
    return doc(url, article(topic, words=5200))


def test_a_long_source_gets_an_analysis_note(tmp_path: Path) -> None:
    url = "https://long.example.org/report"
    b = build(tmp_path, {url: long_doc(url, 40)})
    result = b.pipeline.ingest(url)
    assert isinstance(result, Ingested)
    assert result.note.stage == "complete"
    analysis = b.vault.find_analysis(result.note.note_id)
    assert analysis is not None
    assert (analysis.kind, analysis.analysis_of) == ("source_analysis", result.note.note_id)
    assert b.models.schemas["SourceAnalysis"] == 1
    assert run_json(b.run_dir)["stats"]["source_analyses"] == 1  # type: ignore[index]
    assert (b.run_dir / "notes" / f"{analysis.note_id}.md").exists()


def test_the_analysis_cap_holds_with_parallel_workers(tmp_path: Path) -> None:
    urls = [f"https://long{i}.example.org/r" for i in range(8)]
    served: dict[str, Outcome | list[Outcome]] = {
        u: long_doc(u, 50 + i) for i, u in enumerate(urls)
    }
    profile = PROFILE.model_copy(update={"source_analysis_cap": 3})
    b = build(tmp_path, served, profile=profile, models=FakeModels(analysis_delay=0.05))
    results = b.pipeline.ingest_many([(u, None) for u in urls], max_workers=4)
    assert all(isinstance(r, Ingested) and r.note.stage == "complete" for r in results)
    assert len(b.vault.notes(kind="source_analysis")) == 3
    assert b.models.schemas["SourceAnalysis"] == 3


def test_a_near_duplicate_of_a_long_source_gets_no_analysis(tmp_path: Path) -> None:
    a, copy = "https://long.example.org/a", "https://mirror.example.org/a"
    served: dict[str, Outcome | list[Outcome]] = {
        a: doc(a, article(60, words=5200)),
        copy: doc(copy, edited(article(60, words=5200), every=40)),
    }
    b = build(tmp_path, served)
    b.pipeline.ingest(a)
    result = b.pipeline.ingest(copy)
    assert isinstance(result, Ingested)
    assert result.note.derivative_of is not None
    assert len(b.vault.notes(kind="source_analysis")) == 1


# ---- AC6: kill and resume (in-process) -------------------------------------------------------


def test_a_crash_during_extraction_resumes_without_repeating_work(tmp_path: Path) -> None:
    served, urls = simple_corpus(6)
    first = build(tmp_path, served, models=FakeModels(crash_on_extraction=3))
    with pytest.raises(SimulatedCrash):
        ingest_all(first, urls)
    note3 = first.vault.get_note("n0003")
    assert note3 is not None
    assert note3.stage == "fetched"  # stored, extraction never finished
    assert first.vault.get_note("n0004") is None

    second = build(tmp_path, served)  # a new process: new objects, same files
    assert second.pipeline.resume() == 1
    for url in urls:
        second.pipeline.ingest(url)

    assert second.fetcher.calls == urls[3:]  # only URLs that were never stored
    assert second.models.extracted_urls == [site(3), site(4), site(5), site(6)]  # not 1 and 2
    notes = second.vault.notes(kind="source")
    assert [n.note_id for n in notes] == [f"n{i:04d}" for i in range(1, 7)]
    assert all(n.stage == "complete" for n in notes)
    assert [len(second.vault.claims(n.note_id)) for n in notes] == [1] * 6  # nothing duplicated
    assert export_note_files(second.vault, second.run_dir) == 0


def test_a_crash_during_analysis_does_not_repeat_the_extraction(tmp_path: Path) -> None:
    url = "https://long.example.org/report"
    served: dict[str, Outcome | list[Outcome]] = {url: long_doc(url, 40)}
    first = build(tmp_path, served, models=FakeModels(crash_on_schema=("PartialAnalysis", 1)))
    with pytest.raises(SimulatedCrash):
        first.pipeline.ingest(url)
    stored = first.vault.get_note("n0001")
    assert stored is not None
    assert stored.stage == "extracted"
    assert len(first.vault.claims()) > 0

    second = build(tmp_path, served)
    assert second.pipeline.resume() == 1
    assert second.fetcher.calls == []
    assert second.models.schemas["ChunkExtraction"] == 0  # extraction is not redone
    assert second.models.schemas["SourceAnalysis"] == 1
    done = second.vault.get_note("n0001")
    assert done is not None
    assert done.stage == "complete"
    assert second.vault.find_analysis("n0001") is not None


def test_resume_restores_note_files_lost_in_the_crash(tmp_path: Path) -> None:
    served, urls = simple_corpus(3)
    first = build(tmp_path, served)
    for url in urls:
        first.pipeline.ingest(url)
    (first.run_dir / "notes" / "n0002.md").unlink()
    second = build(tmp_path, served)
    assert second.pipeline.resume() == 0
    assert (second.run_dir / "notes" / "n0002.md").exists()


def test_resume_with_nothing_pending_does_nothing(tmp_path: Path) -> None:
    served, _ = simple_corpus(1)
    b = build(tmp_path, served)
    assert b.pipeline.resume() == 0
    assert b.fetcher.calls == []
    assert b.models.schemas == {}


# ---- AC6: a real SIGKILL ---------------------------------------------------------------------

CHILD = Path(__file__).parent / "crash_child.py"
CHILD_DEADLINE_S = 15


def kill_child_after_second_source(base_dir: Path) -> list[str]:
    """Run the ingesting child, SIGKILL it once source 2 is stored; returns what it printed."""
    child = subprocess.Popen(
        [sys.executable, str(CHILD), str(base_dir)],
        stdout=subprocess.PIPE,
        text=True,
    )
    watchdog = threading.Timer(CHILD_DEADLINE_S, child.kill)  # a hung child must not hang the suite
    watchdog.start()
    printed: list[str] = []
    try:
        assert child.stdout is not None
        for line in child.stdout:
            printed.append(line.strip())
            if line.strip() == "STORED 2":
                break
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=5)
    finally:
        watchdog.cancel()
        child.kill()
        child.wait()
    assert child.returncode == -signal.SIGKILL
    return printed


def test_a_sigkilled_process_is_resumed_by_a_fresh_one(tmp_path: Path) -> None:
    served, urls = simple_corpus(6)
    assert kill_child_after_second_source(tmp_path) == ["STORED 1", "STORED 2"]

    left = Vault(tmp_path / "udr.sqlite", "run-a")  # what the dead process left behind
    stages = {n.url: n.stage for n in left.notes(kind="source")}
    assert stages == {site(1): "complete", site(2): "fetched"}
    assert len(left.claims()) == 1  # source 1's claim only; source 2 was killed before extracting

    fresh = build(tmp_path, served)
    assert fresh.pipeline.resume() == 1
    for url in urls:
        fresh.pipeline.ingest(url)

    assert fresh.fetcher.calls == urls[2:]  # sources 1 and 2 are never fetched again
    assert fresh.models.extracted_urls == urls[1:]  # source 1 is never extracted again
    notes = fresh.vault.notes(kind="source")
    assert [n.stage for n in notes] == ["complete"] * 6
    assert [len(fresh.vault.claims(n.note_id)) for n in notes] == [1] * 6
    assert export_note_files(fresh.vault, fresh.run_dir) == 0


@pytest.mark.parametrize(("sources", "warns"), [(9, False), (10, True)])
def test_the_quality_warning_needs_twenty_claims_before_it_judges(
    tmp_path: Path, sources: int, warns: bool
) -> None:
    served, urls = simple_corpus(sources)  # two claims per source, one of them invented
    b = build(tmp_path, served)
    b.pipeline.ingest_many([(u, None) for u in urls])
    assert bool([e for e in b.events.events if e.type == "extract_quality_low"]) is warns  # type: ignore[attr-defined]
