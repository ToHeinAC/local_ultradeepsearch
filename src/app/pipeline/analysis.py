"""Analysis notes for long sources (PRD M3 step 7): map-reduce on the `summarize` role.

A source too long for one prompt is read in parts; the partial readings are merged in rounds until
they fit one prompt, and a final call writes the analysis. Nothing is truncated: if a call fails
the analysis is skipped, never shortened. Quotes in the result are checked word for word.
"""

from dataclasses import dataclass
from typing import TypeVar

from app.events import EventSink
from app.llm.errors import LLMError
from app.llm.service import LLMService
from app.llm.types import Message, Role
from app.pipeline.chunking import split_paragraph_chunks
from app.pipeline.extraction import Focus
from app.pipeline.profiles import Profile
from app.pipeline.schemas import PartialAnalysis, SourceAnalysis
from app.prompts.notes import (
    ANALYSIS_FINAL_SYSTEM,
    ANALYSIS_FINAL_USER,
    ANALYSIS_MAP_SYSTEM,
    ANALYSIS_MAP_USER,
    ANALYSIS_REDUCE_SYSTEM,
    ANALYSIS_REDUCE_USER,
)
from app.prompts.untrusted import fence_untrusted
from app.store.models import Note
from app.text import contains_quote, normalize_for_match, strip_wrapping_quotes

_T = TypeVar("_T", PartialAnalysis, SourceAnalysis)  # CI runs 3.11: no PEP 695 generics

MAP_CHUNK_CHARS = 28_000  # about 9300 tokens; the summarize context leaves 12 288 for the prompt
REDUCE_BATCH_CHARS = 28_000
MAX_QUOTES = 10
MAX_ROUNDS = 6
NONE_STATED = "None stated."


def needs_analysis(note: Note, profile: Profile, existing: int) -> bool:
    """Long originals get an analysis until the profile's cap is reached."""
    return (
        note.kind == "source"
        and note.word_count >= profile.long_source_words
        and note.derivative_of is None
        and not note.extract_failed
        and existing < profile.source_analysis_cap
    )


class _ReduceFailed(Exception):
    """A merge or the final call failed; the analysis is skipped."""


def _render_partial(index: int, partial: PartialAnalysis) -> str:
    lines = [f"Part {index}:", "Key points:", *(f"- {p}" for p in partial.key_points)]
    if partial.numbers:
        lines.append("Numbers: " + "; ".join(partial.numbers))
    if partial.quotes:
        lines += ["Quotes:", *(f'- "{q}"' for q in partial.quotes)]
    return "\n".join(lines)


def _batches(texts: list[str], budget: int) -> list[list[int]]:
    """Consecutive groups of indices whose joined text stays within ``budget``."""
    groups: list[list[int]] = []
    size = 0
    for index, text in enumerate(texts):
        if groups and size + len(text) + 2 <= budget:
            groups[-1].append(index)
            size += len(text) + 2
        else:
            groups.append([index])
            size = len(text)
    return groups


def _questions(focus: Focus) -> str:
    if not focus.questions:
        return "(none)"
    return "\n".join(f"{i}. {q}" for i, q in enumerate(focus.questions, start=1))


def _section(heading: str, body: str) -> str:
    return f"## {heading}\n{body.strip() or NONE_STATED}\n"


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item.strip()}" for item in items if item.strip())


def render_analysis(note: Note, analysis: SourceAnalysis) -> tuple[str, str]:
    """(title, markdown body) of the analysis note, with the original's headings."""
    quotes = "\n".join(f'> "{q}"' for q in analysis.quotes)
    relevance = f"{analysis.relevance}: {analysis.relevance_to_query.strip()}"
    body = "\n".join(
        [
            _section("Thesis", analysis.thesis),
            _section("Methodology", analysis.methodology),
            _section("Key findings", _bullets(analysis.key_findings)),
            _section("Load-bearing citations", _bullets(analysis.load_bearing_citations)),
            _section("Caveats", analysis.caveats),
            _section("Relevance to the question", relevance),
            _section("Quotes", quotes),
        ]
    )
    return f"Analysis: {note.title}", body


@dataclass(frozen=True)
class _Context:
    note: Note
    focus: Focus
    source_url: str


class SourceAnalyzer:
    def __init__(self, llm: LLMService, events: EventSink) -> None:
        self._llm = llm
        self._events = events

    def analyze(self, note: Note, focus: Focus) -> SourceAnalysis | None:
        """The analysis, or None if it could not be produced (an event says why)."""
        ctx = _Context(note, focus, note.final_url or note.url)
        partials = self._map(ctx)
        if not partials:
            self._fail(note, "map")
            return None
        try:
            analysis = self._reduce(ctx, partials)
        except _ReduceFailed:
            self._fail(note, "reduce")
            return None
        return self._verified(note, analysis)

    def _fail(self, note: Note, stage: str) -> None:
        self._events.emit(
            "source_analysis_failed", level="warning", note_id=note.note_id, stage=stage
        )

    def _call(self, system: str, user: str, schema: type[_T]) -> _T:
        messages: tuple[Message, ...] = (
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        )
        return self._llm.structured(Role.SUMMARIZE, messages, schema)

    def _map(self, ctx: _Context) -> list[PartialAnalysis]:
        chunks = split_paragraph_chunks(ctx.note.body, MAP_CHUNK_CHARS)
        partials: list[PartialAnalysis] = []
        for index, chunk in enumerate(chunks, start=1):
            user = ANALYSIS_MAP_USER.format(
                title=ctx.focus.title,
                questions=_questions(ctx.focus),
                index=index,
                total=len(chunks),
                source=fence_untrusted(ctx.source_url, chunk),
            )
            try:
                partials.append(self._call(ANALYSIS_MAP_SYSTEM, user, PartialAnalysis))
            except LLMError as exc:
                self._events.emit(
                    "source_analysis_chunk_failed",
                    level="warning",
                    note_id=ctx.note.note_id,
                    chunk=index,
                    error=type(exc).__name__,
                )
        return partials

    def _reduce(self, ctx: _Context, partials: list[PartialAnalysis]) -> SourceAnalysis:
        for _ in range(MAX_ROUNDS):
            texts = [_render_partial(i, p) for i, p in enumerate(partials, start=1)]
            if sum(len(t) + 2 for t in texts) <= REDUCE_BATCH_CHARS:
                break
            merged = [
                self._merge(ctx, [partials[i] for i in group])
                for group in _batches(texts, REDUCE_BATCH_CHARS)
            ]
            if len(merged) >= len(partials):
                break  # no progress: let the final call fail loudly rather than loop
            partials = merged
        return self._final(ctx, partials)

    def _merge(self, ctx: _Context, group: list[PartialAnalysis]) -> PartialAnalysis:
        if len(group) == 1:
            return group[0]
        parts = "\n\n".join(_render_partial(i, p) for i, p in enumerate(group, start=1))
        user = ANALYSIS_REDUCE_USER.format(
            title=ctx.focus.title, parts=fence_untrusted(ctx.source_url, parts)
        )
        try:
            return self._call(ANALYSIS_REDUCE_SYSTEM, user, PartialAnalysis)
        except LLMError as exc:
            raise _ReduceFailed from exc

    def _final(self, ctx: _Context, partials: list[PartialAnalysis]) -> SourceAnalysis:
        parts = "\n\n".join(_render_partial(i, p) for i, p in enumerate(partials, start=1))
        user = ANALYSIS_FINAL_USER.format(
            title=ctx.focus.title,
            questions=_questions(ctx.focus),
            parts=fence_untrusted(ctx.source_url, parts),
        )
        try:
            return self._call(ANALYSIS_FINAL_SYSTEM, user, SourceAnalysis)
        except LLMError as exc:
            raise _ReduceFailed from exc

    @staticmethod
    def _verified(note: Note, analysis: SourceAnalysis) -> SourceAnalysis:
        normalized = normalize_for_match(note.body)
        kept = [
            strip_wrapping_quotes(q)
            for q in analysis.quotes
            if contains_quote(normalized, strip_wrapping_quotes(q))
        ]
        return analysis.model_copy(update={"quotes": kept[:MAX_QUOTES]})
