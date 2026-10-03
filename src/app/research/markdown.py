"""Reading a rendered report back: headings outside code fences, the body (the text before the
Sources heading), the Sources entries, the appendix fence and the citation numbers.

Pure text functions; the ship gate and the report renderer share them. Lines are split at `\\n`
only, because the appendix holds the owner's brief and must come back byte for byte.
"""

import re
from collections.abc import Iterator

SOURCES_TITLES = ("quellen", "sources")
APPENDIX_FIRST_WORDS = ("anhang", "appendix")

_OPEN = re.compile(r"^ {0,3}(?P<mark>`{3,}|~{3,})(?P<info>.*)$")
_H2 = re.compile(r"^## (?P<title>.+?)\s*$")
_ENTRY = re.compile(r"^\[(?P<n>\d+)\]\s+(?P<text>.+?)\s*$")
_TEXT_FENCE = re.compile(r"^(?P<mark>`{3,})text\s*$")
_CITATION = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
_CITATION_WITH_SPACE = re.compile(r" ?\[\d+(?:\s*,\s*\d+)*\]")


def _closes(line: str, char: str, length: int) -> bool:
    stripped = line.strip()
    return (
        len(line) - len(line.lstrip(" ")) <= 3
        and len(stripped) >= length
        and set(stripped) == {char}
    )


def _lines(text: str) -> list[str]:
    return text.split("\n")


def outside_fences(text: str) -> Iterator[tuple[int, str]]:
    """``(index, line)`` of every line that is not inside a fenced code block or a fence line."""
    fence: tuple[str, int] | None = None
    for index, line in enumerate(_lines(text)):
        if fence is None:
            found = _OPEN.match(line)
            if found and not (found["mark"][0] == "`" and "`" in found["info"]):
                fence = (found["mark"][0], len(found["mark"]))
            else:
                yield index, line
        elif _closes(line, *fence):
            fence = None


def _h2_at(text: str) -> list[tuple[int, str]]:
    return [(i, m["title"]) for i, line in outside_fences(text) if (m := _H2.match(line))]


def h2_list(text: str) -> list[str]:
    return [title for _, title in _h2_at(text)]


def _first_index(text: str, titles: tuple[str, ...], *, first_word: bool = False) -> int | None:
    for index, title in _h2_at(text):
        key = title.casefold()
        if (key.split()[0] if first_word and key.split() else key) in titles:
            return index
    return None


def body_of(text: str) -> str:
    """The text before the Sources heading (all of it if the report has none)."""
    index = _first_index(text, SOURCES_TITLES)
    if index is None:
        return text
    return "".join(f"{line}\n" for line in _lines(text)[:index])


def sources_entries(text: str) -> dict[int, str]:
    """The Sources list: citation number to the entry's text (everything after `[N] `)."""
    start = _first_index(text, SOURCES_TITLES)
    if start is None:
        return {}
    ends = [i for i, _ in _h2_at(text) if i > start]
    lines = _lines(text)[start + 1 : ends[0] if ends else None]
    return {int(m["n"]): m["text"] for line in lines if (m := _ENTRY.match(line))}


def appendix_fence(text: str) -> str | None:
    """What the appendix's `text` fence holds; None without an appendix or a closed fence."""
    start = _first_index(text, APPENDIX_FIRST_WORDS, first_word=True)
    if start is None:
        return None
    lines = _lines(text)
    opened = next(
        (
            (i, len(m["mark"]))
            for i in range(start + 1, len(lines))
            if (m := _TEXT_FENCE.match(lines[i]))
        ),
        None,
    )
    if opened is None:
        return None
    first, length = opened
    for index in range(first + 1, len(lines)):
        if _closes(lines[index], "`", length):
            return "".join(f"{line}\n" for line in lines[first + 1 : index])
    return None


def citation_numbers(text: str) -> list[int]:
    """Every citation number in ``text``, in order of appearance, repeats included."""
    return [int(n) for group in _CITATION.findall(text) for n in re.findall(r"\d+", group)]


def strip_citations(text: str) -> str:
    """``text`` without its citation markers and the space before each."""
    return _CITATION_WITH_SPACE.sub("", text)


def body_words(text: str) -> int:
    """Words of ``text`` without its citation markers."""
    return len(strip_citations(text).split())
