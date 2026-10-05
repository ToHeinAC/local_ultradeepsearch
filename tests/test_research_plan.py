"""Step 2.1 (PRD M5): the search plan from four lenses, sanitized, hashed, ready for approval."""

from pathlib import Path

import pytest
from research_rig import (
    AUTO_SETTINGS,
    BRIEF,
    DRAFT,
    LIGHT,
    PLAN,
    RULES,
    TEMPLATES,
    FakePreparer,
    ResearchModels,
    llm,
    q,
)

from app.adapters.outbound.errors import DenylistBlocked, OutboundBlocked
from app.events import MemoryEventSink
from app.research.decompose import Decomposer
from app.research.errors import EmptyPlan, PlanBlocked, StalePlan
from app.research.models import Decomposition, PlannedQuery, PlanQueryDraft, SearchPlan
from app.research.plan import (
    Planner,
    check_approvable,
    load_plan,
    plan_hash,
    save_plan,
    trim_queries,
)


def decomposition() -> Decomposition:
    decomposer = Decomposer(llm(ResearchModels()), RULES, MemoryEventSink())
    return decomposer.decompose(
        brief=BRIEF, settings=AUTO_SETTINGS, template=TEMPLATES["auto"]
    ).decomposition


DECOMPOSITION = decomposition()  # items Q1, Q2 and E1


def make(
    models: ResearchModels, preparer: FakePreparer | None = None
) -> tuple[Planner, FakePreparer, MemoryEventSink]:
    events = MemoryEventSink()
    prep = preparer or FakePreparer()
    return Planner(llm(models, events), RULES, LIGHT, prep, events), prep, events


def plan_of(models: ResearchModels, preparer: FakePreparer | None = None) -> SearchPlan:
    planner, _, _ = make(models, preparer)
    return planner.plan(BRIEF, DECOMPOSITION, "Research posture.")


def test_the_plan_numbers_its_queries_and_sends_lens_b_to_scholarly_sources() -> None:
    plan = plan_of(ResearchModels())
    assert [p.query_id for p in plan.queries] == [f"q{n:02d}" for n in range(1, 11)]
    assert [(p.item, p.lens, p.kind) for p in plan.queries[:3]] == [
        ("Q1", "A", "web"),
        ("Q1", "B", "scholarly"),
        ("Q1", "C", "web"),
    ]
    assert plan.queries[0].original == plan.queries[0].sent == "Rückbau Forschungsreaktor Dauer"
    assert all(p.blocked is None for p in plan.queries)


def test_every_query_passes_the_gateway_once_in_plan_order() -> None:
    plan = plan_of(ResearchModels(), prep := FakePreparer())
    assert [c[0] for c in prep.calls] == [p.original for p in plan.queries]
    assert {c[1] for c in prep.calls} == {"2.1"}


def test_the_sanitized_query_and_removed_terms_are_kept_next_to_the_original() -> None:
    original = "Rückbau Forschungsreaktor Dauer"
    prep = FakePreparer(rewrite={original: ("Rückbau Reaktor Dauer", ("Müller AG",))})
    first = plan_of(ResearchModels(), prep).queries[0]
    assert (first.original, first.sent, first.removed_terms) == (
        original,
        "Rückbau Reaktor Dauer",
        ["Müller AG"],
    )


def test_a_query_the_gateway_refuses_stays_in_the_plan_marked_blocked() -> None:
    bad, worse = "Rückbau Forschungsreaktor Dauer", "Genehmigung Rückbau Atomgesetz"
    prep = FakePreparer(
        refuse={bad: DenylistBlocked("term"), worse: OutboundBlocked("sanitizer_failed")}
    )
    plan = plan_of(ResearchModels(), prep)
    assert [(p.sent, p.blocked) for p in plan.queries if p.blocked] == [
        ("", "denylist"),
        ("", "sanitizer_failed"),
    ]
    assert len(plan.queries) == 10


def test_unknown_items_empty_and_duplicate_queries_are_dropped() -> None:
    noisy = {
        "queries": [
            q("Q9", "A", "unbekanntes Item"),
            q("Q1", "A", "  Rückbau   Dauer  "),
            q("Q1", "B", "rückbau dauer"),  # the same query again
            *PLAN["queries"],
        ]
    }
    plan = plan_of(ResearchModels(plans=[noisy]))
    originals = [p.original for p in plan.queries]
    assert "unbekanntes Item" not in originals
    assert [o.casefold() for o in originals].count("rückbau dauer") == 1
    assert originals.count("Rückbau Dauer") == 1
    assert plan.queries[0].original == "Rückbau Dauer"  # whitespace collapsed


def test_a_plan_that_lacks_items_or_adversarial_queries_is_supplemented() -> None:
    short = {"queries": [q("Q1", "A", "Rückbau Dauer"), q("Q1", "C", "Rückbau Kritik")]}
    models = ResearchModels(plans=[short, PLAN])
    plan = plan_of(models)
    assert models.count("PlanDraft") == 2
    second_prompt = models.prompts["PlanDraft"][1]
    assert "Rückbau Dauer" in second_prompt  # the plan so far
    assert "the items Q2, E1" in second_prompt
    assert "more queries with lens C" in second_prompt
    assert {p.item for p in plan.queries} == {"Q1", "Q2", "E1"}
    assert sum(p.lens == "C" for p in plan.queries) >= LIGHT.adversarial_min


def test_a_time_period_without_a_period_pinned_query_is_asked_for() -> None:
    period = {"period": "Q3 2024", "primary_source": "10-Q", "issuer": "Acme"}
    deco_models = ResearchModels(drafts=[{**DRAFT, "time_periods": [period]}])
    events = MemoryEventSink()
    with_period = (
        Decomposer(llm(deco_models, events), RULES, events)
        .decompose(brief=BRIEF, settings=AUTO_SETTINGS, template=TEMPLATES["auto"])
        .decomposition
    )
    models = ResearchModels(plans=[PLAN, {"queries": [q("P1", "D", "Acme 10-Q Q3 2024 filing")]}])
    planner, _, _ = make(models)
    plan = planner.plan(BRIEF, with_period, "")
    assert "a lens D query for the periods P1" in models.prompts["PlanDraft"][1]
    assert [(p.item, p.lens) for p in plan.queries if p.item == "P1"] == [("P1", "D")]


def test_the_thinking_plan_gets_the_larger_output_budget_and_the_supplements_do_not() -> None:
    short = {"queries": [q("Q1", "A", "Rückbau Dauer")]}
    models = ResearchModels(plans=[short])
    events = MemoryEventSink()
    planner = Planner(
        llm(models, events, reason_num_ctx=32768), RULES, LIGHT, FakePreparer(), events
    )
    planner.plan(BRIEF, DECOMPOSITION, "")
    assert models.thinks["PlanDraft"] == [True, False, False]
    assert models.num_predicts["PlanDraft"] == [RULES.thinking_num_predict, 8192, 8192]


def test_supplementing_stops_after_the_configured_rounds_and_uncovered_items_get_a_query() -> None:
    short = {"queries": [q("Q1", "A", "Rückbau Dauer")]}
    models = ResearchModels(plans=[short])
    plan = plan_of(models)
    assert models.count("PlanDraft") == 1 + RULES.plan_supplement_rounds
    by_item = {p.item for p in plan.queries}
    assert by_item == {"Q1", "Q2", "E1"}
    fallback = [p for p in plan.queries if p.item == "Q2"]
    assert [(p.lens, p.kind) for p in fallback] == [("A", "web")]
    assert fallback[0].original == DECOMPOSITION.sub_questions[1]


def test_a_plan_below_the_minimum_is_reported_not_refused() -> None:
    short = {"queries": [q("Q1", "A", "Rückbau Dauer")]}
    planner, _, events = make(ResearchModels(plans=[short]))
    plan = planner.plan(BRIEF, DECOMPOSITION, "")
    (event,) = events.of_type("plan_below_minimum")
    assert event.level == "warning"
    assert event.data["queries"] == len(plan.queries) < LIGHT.planned_searches[0]
    assert event.data["adversarial"] == 0


def test_a_plan_above_the_maximum_is_trimmed_without_losing_coverage() -> None:
    many = [q("Q1", "A", f"Rückbau Aspekt {n}") for n in range(30)]
    models = ResearchModels(plans=[{"queries": [*many, *PLAN["queries"]]}])
    plan = plan_of(models)
    assert len(plan.queries) == LIGHT.planned_searches[1]
    assert {p.item for p in plan.queries} == {"Q1", "Q2", "E1"}
    assert sum(p.lens == "C" for p in plan.queries) >= LIGHT.adversarial_min


def drafts(*raw: dict[str, str]) -> list[PlanQueryDraft]:
    return [PlanQueryDraft.model_validate(item) for item in raw]


def test_trimming_keeps_single_item_queries_period_queries_and_adversarial_minimum() -> None:
    queries = drafts(
        *(q("Q1", "A", f"a{n}") for n in range(6)),
        q("Q2", "A", "only q2"),
        q("P1", "D", "filing"),
        *(q("Q1", "C", f"c{n}") for n in range(3)),
    )
    kept = trim_queries(queries, maximum=6, adversarial_min=3)
    texts = [x.query for x in kept]
    assert texts == ["a0", "only q2", "filing", "c0", "c1", "c2"]


def test_trimming_takes_from_the_most_queried_item_first() -> None:
    queries = drafts(
        q("Q1", "A", "x1"), q("Q1", "A", "x2"), q("Q2", "A", "y1"), q("Q2", "A", "y2"),
        q("Q2", "A", "y3"),
    )  # fmt: skip
    assert [x.query for x in trim_queries(queries, maximum=4, adversarial_min=0)] == [
        "x1",
        "x2",
        "y1",
        "y2",
    ]


def test_trimming_never_drops_a_period_pinned_query_even_from_a_busy_item() -> None:
    queries = drafts(
        q("Q1", "A", "a"), q("Q1", "A", "b"), q("P1", "A", "pa"), q("P1", "D", "filing")
    )
    assert [x.query for x in trim_queries(queries, maximum=3, adversarial_min=0)] == [
        "a",
        "b",
        "filing",
    ]


def test_trim_gives_up_rather_than_dropping_protected_queries() -> None:
    queries = drafts(q("Q1", "A", "a"), q("Q2", "A", "b"), q("P1", "D", "c"))
    assert len(trim_queries(queries, maximum=1, adversarial_min=0)) == 3


# ---- hash, file, approval -------------------------------------------------------------------


def test_the_hash_depends_on_every_field_that_matters() -> None:
    base = plan_of(ResearchModels())
    again = plan_of(ResearchModels())
    assert plan_hash(base) == plan_hash(again)
    first = base.queries[0]
    for change in (
        {"original": "anders"},
        {"sent": "anders"},
        {"lens": "B"},
        {"item": "Q2"},
        {"blocked": "denylist"},
        {"removed_terms": ["x"]},
        {"kind": "scholarly"},
    ):
        changed = SearchPlan(queries=(first.model_copy(update=change), *base.queries[1:]))
        assert plan_hash(changed) != plan_hash(base), change
    assert plan_hash(SearchPlan(queries=base.queries[::-1])) != plan_hash(base)


def test_the_plan_is_saved_with_its_hash_and_loads_back(tmp_path: Path) -> None:
    plan = plan_of(ResearchModels())
    save_plan(tmp_path, plan)
    import json

    stored = json.loads((tmp_path / "search-plan.json").read_text(encoding="utf-8"))
    assert stored["plan_sha256"] == plan_hash(plan)
    assert load_plan(tmp_path) == plan


def test_a_hand_edited_file_changes_the_hash_it_must_be_approved_with(tmp_path: Path) -> None:
    plan = plan_of(ResearchModels())
    save_plan(tmp_path, plan)
    path = tmp_path / "search-plan.json"
    path.write_text(
        path.read_text(encoding="utf-8").replace("Rückbau Forschungsreaktor Dauer", "Neu")
    )
    loaded = load_plan(tmp_path)
    assert plan_hash(loaded) != plan_hash(plan)
    with pytest.raises(StalePlan):
        check_approvable(loaded, plan_hash(plan))
    check_approvable(loaded, plan_hash(loaded))


def test_only_the_current_hash_of_a_clean_plan_approves() -> None:
    plan = plan_of(ResearchModels())
    check_approvable(plan, plan_hash(plan))
    with pytest.raises(StalePlan):
        check_approvable(plan, "0" * 64)


def test_a_plan_with_a_blocked_query_cannot_be_approved() -> None:
    blocked = PlannedQuery(
        query_id="q01", item="Q1", lens="A", kind="web", original="x", sent="", blocked="denylist"
    )
    plan = SearchPlan(queries=(blocked,))
    with pytest.raises(PlanBlocked, match="q01"):
        check_approvable(plan, plan_hash(plan))


def test_an_empty_plan_cannot_be_approved() -> None:
    plan = SearchPlan(queries=())
    with pytest.raises(EmptyPlan):
        check_approvable(plan, plan_hash(plan))
