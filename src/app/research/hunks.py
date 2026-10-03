"""The patch engine (PRD AD5, D10). After the first draft a report changes only through hunks:
`{old, new}` pairs that a model proposes and code decides on and applies. A hunk is valid if its
`old` text occurs exactly once, is short, and neither it nor `new` touches a heading. Polish adds
the rule "cut only"; readability adds one rule per allowed category.
"""

import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

CATEGORY_ORDER = (
    "remove-hr",
    "merge-paragraphs",
    "break-paragraph",
    "make-list",
    "make-table",
    "bold-keyterms",
    "add-whitespace",
)

_HEADING = re.compile(r"(?m)^#{1,6}\s")
_MARKER = re.compile(r"\[S\d+\]")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_WORD = re.compile(r"\w+")
_RULE = re.compile(r"(?:-{3,}|\*{3,}|_{3,})")
_PARAGRAPHS = re.compile(r"\n\s*\n")
_LIST_MARK = re.compile(r"(?m)^\s*(?:[-*+]|\d+[.)])\s+")
_BLANKS = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class Hunk:
    old: str
    new: str


@dataclass(frozen=True)
class RejectedHunk:
    hunk: Hunk
    reason: str


@dataclass(frozen=True)
class PolishOutcome:
    text: str
    applied: tuple[Hunk, ...]
    rejected: tuple[RejectedHunk, ...]
    net_chars: int


@dataclass(frozen=True)
class Recommendation:
    id: str
    category: str
    current: str
    recommended: str
    rationale: str = ""


@dataclass(frozen=True)
class Decision:
    id: str
    status: Literal["applied", "skipped", "edit_failure"]
    reason: str = ""


@dataclass(frozen=True)
class ReadabilityOutcome:
    text: str
    decisions: tuple[Decision, ...]
    net_chars: int


def check_hunk(text: str, hunk: Hunk, max_chars: int) -> str | None:
    """Why ``hunk`` may not be applied to ``text``, or None if it may."""
    if not hunk.old:
        return "empty_old"
    if len(hunk.old) > max_chars:
        return "too_long"
    if _HEADING.search(hunk.old) or _HEADING.search(hunk.new):
        return "touches_heading"
    count = text.count(hunk.old)
    if count == 0:
        return "not_found"
    return "ambiguous" if count > 1 else None


def apply_hunk(text: str, hunk: Hunk) -> str:
    return text.replace(hunk.old, hunk.new, 1)


def check_polish(hunk: Hunk) -> str | None:
    """Polish only cuts: no added characters, and every citation marker and number stays."""
    if len(hunk.new) > len(hunk.old):
        return "adds_text"
    if Counter(_MARKER.findall(hunk.old)) - Counter(_MARKER.findall(hunk.new)):
        return "drops_citation"
    if Counter(_NUMBER.findall(hunk.old)) - Counter(_NUMBER.findall(hunk.new)):
        return "drops_number"
    return None


def apply_polish(text: str, hunks: Sequence[Hunk], max_chars: int) -> PolishOutcome:
    """The valid, cut-only hunks applied in order; the others are returned with their reason."""
    current = text
    applied: list[Hunk] = []
    rejected: list[RejectedHunk] = []
    for hunk in hunks:
        reason = check_hunk(current, hunk, max_chars) or check_polish(hunk)
        if reason is None:
            current = apply_hunk(current, hunk)
            applied.append(hunk)
        else:
            rejected.append(RejectedHunk(hunk, reason))
    return PolishOutcome(current, tuple(applied), tuple(rejected), len(current) - len(text))


# ---- readability ----------------------------------------------------------------------------


def _words(text: str) -> list[str]:
    return [w.casefold() for w in _WORD.findall(text)]


def _paragraphs(text: str) -> int:
    return len(_PARAGRAPHS.split(text.strip()))


def _same_words(rec: Recommendation, recommended: str | None = None) -> bool:
    return _words(rec.current) == _words(rec.recommended if recommended is None else recommended)


def _rule_lines(text: str) -> list[str]:
    return [line for line in text.split("\n") if _RULE.fullmatch(line.strip())]


def _remove_hr(rec: Recommendation) -> bool:
    if not _rule_lines(rec.current):
        return False
    kept = "\n".join(
        line for line in rec.current.split("\n") if line not in _rule_lines(rec.current)
    )
    return _BLANKS.sub("\n\n", kept).strip() == _BLANKS.sub("\n\n", rec.recommended).strip()


def _merge(rec: Recommendation) -> bool:
    return _same_words(rec) and _paragraphs(rec.recommended) < _paragraphs(rec.current)


def _break(rec: Recommendation) -> bool:
    return _same_words(rec) and _paragraphs(rec.recommended) > _paragraphs(rec.current)


def _make_list(rec: Recommendation) -> bool:
    marks = _LIST_MARK.findall(rec.recommended)
    bare = _LIST_MARK.sub("", rec.recommended)
    return len(marks) >= 2 and Counter(_words(rec.current)) == Counter(_words(bare))


def _make_table(rec: Recommendation) -> bool:
    rows = [line for line in rec.recommended.split("\n") if "|" in line]
    return len(rows) >= 2 and Counter(_words(rec.current)) == Counter(_words(rec.recommended))


def _bold(rec: Recommendation) -> bool:
    plain = rec.recommended.replace("**", "")
    return "**" in rec.recommended and rec.recommended != rec.current and _same_words(rec, plain)


def _whitespace(rec: Recommendation) -> bool:
    return rec.recommended != rec.current and _same_words(rec)


_RULES: dict[str, Callable[[Recommendation], bool]] = {
    "remove-hr": _remove_hr,
    "merge-paragraphs": _merge,
    "break-paragraph": _break,
    "make-list": _make_list,
    "make-table": _make_table,
    "bold-keyterms": _bold,
    "add-whitespace": _whitespace,
}


def check_recommendation(text: str, rec: Recommendation, max_chars: int) -> str | None:
    """Why ``rec`` may not be applied to ``text``, or None if it may."""
    if rec.category not in _RULES:
        return "category_not_allowed"
    reason = check_hunk(text, Hunk(rec.current, rec.recommended), max_chars)
    if reason is not None:
        return reason
    return None if _RULES[rec.category](rec) else "bad_change"


def _order(rec: Recommendation) -> int:
    return (
        CATEGORY_ORDER.index(rec.category)
        if rec.category in CATEGORY_ORDER
        else len(CATEGORY_ORDER)
    )


def apply_recommendations(
    text: str, recs: Sequence[Recommendation], max_chars: int
) -> ReadabilityOutcome:
    """The valid recommendations applied in category order (each against the text as it is by
    then); every other one is logged as skipped, or as an edit failure if its text is gone."""
    current = text
    decisions: list[Decision] = []
    for rec in sorted(recs, key=_order):
        reason = check_recommendation(current, rec, max_chars)
        if reason is None:
            current = apply_hunk(current, Hunk(rec.current, rec.recommended))
            decisions.append(Decision(rec.id, "applied"))
        else:
            status = "edit_failure" if reason == "not_found" else "skipped"
            decisions.append(Decision(rec.id, status, reason))
    return ReadabilityOutcome(current, tuple(decisions), len(current) - len(text))
