"""Step 2.1 (PRD M5): the search plan. The `reason` model drafts queries through lenses A to D, code
validates and trims them, and every query is sanitized before the owner sees the plan. The owner
approves exactly what will be sent: the hash covers the sent text, never the local original."""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import get_args

from app.adapters.outbound.errors import DenylistBlocked, OutboundBlocked
from app.artifacts import write_json
from app.brief.errors import InvalidInput, WrongState
from app.events import EventSink
from app.llm.service import LLMService
from app.llm.types import Message, Role
from app.pipeline.profiles import Profile
from app.prompts import research as prompts
from app.research.models import Item
from app.research.ports import ResearchGateway
from app.research.schemas import PlannedQuery, SearchPlanDraft
from app.research.workspace import set_scaffold_section
from app.store.research import Channel, Lens, QueryDraft, QueryRow, ResearchStore
from app.text import normalize_for_match

EDITABLE = ("draft", "planned", "blocked")


@dataclass(frozen=True)
class PlanScope:
    """Everything the plan operations of one run touch."""

    run_id: str
    run_dir: Path
    store: ResearchStore
    gateway: ResearchGateway
    items: Sequence[Item]
    scholarly: bool  # an assigned domain is scholarly-first


@dataclass(frozen=True)
class PlanCheck:
    kept: list[PlannedQuery]
    problems: list[str]


def channel_for(lens: Lens, scholarly: bool) -> Channel:
    return "scholarly" if lens == "depth" and scholarly else "web"


# ---- validation (pure) ----------------------------------------------------------------------


def _problems(kept: Sequence[PlannedQuery], items: Sequence[Item], profile: Profile) -> list[str]:
    problems: list[str] = []
    adversarial = sum(1 for q in kept if q.lens == "adversarial")
    if adversarial < profile.adversarial_min:
        problems.append(
            f"only {adversarial} adversarial queries, at least {profile.adversarial_min} needed"
        )
    covered = {q.item_id for q in kept}
    problems += [f"item {i.item_id} has no query" for i in items if i.item_id not in covered]
    pinned = {q.item_id for q in kept if q.lens == "period"}
    problems += [
        f"period item {i.item_id} has no period query"
        for i in items
        if i.kind == "period" and i.item_id not in pinned
    ]
    if len(kept) < profile.planned_searches[0]:
        problems.append(f"only {len(kept)} queries, at least {profile.planned_searches[0]} needed")
    return problems


def validate_plan(
    drafts: Sequence[PlannedQuery], items: Sequence[Item], profile: Profile
) -> PlanCheck:
    """Drop queries for unknown items, empty ones and duplicates; list what the plan lacks."""
    known = {i.item_id for i in items}
    seen: set[str] = set()
    kept: list[PlannedQuery] = []
    for query in drafts:
        key = normalize_for_match(query.query)
        if query.item_id in known and key and key not in seen:
            seen.add(key)
            kept.append(query)
    return PlanCheck(kept, _problems(kept, items, profile))


def fit_plan(
    kept: Sequence[PlannedQuery], items: Sequence[Item], profile: Profile
) -> list[PlannedQuery]:
    """At most the profile's maximum: adversarial queries up to the minimum first, then
    round-robin over the items, in plan order."""
    limit = profile.planned_searches[1]
    if len(kept) <= limit:
        return list(kept)
    chosen = [i for i, q in enumerate(kept) if q.lens == "adversarial"][: profile.adversarial_min]
    queues = [[i for i, q in enumerate(kept) if q.item_id == item.item_id] for item in items]
    while len(chosen) < limit and any(queues):
        for queue in queues:
            while queue and queue[0] in chosen:
                queue.pop(0)
            if queue and len(chosen) < limit:
                chosen.append(queue.pop(0))
    return [kept[i] for i in sorted(chosen)]


# ---- drafting -------------------------------------------------------------------------------


def _items_text(items: Sequence[Item]) -> str:
    return "\n".join(f"{i.item_id} ({i.kind.replace('_', '-')}): {i.text}" for i in items)


def _ask_plan(llm: LLMService, system: str, user: str) -> list[PlannedQuery]:
    messages: list[Message] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    return llm.structured(Role.REASON, messages, SearchPlanDraft, think=False).queries


def draft_plan(
    scope: PlanScope, llm: LLMService, events: EventSink, *, brief: str, profile: Profile, shim: str
) -> list[PlannedQuery]:
    """The plan from the model, with one repair call if it falls short; a plan still short is
    used anyway, with an event and a note in the scaffold."""
    has_periods = any(i.kind == "period" for i in scope.items)
    system = prompts.PLAN_SYSTEM.format(
        depth_note=prompts.PLAN_DEPTH_SCHOLARLY if scope.scholarly else prompts.PLAN_DEPTH_WEB,
        period_lens=prompts.PLAN_PERIOD_LENS if has_periods else "",
        min_queries=profile.planned_searches[0],
        max_queries=profile.planned_searches[1],
        adversarial_min=profile.adversarial_min,
    )
    system = f"{system}\n\n{shim}"
    user = prompts.PLAN_USER.format(items=_items_text(scope.items), brief=brief)
    check = validate_plan(_ask_plan(llm, system, user), scope.items, profile)
    if check.problems:
        repair = prompts.PLAN_REPAIR.format(problems="\n".join(f"- {p}" for p in check.problems))
        check = validate_plan(_ask_plan(llm, system, user + repair), scope.items, profile)
    if check.problems:
        events.emit("plan_short", level="warning", problems=check.problems)
        notes = "\n".join(f"- {p}" for p in check.problems)
        set_scaffold_section(scope.run_dir, "Search plan notes", notes)
    return fit_plan(check.kept, scope.items, profile)


# ---- sanitizing, hash, file -----------------------------------------------------------------


def _sanitize(scope: PlanScope, row: QueryRow, step: str) -> None:
    try:
        prepared = scope.gateway.prepare_query(row.original, step=step)
    except DenylistBlocked:
        scope.store.set_sanitized(scope.run_id, row.query_id, "", [], "blocked", "denylist")
    except OutboundBlocked as exc:
        scope.store.set_sanitized(scope.run_id, row.query_id, "", [], "blocked", exc.reason)
    else:
        scope.store.set_sanitized(
            scope.run_id, row.query_id, prepared.sent, prepared.removed_terms, "planned"
        )


def sanitize_pending(scope: PlanScope, *, wave: int, step: str) -> None:
    """Sanitize every `draft` row of ``wave``, each stored before the next is sent to the
    sanitizer, so a resume only handles the rest."""
    for row in scope.store.rows(scope.run_id, wave=wave):
        if row.state == "draft":
            _sanitize(scope, row, step)


def plan_sha256(rows: Sequence[QueryRow]) -> str:
    """The approval hash: wave-1 rows that are not deleted, by id, as sent (never the original)."""
    shown = [
        {
            "query_id": r.query_id,
            "item_id": r.item_id,
            "lens": r.lens,
            "channel": r.channel,
            "sent": r.sent,
            "state": r.state,
        }
        for r in sorted(rows, key=lambda r: r.query_id)
        if r.wave == 1 and r.state != "deleted"
    ]
    text = json.dumps(shown, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_plan_file(scope: PlanScope) -> str:
    """Write `search-plan.json` (the originals stay local) and return the plan hash."""
    rows = scope.store.rows(scope.run_id)
    sha = plan_sha256(rows)
    write_json(
        scope.run_dir / "search-plan.json",
        {
            "plan_sha256": sha,
            "items": [asdict(i) for i in scope.items],
            "rows": [
                {k: v for k, v in asdict(r).items() if k not in ("run_id", "hits")} for r in rows
            ],
        },
    )
    return sha


def create_plan(
    scope: PlanScope, llm: LLMService, events: EventSink, *, brief: str, profile: Profile, shim: str
) -> list[QueryRow]:
    """Draft the plan once (the rows are the record), sanitize what is pending, write the file."""
    if not scope.store.rows(scope.run_id, wave=1):
        drafts = draft_plan(scope, llm, events, brief=brief, profile=profile, shim=shim)
        scope.store.insert_drafts(
            scope.run_id,
            1,
            [
                QueryDraft(q.item_id, q.lens, channel_for(q.lens, scope.scholarly), q.query)
                for q in drafts
            ],
        )
    sanitize_pending(scope, wave=1, step="2.1")
    write_plan_file(scope)
    return scope.store.rows(scope.run_id, wave=1)


# ---- the owner's edits ----------------------------------------------------------------------


def _editable(scope: PlanScope, query_id: str) -> QueryRow:
    row = scope.store.get(scope.run_id, query_id)
    if row.wave != 1 or row.state not in EDITABLE:
        raise WrongState(f"query {query_id} is {row.state} and cannot be changed")
    return row


def _text(text: str) -> str:
    cleaned = " ".join(text.split())
    if not cleaned:
        raise InvalidInput("the query must not be empty")
    return cleaned


def edit_query(scope: PlanScope, query_id: str, text: str) -> QueryRow:
    """New text for a planned query; it is sanitized again before it can be approved."""
    _editable(scope, query_id)
    row = scope.store.edit(scope.run_id, query_id, _text(text))
    _sanitize(scope, row, "2.1")
    write_plan_file(scope)
    return scope.store.get(scope.run_id, query_id)


def add_query(scope: PlanScope, item_id: str, lens: str, text: str) -> QueryRow:
    if item_id not in {i.item_id for i in scope.items}:
        raise InvalidInput(f"unknown item {item_id!r}")
    if lens not in get_args(Lens):
        raise InvalidInput(f"lens must be one of {', '.join(get_args(Lens))}, got {lens!r}")
    chosen: Lens = lens  # type: ignore[assignment]  # checked above
    row = scope.store.add(
        scope.run_id, item_id, chosen, channel_for(chosen, scope.scholarly), _text(text)
    )
    _sanitize(scope, row, "2.1")
    write_plan_file(scope)
    return scope.store.get(scope.run_id, row.query_id)


def delete_query(scope: PlanScope, query_id: str) -> None:
    _editable(scope, query_id)
    scope.store.delete(scope.run_id, query_id)
    write_plan_file(scope)
