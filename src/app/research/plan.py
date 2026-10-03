"""Step 2.1 (PRD M5): the search plan.

`reason` proposes queries from four lenses; code enforces the profile's bounds, fills what is
missing, and sends every query through the gateway's denylist and sanitizer. The owner approves
the plan by its hash (PRD §3.2), so what was approved is exactly what is searched.
"""

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from app.adapters.outbound.errors import DenylistBlocked, OutboundBlocked
from app.adapters.outbound.gateway import PreparedQuery
from app.artifacts import write_json
from app.events import EventSink
from app.llm.service import LLMService
from app.llm.types import Message, Role
from app.pipeline.profiles import ResearchBudget, RunRules
from app.prompts import research as prompts
from app.research.errors import EmptyPlan, PlanBlocked, StalePlan
from app.research.models import (
    AtomicItem,
    Decomposition,
    Lens,
    PlanDraft,
    PlannedQuery,
    PlanQueryDraft,
    QueryKind,
    SearchPlan,
    atomic_items,
    render_items,
)
from app.text import normalize_for_match

PLAN_FILE = "search-plan.json"
STEP = "2.1"


class QueryPreparer(Protocol):
    """The outbound gateway's `prepare_query`, or a fake in tests."""

    def prepare_query(self, query: str, *, step: str) -> PreparedQuery: ...


def kind_of(lens: str) -> QueryKind:
    """Lens B goes to the scholarly sources, every other lens to the web."""
    return "scholarly" if lens == "B" else "web"


def prepare(
    preparer: QueryPreparer, query_id: str, item: str, lens: Lens, text: str
) -> PlannedQuery:
    """One plan line, sent through the gateway's denylist and sanitizer. A refused query is kept,
    marked blocked, so the owner sees it and must edit or delete it."""
    sent, removed, blocked = "", [], None
    try:
        prepared = preparer.prepare_query(text, step=STEP)
        sent, removed = prepared.sent, list(prepared.removed_terms)
    except DenylistBlocked:
        blocked = "denylist"
    except OutboundBlocked as exc:
        blocked = exc.reason
    return PlannedQuery(
        query_id=query_id,
        item=item,
        lens=lens,
        kind=kind_of(lens),
        original=text,
        sent=sent,
        removed_terms=removed,
        blocked=blocked,
    )


def plan_hash(plan: SearchPlan) -> str:
    """sha256 of the plan's canonical JSON: the value an approval must name."""
    data = [query.model_dump(mode="json") for query in plan.queries]
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def save_plan(run_dir: Path, plan: SearchPlan) -> None:
    write_json(
        run_dir / PLAN_FILE,
        {
            "plan_sha256": plan_hash(plan),
            "queries": [query.model_dump(mode="json") for query in plan.queries],
        },
    )


def load_plan(run_dir: Path) -> SearchPlan:
    """The plan in `search-plan.json`. The stored hash is informative: it is recomputed, so a
    hand-edited file needs the approval of its own, new hash."""
    stored = json.loads((run_dir / PLAN_FILE).read_text(encoding="utf-8"))
    return SearchPlan.model_validate({"queries": stored["queries"]})


def check_approvable(plan: SearchPlan, sha256: str) -> None:
    """Raise unless ``sha256`` is the plan's hash and every query can be sent."""
    if sha256 != plan_hash(plan):
        raise StalePlan("the hash does not belong to the current search plan")
    if not plan.queries:
        raise EmptyPlan("the search plan has no queries")
    blocked = [query.query_id for query in plan.queries if query.blocked]
    if blocked:
        raise PlanBlocked(f"queries that cannot be sent: {', '.join(blocked)}")


def _victim(kept: Sequence[PlanQueryDraft], adversarial_min: int) -> int | None:
    per_item = Counter(query.item for query in kept)
    adversarial = sum(query.lens == "C" for query in kept)
    candidates = [
        index
        for index, query in enumerate(kept)
        if query.lens != "D"
        and per_item[query.item] > 1
        and not (query.lens == "C" and adversarial <= adversarial_min)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda i: (per_item[kept[i].item], i))


def trim_queries(
    queries: Sequence[PlanQueryDraft], *, maximum: int, adversarial_min: int
) -> list[PlanQueryDraft]:
    """Drop the last query of the most-queried item until at most ``maximum`` remain. The only
    query of an item, period-pinned queries and the adversarial minimum are never dropped, so
    the plan may stay above the maximum rather than lose coverage."""
    kept = list(queries)
    while len(kept) > maximum:
        victim = _victim(kept, adversarial_min)
        if victim is None:
            break
        del kept[victim]
    return kept


def _normalized(
    raw: Sequence[PlanQueryDraft], known: set[str], seen: set[str]
) -> list[PlanQueryDraft]:
    """Queries of known items with collapsed whitespace, without repeats of anything in ``seen``."""
    kept: list[PlanQueryDraft] = []
    for draft in raw:
        text = " ".join(draft.query.split())
        key = normalize_for_match(text)
        if draft.item in known and text and key not in seen:
            seen.add(key)
            kept.append(draft.model_copy(update={"query": text}))
    return kept


class Planner:
    def __init__(
        self,
        service: LLMService,
        rules: RunRules,
        budget: ResearchBudget,
        preparer: QueryPreparer,
        events: EventSink,
    ) -> None:
        self._service = service
        self._rules = rules
        self._budget = budget
        self._preparer = preparer
        self._events = events

    def plan(self, brief: str, decomposition: Decomposition, research_shim: str) -> SearchPlan:
        """The prepared search plan. A model failure propagates (the step runs again on resume);
        a query the gateway refuses stays in the plan, marked blocked."""
        items = atomic_items(decomposition)
        known = {item.id for item in items}
        seen: set[str] = set()
        user = prompts.PLAN_USER.format(
            brief=brief,
            domains=", ".join(decomposition.domains) or "none named",
            research_shim=research_shim,
            items=render_items(items),
        )
        queries = _normalized(self._ask(user, think=True).queries, known, seen)
        for _ in range(self._rules.plan_supplement_rounds):
            missing = self._missing(queries, items)
            if not missing:
                break
            more = self._ask(user + self._more(queries, missing), think=False)
            queries += _normalized(more.queries, known, seen)
        queries += self._fallback(queries, items)
        queries = trim_queries(
            queries,
            maximum=self._budget.planned_searches[1],
            adversarial_min=self._budget.adversarial_min,
        )
        self._warn_if_short(queries)
        return SearchPlan(
            queries=tuple(
                prepare(self._preparer, f"q{n:02d}", query.item, query.lens, query.query)
                for n, query in enumerate(queries, 1)
            )
        )

    def _ask(self, user: str, *, think: bool) -> PlanDraft:
        low, high = self._budget.planned_searches
        system = prompts.PLAN_SYSTEM.format(
            min_queries=low, max_queries=high, adversarial_min=self._budget.adversarial_min
        )
        messages: list[Message] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        return self._service.structured(Role.REASON, messages, PlanDraft, think=think)

    @staticmethod
    def _more(queries: Sequence[PlanQueryDraft], missing: str) -> str:
        listed = "\n".join(f"{q.item} {q.lens}: {q.query}" for q in queries)
        return prompts.PLAN_MORE_USER.format(plan=listed, missing=missing)

    def _missing(self, queries: Sequence[PlanQueryDraft], items: Sequence[AtomicItem]) -> str:
        """What the plan lacks, as the supplement prompt words it; empty if nothing."""
        covered = {query.item for query in queries}
        parts: list[str] = []
        uncovered = [i.id for i in items if i.id not in covered]
        if uncovered:
            parts.append(prompts.PLAN_MISSING_ITEMS.format(items=", ".join(uncovered)))
        unpinned = [
            i.id
            for i in items
            if i.kind == "period" and not any(q.item == i.id and q.lens == "D" for q in queries)
        ]
        if unpinned:
            parts.append(prompts.PLAN_MISSING_PERIODS.format(items=", ".join(unpinned)))
        adversarial = sum(query.lens == "C" for query in queries)
        if adversarial < self._budget.adversarial_min:
            count = self._budget.adversarial_min - adversarial
            parts.append(prompts.PLAN_MISSING_ADVERSARIAL.format(count=count))
        short = self._budget.planned_searches[0] - len(queries)
        if short > 0:
            parts.append(prompts.PLAN_MISSING_TOTAL.format(count=short))
        return " ".join(parts)

    @staticmethod
    def _fallback(
        queries: Sequence[PlanQueryDraft], items: Sequence[AtomicItem]
    ) -> list[PlanQueryDraft]:
        """The item's own text as a breadth query for every item still without a query."""
        covered = {query.item for query in queries}
        return [
            PlanQueryDraft(item=i.id, lens="A", query=" ".join(i.text.split()))
            for i in items
            if i.id not in covered
        ]

    def _warn_if_short(self, queries: Sequence[PlanQueryDraft]) -> None:
        adversarial = sum(query.lens == "C" for query in queries)
        if (
            len(queries) < self._budget.planned_searches[0]
            or adversarial < self._budget.adversarial_min
        ):
            self._events.emit(
                "plan_below_minimum",
                level="warning",
                queries=len(queries),
                adversarial=adversarial,
            )
