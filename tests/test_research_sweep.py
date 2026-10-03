"""Step 2 (PRD M5): search, fetch, coverage check, second wave, `coverage-gaps.md`."""

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from fixtures_corpus import Built, Outcome, SimulatedCrash, article, build, doc, failure, site
from research_rig import (
    BRIEF,
    LIGHT,
    RULES,
    FakePreparer,
    FakeSearcher,
    ResearchModels,
    SearchCrash,
    llm,
    q,
)
from support import make_settings

from app.adapters.outbound.errors import DenylistBlocked, SearchUnavailable
from app.adapters.outbound.types import ScholarlyRecord, SearchHit
from app.events import MemoryEventSink
from app.pipeline.profiles import ResearchBudget
from app.pipeline.strategies import load_strategies
from app.research.models import (
    Decomposition,
    Entity,
    Levers,
    PlannedQuery,
    SearchPlan,
    atomic_items,
)
from app.research.sweep import SweepDeps, Sweeper, SweepResult
from app.store.db import Database
from app.store.research import SearchStore

RUN_ID = "run-a"


def pq(query_id: str, item: str, lens: str, text: str, blocked: str | None = None) -> PlannedQuery:
    return PlannedQuery(
        query_id=query_id,
        item=item,
        lens=lens,  # type: ignore[arg-type]
        kind="scholarly" if lens == "B" else "web",
        original=text,
        sent="" if blocked else text,
        blocked=blocked,
    )


PLAN = SearchPlan(
    queries=(
        pq("q01", "Q1", "A", "alpha"),
        pq("q02", "Q1", "B", "beta"),
        pq("q03", "Q2", "A", "gamma"),
        pq("q04", "E1", "C", "delta"),
    )
)


def hit(i: int) -> SearchHit:
    return SearchHit(f"Page {i}", site(i), "snippet", "tavily")


def paper(i: int, **overrides: object) -> ScholarlyRecord:
    fields: dict[str, object] = {
        "source": "openalex",
        "title": f"Paper {i}",
        "url": site(i),
        "doi": f"10.1234/p{i}",
        "year": 2020,
    }
    return ScholarlyRecord(**{**fields, **overrides})  # type: ignore[arg-type]


def searcher(**kwargs: object) -> FakeSearcher:
    web = {
        "alpha": [hit(1), hit(2), hit(3)],
        "gamma": [hit(5)],
        "delta": [hit(6), hit(7)],
        "gamma zwei": [hit(8), hit(9)],
        "gamma kritik": [hit(9), hit(10)],
    }
    scholarly = {("openalex", "beta"): [paper(4)]}
    return FakeSearcher(web=web, scholarly=scholarly, **kwargs)  # type: ignore[arg-type]


def decomposition(**overrides: object) -> Decomposition:
    base = Decomposition(
        sub_questions=["Wie lange dauert der Rückbau?", "Welche Genehmigungen sind nötig?"],
        entities=[Entity(name="Forschungsreaktor")],
        required_formats=[],
        required_sections=[],
        required_section_headings=["A", "B"],
        time_horizons=[],
        time_periods=[],
        scope_conditions=[],
        domains=["regulation_de_eu"],
        pipeline_tier="light",
        tier_recommendation="light",
        tier_rationale="",
        response_format="short",
        modality="collect",
        levers=Levers(),
    )
    return base.model_copy(update=overrides)


@dataclass
class Rig:
    sweeper: Sweeper
    built: Built
    searcher: FakeSearcher
    preparer: FakePreparer
    models: ResearchModels
    searches: SearchStore
    events: MemoryEventSink
    run_dir: Path


def rig(
    tmp_path: Path,
    *,
    found: FakeSearcher | None = None,
    models: ResearchModels | None = None,
    preparer: FakePreparer | None = None,
    budget: ResearchBudget = LIGHT,
    served: dict[str, Outcome] | None = None,
) -> Rig:
    """One 'process' over the files in ``tmp_path``; calling it again is a restart."""
    events = MemoryEventSink()
    pages = served or {
        **{site(i): doc(site(i), article(i), title=f"Article {i}") for i in range(1, 15)}
    }
    built = build(tmp_path, pages, events=events, run_id=RUN_ID)
    models = models or ResearchModels(plans=[{"queries": []}])
    db = Database(tmp_path / "udr.sqlite")
    searches = SearchStore(db)
    found = found or searcher()
    preparer = preparer or FakePreparer()
    deps = SweepDeps(
        run_id=RUN_ID,
        run_dir=built.run_dir,
        searches=searches,
        searcher=found,
        preparer=preparer,
        ingestor=built.pipeline,
        vault=built.vault,
        service=llm(models, events),
        strategies=load_strategies(make_settings().config_dir),
        budget=budget,
        rules=RULES,
        events=events,
    )
    return Rig(Sweeper(deps), built, found, preparer, models, searches, events, built.run_dir)


def replace_ingestor(sweeper: Sweeper, ingestor: object) -> SweepDeps:
    return dataclasses.replace(sweeper._d, ingestor=ingestor)  # type: ignore[arg-type]


def run(r: Rig, plan: SearchPlan = PLAN, deco: Decomposition | None = None) -> SweepResult:
    return r.sweeper.run(BRIEF, plan, deco or decomposition())


def sources(r: Rig) -> list[str]:
    return sorted(n.url for n in r.built.vault.notes(kind="source"))


WAVE2 = {"queries": [q("Q2", "A", "gamma zwei"), q("Q2", "C", "gamma kritik")]}


def test_every_query_is_searched_once_at_the_sources_of_its_kind(tmp_path: Path) -> None:
    r = rig(tmp_path)
    run(r)
    first = r.searcher.calls[:5]
    assert first == [
        ("web", "alpha"),
        ("openalex", "beta"),
        ("crossref", "beta"),
        ("web", "gamma"),
        ("web", "delta"),
    ]
    assert [(s.query_id, s.source, s.wave) for s in r.searches.all(RUN_ID)][:5] == [
        ("q01", "web", 1),
        ("q02", "openalex", 1),
        ("q02", "crossref", 1),
        ("q03", "web", 1),
        ("q04", "web", 1),
    ]


def test_arxiv_is_added_for_science_and_technical_domains(tmp_path: Path) -> None:
    r = rig(tmp_path)
    run(r, deco=decomposition(domains=["science_medicine"]))
    assert ("arxiv", "beta") in r.searcher.calls
    other = rig(tmp_path / "other")
    run(other)
    assert ("arxiv", "beta") not in other.searcher.calls


def test_the_first_wave_fetches_what_the_searches_found(tmp_path: Path) -> None:
    r = rig(tmp_path)
    result = run(r)
    assert sources(r)[:7] == [site(i) for i in range(1, 8)]
    assert result.counts["Q1"] == 4  # three web pages and the paper
    assert result.counts["E1"] == 2
    notes = {n.url: n for n in r.built.vault.notes(kind="source")}
    assert notes[site(4)].meta["doi"] == "10.1234/p4"  # what the search knew travels along
    assert notes[site(4)].source_tier == "institutional"


def test_a_candidate_cap_limits_the_first_wave(tmp_path: Path) -> None:
    r = rig(
        tmp_path, budget=LIGHT.model_copy(update={"deduped_urls": (1, 3), "fetch_waves": (1, 1)})
    )
    run(r)
    assert len(r.built.fetcher.calls) == 3


def test_a_thin_item_gets_a_second_wave_of_new_queries(tmp_path: Path) -> None:
    models = ResearchModels(plans=[WAVE2])
    r = rig(tmp_path, models=models)
    result = run(r)
    assert result.wave2 is True
    assert models.count("PlanDraft") == 1
    prompt = models.prompts["PlanDraft"][0]
    assert "Q2: question: Welche Genehmigungen sind nötig?" in prompt
    assert "gamma" in prompt  # what was already tried
    assert "Q1" not in prompt.split("Thin items")[1].split("Queries already tried")[0]
    assert r.preparer.calls == [("gamma zwei", "2"), ("gamma kritik", "2")]
    waves = [(s.query_id, s.wave) for s in r.searches.all(RUN_ID) if s.wave == 2]
    assert waves == [("w2-q01", 2), ("w2-q02", 2)]
    assert result.counts["Q2"] == 4  # site 5 and the new pages 8, 9, 10
    assert site(10) in sources(r)
    stored = json.loads((r.run_dir / "temp" / "wave2-plan.json").read_text(encoding="utf-8"))
    assert [x["query_id"] for x in stored["queries"]] == ["w2-q01", "w2-q02"]


def test_no_second_wave_when_nothing_is_thin(tmp_path: Path) -> None:
    models = ResearchModels(plans=[WAVE2])
    found = searcher()
    found.web["gamma"] = [hit(5), hit(11)]
    r = rig(tmp_path, models=models, found=found)
    result = run(r)
    assert result.wave2 is False
    assert models.count("PlanDraft") == 0
    assert not (r.run_dir / "temp" / "wave2-plan.json").exists()


def test_no_second_wave_when_the_profile_allows_one_wave_only(tmp_path: Path) -> None:
    models = ResearchModels(plans=[WAVE2])
    r = rig(tmp_path, models=models, budget=LIGHT.model_copy(update={"fetch_waves": (1, 1)}))
    assert run(r).wave2 is False
    assert models.count("PlanDraft") == 0


def test_second_wave_queries_repeating_a_tried_one_or_naming_other_items_are_dropped(
    tmp_path: Path,
) -> None:
    noisy = {
        "queries": [
            q("Q2", "A", "GAMMA"),  # tried already
            q("Q1", "A", "fremdes item"),  # not a thin item
            q("Q2", "A", "gamma zwei"),
            q("Q2", "A", "Gamma  Zwei"),  # repeated within the answer
        ]
    }
    r = rig(tmp_path, models=ResearchModels(plans=[noisy]))
    run(r)
    assert [c[0] for c in r.preparer.calls] == ["gamma zwei"]


def test_a_second_wave_query_the_gateway_refuses_is_recorded_and_not_searched(
    tmp_path: Path,
) -> None:
    prep = FakePreparer(refuse={"gamma zwei": DenylistBlocked("term")})
    r = rig(tmp_path, models=ResearchModels(plans=[WAVE2]), preparer=prep)
    run(r)
    assert ("web", "gamma zwei") not in r.searcher.calls
    assert ("web", "gamma kritik") in r.searcher.calls
    (event,) = r.events.of_type("wave2_query_blocked")
    assert event.data["reason"] == "denylist"
    stored = json.loads((r.run_dir / "temp" / "wave2-plan.json").read_text(encoding="utf-8"))
    assert [x["blocked"] for x in stored["queries"]] == ["denylist", None]


def test_the_second_wave_does_not_fetch_a_page_again(tmp_path: Path) -> None:
    found = searcher()
    found.web["gamma zwei"] = [hit(1), hit(8)]
    r = rig(tmp_path, models=ResearchModels(plans=[WAVE2]), found=found)
    run(r)
    assert r.built.fetcher.count(site(1)) == 1


def test_a_second_wave_takes_at_most_the_configured_pages_per_item(tmp_path: Path) -> None:
    found = searcher()
    found.web["gamma zwei"] = [hit(i) for i in range(8, 15)]
    found.web["gamma kritik"] = []
    r = rig(tmp_path, models=ResearchModels(plans=[WAVE2]), found=found)
    run(r)
    late = {site(i) for i in range(8, 15)}
    assert len([u for u in r.built.fetcher.calls if u in late]) == RULES.wave2_urls_per_item == 5


def test_a_page_rejected_in_the_first_wave_does_not_use_up_a_second_wave_slot(
    tmp_path: Path,
) -> None:
    served: dict[str, Outcome] = {site(i): doc(site(i), article(i)) for i in range(1, 15)}
    served[site(2)] = failure(site(2), "http_404")
    found = searcher()
    found.web["gamma zwei"] = [hit(i) for i in (2, 8, 9, 10, 11, 12)]
    found.web["gamma kritik"] = []
    r = rig(tmp_path, models=ResearchModels(plans=[WAVE2]), found=found, served=served)
    run(r)
    late = {site(i) for i in range(8, 13)}
    assert len([u for u in r.built.fetcher.calls if u in late]) == 5


def test_a_second_wave_asks_for_no_more_queries_per_item_than_configured(tmp_path: Path) -> None:
    many = {"queries": [q("Q2", "A", f"gamma variante {n}") for n in range(8)]}
    r = rig(tmp_path, models=ResearchModels(plans=[many]))
    run(r)
    assert len(r.preparer.calls) == RULES.wave2_queries_per_item[1] == 3


def test_pending_work_of_an_earlier_run_is_finished_before_anything_else(tmp_path: Path) -> None:
    r = rig(tmp_path)
    steps: list[str] = []
    inner = r.sweeper._d.ingestor

    class Spy:
        def resume(self) -> int:
            steps.append("resume")
            return inner.resume()

        def ingest_many(self, items, *, max_workers: int = 4):
            steps.append("ingest")
            return inner.ingest_many(items, max_workers=max_workers)

    spied = Sweeper(replace_ingestor(r.sweeper, Spy()))
    spied.run(BRIEF, PLAN, decomposition())
    assert steps[0] == "resume"
    assert "ingest" in steps


# ---- search failures ------------------------------------------------------------------------


def test_a_search_that_fails_is_stored_empty_and_the_run_goes_on(tmp_path: Path) -> None:
    fail = {("web", "alpha"): SearchUnavailable("both down")}
    r = rig(tmp_path, found=searcher(fail=fail))
    run(r)
    (event,) = r.events.of_type("search_failed")
    assert (event.data["query_id"], event.data["source"]) == ("q01", "web")
    assert [s.results_json for s in r.searches.all(RUN_ID) if s.query_id == "q01"] == ["[]"]
    assert site(5) in sources(r)  # the other queries still delivered


def test_a_query_blocked_at_the_wire_is_stored_empty(tmp_path: Path) -> None:
    fail = {("web", "delta"): DenylistBlocked("term")}
    r = rig(tmp_path, found=searcher(fail=fail))
    run(r)
    (event,) = r.events.of_type("search_blocked")
    assert event.level == "warning"
    assert event.data["query_id"] == "q04"


def test_a_planned_query_marked_blocked_is_never_searched(tmp_path: Path) -> None:
    plan = SearchPlan(queries=(*PLAN.queries[:3], pq("q04", "E1", "C", "delta", "denylist")))
    r = rig(tmp_path)
    run(r, plan)
    assert ("web", "delta") not in r.searcher.calls
    assert not [s for s in r.searches.all(RUN_ID) if s.query_id == "q04"]


# ---- resuming (AD10) ------------------------------------------------------------------------


def test_a_second_run_over_the_same_files_searches_and_fetches_nothing(tmp_path: Path) -> None:
    first = rig(tmp_path, models=ResearchModels(plans=[WAVE2]))
    run(first)
    second = rig(tmp_path, models=ResearchModels(plans=[WAVE2]))
    result = run(second)
    assert second.searcher.calls == []
    assert second.built.fetcher.calls == []
    assert second.models.count("PlanDraft") == 0  # the stored second-wave plan is reused
    assert result.counts["Q2"] == 4


def test_a_crash_between_searches_costs_only_the_search_in_flight(tmp_path: Path) -> None:
    first = rig(tmp_path, found=searcher(crash_at=3))
    with pytest.raises(SearchCrash):
        run(first)
    assert len(first.searches.all(RUN_ID)) == 2  # alpha and beta at openalex
    second = rig(tmp_path)
    run(second)
    assert second.searcher.calls[:3] == [
        ("crossref", "beta"),
        ("web", "gamma"),
        ("web", "delta"),
    ]
    pairs = [(s.query_id, s.source) for s in second.searches.all(RUN_ID)]
    assert len(pairs) == len(set(pairs))


def test_a_crash_while_fetching_resumes_without_searching_again(tmp_path: Path) -> None:
    first = rig(tmp_path)
    first.built.models.crash_on_extraction = 3  # the third page's extraction dies
    with pytest.raises(SimulatedCrash):
        run(first)
    second = rig(tmp_path)
    result = run(second)
    assert second.searcher.calls == []  # every search was stored before the crash
    assert result.counts["Q1"] == 4
    assert len(sources(second)) == 7


# ---- coverage-gaps.md -----------------------------------------------------------------------


def gaps_text(r: Rig) -> str:
    return (r.run_dir / "temp" / "coverage-gaps.md").read_text(encoding="utf-8")


def test_the_coverage_report_lists_every_item_with_its_status(tmp_path: Path) -> None:
    r = rig(tmp_path, models=ResearchModels(plans=[WAVE2]))
    run(r)
    text = gaps_text(r)
    assert "| Q1 |" in text
    assert "well covered" in text
    assert "| E1 |" in text
    assert "adequate" in text
    assert "| Q2 |" in text
    assert "Retracted" not in text  # no section without a retracted source


def test_a_thin_item_is_flagged_prominently_when_no_second_wave_ran(tmp_path: Path) -> None:
    r = rig(tmp_path, budget=LIGHT.model_copy(update={"fetch_waves": (1, 1)}))
    run(r)
    text = gaps_text(r)
    assert "| Q2 | " in text
    assert "thin" in text
    assert "**" in text.split("| Q2 |")[1].split("\n")[0]


def test_a_shortfall_against_the_minimum_is_stated(tmp_path: Path) -> None:
    r = rig(tmp_path, budget=LIGHT.model_copy(update={"sources_min": 50}))
    run(r)
    assert "below the minimum of 50" in gaps_text(r)
    ok = rig(tmp_path / "ok", budget=LIGHT.model_copy(update={"sources_min": 3}))
    run(ok)
    assert "below the minimum" not in gaps_text(ok)
    assert any(e.type == "sources_below_minimum" for e in r.events.events)


def test_a_retracted_source_is_flagged(tmp_path: Path) -> None:
    found = searcher()
    found.scholarly[("openalex", "beta")] = [paper(4, is_retracted=True)]
    r = rig(tmp_path, found=found)
    run(r)
    text = gaps_text(r)
    assert "## Retracted sources" in text
    assert site(4) in text.split("## Retracted sources")[1]


def test_the_counts_cover_exactly_the_items_of_the_decomposition(tmp_path: Path) -> None:
    r = rig(tmp_path)
    result = run(r)
    assert sorted(result.counts) == [
        i.id for i in sorted(atomic_items(decomposition()), key=lambda i: i.id)
    ]


def test_a_page_that_cannot_be_fetched_is_rejected_not_fatal(tmp_path: Path) -> None:
    served: dict[str, Outcome] = {site(i): doc(site(i), article(i)) for i in range(1, 15)}
    served[site(2)] = failure(site(2), "http_404")
    r = rig(tmp_path, served=served)
    run(r)
    assert site(2) not in sources(r)
    assert any(x.url == site(2) for x in r.built.vault.rejections())


def test_the_model_is_not_called_in_a_sweep_without_thin_items(tmp_path: Path) -> None:
    found = searcher()
    found.web["gamma"] = [hit(5), hit(11)]
    r = rig(tmp_path, found=found)
    run(r)
    assert r.models.calls == {}
