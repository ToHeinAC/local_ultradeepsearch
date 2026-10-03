"""Quoted spans (PRD G6): quotation marks are reserved for verbatim source text.

A span of at least `min_words` words in any of the PRD's quotation styles must occur in a note
that the same sentence cites. How a citation names a note differs between the rendered report
(`[3]`) and the editable sections (`[S3]`), so the caller passes a resolver.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from app.text import contains_quote

# (opening, closing) code points: German double, English double, ASCII, guillemets both ways,
# German single
_PAIRS = (
    (0x201E, 0x201C),
    (0x201C, 0x201D),
    (0x22, 0x22),
    (0xAB, 0xBB),
    (0xBB, 0xAB),
    (0x201A, 0x2018),
)
_END = re.compile(r"[.!?](?=\s|$)")


@dataclass(frozen=True)
class Quote:
    open: str
    span: str
    close: str
    start: int  # of the opening mark
    end: int  # after the closing mark


@dataclass(frozen=True)
class QuoteFailure:
    open: str
    span: str
    close: str
    reason: str  # "uncited" or "not_in_source"


def _patterns() -> list[re.Pattern[str]]:
    result: list[re.Pattern[str]] = []
    for opening, closing in _PAIRS:
        o, c = re.escape(chr(opening)), re.escape(chr(closing))
        result.append(re.compile(f"(?P<o>{o})(?P<s>[^\\n{o}{c}]+)(?P<c>{c})"))
    return result


_PATTERNS = _patterns()


def find_quotes(text: str) -> list[Quote]:
    """Every quoted span of ``text`` in order; a span inside another is dropped."""
    found = sorted(
        (
            Quote(m["o"], m["s"], m["c"], m.start(), m.end())
            for p in _PATTERNS
            for m in p.finditer(text)
        ),
        key=lambda q: q.start,
    )
    result: list[Quote] = []
    for quote in found:
        if not result or quote.start >= result[-1].end:
            result.append(quote)
    return result


def _window(text: str, quote: Quote) -> str:
    """The sentence around ``quote``, kept inside its paragraph."""
    left = 0
    for match in _END.finditer(text, 0, quote.start):
        left = match.end()
    paragraph_start = text.rfind("\n\n", 0, quote.start)
    left = max(left, paragraph_start + 2 if paragraph_start != -1 else 0)
    after = _END.search(text, quote.end)
    right = after.end() if after else len(text)
    paragraph_end = text.find("\n\n", quote.end)
    if paragraph_end != -1:
        right = min(right, paragraph_end)
    return text[left:right]


def quote_failures(
    text: str,
    cited: Callable[[str], list[str]],
    note_texts: Mapping[str, str],
    min_words: int,
) -> list[QuoteFailure]:
    """The quotes of ``text`` that are not verbatim in a cited note.

    ``cited(window)`` names the notes a sentence cites; ``note_texts`` maps note ids to their
    `normalize_for_match`-ed text. Quotes shorter than ``min_words`` words are not checked."""
    failures: list[QuoteFailure] = []
    for quote in find_quotes(text):
        if len(quote.span.split()) < min_words:
            continue
        notes = [n for n in cited(_window(text, quote)) if n in note_texts]
        if not notes:
            reason = "uncited"
        elif any(contains_quote(note_texts[n], quote.span.rstrip(".,;:!?")) for n in notes):
            continue
        else:
            reason = "not_in_source"
        failures.append(QuoteFailure(quote.open, quote.span, quote.close, reason))
    return failures
