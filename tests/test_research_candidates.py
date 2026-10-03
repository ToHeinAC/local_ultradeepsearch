"""Turning stored search results into candidates, a first wave, and per-item coverage."""

import json

import pytest
from support import make_note, make_settings

from app.adapters.outbound.types import ScholarlyRecord, SearchHit
from app.pipeline.strategies import load_strategies
from app.research.candidates import (
    Candidate,
    collect_candidates,
    coverage_counts,
    decode_results,
    encode_results,
    select_for_items,
    select_wave,
)
from app.research.models import Lens, PlannedQuery
from app.store.models import Note, SourceMeta
from app.store.research import SearchRow

STRATEGIES = load_strategies(make_settings().config_dir)


def planned(query_id: str, item: str, lens: Lens = "A") -> PlannedQuery:
    kind = "scholarly" if lens == "B" else "web"
    return PlannedQuery(query_id=query_id, item=item, lens=lens, kind=kind, original="x", sent="x")


def row(
    query_id: str, source: str, hits: list[SearchHit | ScholarlyRecord], wave: int = 1
) -> SearchRow:
    return SearchRow(query_id, source, wave, encode_results(hits))


def web(url: str, title: str = "T") -> SearchHit:
    return SearchHit(title, url, "snippet", "tavily")


def collected(rows: list[SearchRow], queries: list[PlannedQuery]) -> list[Candidate]:
    return collect_candidates({q.query_id: q for q in queries}, rows, STRATEGIES)


# ---- encoding -------------------------------------------------------------------------------


def test_web_and_scholarly_hits_round_trip() -> None:
    record = ScholarlyRecord(
        source="openalex",
        title="A paper",
        url="https://doi.org/10.1/x",
        doi="10.1/X",
        year=2020,
        authors=("Ada",),
        venue="Nature",
        cited_by_count=7,
        is_retracted=True,
        oa_url="https://oa.example.org/x.pdf",
    )
    hits = decode_results(encode_results([web("https://a.example.org/p", "Web page"), record]))
    assert (hits[0].url, hits[0].title, hits[0].meta) == (
        "https://a.example.org/p",
        "Web page",
        SourceMeta(),
    )
    assert hits[1].url == "https://oa.example.org/x.pdf"  # the open copy is fetched
    assert hits[1].meta == SourceMeta(
        doi="10.1/X",
        scholarly=True,
        year=2020,
        authors=("Ada",),
        venue="Nature",
        cited_by_count=7,
        is_retracted=True,
        oa_url="https://oa.example.org/x.pdf",
    )


def test_a_scholarly_hit_without_an_open_copy_uses_its_landing_page() -> None:
    record = ScholarlyRecord(source="crossref", title="T", url="https://doi.org/10.1/x")
    (hit,) = decode_results(encode_results([record]))
    assert hit.url == "https://doi.org/10.1/x"
    assert json.loads(encode_results([record]))[0]["scholarly"] is True


def test_no_results_encode_as_an_empty_list() -> None:
    assert decode_results(encode_results([])) == []


# ---- candidates -----------------------------------------------------------------------------


def test_the_same_page_from_several_queries_is_one_candidate_with_all_its_items() -> None:
    rows = [
        row("q1", "web", [web("https://a.example.org/p"), web("https://b.example.org/p")]),
        row("q2", "web", [web("https://www.a.example.org/p/?utm_source=x")]),
    ]
    found = collected(rows, [planned("q1", "Q1"), planned("q2", "Q2")])
    assert [c.url for c in found] == ["https://a.example.org/p", "https://b.example.org/p"]
    assert found[0].items == ("Q1", "Q2")
    assert found[1].items == ("Q1",)
    assert found[0].key == "https://a.example.org/p"


def test_a_candidate_keeps_its_best_rank_and_prefers_scholarly_metadata() -> None:
    record = ScholarlyRecord(
        source="openalex", title="T", url="https://a.example.org/p", doi="10.1/x", year=2020
    )
    rows = [
        row("q1", "web", [web("https://x.example.org/1"), web("https://a.example.org/p")]),
        row("q2", "openalex", [record]),
    ]
    found = collected(rows, [planned("q1", "Q1"), planned("q2", "Q2", "B")])
    (merged,) = [c for c in found if c.key == "https://a.example.org/p"]
    assert merged.rank == 0
    assert merged.meta.scholarly
    assert merged.meta.doi == "10.1/x"


def test_a_later_worse_rank_does_not_replace_the_best_one() -> None:
    rows = [
        row("q1", "web", [web("https://a.example.org/p")]),
        row("q2", "web", [web("https://b.example.org/p"), web("https://a.example.org/p")]),
    ]
    found = collected(rows, [planned("q1", "Q1"), planned("q2", "Q2")])
    assert {c.url: c.rank for c in found} == {
        "https://a.example.org/p": 0,
        "https://b.example.org/p": 0,
    }


def test_candidates_carry_the_source_tier_weight_and_their_wave() -> None:
    rows = [
        row(
            "q1",
            "web",
            [web("https://www.gesetze-im-internet.de/atg/"), web("https://blog.example.org/x")],
        ),
        row("w2-q01", "web", [web("https://late.example.org/y")], wave=2),
    ]
    found = collected(rows, [planned("q1", "Q1"), planned("w2-q01", "Q1")])
    weights = {c.url.split("/")[2]: c.weight for c in found}
    assert weights["www.gesetze-im-internet.de"] == 1.0  # ground truth
    assert weights["blog.example.org"] == 0.6  # unknown
    assert [c.wave for c in found] == [1, 1, 2]


def test_results_of_queries_that_are_not_in_the_plan_are_ignored() -> None:
    found = collected([row("q9", "web", [web("https://a.example.org/p")])], [planned("q1", "Q1")])
    assert found == []


# ---- selection ------------------------------------------------------------------------------


def cand(
    url: str, items: tuple[str, ...], weight: float = 0.6, rank: int = 0, order: int = 0
) -> Candidate:
    return Candidate(url, url, items, SourceMeta(), weight, rank, 1, order)


def test_the_first_wave_takes_turns_between_items_best_first() -> None:
    pool = [
        cand("https://q1-low", ("Q1",), 0.4, order=0),
        cand("https://q1-best", ("Q1",), 1.0, order=1),
        cand("https://q2-a", ("Q2",), 0.6, order=2),
        cand("https://q2-b", ("Q2",), 0.6, rank=1, order=3),
        cand("https://q1-mid", ("Q1",), 0.6, order=4),
    ]
    chosen = select_wave(pool, ["Q1", "Q2"], cap=4)
    assert [c.url for c in chosen] == [
        "https://q1-best",
        "https://q2-a",
        "https://q1-mid",
        "https://q2-b",
    ]


def test_a_page_serving_two_items_counts_once_in_the_cap() -> None:
    pool = [cand("https://both", ("Q1", "Q2")), cand("https://q2-only", ("Q2",), order=1)]
    assert [c.url for c in select_wave(pool, ["Q1", "Q2"], cap=2)] == [
        "https://both",
        "https://q2-only",
    ]


def test_selection_takes_everything_when_there_are_fewer_than_the_cap() -> None:
    pool = [cand("https://a", ("Q1",))]
    assert [c.url for c in select_wave(pool, ["Q1", "Q2"], cap=30)] == ["https://a"]
    assert select_wave([], ["Q1"], cap=5) == []


def test_ties_are_broken_by_rank_then_discovery_order() -> None:
    pool = [
        cand("https://late", ("Q1",), rank=2, order=0),
        cand("https://early-b", ("Q1",), rank=0, order=2),
        cand("https://early-a", ("Q1",), rank=0, order=1),
    ]
    assert [c.url for c in select_wave(pool, ["Q1"], cap=3)] == [
        "https://early-a",
        "https://early-b",
        "https://late",
    ]


def test_wave_two_takes_a_fixed_number_per_thin_item_skipping_known_pages() -> None:
    pool = [
        cand("https://known", ("Q1",), 1.0, order=0),
        cand("https://q1-a", ("Q1",), 0.6, order=1),
        cand("https://q1-b", ("Q1",), 0.6, rank=1, order=2),
        cand("https://q1-c", ("Q1",), 0.6, rank=2, order=3),
        cand("https://q2-a", ("Q2",), 0.6, order=4),
        cand("https://other", ("Q3",), 0.6, order=5),
    ]
    chosen = select_for_items(pool, ["Q1", "Q2"], per_item=2, exclude={"https://known"})
    assert [c.url for c in chosen] == ["https://q1-a", "https://q1-b", "https://q2-a"]


# ---- coverage -------------------------------------------------------------------------------


def note(note_id: str, url: str, **overrides: object) -> Note:
    fields: dict[str, object] = {"stage": "complete", **overrides}
    return make_note(note_id=note_id, canonical_url=url, url=url, **fields)


def test_a_complete_original_counts_for_every_item_whose_query_found_it() -> None:
    pool = [cand("https://a", ("Q1", "Q2")), cand("https://b", ("Q1",))]
    notes = [note("n1", "https://a"), note("n2", "https://b")]
    assert coverage_counts(notes, pool, ["Q1", "Q2", "E1"]) == {"Q1": 2, "Q2": 1, "E1": 0}


@pytest.mark.parametrize(
    "overrides",
    [
        {"stage": "extracted"},
        {"derivative_of": "n9"},
        {"extract_failed": True},
        {"kind": "source_analysis"},
    ],
)
def test_derivatives_failed_extractions_unfinished_notes_and_analyses_do_not_count(
    overrides: dict[str, object],
) -> None:
    notes = [note("n1", "https://a", **overrides)]
    assert coverage_counts(notes, [cand("https://a", ("Q1",))], ["Q1"]) == {"Q1": 0}


def test_a_note_reached_through_another_url_of_the_same_doi_still_counts() -> None:
    meta = SourceMeta(doi="10.1234/abc")
    mirror = Candidate("https://mirror", "https://mirror", ("Q2",), meta, 0.6, 0, 1, 0)
    notes = [note("n1", "https://first-url", doi="10.1234/abc")]
    assert coverage_counts(notes, [mirror], ["Q1", "Q2"]) == {"Q1": 0, "Q2": 1}
