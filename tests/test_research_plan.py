"""Step 2.1 (PRD M5): the search plan — drafting, validation, sanitizing, hash and edits."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from research_rig import SANITIZER_FAILS, FakeGateway
from support import make_settings

from app.brief.errors import InvalidInput, WrongState
from app.events import MemoryEventSink
from app.llm.fakes import CallbackTransport, reply
from app.llm.roles import build_registry
from app.llm.service import LLMService
from app.llm.types import ChatReply, ChatRequest, Endpoint
from app.pipeline.profiles import load_profile
from app.prompts import research as prompts
from app.research.models import Item
from app.research.plan import (
    PlanScope,
    add_query,
    channel_for,
    create_plan,
    delete_query,
    edit_query,
    fit_plan,
    plan_sha256,
    validate_plan,
)
from app.research.schemas import PlannedQuery
from app.store.db import Database
from app.store.research import ResearchStore
from app.store.runs import RunStore

SETTINGS = make_settings()
PROFILE = load_profile("light", SETTINGS.config_dir)  # 8..20 queries, 5 adversarial
URLS = {Endpoint.OWN: "http://own", Endpoint.SHARED: "http://shared"}
REGISTRY = build_registry(SETTINGS)
BRIEF = "# Wie teuer ist der Rückbau?\n\n## Forschungsfragen\n\n1. Kosten\n"
ITEMS = [
    Item("i01", "sub_question", "Kosten des Rückbaus"),
    Item("i02", "entity", "Kernkraftwerk Obrigheim"),
    Item("i03", "period", "2005-2025"),
]
SHIM = "## Run directives\n\n- register: analyze\n"


def pq(item: str, lens: str, query: str) -> dict[str, str]:
    return {"item_id": item, "lens": lens, "query": query}


def good_plan() -> list[dict[str, str]]:
    plan = [pq("i01", "breadth", "Rückbau Kosten Kernkraftwerk")]
    plan += [pq("i02", "depth", "Obrigheim Stilllegung Bericht")]
    plan += [pq("i03", "period", "Rückbaukosten 2005 bis 2025 Bericht")]
    plan += [pq("i01", "adversarial", f"Kritik Rückbaukosten Aspekt {n}") for n in range(5)]
    return plan


def planned(*rows: dict[str, str]) -> list[PlannedQuery]:
    return [PlannedQuery.model_validate(r) for r in rows]


@dataclass
class Models:
    plans: list[list[dict[str, str]]] = field(default_factory=lambda: [good_plan()])
    calls: list[ChatRequest] = field(default_factory=lambda: [])

    def __call__(self, request: ChatRequest) -> ChatReply | Exception:
        first = request.messages[0]["content"].split("\n", 1)[0]
        assert first == prompts.PLAN_SYSTEM.split("\n", 1)[0]
        self.calls.append(request)
        return reply(json.dumps({"queries": self.plans[min(len(self.calls), len(self.plans)) - 1]}))


@dataclass
class Rig:
    scope: PlanScope
    gateway: FakeGateway
    models: Models
    llm: LLMService
    events: MemoryEventSink
    store: ResearchStore

    def create(self) -> Any:
        return create_plan(
            self.scope, self.llm, self.events, brief=BRIEF, profile=PROFILE, shim=SHIM
        )

    def sha(self) -> str:
        return plan_sha256(self.store.rows(self.scope.run_id, wave=1))


def rig(
    tmp_path: Path,
    models: Models | None = None,
    gateway: FakeGateway | None = None,
    *,
    scholarly: bool = False,
) -> Rig:
    db = Database(tmp_path / "udr.sqlite")
    run_id = (
        RunStore(db)
        .create_external_run(
            sha256="ab" * 32,
            brief_path="b.md",
            tier="light",
            template_id="auto",
            response_format="structured",
            report_language="de",
            approved_at=datetime(2026, 10, 4, tzinfo=UTC),
        )
        .run_id
    )
    models = models or Models()
    gateway = gateway or FakeGateway()
    events = MemoryEventSink()
    llm = LLMService(
        REGISTRY, URLS, CallbackTransport(models), events, timeout_s=5, sleep=lambda _s: None
    )
    store = ResearchStore(db)
    scope = PlanScope(run_id, tmp_path / "run", store, gateway, ITEMS, scholarly)
    return Rig(scope, gateway, models, llm, events, store)


# ---- validation (pure) ----------------------------------------------------------------------


def test_a_good_plan_has_no_problems() -> None:
    check = validate_plan(planned(*good_plan()), ITEMS, PROFILE)
    assert check.problems == []
    assert len(check.kept) == len(good_plan())


def test_unknown_items_empty_and_duplicate_queries_are_dropped() -> None:
    rows = [*good_plan(), pq("i99", "breadth", "x"), pq("i01", "breadth", "  ")]
    rows.append(pq("i02", "breadth", "rückbau  KOSTEN kernkraftwerk"))  # a duplicate
    check = validate_plan(planned(*rows), ITEMS, PROFILE)
    assert len(check.kept) == len(good_plan())


@pytest.mark.parametrize(
    ("drop", "problem"),
    [
        (lambda p: [q for q in p if q["lens"] != "adversarial"][:3] + p[3:7], "adversarial"),
        (lambda p: [q for q in p if q["item_id"] != "i02"], "i02"),
        (lambda p: [q for q in p if q["lens"] != "period"], "period"),
        (lambda p: p[:4], "at least"),
    ],
)
def test_validation_problems(drop: Any, problem: str) -> None:
    check = validate_plan(planned(*drop(good_plan())), ITEMS, PROFILE)
    assert any(problem in p for p in check.problems), check.problems


def test_an_oversized_plan_keeps_adversarial_queries_and_every_item() -> None:
    rows = good_plan() + [pq("i01", "breadth", f"Kosten Variante {n}") for n in range(30)]
    kept = fit_plan(planned(*rows), ITEMS, PROFILE)
    assert len(kept) == PROFILE.planned_searches[1]
    assert sum(1 for q in kept if q.lens == "adversarial") >= PROFILE.adversarial_min
    assert {q.item_id for q in kept} == {"i01", "i02", "i03"}


def test_a_plan_within_bounds_is_kept_as_it_is() -> None:
    rows = planned(*good_plan())
    assert fit_plan(rows, ITEMS, PROFILE) == rows


@pytest.mark.parametrize(
    ("lens", "scholarly", "channel"),
    [
        ("depth", True, "scholarly"),
        ("depth", False, "web"),
        ("breadth", True, "web"),
        ("adversarial", True, "web"),
        ("period", True, "web"),
    ],
)
def test_channel_by_lens_and_domain(lens: str, scholarly: bool, channel: str) -> None:
    assert channel_for(lens, scholarly) == channel  # type: ignore[arg-type]


# ---- drafting and sanitizing ----------------------------------------------------------------


def test_the_plan_is_drafted_sanitized_and_written(tmp_path: Path) -> None:
    r = rig(tmp_path, scholarly=True)
    rows = r.create()
    assert len(r.models.calls) == 1
    request = r.models.calls[0]
    assert not request.think
    assert request.messages[0]["content"].endswith(SHIM)
    assert BRIEF in request.messages[-1]["content"]
    assert all(row.state == "planned" and row.sent for row in rows)
    depth = next(row for row in rows if row.lens == "depth")
    assert depth.channel == "scholarly"
    data = json.loads((r.scope.run_dir / "search-plan.json").read_text(encoding="utf-8"))
    assert data["plan_sha256"] == r.sha()
    assert [i["item_id"] for i in data["items"]] == ["i01", "i02", "i03"]
    assert data["rows"][0]["original"] == "Rückbau Kosten Kernkraftwerk"


def test_a_short_plan_gets_one_repair_then_proceeds_with_a_note(tmp_path: Path) -> None:
    short = good_plan()[:3]
    r = rig(tmp_path, Models(plans=[short, short]))
    rows = r.create()
    assert len(r.models.calls) == 2
    assert "adversarial" in r.models.calls[1].messages[-1]["content"]
    assert len(rows) == 3
    assert len(r.events.of_type("plan_short")) == 1
    scaffold = (r.scope.run_dir / "scaffold.md").read_text(encoding="utf-8")
    assert "## Search plan notes" in scaffold


def test_a_repaired_plan_is_used(tmp_path: Path) -> None:
    r = rig(tmp_path, Models(plans=[good_plan()[:3], good_plan()]))
    rows = r.create()
    assert len(rows) == len(good_plan())
    assert r.events.of_type("plan_short") == []


def test_a_denylisted_query_is_blocked_before_any_provider(tmp_path: Path) -> None:
    plan = [*good_plan(), pq("i01", "breadth", "Geheimprojekt Kosten")]
    r = rig(tmp_path, Models(plans=[plan]))
    rows = r.create()
    blocked = [row for row in rows if row.state == "blocked"]
    assert [(b.original, b.reason, b.sent) for b in blocked] == [
        ("Geheimprojekt Kosten", "denylist", "")
    ]
    assert r.gateway.web_calls == []
    assert r.gateway.scholarly_calls == []


def test_a_sanitizer_failure_blocks_the_query(tmp_path: Path) -> None:
    plan = [*good_plan(), pq("i01", "breadth", f"Kosten {SANITIZER_FAILS}")]
    rows = rig(tmp_path, Models(plans=[plan])).create()
    assert [(r.state, r.reason) for r in rows if r.state == "blocked"] == [
        ("blocked", "sanitizer_failed")
    ]


def test_removed_terms_are_recorded(tmp_path: Path) -> None:
    plan = [*good_plan(), pq("i02", "breadth", "Firma X Rückbau Angebot")]
    rows = rig(tmp_path, Models(plans=[plan])).create()
    row = rows[-1]
    assert (row.sent, row.removed, row.state) == ("Rückbau Angebot", ("Firma X",), "planned")


def test_a_resumed_plan_sanitizes_only_the_drafts(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.create()
    first = r.scope.run_id
    r.store.edit(first, "q002", "Obrigheim neu")  # a row left at draft by a crash
    r.gateway.prepared.clear()
    r.create()
    assert len(r.models.calls) == 1  # no second drafting
    assert r.gateway.prepared == ["Obrigheim neu"]


# ---- the hash and edits ---------------------------------------------------------------------


def test_the_hash_follows_sent_text_not_the_original(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.create()
    before = r.sha()
    conn = Database(tmp_path / "udr.sqlite").conn  # a second connection, as another process
    conn.execute("UPDATE plan_queries SET original = 'lokal' WHERE query_id = 'q002'")
    assert r.sha() == before
    r.store.set_sanitized(r.scope.run_id, "q001", "anders", [], "planned")
    assert r.sha() != before


def test_edit_add_and_delete_change_the_hash_and_the_file(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.create()
    hashes = {r.sha()}
    edited = edit_query(r.scope, "q001", "Firma X Rückbau neu")
    assert (edited.state, edited.sent, edited.removed) == ("planned", "Rückbau neu", ("Firma X",))
    hashes.add(r.sha())
    added = add_query(r.scope, "i02", "depth", "Obrigheim Gutachten")
    assert (added.query_id, added.channel, added.state) == ("q009", "web", "planned")
    hashes.add(r.sha())
    delete_query(r.scope, "q003")
    hashes.add(r.sha())
    assert len(hashes) == 4
    data = json.loads((r.scope.run_dir / "search-plan.json").read_text(encoding="utf-8"))
    assert data["plan_sha256"] == r.sha()
    assert next(row for row in data["rows"] if row["query_id"] == "q003")["state"] == "deleted"


def test_an_edit_into_a_denylisted_query_blocks_it(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.create()
    assert edit_query(r.scope, "q001", "Geheimprojekt").state == "blocked"


def test_bad_edits_are_rejected(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.create()
    with pytest.raises(InvalidInput, match="unknown item"):
        add_query(r.scope, "i99", "breadth", "x")
    with pytest.raises(InvalidInput, match="lens"):
        add_query(r.scope, "i01", "sideways", "x")
    with pytest.raises(InvalidInput, match="empty"):
        edit_query(r.scope, "q001", "   ")
    r.store.set_result(r.scope.run_id, "q002", "done", [])
    with pytest.raises(WrongState):
        edit_query(r.scope, "q002", "x")
    with pytest.raises(WrongState):
        delete_query(r.scope, "q002")
