"""The Phase-1 service (PRD M4): what the CLI now, and REST and MCP in M6, call.

It validates every input *before* it resumes a session's graph, because an invalid value that
reached an interrupt would stay a pending write and the session could not continue. One lock per
session keeps two calls from interleaving; "the first approval wins" follows from that.

A model error does not lose the session: the call returns a view with `waiting_for == "work"` and
`error` set, and `retry` continues from the last checkpoint. A crash (any `BaseException`) is never
caught; `recover` continues such sessions after a restart.
"""

import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ValidationError

from app.brief.errors import BriefError, InvalidInput, NotFound, StaleBrief, WrongState
from app.brief.flow import initial_state, resolve_settings
from app.brief.language import detect_interview_language
from app.brief.parse import BriefParseError, parse_brief
from app.brief.protocol import (
    AnswerInput,
    Approve,
    AskReply,
    Edit,
    OfferReply,
    Revise,
    SettingsChange,
)
from app.brief.uploads import UploadFile, UploadIngestor
from app.graphs.brief import BriefRunner, Snapshot
from app.llm.errors import LLMError
from app.pipeline.profiles import Phase1Limits, ResponseFormats
from app.store.runs import RunStore
from app.store.sessions import SessionRow, SessionStore
from app.templates import ReportTemplate, TemplateError, get_template

_M = TypeVar("_M", bound=BaseModel)  # CI runs 3.11: no PEP 695 generics
Waiting = Literal["questions", "offer", "decision", "work", "nothing"]
Tier = Literal["light", "full", "auto"]
# Hands the graph call of a session to a job runner; the job returns a model error's text or None.
Background = Callable[[str, Callable[[], str | None]], None]
_WAITING_BY_PAYLOAD: dict[str, Waiting] = {
    "questions": "questions",
    "finished_prompt": "offer",
    "decision": "decision",
}
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ServiceDeps:
    store: SessionStore
    runs: RunStore
    ingestor: UploadIngestor
    runner: BriefRunner
    limits: Phase1Limits
    templates: Mapping[str, ReportTemplate]
    formats: ResponseFormats
    drafts_dir: Path  # where `save` writes the draft of a parked session
    now: Callable[[], datetime]
    # With a runner the graph calls return at once and a job does the work (API); without one
    # they run in the caller (CLI).
    background: Background | None = None
    summarize_models: tuple[str, ...] = ()  # what `approve` accepts; empty means any


@dataclass(frozen=True)
class UploadSummary:
    name: str
    kind: str
    pages: int
    stage: str
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class SessionView:
    """Everything a caller needs to show a session and to know what it may do next."""

    session_id: str
    status: str
    interview_language: str
    round: int
    max_rounds: int
    waiting_for: Waiting  # "work": stopped between steps; `retry` or `recover` continues it
    questions: tuple[dict[str, str], ...]
    checklist: dict[str, str]
    pasted: str  # the owner's finished prompt, while it is offered
    refusal: str  # why it cannot be installed as it is
    brief_text: str | None
    brief_sha256: str | None
    recommendation: dict[str, Any] | None
    settings: dict[str, str] | None
    notices: tuple[str, ...]
    uploads: tuple[UploadSummary, ...]
    run_id: str | None
    archive_path: str | None
    draft_path: str | None  # the draft file of a parked session
    error: str | None
    busy: bool = False  # a background job works on the session; poll until it is false


def _validated(model: type[_M], value: object) -> _M:
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise InvalidInput(f"{where}: {first['msg']}" if where else str(first["msg"])) from exc


def _waiting(snapshot: Snapshot) -> Waiting:
    if snapshot.interrupt is not None:
        return _WAITING_BY_PAYLOAD[snapshot.interrupt["type"]]
    return "work" if snapshot.next_nodes or not snapshot.values else "nothing"


class BriefService:
    def __init__(self, deps: ServiceDeps) -> None:
        self._d = deps
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    # ---- plumbing -------------------------------------------------------------------------

    def _lock(self, session_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(session_id, threading.Lock())

    def _row(self, session_id: str) -> SessionRow:
        row = self._d.store.get(session_id)
        if row is None:
            raise NotFound(session_id)
        return row

    def _run_now(self, action: Callable[[], None]) -> str | None:
        """Run graph work; a model error is reported, not raised, so the session stays usable."""
        try:
            action()
        except LLMError as exc:
            return f"{type(exc).__name__}: {exc}"
        return None

    def _run(self, session_id: str, action: Callable[[], None]) -> str | None:
        """Graph work for the session, whose lock the caller holds. With a background runner a
        job does it later, under the same lock, and the caller gets `None` at once."""
        background = self._d.background
        if background is None:
            return self._run_now(action)

        def job() -> str | None:
            with self._lock(session_id):
                return self._run_now(action)

        background(session_id, job)
        return None

    def _payload(self, session_id: str, expected: str) -> dict[str, Any]:
        """The payload the session waits on, which must be of the ``expected`` kind."""
        self._row(session_id)
        waiting = self._d.runner.snapshot(session_id).interrupt
        if waiting is None or waiting["type"] != expected:
            raise WrongState(f"the session is not waiting for {expected}")
        return waiting

    def _view(self, session_id: str, error: str | None = None) -> SessionView:
        row = self._row(session_id)
        snapshot = self._d.runner.snapshot(session_id)
        values, payload = snapshot.values, snapshot.interrupt or {}
        recommendation = values.get("recommendation")
        settings = None
        if values:
            settings = resolve_settings(
                values["settings"],
                row.interview_language,
                recommendation["response_format"] if recommendation else None,
                self._d.templates,
            ).model_dump()
        return SessionView(
            session_id=session_id,
            status=row.status,
            interview_language=row.interview_language,
            round=int(values.get("round", 0)),
            max_rounds=self._d.limits.max_rounds,
            waiting_for=_waiting(snapshot),
            questions=tuple(payload.get("questions", ())),
            checklist=dict(values.get("checklist", {})),
            pasted=str(payload.get("pasted", "")),
            refusal=str(values.get("refusal", "")),
            brief_text=row.brief_text,
            brief_sha256=row.brief_sha256,
            recommendation=recommendation,
            settings=settings,
            notices=(row.digest_notice,) if row.digest_notice else (),
            uploads=tuple(
                UploadSummary(u.name, u.kind, u.pages, u.stage, u.warnings)
                for u in self._d.store.uploads(session_id)
            ),
            run_id=row.run_id,
            archive_path=row.archive_path,
            draft_path=self._draft_path(row),
            error=error,
        )

    def _draft_path(self, row: SessionRow) -> str | None:
        return str(self._d.drafts_dir / f"{row.session_id}.md") if row.status == "saved" else None

    def _discard(self, session_id: str) -> None:
        self._d.store.delete_session(session_id)
        self._d.ingestor.remove_session_files(session_id)

    # ---- the interview --------------------------------------------------------------------

    def start(self, question: str, files: Sequence[UploadFile] = ()) -> SessionView:
        """Open a session from the owner's first message. A rejected upload leaves nothing."""
        if not question.strip():
            raise InvalidInput("the question must not be empty")
        text = question  # as the owner typed it: a pasted prompt is kept byte for byte
        language = detect_interview_language(question, self._d.limits)
        session_id = self._d.store.create(language).session_id
        try:
            self._d.ingestor.accept(session_id, files)
        except BriefError:
            self._discard(session_id)
            raise
        with self._lock(session_id):
            error = self._run(
                session_id, lambda: self._d.runner.start(initial_state(session_id, text, language))
            )
        return self._view(session_id, error)

    def add_files(self, session_id: str, files: Sequence[UploadFile]) -> SessionView:
        """Add uploads while questions are open; they are read before the next assessment."""
        with self._lock(session_id):
            self._payload(session_id, "questions")
            self._d.ingestor.accept(session_id, files)
        return self._view(session_id)

    def answer(
        self,
        session_id: str,
        answers: Sequence[AnswerInput],
        *,
        note: str = "",
        genug: bool = False,
    ) -> SessionView:
        with self._lock(session_id):
            asked = self._payload(session_id, "questions")["questions"]
            reply = _validated(
                AskReply,
                {"answers": [a.model_dump() for a in answers], "note": note, "genug": genug},
            )
            if len(reply.answers) != len(asked):
                raise InvalidInput(
                    f"expected {len(asked)} answer(s) for {len(asked)} question(s), "
                    f"got {len(reply.answers)}"
                )
            error = self._run(
                session_id, lambda: self._d.runner.resume(session_id, reply.model_dump())
            )
        return self._view(session_id, error)

    def choose_offer(self, session_id: str, *, strengthen: bool) -> SessionView:
        """Answer the offer for a pasted finished prompt: strengthen it once, or install it."""
        with self._lock(session_id):
            offer = self._payload(session_id, "finished_prompt")
            if offer["refusal"] and not strengthen:
                raise InvalidInput(
                    f"the prompt cannot be installed as it is ({offer['refusal']}); "
                    "strengthen it instead"
                )
            reply = OfferReply(strengthen=strengthen)
            error = self._run(
                session_id, lambda: self._d.runner.resume(session_id, reply.model_dump())
            )
        return self._view(session_id, error)

    # ---- the decision ---------------------------------------------------------------------

    def _decide(self, session_id: str, value: dict[str, Any]) -> SessionView:
        error = self._run(session_id, lambda: self._d.runner.resume(session_id, value))
        return self._view(session_id, error)

    def revise(self, session_id: str, feedback: str) -> SessionView:
        with self._lock(session_id):
            self._payload(session_id, "decision")
            decision = _validated(Revise, {"action": "revise", "feedback": feedback})
            return self._decide(session_id, decision.model_dump())

    def edit(self, session_id: str, text: str) -> SessionView:
        """Replace the brief text by hand; it must still have a title and numbered questions."""
        with self._lock(session_id):
            self._payload(session_id, "decision")
            decision = _validated(Edit, {"action": "edit", "text": text})
            try:
                parse_brief(decision.text)
            except BriefParseError as exc:
                raise InvalidInput(str(exc)) from exc
            return self._decide(session_id, decision.model_dump())

    def set_settings(
        self,
        session_id: str,
        *,
        report_language: str | None = None,
        response_format: str | None = None,
        template_id: str | None = None,
    ) -> SessionView:
        with self._lock(session_id):
            self._payload(session_id, "decision")
            decision = _validated(
                SettingsChange,
                {
                    "action": "settings",
                    "report_language": report_language,
                    "response_format": response_format,
                    "template_id": template_id,
                },
            )
            if decision.template_id is not None:
                try:
                    get_template(self._d.templates, decision.template_id)
                except TemplateError as exc:
                    raise InvalidInput(str(exc)) from exc
            return self._decide(session_id, decision.model_dump())

    def save(self, session_id: str) -> SessionView:
        """Park the session at its decision and write the draft for `udr run --brief`."""
        with self._lock(session_id):
            self._payload(session_id, "decision")
            return self._decide(session_id, {"action": "save"})

    def approve(
        self,
        session_id: str,
        sha256: str,
        tier: Tier,
        summarize_model: str | None = None,
    ) -> SessionView:
        """Approve the brief whose hash is ``sha256`` (M4 AC1): archive it and create the run.

        `auto` applies the recommendation. A stale hash is `StaleBrief`; a session that is not at
        its decision (also one already approved) is `WrongState`."""
        if tier not in ("light", "full", "auto"):
            raise InvalidInput(f"tier must be light, full or auto, got {tier!r}")
        if not _SHA256.fullmatch(sha256):
            raise InvalidInput("the hash must be 64 lowercase hex digits")
        models = self._d.summarize_models
        if summarize_model is not None and models and summarize_model not in models:
            raise InvalidInput(f"summarize_model must be one of {', '.join(models)}")
        with self._lock(session_id):
            decision = self._payload(session_id, "decision")
            if self._row(session_id).brief_sha256 != sha256:
                raise StaleBrief("the hash does not belong to the current brief")
            chosen = decision["recommendation"]["tier"] if tier == "auto" else tier
            approval = Approve(
                action="approve",
                sha256=sha256,
                tier=chosen,
                summarize_model=summarize_model,
                at=self._d.now(),
            )
            return self._decide(session_id, approval.model_dump(mode="json"))

    # ---- reading and continuing -----------------------------------------------------------

    def get(self, session_id: str) -> SessionView:
        return self._view(session_id)

    def templates(self) -> list[tuple[str, str]]:
        """(id, name) of every report template a session can choose."""
        return [(t.id, t.name) for t in self._d.templates.values()]

    def list_sessions(self) -> list[SessionRow]:
        return self._d.store.list_sessions()

    def retry(self, session_id: str) -> SessionView:
        """Continue a session that stopped on a model error from its last checkpoint."""
        with self._lock(session_id):
            self._row(session_id)
            error = None
            if _waiting(self._d.runner.snapshot(session_id)) == "work":
                error = self._run(session_id, lambda: self._d.runner.proceed(session_id))
        return self._view(session_id, error)

    def recover(self) -> list[str]:
        """After a restart: remove sessions that never got going, continue those that stopped
        between steps, and clean up files a crashed upload left behind. Returns the ids touched."""
        touched: list[str] = []
        for row in self._d.store.list_sessions():
            with self._lock(row.session_id):
                if row.status == "approved":
                    continue
                snapshot = self._d.runner.snapshot(row.session_id)
                if not snapshot.values:  # the first call died before the graph started
                    self._discard(row.session_id)
                    touched.append(row.session_id)
                    continue
                self._d.ingestor.cleanup_orphans(row.session_id)
                if _waiting(snapshot) == "work":
                    self._run_now(lambda sid=row.session_id: self._d.runner.proceed(sid))
                    touched.append(row.session_id)
        return touched
