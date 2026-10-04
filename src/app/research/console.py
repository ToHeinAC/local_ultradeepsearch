"""The plan review of `udr run` (PRD M5 D3): German prompts on a terminal.

The owner sees the sanitized queries exactly as they would be sent and approves, edits (in
`$EDITOR`, one query per line), deletes, adds or quits. Input, output and the editor are injected
(`ConsoleIO`), so the loop runs on scripted keystrokes in tests. It only talks to
`ResearchService`; the service decides what is allowed, and its errors are shown as messages while
the loop goes on."""

import re
from typing import Literal

from app.brief.console import RULE, ConsoleIO
from app.brief.errors import BriefError, WrongState
from app.research.errors import InvalidEdit, ResearchError
from app.research.models import PlannedQuery, SearchPlan
from app.research.service import ResearchService

Outcome = Literal["approved", "quit"]
MENU_TEXT = "[f] Freigeben  [b] Bearbeiten  [l] Löschen  [n] Neu  [q] Beenden"
HEADER = "Nr | Item | Linse | Kanal | Gesendete Query | Entfernt | Status"
LENS_HELP = "Linse (A Breite, B Literatur, C Gegenposition, D Zeitraum)"
_ERRORS = (BriefError, ResearchError)
_LINE_ID = re.compile(r"q(\d+)\s*\|")


def _status(query: PlannedQuery) -> str:
    return f"GESPERRT: {query.blocked}" if query.blocked else "bereit"


def render_plan(plan: SearchPlan) -> list[str]:
    """The plan as table lines."""
    lines = [HEADER]
    for q in plan.queries:
        removed = ", ".join(q.removed_terms) or "-"
        shown = q.sent or q.original
        lines.append(
            f"{int(q.query_id[1:])} | {q.item} | {q.lens} | {q.kind} | {shown} | {removed} | "
            f"{_status(q)}"
        )
    return lines


def _without(text: str, number: int) -> str:
    """The plan text without the line of query ``number``."""
    kept = [
        line
        for line in text.split("\n")
        if not ((m := _LINE_ID.match(line.strip())) and int(m[1]) == number)
    ]
    if len(kept) == len(text.split("\n")):
        raise InvalidEdit(f"keine Query mit der Nummer {number}")
    return "\n".join(kept)


def _number(io: ConsoleIO) -> int:
    answer = io.ask("Nr der Query").strip()
    if not answer.isdigit():
        raise InvalidEdit(f"'{answer}' ist keine Nummer")
    return int(answer)


def _edit(service: ResearchService, io: ConsoleIO, run_id: str) -> None:
    edited = io.edit(service.plan_text(run_id))
    if edited is not None:
        service.update_plan(run_id, edited)


def _delete(service: ResearchService, io: ConsoleIO, run_id: str) -> None:
    service.update_plan(run_id, _without(service.plan_text(run_id), _number(io)))


def _add(service: ResearchService, io: ConsoleIO, run_id: str) -> None:
    item = io.ask("Item (z. B. i01)").strip()
    lens = io.ask(LENS_HELP).strip().upper() or "A"
    text = io.ask("Text der Query").strip()
    service.update_plan(run_id, f"{service.plan_text(run_id)}- | {item} | {lens} | {text}\n")


def _approve(service: ResearchService, io: ConsoleIO, run_id: str) -> Outcome:
    service.approve_plan(run_id, str(service.view(run_id).plan_sha256))
    return "approved"


_ACTIONS = {"b": _edit, "l": _delete, "n": _add}


def _show(service: ResearchService, io: ConsoleIO, run_id: str) -> None:
    view = service.view(run_id)
    if view.plan is None or view.waiting_for != "plan":
        raise WrongState("der Lauf wartet nicht auf die Freigabe seines Plans")
    io.say(RULE)
    io.say("\n".join(render_plan(view.plan)))
    io.say(RULE)


def _step(service: ResearchService, io: ConsoleIO, run_id: str) -> Outcome | None:
    choice = io.ask(MENU_TEXT).strip().lower()
    if choice == "q":
        return "quit"
    if choice == "f":
        return _approve(service, io, run_id)
    if choice in _ACTIONS:
        _ACTIONS[choice](service, io, run_id)
    else:
        io.say("Bitte f, b, l, n oder q eingeben.")
    return None


def run_plan_review(service: ResearchService, io: ConsoleIO, run_id: str) -> Outcome:
    """Show the plan until the owner approves it (the run then continues to its end) or quits. A
    run that is not waiting for the approval ends the review at once."""
    while True:
        try:
            _show(service, io, run_id)
        except WrongState as exc:
            io.say(f"Das ging nicht: {exc}")
            return "quit"
        try:
            outcome = _step(service, io, run_id)
        except _ERRORS as exc:
            io.say(f"Das ging nicht: {exc}")
            continue
        if outcome is not None:
            return outcome
