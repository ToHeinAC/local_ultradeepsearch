"""Turn a stored source into a summary and verbatim-checked claims (PRD M3 steps 5 and 6).

The `extract` model reads the source in chunks and proposes claims; code decides what survives.
A claim is kept only if its `quoted_support` occurs word for word in the stored text, so nothing
the model invents can enter the corpus. The source text itself is never rewritten.
"""

import re
from dataclasses import dataclass

from app.events import EventSink
from app.llm.errors import LLMError
from app.llm.service import LLMService
from app.llm.types import Message, Role
from app.pipeline.chunking import LengthClass, length_class, split_paragraph_chunks, word_count
from app.pipeline.schemas import ChunkExtraction, ClaimDraft, MergedSummary
from app.prompts.notes import (
    EXTRACT_SYSTEM,
    EXTRACT_USER,
    SUMMARY_MERGE_SYSTEM,
    SUMMARY_MERGE_USER,
)
from app.prompts.untrusted import fence_untrusted
from app.store.models import NewClaim, Note
from app.text import contains_quote, normalize_for_match, strip_wrapping_quotes

CHUNK_CHARS = 12_000  # about 4000 tokens; the extract context leaves 6144 for the prompt
CHUNK_CLAIM_LIMIT = 8
MAX_QUOTE_CHARS = 500
LEAD_CHARS = 400
CLAIM_CAPS: dict[LengthClass, int] = {"short": 8, "medium": 15, "long": 25}
SUMMARY_HINTS: dict[LengthClass, str] = {
    "short": "1-2 sentences",
    "medium": "1-2 paragraphs",
    "long": "3-6 paragraphs",
}
PART_HINT = "2-4 sentences (this is one part of a longer document)"
CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Focus:
    """What the run is about, parsed deterministically from the brief (PRD AD6)."""

    title: str
    questions: tuple[str, ...]


@dataclass(frozen=True)
class Extraction:
    summary: str
    claims: tuple[NewClaim, ...]
    dropped: int  # claims the model proposed that failed verification
    failed: bool  # no chunk could be processed; the summary is only a lead


def lead(text: str, limit: int = LEAD_CHARS) -> str:
    """The first whole sentences of ``text`` up to ``limit`` characters (a word-boundary cut if
    the first sentence alone is longer)."""
    body = " ".join(text.split())
    out = ""
    for sentence in _SENTENCE_END.split(body):
        candidate = f"{out} {sentence}" if out else sentence
        if len(candidate) > limit:
            break
        out = candidate
    if out or not body:
        return out
    return body[: limit + 1].rsplit(" ", 1)[0].strip() or body[:limit]


def _verbatim(draft: ClaimDraft, normalized_body: str) -> bool:
    quote = strip_wrapping_quotes(draft.quoted_support)
    return len(quote) <= MAX_QUOTE_CHARS and contains_quote(normalized_body, quote)


def _to_claim(draft: ClaimDraft) -> NewClaim:
    return NewClaim(
        claim=draft.claim.strip(),
        stance=draft.stance,
        stance_target=draft.stance_target.strip(),
        evidence_type=draft.evidence_type,
        scope_conditions=draft.scope_conditions.strip(),
        quoted_support=strip_wrapping_quotes(draft.quoted_support),
        numbers=tuple(draft.numbers),
        entities=tuple(draft.entities),
        time_period=draft.time_period,
        region=draft.region,
        confidence=draft.confidence,
    )


def _select(claims: list[NewClaim], cap: int) -> list[NewClaim]:
    """Drop repeated claims, then keep the ``cap`` best (confidence, then having numbers) in
    document order."""
    unique: dict[str, NewClaim] = {}
    for claim in claims:
        unique.setdefault(normalize_for_match(claim.claim), claim)
    items = list(unique.values())
    if len(items) <= cap:
        return items
    ranked = sorted(
        range(len(items)),
        key=lambda i: (CONFIDENCE_RANK.get(items[i].confidence, 3), not items[i].numbers, i),
    )
    return [items[i] for i in sorted(ranked[:cap])]


def _questions(focus: Focus) -> str:
    if not focus.questions:
        return "(none)"
    return "\n".join(f"{i}. {q}" for i, q in enumerate(focus.questions, start=1))


class NoteExtractor:
    def __init__(self, llm: LLMService, events: EventSink) -> None:
        self._llm = llm
        self._events = events

    def extract(self, note: Note, focus: Focus) -> Extraction:
        chunks = split_paragraph_chunks(note.body, CHUNK_CHARS)
        if not chunks:
            return Extraction("", (), 0, True)
        label = length_class(word_count(note.body))
        results = [
            self._chunk(note, focus, chunk, index, len(chunks), label)
            for index, chunk in enumerate(chunks, start=1)
        ]
        good = [r for r in results if r is not None]
        if not good:
            return Extraction(lead(note.body), (), 0, True)
        normalized = normalize_for_match(note.body)
        verified: list[NewClaim] = []
        dropped = 0
        for result in good:
            for draft in result.claims:
                if _verbatim(draft, normalized):
                    verified.append(_to_claim(draft))
                else:
                    dropped += 1
        claims = _select(verified, CLAIM_CAPS[label])
        summary = self._summary(note, focus, [r.summary for r in good], label)
        return Extraction(summary, tuple(claims), dropped, False)

    def _chunk(
        self,
        note: Note,
        focus: Focus,
        chunk: str,
        index: int,
        total: int,
        label: LengthClass,
    ) -> ChunkExtraction | None:
        user = EXTRACT_USER.format(
            title=focus.title,
            questions=_questions(focus),
            index=index,
            total=total,
            length_class=label,
            summary_hint=SUMMARY_HINTS[label] if total == 1 else PART_HINT,
            claim_limit=CHUNK_CLAIM_LIMIT,
            source=fence_untrusted(note.final_url or note.url, chunk),
        )
        messages: tuple[Message, ...] = (
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": user},
        )
        try:
            return self._llm.structured(Role.EXTRACT, messages, ChunkExtraction)
        except LLMError as exc:
            self._events.emit(
                "extract_chunk_failed",
                level="warning",
                note_id=note.note_id,
                chunk=index,
                error=type(exc).__name__,
            )
            return None

    def _summary(self, note: Note, focus: Focus, parts: list[str], label: LengthClass) -> str:
        texts = [p.strip() for p in parts if p.strip()]
        if not texts:
            return lead(note.body)
        if len(texts) == 1:
            return texts[0]
        listing = "\n".join(f"{i}. {t}" for i, t in enumerate(texts, start=1))
        user = SUMMARY_MERGE_USER.format(
            title=focus.title,
            summary_hint=SUMMARY_HINTS[label],
            parts=fence_untrusted(note.final_url or note.url, listing),
        )
        messages: tuple[Message, ...] = (
            {"role": "system", "content": SUMMARY_MERGE_SYSTEM},
            {"role": "user", "content": user},
        )
        try:
            merged = self._llm.structured(Role.SUMMARIZE, messages, MergedSummary).summary.strip()
        except LLMError as exc:
            self._events.emit(
                "summary_merge_failed",
                level="warning",
                note_id=note.note_id,
                error=type(exc).__name__,
            )
            merged = ""
        return merged or " ".join(texts)
