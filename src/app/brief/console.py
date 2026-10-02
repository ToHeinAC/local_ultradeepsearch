"""The interactive loop of `udr brief` (PRD M4): German prompts on a terminal.

Input, output and the editor are injected (`ConsoleIO`), so the loop runs on scripted keystrokes in
tests and on `typer` in the command. It only talks to `BriefService`; the service decides what is
allowed. A `BriefError` (not a crash) is shown as a message and the loop goes on.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from app.brief.errors import BriefError
from app.brief.protocol import AnswerInput
from app.brief.render import canonical_text
from app.brief.service import BriefService, SessionView

Outcome = Literal["approved", "saved", "quit"]
RULE = "-" * 72
TIER_NAMES = {"light": "Lite", "full": "Full"}
_TIER_INPUT = {"l": "light", "lite": "light", "light": "light", "f": "full", "full": "full"}
_MENU = {
    "f": "approve",
    "freigeben": "approve",
    "u": "revise",
    "überarbeiten": "revise",
    "ueberarbeiten": "revise",
    "b": "edit",
    "bearbeiten": "edit",
    "e": "settings",
    "einstellungen": "settings",
    "s": "save",
    "speichern": "save",
    "q": "quit",
    "beenden": "quit",
}
MENU_TEXT = (
    "[f] Freigeben   [u] Überarbeiten   [b] Bearbeiten   [e] Einstellungen   "
    "[s] Speichern   [q] Beenden"
)


@dataclass(frozen=True)
class ConsoleIO:
    ask: Callable[[str], str]  # shows a prompt, returns the typed line
    say: Callable[[str], None]
    edit: Callable[[str], str | None]  # opens text in an editor; None if left unchanged


Handler = Callable[[BriefService, ConsoleIO, SessionView], Outcome | None]


def _show_brief(io: ConsoleIO, text: str) -> None:
    io.say(RULE)
    io.say(text.rstrip("\n"))
    io.say(RULE)


def _resume_hint(io: ConsoleIO, session_id: str) -> None:
    io.say(f"Die Sitzung bleibt offen. Weiter mit: udr brief --session {session_id}")


# ---- the question rounds --------------------------------------------------------------------


def _answer_for(io: ConsoleIO, number: int, question: dict[str, str]) -> tuple[AnswerInput, bool]:
    """One question: the owner's answer, and whether they ended the rounds ("genug")."""
    io.say(f"\n{number}. {question['question']}")
    if question["candidate"]:
        io.say(f"   Vorschlag: {question['candidate']}")
    reply = io.ask("   Antwort (Enter = Vorschlag, ? = weiß nicht, genug = beenden)").strip()
    if reply.lower() == "genug":
        return AnswerInput(kind="unknown"), True
    if reply == "?" or (reply == "" and not question["candidate"]):
        return AnswerInput(kind="unknown"), False
    if reply == "":
        return AnswerInput(kind="accept"), False
    return AnswerInput(kind="text", text=reply), False


def _questions(service: BriefService, io: ConsoleIO, view: SessionView) -> Outcome | None:
    io.say(f"\nRunde {view.round + 1} von höchstens {view.max_rounds}")
    for notice in view.notices:
        io.say(f"Hinweis: {notice}")
    given: list[AnswerInput] = []
    genug = False
    for number, question in enumerate(view.questions, start=1):
        if genug:
            given.append(AnswerInput(kind="unknown"))  # enough: the rest stays unknown
            continue
        answer, genug = _answer_for(io, number, question)
        given.append(answer)
    note = io.ask("Weitere Hinweise (Enter = keine)").strip()
    service.answer(view.session_id, given, note=note, genug=genug)
    return None


# ---- a pasted finished prompt ---------------------------------------------------------------


def _offer(service: BriefService, io: ConsoleIO, view: SessionView) -> Outcome | None:
    io.say("\nFertiger Prompt erkannt:")
    _show_brief(io, view.pasted)
    if view.refusal:
        io.say(f"Er kann nicht unverändert übernommen werden: {view.refusal}")
        io.ask("Er wird deshalb verstärkt (Enter)")
        service.choose_offer(view.session_id, strengthen=True)
        return None
    choice = io.ask("[v] verstärken oder [u] unverändert übernehmen").strip().lower()
    if choice not in ("v", "u"):
        io.say("Bitte v oder u eingeben.")
        return None
    service.choose_offer(view.session_id, strengthen=choice == "v")
    return None


# ---- the decision ---------------------------------------------------------------------------


def _show_decision(io: ConsoleIO, view: SessionView) -> None:
    _show_brief(io, view.brief_text or "")
    if view.recommendation:
        tier = TIER_NAMES.get(view.recommendation["tier"], view.recommendation["tier"])
        io.say(f"Empfehlung: {tier} ({view.recommendation['response_format']})")
        io.say(view.recommendation["rationale"])
    if view.settings:
        s = view.settings
        io.say(
            f"Einstellungen: Berichtssprache: {s['report_language']}, "
            f"Format: {s['response_format']}, Vorlage: {s['template_id']}"
        )
    for notice in view.notices:
        io.say(f"Hinweis: {notice}")
    io.say(MENU_TEXT)


def _choose_tier(io: ConsoleIO, view: SessionView) -> Literal["light", "full"]:
    recommended = view.recommendation["tier"] if view.recommendation else "full"
    while True:
        prompt = f"Tiefe: [l] Lite, [f] Full (Enter = Empfehlung: {TIER_NAMES[recommended]})"
        reply = io.ask(prompt).strip().lower()
        chosen = recommended if reply == "" else _TIER_INPUT.get(reply)
        if chosen in ("light", "full"):
            return chosen  # type: ignore[return-value]  # narrowed by the membership test
        io.say("Bitte l oder f eingeben.")


def _approve(service: BriefService, io: ConsoleIO, view: SessionView) -> Outcome:
    tier = _choose_tier(io, view)
    done = service.approve(view.session_id, str(view.brief_sha256), tier)
    io.say(f"\nFreigegeben ({TIER_NAMES[tier]}).")
    io.say(f"SHA-256: {done.brief_sha256}")
    io.say(f"Archiv:  {done.archive_path}")
    io.say(f"Lauf:    {done.run_id}")
    return "approved"


def _revise(service: BriefService, io: ConsoleIO, view: SessionView) -> None:
    feedback = io.ask("Was soll geändert werden?").strip()
    if feedback:
        service.revise(view.session_id, feedback)


def _edit(service: BriefService, io: ConsoleIO, view: SessionView) -> None:
    shown = view.brief_text or ""
    edited = io.edit(shown)
    if edited is None or canonical_text(edited) == canonical_text(shown):
        io.say("Keine Änderung.")
        return
    service.edit(view.session_id, edited)


def _settings(service: BriefService, io: ConsoleIO, view: SessionView) -> None:
    current = view.settings or {}
    io.say("Vorlagen: " + ", ".join(f"{tid} ({name})" for tid, name in service.templates()))
    language = io.ask(f"Berichtssprache [{current.get('report_language', '')}]").strip()
    fmt = io.ask(
        f"Format short/structured/argumentative [{current.get('response_format', '')}]"
    ).strip()
    template = io.ask(f"Vorlage [{current.get('template_id', '')}]").strip()
    if not (language or fmt or template):
        io.say("Keine Änderung.")
        return
    service.set_settings(
        view.session_id,
        report_language=language or None,
        response_format=fmt or None,
        template_id=template or None,
    )


def _save(service: BriefService, io: ConsoleIO, view: SessionView) -> Outcome:
    parked = service.save(view.session_id)
    io.say(f"Entwurf gespeichert: {parked.draft_path}")
    io.say(f"Weiter mit: udr brief --session {view.session_id}")
    return "saved"


def _decision(service: BriefService, io: ConsoleIO, view: SessionView) -> Outcome | None:
    _show_decision(io, view)
    action = _MENU.get(io.ask("Auswahl").strip().lower())
    if action is None:
        io.say("Bitte einen der Buchstaben f, u, b, e, s, q eingeben.")
    elif action == "approve":
        return _approve(service, io, view)
    elif action == "save":
        return _save(service, io, view)
    elif action == "quit":
        _resume_hint(io, view.session_id)
        return "quit"
    else:
        {"revise": _revise, "edit": _edit, "settings": _settings}[action](service, io, view)
    return None


# ---- stopped and finished sessions ----------------------------------------------------------


def _work(service: BriefService, io: ConsoleIO, view: SessionView) -> Outcome | None:
    io.say("Die Sitzung wurde unterbrochen (zum Beispiel durch einen Modellfehler).")
    if io.ask("Erneut versuchen? [j/n]").strip().lower() not in ("j", "ja"):
        _resume_hint(io, view.session_id)
        return "quit"
    retried = service.retry(view.session_id)
    if retried.error:
        io.say(f"Fehler: {retried.error}")
    return None


def _finished(_service: BriefService, io: ConsoleIO, view: SessionView) -> Outcome:
    io.say("Diese Sitzung ist bereits freigegeben.")
    io.say(f"Archiv: {view.archive_path}")
    io.say(f"Lauf:   {view.run_id}")
    return "approved"


_HANDLERS: dict[str, Handler] = {
    "questions": _questions,
    "offer": _offer,
    "decision": _decision,
    "work": _work,
    "nothing": _finished,
}


def run_session(service: BriefService, io: ConsoleIO, session_id: str) -> Outcome:
    """Drive a session until it is approved, saved or the owner leaves it for later."""
    while True:
        view = service.get(session_id)
        try:
            outcome = _HANDLERS[view.waiting_for](service, io, view)
        except BriefError as exc:
            io.say(f"Das ging nicht: {exc}")
            continue
        if outcome is not None:
            return outcome
