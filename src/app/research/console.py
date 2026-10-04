"""The plan review of `udr run` (PRD M5): German prompts on a terminal.

The owner sees the sanitized queries exactly as they would be sent and approves, edits, deletes or
adds. Input and output are injected (`ConsoleIO`), so the loop runs on scripted keystrokes in
tests. It only talks to `ResearchService`; the service decides what is allowed, and its errors are
shown as messages while the loop goes on."""

from typing import Literal

from app.brief.console import RULE, ConsoleIO
from app.brief.errors import BriefError, WrongState
from app.research.errors import PlanBlocked, ResearchError, StalePlan
from app.research.models import PlanView
from app.research.service import ResearchService
from app.store.research import QueryRow

Outcome = Literal["approved", "quit"]
MENU_TEXT = "[f] Freigeben  [b] Bearbeiten  [l] Löschen  [n] Neu  [q] Beenden"
LENSES = ("breadth", "depth", "adversarial", "period")
_STATES = {"planned": "bereit", "draft": "Entwurf", "done": "erledigt", "failed": "fehlgeschlagen"}
_HEADER = "Nr | Item | Linse | Kanal | Gesendete Query | Entfernt | Status"
_ERRORS = (BriefError, StalePlan, PlanBlocked, ResearchError)


def _status(row: QueryRow) -> str:
    if row.state == "blocked":
        return f"GESPERRT: {row.reason}"
    return _STATES.get(row.state, row.state)


def render_plan(view: PlanView) -> list[str]:
    """The plan as table lines; deleted queries are left out."""
    lines = [_HEADER]
    for row in view.rows:
        if row.state == "deleted":
            continue
        shown = row.sent or row.original
        removed = ", ".join(row.removed) or "-"
        lines.append(
            f"{int(row.query_id[1:])} | {row.item_id} | {row.lens} | {row.channel} | "
            f"{shown} | {removed} | {_status(row)}"
        )
    return lines


def _query_id(io: ConsoleIO) -> str:
    number = io.ask("Nr der Query").strip()
    if not number.isdigit():
        raise ResearchError(f"'{number}' ist keine Nummer")
    return f"q{int(number):03d}"


def _edit(service: ResearchService, io: ConsoleIO, run_id: str) -> None:
    query_id = _query_id(io)
    service.edit_query(run_id, query_id, io.ask("Neuer Text"))


def _delete(service: ResearchService, io: ConsoleIO, run_id: str) -> None:
    service.delete_query(run_id, _query_id(io))


def _add(service: ResearchService, io: ConsoleIO, run_id: str) -> None:
    item = io.ask("Item (z. B. i01)").strip()
    lens = io.ask(f"Linse ({', '.join(LENSES)}; Enter = breadth)").strip() or "breadth"
    service.add_query(run_id, item, lens, io.ask("Text der Query"))


def _approve(service: ResearchService, io: ConsoleIO, run_id: str) -> Outcome | None:
    view = service.plan(run_id)
    if not view.approvable:
        io.say("Der Plan enthält eine gesperrte Query oder nichts zu suchen.")
        return None
    service.approve_plan(run_id, view.plan_sha256)
    return "approved"


_ACTIONS = {"b": _edit, "l": _delete, "n": _add}


def _show(service: ResearchService, io: ConsoleIO, run_id: str) -> None:
    view = service.plan(run_id)
    io.say(RULE)
    io.say("\n".join(render_plan(view)))
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
    """Show the plan until the owner approves it (the run then continues) or quits. A run that is
    not waiting for the approval ends the review at once."""
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
