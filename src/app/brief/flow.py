"""The plain-Python parts of the brief flow, so the graph module only wires LangGraph around them:
the initial state, settings resolution, checklist helpers and the brief context."""

from collections.abc import Mapping, Sequence
from typing import Any

from app.brief.models import (
    CHECKLIST_ORDER,
    CONTENT_ITEMS,
    Answer,
    Checklist,
    ChecklistItem,
    SessionSettings,
)
from app.brief.protocol import AskReply
from app.brief.render import BriefContext
from app.pipeline.profiles import ResponseFormats
from app.templates import ReportTemplate, get_template

NOTE_QUESTION = "(additional information from the owner)"
DEFAULT_TEMPLATE = "auto"


def initial_state(session_id: str, question: str, language: str) -> dict[str, Any]:
    """The state a brief graph starts with. Plain JSON data: checkpoints store it as it is."""
    return {
        "session_id": session_id,
        "question": question,
        "language": language,
        "round": 0,
        "answers": [],
        "checklist": {},
        "pending": [],
        "finished_prompt": False,
        "genug": False,
        "strengthen": False,
        "refusal": "",
        "digest": "",
        "digest_notice": "",
        "draft": None,
        "register": "",
        "verbatim": False,
        "brief_text": "",
        "brief_sha": "",
        "recommendation": None,
        "settings": {"report_language": None, "response_format": None, "template_id": None},
        "decision": None,
        "result": None,
    }


def final_checklist(checklist: Checklist, answers: Sequence[Answer]) -> Checklist:
    """The checklist with every item the owner answered marked clear. "Don't know" answers and
    free notes do not clear anything."""
    result = dict(checklist)
    for answer in answers:
        if answer.value.strip() and answer.question != NOTE_QUESTION:  # "don't know" has no value
            result[answer.item] = "clear"
    return result


def missing_items(checklist: Checklist) -> tuple[ChecklistItem, ...]:
    """Content items still unknown, in checklist order. Output and depth have defaults instead."""
    return tuple(
        item
        for item in CHECKLIST_ORDER
        if item in CONTENT_ITEMS and checklist.get(item) == "missing"
    )


def resolve_settings(
    chosen: Mapping[str, str | None],
    interview_language: str,
    recommended_format: str | None,
    templates: Mapping[str, ReportTemplate],
) -> SessionSettings:
    """What the owner chose, else the defaults: the interview language, template `auto`, and the
    recommended response format (or the template's own default)."""
    template_id = chosen.get("template_id") or DEFAULT_TEMPLATE
    response_format = (
        chosen.get("response_format")
        or recommended_format
        or get_template(templates, template_id).default_response_format
    )
    return SessionSettings(
        report_language=chosen.get("report_language") or interview_language,
        response_format=response_format,  # type: ignore[arg-type]  # validated by SessionSettings
        template_id=template_id,
    )


def build_context(
    state: Mapping[str, Any], templates: Mapping[str, ReportTemplate], formats: ResponseFormats
) -> BriefContext:
    answers = [Answer.model_validate(a) for a in state["answers"]]
    recommendation = state["recommendation"]
    settings = resolve_settings(
        state["settings"],
        state["language"],
        recommendation["response_format"] if recommendation else None,
        templates,
    )
    return BriefContext(
        settings=settings,
        template=get_template(templates, settings.template_id),
        fmt=formats.named(settings.response_format),
        rounds=state["round"],
        missing=missing_items(final_checklist(state["checklist"], answers)),
        unknown_questions=tuple(a.question for a in answers if a.kind == "unknown"),
        upload_digest=state["digest"],
        interview_language=state["language"],
    )


def turn_answers(
    pending: Sequence[Mapping[str, str]], reply: AskReply, *, round_no: int
) -> list[Answer]:
    """Pair the owner's replies with the questions that were asked; a free note becomes one more
    answer. Raises `ValueError` if the number of replies does not match the questions."""
    if len(reply.answers) != len(pending):
        raise ValueError(
            f"expected {len(pending)} answer(s) for {len(pending)} question(s), "
            f"got {len(reply.answers)}"
        )
    answers = [
        Answer(
            round=round_no,
            item=q["item"],  # type: ignore[arg-type]  # written by the assessment
            question=q["question"],
            candidate=q["candidate"],
            kind=given.kind,
            text=given.text.strip(),
        )
        for q, given in zip(pending, reply.answers, strict=True)
    ]
    if reply.note.strip():
        answers.append(
            Answer(
                round=round_no,
                item="context",
                question=NOTE_QUESTION,
                candidate="",
                kind="text",
                text=reply.note.strip(),
            )
        )
    return answers
