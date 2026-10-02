"""The interview language, taken from the owner's first message (PRD M4 AC7)."""

from collections.abc import Callable
from typing import Protocol, cast

from langdetect import (  # pyright: ignore[reportMissingTypeStubs]
    DetectorFactory,
    detect_langs,  # pyright: ignore[reportUnknownVariableType]
)

from app.pipeline.profiles import Phase1Limits

DEFAULT_LANGUAGE = "de"
DetectFn = Callable[[str], list[tuple[str, float]]]

# langdetect is randomised unless seeded; the same text must always give the same answer.
DetectorFactory.seed = 0


class _Candidate(Protocol):
    lang: str
    prob: float


def _detect(text: str) -> list[tuple[str, float]]:
    found = cast("list[_Candidate]", detect_langs(text))
    return [(candidate.lang, candidate.prob) for candidate in found]


def detect_interview_language(
    text: str, limits: Phase1Limits, *, detect: DetectFn = _detect
) -> str:
    """The language of ``text`` when it is long enough and the detector is confident; else German.

    Regional codes are reduced to the language (`zh-cn` → `zh`)."""
    stripped = text.strip()
    if len(stripped) < limits.language_min_chars:
        return DEFAULT_LANGUAGE
    try:
        candidates = detect(stripped)
    except Exception:  # detection is best effort; any failure means the default
        return DEFAULT_LANGUAGE
    if not candidates:
        return DEFAULT_LANGUAGE
    code, probability = max(candidates, key=lambda candidate: candidate[1])
    return (
        code.split("-")[0] if probability >= limits.language_min_probability else DEFAULT_LANGUAGE
    )
