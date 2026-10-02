"""The digest of the uploaded files that goes into the brief (PRD M4): at most N words, every line
with its file and page. The model picks and merges facts; code checks provenance and the budget."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.brief.labels import Labels
from app.brief.schemas import DigestItem, UploadDigest
from app.events import EventSink
from app.llm.errors import LLMError
from app.llm.service import LLMService
from app.llm.types import Message, Role
from app.pipeline.analysis import MAX_ROUNDS, REDUCE_BATCH_CHARS
from app.pipeline.profiles import Phase1Limits
from app.prompts.brief import UPLOAD_DIGEST_SYSTEM, UPLOAD_DIGEST_USER
from app.prompts.untrusted import fence_untrusted

BATCH_CHARS = REDUCE_BATCH_CHARS  # facts per prompt; more are condensed in stages
MAX_STAGES = MAX_ROUNDS


@dataclass(frozen=True)
class FactLine:
    file: str
    page: int
    fact: str


@dataclass(frozen=True)
class Digest:
    text: str
    notice: str  # why the digest is not simply all facts ("" if it is)


def _line(text: str) -> str:
    return " ".join(text.split())


def _input_line(fact: FactLine) -> str:
    return f"[{fact.file}, page {fact.page}] {_line(fact.fact)}"


def _pack(lines: Sequence[str], limit: int) -> list[list[str]]:
    """Consecutive lines in groups of at most ``limit`` characters (a long line stands alone)."""
    groups: list[list[str]] = []
    size = 0
    for line in lines:
        if groups and size + len(line) + 1 <= limit:
            groups[-1].append(line)
            size += len(line) + 1
        else:
            groups.append([line])
            size = len(line) + 1
    return groups


def _fitting_prefix(lines: Sequence[str], limit: int) -> list[str]:
    kept: list[str] = []
    size = 0
    for line in lines:
        if kept and size + len(line) + 1 > limit:
            break
        kept.append(line)
        size += len(line) + 1
    return kept


def _render(facts: Sequence[FactLine], labels: Labels, words: int) -> tuple[str, bool]:
    """Lines `- fact (file, S. n)` until the word budget is used; True if items were left out."""
    lines: list[str] = []
    used = 0
    for fact in facts:
        line = f"- {_line(fact.fact)} ({fact.file}, {labels.page} {fact.page})"
        used += len(line.split())
        if used > words:
            return "\n".join(lines), True
        lines.append(line)
    return "\n".join(lines), False


def _notice(labels: Labels, *, staged: bool, cut: bool, words: int, plain: bool = False) -> str:
    parts = [labels.digest_plain] if plain else []
    parts += [labels.digest_staged] if staged else []
    parts += [labels.digest_cut.format(words=words)] if cut else []
    return " ".join(parts)


def plain_digest(facts: Sequence[FactLine], labels: Labels, limits: Phase1Limits) -> Digest:
    """The facts in order up to the budget, made without the model (used when it fails)."""
    text, cut = _render(facts, labels, limits.upload_digest_words)
    return Digest(
        text, _notice(labels, staged=False, cut=cut, words=limits.upload_digest_words, plain=True)
    )


def _ask(
    llm: LLMService, question: str, lines: Sequence[str], limits: Phase1Limits
) -> list[DigestItem]:
    messages: list[Message] = [
        {"role": "system", "content": UPLOAD_DIGEST_SYSTEM},
        {
            "role": "user",
            "content": UPLOAD_DIGEST_USER.format(
                question=question,
                fenced=fence_untrusted("uploaded files", "\n".join(lines)),
                max_words=limits.upload_digest_words,
            ),
        },
    ]
    return llm.structured(Role.SUMMARIZE, messages, UploadDigest, think=False).items


def _verified(items: Sequence[DigestItem], pages: Mapping[str, int]) -> list[FactLine]:
    """Only items naming a known file and a page inside it; the file is written as stored."""
    names = {name.lower(): name for name in pages}
    kept: list[FactLine] = []
    for item in items:
        name = names.get(item.file.strip().lower())
        if name is not None and 1 <= item.page <= pages[name] and _line(item.fact):
            kept.append(FactLine(name, item.page, _line(item.fact)))
    return kept


def _condense(
    llm: LLMService,
    facts: Sequence[FactLine],
    pages: Mapping[str, int],
    question: str,
    limits: Phase1Limits,
) -> tuple[list[FactLine], bool]:
    """Facts that fit one prompt, and whether stages were needed to get there."""
    current = list(facts)
    staged = False
    for _ in range(MAX_STAGES):
        lines = [_input_line(f) for f in current]
        if sum(len(line) + 1 for line in lines) <= BATCH_CHARS:
            return current, staged
        staged = True
        current = [
            kept
            for group in _pack(lines, BATCH_CHARS)
            for kept in _verified(_ask(llm, question, group, limits), pages)
        ]
    return current, True


def build_digest(
    llm: LLMService,
    events: EventSink,
    facts: Sequence[FactLine],
    *,
    pages: Mapping[str, int],
    question: str,
    labels: Labels,
    limits: Phase1Limits,
) -> Digest:
    """At most ``limits.upload_digest_words`` words of the most relevant facts, each with its file
    and page. Too many facts are condensed in stages; a failing model gives a plain digest."""
    if not facts:
        return Digest("", "")
    try:
        current, staged = _condense(llm, facts, pages, question, limits)
        lines = _fitting_prefix([_input_line(f) for f in current], BATCH_CHARS)
        items = _verified(_ask(llm, question, lines, limits), pages)
    except LLMError as exc:
        events.emit("upload_digest_failed", level="warning", error=type(exc).__name__)
        return plain_digest(facts, labels, limits)
    words = limits.upload_digest_words
    text, cut = _render(items, labels, words)
    return Digest(text, _notice(labels, staged=staged, cut=cut, words=words))
