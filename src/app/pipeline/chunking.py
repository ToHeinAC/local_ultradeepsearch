"""Splitting long texts into model-sized chunks, and the length classes that scale the output."""

import re
from typing import Literal

LengthClass = Literal["short", "medium", "long"]
SHORT_WORDS = 1500
MEDIUM_WORDS = 5000

_PARAGRAPH = re.compile(r"\n\s*\n")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def word_count(text: str) -> int:
    return len(text.split())


def length_class(words: int) -> LengthClass:
    if words < SHORT_WORDS:
        return "short"
    return "medium" if words < MEDIUM_WORDS else "long"


def _hard_split(text: str, limit: int) -> list[str]:
    """Cut at the last space within ``limit``, or exactly at ``limit`` when there is none."""
    pieces: list[str] = []
    rest = text.strip()
    while len(rest) > limit:
        cut = rest.rfind(" ", 0, limit + 1)
        cut = cut if cut > 0 else limit
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    return [*pieces, rest] if rest else pieces


def _pack(units: list[str], limit: int, separator: str) -> list[str]:
    chunks: list[str] = []
    current = ""
    for unit in units:
        joined = f"{current}{separator}{unit}" if current else unit
        if len(joined) <= limit:
            current = joined
            continue
        if current:
            chunks.append(current)
        current = unit
    return [*chunks, current] if current else chunks


def _fit_paragraph(paragraph: str, limit: int) -> list[str]:
    """The paragraph itself if it fits; else sentence-packed pieces; else hard-split sentences."""
    if len(paragraph) <= limit:
        return [paragraph]
    sentences = [s for part in _SENTENCE_END.split(paragraph) for s in _hard_split(part, limit)]
    return _pack(sentences, limit, " ")


def split_paragraph_chunks(text: str, limit: int) -> list[str]:
    """Chunks of at most ``limit`` characters, split at blank lines, then sentence ends.

    Nothing is dropped or reordered; only whitespace between chunks is lost.
    """
    paragraphs = [p.strip() for p in _PARAGRAPH.split(text) if p.strip()]
    units = [piece for paragraph in paragraphs for piece in _fit_paragraph(paragraph, limit)]
    return _pack(units, limit, "\n\n")
