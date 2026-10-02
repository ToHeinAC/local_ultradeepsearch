"""Reading a brief back: its title and numbered research questions (PRD AD6: parsed by code)."""

import re
from dataclasses import dataclass

from app.brief.labels import DE, EN

_TITLE = re.compile(r"^# (?P<text>\S.*?)\s*$")
_H2 = re.compile(r"^## (?P<text>.+?)\s*$")
_ITEM = re.compile(r"^ {0,3}\d+[.)]\s+(?P<text>\S.*?)\s*$")
_QUESTION_HEADINGS = {DE.questions.casefold(), EN.questions.casefold()}
_OUTPUT_HEADINGS = {DE.output.casefold(), EN.output.casefold()}


class BriefParseError(ValueError):
    """The text has no title or no numbered research questions, so Phase 2 could not use it."""


@dataclass(frozen=True)
class ParsedBrief:
    title: str
    research_questions: tuple[str, ...]


def parse_brief(text: str) -> ParsedBrief:
    """The first `# ` line is the title. Research questions are the numbered items under the
    research-questions heading; without such items, the numbered items before the Output section."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    title = next((m["text"] for line in lines if (m := _TITLE.match(line))), None)
    if title is None:
        raise BriefParseError("the brief needs a title line starting with '# '")
    under_heading: list[str] = []
    elsewhere: list[str] = []
    section = ""
    for line in lines:
        if heading := _H2.match(line):
            section = heading["text"].casefold()
        elif item := _ITEM.match(line):
            if section in _QUESTION_HEADINGS:
                under_heading.append(item["text"])
            elif section not in _OUTPUT_HEADINGS:
                elsewhere.append(item["text"])
    questions = under_heading or elsewhere
    if not questions:
        raise BriefParseError("the brief needs numbered research questions ('1. ...')")
    return ParsedBrief(title, tuple(questions))
