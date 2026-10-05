"""The interview of Phase 1 (PRD M4): the `reason` model assesses, drafts, revises and recommends;
code normalises what comes back so the loop does not depend on the model keeping its promises.

`assess` runs without thinking (a round should take seconds); draft, revise, strengthen and the
tier recommendation think, because they run once per decision and quality matters there.
"""

from collections.abc import Collection, Sequence
from typing import TypeVar

from pydantic import BaseModel

from app.brief.labels import language_name
from app.brief.models import CHECKLIST_ORDER, Answer, Checklist
from app.brief.schemas import (
    Assessment,
    BriefDraft,
    ChecklistEntry,
    Question,
    TierRecommendation,
)
from app.llm.service import LLMService
from app.llm.types import Message, Role
from app.pipeline.profiles import Phase1Limits, ResponseFormats
from app.prompts.brief import (
    ASSESS_DIGEST,
    ASSESS_SYSTEM,
    ASSESS_TRANSCRIPT,
    ASSESS_USER,
    DRAFT_SYSTEM,
    DRAFT_USER,
    REVISE_SYSTEM,
    REVISE_USER,
    STRENGTHEN_SYSTEM,
    STRENGTHEN_USER,
    TIER_SYSTEM,
    TIER_USER,
)
from app.prompts.untrusted import fence_untrusted
from app.text import normalize_for_match

_M = TypeVar("_M", bound=BaseModel)  # CI runs 3.11: no PEP 695 generics
UNKNOWN_ANSWER = "(the owner does not know)"
_FORMAT_HINTS = {
    "short": "a direct answer",
    "structured": "scannable, breadth-first coverage",
    "argumentative": "a defended thesis with evidence chains",
}


def render_transcript(answers: Sequence[Answer]) -> str:
    """The questions asked and what the owner answered, as the prompts show them."""
    return "\n\n".join(f"Q: {a.question}\nA: {a.value or UNKNOWN_ANSWER}" for a in answers)


def checklist_of(assessment: Assessment) -> Checklist:
    return {entry.item: entry.status for entry in assessment.checklist}


def normalize_assessment(
    raw: Assessment, limits: Phase1Limits, *, round_no: int, asked: Collection[str] = ()
) -> Assessment:
    """The model's assessment made safe to act on.

    One entry per checklist item (first one wins, missing ones are `missing`); depth is never
    `missing` because the owner chooses the tier explicitly; questions only about `missing` items
    (a clear item needs none, an assumed one is listed in the brief), none twice, none about an
    item in ``asked`` (the model re-opens answered items), in checklist order; at most the
    configured number of questions; only round 1 may report a finished prompt."""
    first: dict[str, ChecklistEntry] = {}
    for entry in raw.checklist:
        first.setdefault(entry.item, entry)
    entries = [
        first.get(item) or ChecklistEntry(item=item, status="missing") for item in CHECKLIST_ORDER
    ]
    entries = [
        e.model_copy(update={"status": "assumed"})
        if e.item == "depth" and e.status == "missing"
        else e
        for e in entries
    ]
    status = {e.item: e.status for e in entries}
    seen: set[str] = set()
    kept: list[Question] = []
    for q in raw.questions:
        key = normalize_for_match(q.question)
        if status[q.item] == "missing" and q.item not in asked and key not in seen:
            seen.add(key)
            kept.append(q)
    kept.sort(key=lambda q: CHECKLIST_ORDER.index(q.item))
    return Assessment(
        checklist=entries,
        questions=kept[: limits.max_questions_per_round],
        finished_prompt=raw.finished_prompt and round_no == 1,
    )


def _digest_block(digest: str) -> str:
    return ASSESS_DIGEST.format(fenced=fence_untrusted("uploaded files", digest)) if digest else ""


class Interviewer:
    def __init__(self, llm: LLMService, limits: Phase1Limits, formats: ResponseFormats) -> None:
        self._llm = llm
        self._limits = limits
        self._formats = formats

    def _ask(self, system: str, user: str, schema: type[_M], *, think: bool) -> _M:
        messages: list[Message] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        return self._llm.structured(Role.REASON, messages, schema, think=think)

    def assess(
        self,
        *,
        question: str,
        language: str,
        answers: Sequence[Answer],
        digest: str,
        round_no: int,
    ) -> Assessment:
        transcript = render_transcript(answers)
        user = ASSESS_USER.format(
            language=language_name(language, "en"),
            round=round_no,
            max_rounds=self._limits.max_rounds,
            max_questions=self._limits.max_questions_per_round,
            question=question,
            transcript_block=ASSESS_TRANSCRIPT.format(transcript=transcript) if transcript else "",
            digest_block=_digest_block(digest),
        )
        raw = self._ask(ASSESS_SYSTEM, user, Assessment, think=False)
        asked = {a.item for a in answers}
        return normalize_assessment(raw, self._limits, round_no=round_no, asked=asked)

    def draft(
        self,
        *,
        question: str,
        language: str,
        answers: Sequence[Answer],
        checklist: Checklist,
        digest: str,
    ) -> BriefDraft:
        status = "\n".join(
            f"{item}: {checklist[item]}" for item in CHECKLIST_ORDER if item in checklist
        )
        user = DRAFT_USER.format(
            language=language_name(language, "en"),
            question=question,
            transcript=render_transcript(answers) or "(no questions were asked)",
            checklist=status,
            digest_block=_digest_block(digest),
        )
        return self._ask(DRAFT_SYSTEM, user, BriefDraft, think=True)

    def revise(self, *, brief: str, feedback: str, language: str) -> BriefDraft:
        user = REVISE_USER.format(
            language=language_name(language, "en"), brief=brief, feedback=feedback
        )
        return self._ask(REVISE_SYSTEM, user, BriefDraft, think=True)

    def strengthen(self, *, pasted: str, language: str) -> BriefDraft:
        user = STRENGTHEN_USER.format(language=language_name(language, "en"), pasted=pasted)
        return self._ask(STRENGTHEN_SYSTEM, user, BriefDraft, think=True)

    def recommend_tier(self, *, brief: str, language: str) -> TierRecommendation:
        formats = "\n".join(
            f"- {name}: {fmt.words[0]} to {fmt.words[1]} words, {_FORMAT_HINTS[name]}"
            for name, fmt in (
                ("short", self._formats.short),
                ("structured", self._formats.structured),
                ("argumentative", self._formats.argumentative),
            )
        )
        user = TIER_USER.format(language=language_name(language, "en"), brief=brief)
        return self._ask(TIER_SYSTEM.format(formats=formats), user, TierRecommendation, think=True)
