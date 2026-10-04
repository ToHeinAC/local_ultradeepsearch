"""The `brief` graph (PRD M4, AD1, AD8, AD10): Phase 1 from the owner's first message to an
approved brief. The thread id is the session id and the checkpointer is SQLite, so a session
survives a restart and continues at its pending interrupt.

    ingest_uploads -> assess -> ask (interrupt) -> ingest_uploads ...   (the question rounds)
    ingest_uploads -> draft_brief          (after the last round: files added in it are read too)
                          \\-> offer (interrupt) -> strengthen_brief | install_verbatim
    draft_brief -> recommend -> decide (interrupt) -> revise | edit | settings | save -> decide
                                                  \\-> finalize -> END

Rules that keep it resumable:
- LangGraph runs an interrupted node again from its start when the interrupt is answered, so the
  nodes with an `interrupt` (`ask`, `offer`, `decide`) do no model work and write nothing before it.
- Every other node is idempotent: it overwrites what it wrote, or finds it already done.
- The database is the authority for the brief text and its hash; `finalize` re-checks the hash
  and a second run of it returns the same archive file and the same run.
- Values handed to `interrupt` and returned from it are plain JSON, validated by the service
  before a resume (see `app.brief.protocol`).
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.types import Command, interrupt

from app.brief.archive import archive_brief, write_draft
from app.brief.errors import NotFound, StaleBrief
from app.brief.flow import build_context, final_checklist, turn_answers
from app.brief.interview import Interviewer, checklist_of
from app.brief.models import Answer
from app.brief.parse import BriefParseError
from app.brief.protocol import (
    Approve,
    AskReply,
    Edit,
    OfferReply,
    Revise,
    SettingsChange,
    parse_decision,
)
from app.brief.render import (
    canonical_text,
    extract_register,
    render_brief,
    render_verbatim,
    replace_output,
)
from app.brief.schemas import BriefDraft
from app.brief.uploads import UploadIngestor
from app.pipeline.profiles import Phase1Limits, ResponseFormats
from app.store.db import connect
from app.store.runs import RunStore
from app.store.sessions import SessionRow, SessionStore
from app.templates import ReportTemplate


class BriefState(TypedDict):
    """Everything the graph remembers; plain JSON, because checkpoints store it as it is."""

    session_id: str
    question: str  # the owner's first message
    language: str
    round: int  # question rounds the owner has answered
    answers: list[dict[str, Any]]
    checklist: dict[str, str]
    pending: list[dict[str, str]]  # the questions of the open round
    finished_prompt: bool
    genug: bool
    strengthen: bool
    refusal: str  # why a pasted prompt could not be installed as it is
    digest: str
    digest_notice: str
    draft: dict[str, Any] | None
    register: str
    verbatim: bool
    brief_text: str
    brief_sha: str
    recommendation: dict[str, Any] | None
    settings: dict[str, str | None]  # only what the owner chose
    decision: dict[str, Any] | None
    result: dict[str, str] | None


@dataclass(frozen=True)
class BriefDeps:
    store: SessionStore
    runs: RunStore
    ingestor: UploadIngestor
    interviewer: Interviewer
    templates: dict[str, ReportTemplate]
    formats: ResponseFormats
    limits: Phase1Limits
    briefs_dir: Any  # pathlib.Path: where approved briefs are archived
    drafts_dir: Any  # pathlib.Path: where parked drafts are written


Update = dict[str, Any]


class _Nodes:
    def __init__(self, deps: BriefDeps) -> None:
        self._d = deps

    # ---- helpers --------------------------------------------------------------------------

    def _session(self, state: BriefState) -> SessionRow:
        session = self._d.store.get(state["session_id"])
        if session is None:
            raise NotFound(state["session_id"])
        return session

    def _answers(self, state: BriefState) -> list[Answer]:
        return [Answer.model_validate(a) for a in state["answers"]]

    def _render(self, state: dict[str, Any]) -> str:
        ctx = build_context(state, self._d.templates, self._d.formats)
        if state["verbatim"]:
            return render_verbatim(state["question"], ctx)
        return render_brief(BriefDraft.model_validate(state["draft"]), ctx)

    def _draft_update(self, state: BriefState, draft: BriefDraft) -> Update:
        merged = {**state, "draft": draft.model_dump(), "verbatim": False}
        return {
            "draft": draft.model_dump(),
            "verbatim": False,
            "register": draft.tone,
            "brief_text": self._render(merged),
        }

    # ---- the question rounds --------------------------------------------------------------

    def ingest_uploads(self, state: BriefState) -> Update:
        self._d.ingestor.process(state["session_id"], state["question"], state["language"])
        session = self._session(state)
        return {"digest": session.upload_digest, "digest_notice": session.digest_notice}

    def assess(self, state: BriefState) -> Update:
        result = self._d.interviewer.assess(
            question=state["question"],
            language=state["language"],
            answers=self._answers(state),
            digest=state["digest"],
            round_no=state["round"] + 1,
        )
        return {
            "checklist": dict(checklist_of(result)),
            "pending": [q.model_dump() for q in result.questions],
            "finished_prompt": result.finished_prompt,
        }

    def ask(self, state: BriefState) -> Update:
        round_no = state["round"] + 1
        value = interrupt(
            {
                "type": "questions",
                "round": round_no,
                "questions": state["pending"],
                "digest_notice": state["digest_notice"],
            }
        )
        given = turn_answers(state["pending"], AskReply.model_validate(value), round_no=round_no)
        return {
            "answers": [*state["answers"], *(a.model_dump() for a in given)],
            "round": round_no,
            "genug": AskReply.model_validate(value).genug,
            "pending": [],
        }

    # ---- a pasted finished prompt ---------------------------------------------------------

    def offer(self, state: BriefState) -> Update:
        value = interrupt(
            {"type": "finished_prompt", "pasted": state["question"], "refusal": state["refusal"]}
        )
        return {"strengthen": OfferReply.model_validate(value).strengthen, "finished_prompt": False}

    def strengthen_brief(self, state: BriefState) -> Update:
        draft = self._d.interviewer.strengthen(pasted=state["question"], language=state["language"])
        return {**self._draft_update(state, draft), "refusal": ""}

    def install_verbatim(self, state: BriefState) -> Update:
        ctx = build_context(state, self._d.templates, self._d.formats)
        try:
            text = render_verbatim(state["question"], ctx)
        except BriefParseError as exc:
            return {"refusal": str(exc)}
        return {"verbatim": True, "draft": None, "register": "", "refusal": "", "brief_text": text}

    # ---- the brief and the decision -------------------------------------------------------

    def draft_brief(self, state: BriefState) -> Update:
        answers = self._answers(state)
        draft = self._d.interviewer.draft(
            question=state["question"],
            language=state["language"],
            answers=answers,
            checklist=final_checklist(state["checklist"], answers),  # type: ignore[arg-type]
            digest=state["digest"],
        )
        return self._draft_update(state, draft)

    def recommend(self, state: BriefState) -> Update:
        rec = self._d.interviewer.recommend_tier(
            brief=state["brief_text"], language=state["language"]
        )
        text = self._render({**state, "recommendation": rec.model_dump()})
        sha = self._d.store.set_brief(state["session_id"], text)
        if self._session(state).status == "interviewing":
            self._d.store.set_status(state["session_id"], "awaiting_decision")
        return {"recommendation": rec.model_dump(), "brief_text": text, "brief_sha": sha}

    def decide(self, state: BriefState) -> Update:
        session = self._session(state)
        ctx = build_context(state, self._d.templates, self._d.formats)
        value = interrupt(
            {
                "type": "decision",
                "brief": state["brief_text"],
                "sha256": state["brief_sha"],
                "recommendation": state["recommendation"],
                "settings": ctx.settings.model_dump(),
                "status": session.status,
                "notices": [state["digest_notice"]] if state["digest_notice"] else [],
            }
        )
        if session.status == "saved":  # any action continues a parked session
            self._d.store.set_status(state["session_id"], "awaiting_decision")
        return {"decision": value}

    def revise_brief(self, state: BriefState) -> Update:
        decision = parse_decision(state["decision"])
        assert isinstance(decision, Revise)
        draft = self._d.interviewer.revise(
            brief=state["brief_text"], feedback=decision.feedback, language=state["language"]
        )
        update = self._draft_update(state, draft)
        update["brief_sha"] = self._d.store.set_brief(state["session_id"], update["brief_text"])
        return update

    def apply_edit(self, state: BriefState) -> Update:
        decision = parse_decision(state["decision"])
        assert isinstance(decision, Edit)
        text = canonical_text(decision.text)
        sha = self._d.store.set_brief(state["session_id"], text)
        return {"brief_text": text, "brief_sha": sha, "register": extract_register(text)}

    def apply_settings(self, state: BriefState) -> Update:
        decision = parse_decision(state["decision"])
        assert isinstance(decision, SettingsChange)
        changes = decision.model_dump(exclude={"action"}, exclude_none=True)
        chosen = {**state["settings"], **changes}
        ctx = build_context({**state, "settings": chosen}, self._d.templates, self._d.formats)
        text = replace_output(state["brief_text"], ctx, state["register"])
        shown = ctx.settings
        self._d.store.set_settings(
            state["session_id"], shown.report_language, shown.response_format, shown.template_id
        )
        return {
            "settings": chosen,
            "brief_text": text,
            "brief_sha": self._d.store.set_brief(state["session_id"], text),
        }

    def park(self, state: BriefState) -> Update:
        write_draft(self._d.drafts_dir, state["session_id"], state["brief_text"])
        if self._session(state).status == "awaiting_decision":
            self._d.store.set_status(state["session_id"], "saved")
        return {"decision": None}

    def finalize(self, state: BriefState) -> Update:
        decision = parse_decision(state["decision"])
        assert isinstance(decision, Approve)
        session = self._session(state)
        if session.brief_text is None or session.brief_sha256 != decision.sha256:
            raise StaleBrief("the hash does not belong to the current brief")
        path = archive_brief(self._d.briefs_dir, session.brief_text, decision.at)
        run = self._d.runs.approve(
            state["session_id"],
            sha256=decision.sha256,
            brief_path=str(path),
            tier=decision.tier,
            summarize_model=decision.summarize_model,
        )
        shown = build_context(cast("dict[str, Any]", state), self._d.templates, self._d.formats)
        self._d.runs.set_settings(
            run.run_id,
            json.dumps(
                {
                    **shown.settings.model_dump(),
                    "interview_language": state["language"],
                    "tier": decision.tier,
                    "summarize_model": decision.summarize_model,
                }
            ),
        )
        return {
            "result": {"run_id": run.run_id, "archive_path": str(path), "sha256": decision.sha256}
        }


def _route_assess(state: BriefState) -> str:
    if state["finished_prompt"]:
        return "offer"
    return "ask" if state["pending"] else "draft_brief"


def _route_offer(state: BriefState) -> str:
    return "strengthen_brief" if state["strengthen"] else "install_verbatim"


def _route_after_ingest(deps: BriefDeps, state: BriefState) -> str:
    done = state["genug"] or state["round"] >= deps.limits.max_rounds
    return "draft_brief" if done else "assess"


def _route_verbatim(state: BriefState) -> str:
    return "offer" if state["refusal"] else "recommend"


_DECISION_NODES = {
    "approve": "finalize",
    "revise": "revise_brief",
    "edit": "apply_edit",
    "settings": "apply_settings",
    "save": "park",
}


def _route_decide(state: BriefState) -> str:
    return _DECISION_NODES[parse_decision(state["decision"]).action]


def build_brief_graph(deps: BriefDeps, checkpointer: BaseCheckpointSaver[Any]) -> Any:
    """The compiled brief graph, checkpointed by ``checkpointer``."""
    nodes = _Nodes(deps)
    # LangGraph's builder signatures are partly untyped; the nodes and routers around it are typed.
    builder = cast("Any", StateGraph(BriefState))

    def after_ingest(state: BriefState) -> str:
        return _route_after_ingest(deps, state)

    for name in (
        "ingest_uploads",
        "assess",
        "ask",
        "offer",
        "strengthen_brief",
        "install_verbatim",
        "draft_brief",
        "recommend",
        "decide",
        "revise_brief",
        "apply_edit",
        "apply_settings",
        "park",
        "finalize",
    ):
        builder.add_node(name, getattr(nodes, name))
    builder.add_edge(START, "ingest_uploads")
    builder.add_conditional_edges("ingest_uploads", after_ingest, ["assess", "draft_brief"])
    builder.add_conditional_edges("assess", _route_assess, ["offer", "ask", "draft_brief"])
    builder.add_edge("ask", "ingest_uploads")
    builder.add_conditional_edges("offer", _route_offer, ["strengthen_brief", "install_verbatim"])
    builder.add_conditional_edges("install_verbatim", _route_verbatim, ["offer", "recommend"])
    builder.add_edge("strengthen_brief", "recommend")
    builder.add_edge("draft_brief", "recommend")
    builder.add_edge("recommend", "decide")
    builder.add_conditional_edges("decide", _route_decide, list(_DECISION_NODES.values()))
    for name in ("revise_brief", "apply_edit", "apply_settings", "park"):
        builder.add_edge(name, "decide")
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer)


@dataclass(frozen=True)
class Snapshot:
    """Where a session's graph stands, without LangGraph types."""

    values: dict[str, Any]  # empty if the graph never started
    interrupt: dict[str, Any] | None  # the payload it waits on, if it waits
    next_nodes: tuple[str, ...]  # what would run next; empty when finished


class BriefRunner:
    """The only door to a compiled brief graph for code outside this package."""

    def __init__(self, graph: Any) -> None:
        self._graph = graph

    @staticmethod
    def _config(session_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": session_id}}

    def _invoke(self, value: Any, session_id: str) -> None:
        # LangGraph writes checkpoints in the background by default ("async"), so a SIGKILL could
        # lose the latest steps. "sync" persists each step before the next one starts (AD10).
        self._graph.invoke(value, self._config(session_id), durability="sync")

    def start(self, state: dict[str, Any]) -> None:
        self._invoke(state, state["session_id"])

    def resume(self, session_id: str, value: dict[str, Any]) -> None:
        """Answer the pending interrupt with ``value``."""
        self._invoke(Command(resume=value), session_id)

    def proceed(self, session_id: str) -> None:
        """Continue from the last checkpoint (after a crash or a model error)."""
        self._invoke(None, session_id)

    def snapshot(self, session_id: str) -> Snapshot:
        state = self._graph.get_state(self._config(session_id))
        waiting = [i.value for task in state.tasks for i in task.interrupts]
        return Snapshot(dict(state.values), waiting[0] if waiting else None, tuple(state.next))


def open_checkpointer(path: Path) -> SqliteSaver:
    """The SQLite checkpointer of the graphs (`data/checkpoints.sqlite`): WAL, full fsync."""
    return SqliteSaver(connect(path))
