"""The language of the report body (PRD G12): langdetect on a few samples of it."""

from math import ceil
from typing import cast

from langdetect import (  # pyright: ignore[reportMissingTypeStubs]
    DetectorFactory,
    detect,  # pyright: ignore[reportUnknownVariableType]
)
from langdetect.lang_detect_exception import (  # pyright: ignore[reportMissingTypeStubs]
    LangDetectException,
)

from app.research.markdown import strip_citations

MIN_SAMPLE_CHARS = 40  # shorter paragraphs say too little about their language

DetectorFactory.seed = 0  # the same text must always give the same answer


def detect_code(text: str) -> str | None:
    """The two-letter language code of ``text``, or None if it cannot be told."""
    try:
        return cast("str", detect(text)).split("-")[0]
    except LangDetectException:
        return None


def language_samples(body: str, count: int) -> list[str]:
    """The longest paragraph of each of ``count`` consecutive parts of the body, without citations
    and headings. Fewer samples if the body has fewer usable paragraphs."""
    paragraphs = [strip_citations(p).strip() for p in body.split("\n\n")]
    usable = [p for p in paragraphs if len(p) >= MIN_SAMPLE_CHARS and not p.startswith("#")]
    if not usable:
        return []
    size = ceil(len(usable) / count)
    parts = [usable[i : i + size] for i in range(0, len(usable), size)]
    return [max(part, key=len) for part in parts]
