"""What does not belong in a report (PRD G7): front matter, thinking text, scaffold headers and
the vocabulary of the pipeline. Shared by the ship gate and its fixes."""

import re

from app.llm.structured import remove_think
from app.research.markdown import outside_fences

LEAKAGE = (
    r"\bLocus \d+",
    r"\bTension \d+",
    r"comparisons\.md",
    r"\binterim\b",
    r"cross-locus",
    r"scaffold",
    r"hyperresearch",
    r"\[\[",
    r"<think>",
)
SCAFFOLD_HEADERS = (
    "user prompt",
    "run config",
    "modality",
    "tier rationale",
    "wrapper requirements",
)
LEAK = re.compile("|".join(LEAKAGE), re.IGNORECASE)
_HEADER = re.compile(r"^#{1,6}\s+(?P<title>.+?)\s*$")
_FRONT_MATTER = re.compile(r"\A\s*---\n.*?\n---[ \t]*\n", re.DOTALL)
_BLANKS = re.compile(r"\n{3,}")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def leaks(text: str) -> list[str]:
    """What in ``text`` (without its title line) belongs to the pipeline, not to a report."""
    found: list[str] = []
    if text.lstrip().startswith("---"):
        found.append("front matter")
    for _, line in outside_fences(text):
        header = _HEADER.match(line)
        if header and header["title"].casefold() in SCAFFOLD_HEADERS:
            found.append(f"scaffold header: {line.strip()}")
    found += [f"vocabulary: {m[0]}" for m in LEAK.finditer(text)]
    return found


def scrub(text: str) -> str:
    """``text`` without front matter, thinking text and scaffold header lines. The vocabulary
    cannot be cut out by rule; `leak_sentences` finds it for a model to reword."""
    result = _FRONT_MATTER.sub("", remove_think(text), count=1)
    kept = [
        line
        for line in result.split("\n")
        if not (m := _HEADER.match(line)) or m["title"].casefold() not in SCAFFOLD_HEADERS
    ]
    return _BLANKS.sub("\n\n", "\n".join(kept)).strip()


def leak_sentences(text: str) -> list[str]:
    """The sentences of ``text`` that contain pipeline vocabulary, each once."""
    sentences = [s for paragraph in text.split("\n\n") for s in _SENTENCE_END.split(paragraph)]
    return list(dict.fromkeys(s.strip() for s in sentences if LEAK.search(s)))
