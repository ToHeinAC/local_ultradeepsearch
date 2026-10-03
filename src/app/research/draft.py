"""Step 10 (PRD M5, AD4): the report written one H2 section at a time.

Each section is one `reason` call over its own evidence pack, with a word budget that adds up to
the middle of the format's range. A section is saved as soon as it exists, so a resumed run
continues at the first missing one. A section without evidence is not written by the model: it
states the gap, so nothing is invented.
"""

import re
from collections.abc import Sequence
from pathlib import Path

from app.brief.labels import language_name
from app.events import EventSink
from app.llm.service import LLMService
from app.llm.types import Message, Role
from app.pipeline.profiles import FormatRange, RunRules
from app.prompts import research as prompts
from app.research.evidence import EvidenceKeys, PackBuilder, section_query
from app.research.models import Decomposition
from app.research.sections import load_section, save_section
from app.templates import Section

DRAFT_THINK = False  # a section is plain prose; thinking would only spend the output budget
NO_EVIDENCE = {
    "de": "Zu diesem Abschnitt liegen keine belastbaren Belege vor.",
    "en": "There is no reliable evidence for this section.",
}
_FRONT_MATTER = re.compile(r"\A---\n.*?\n---[ \t]*\n", re.DOTALL)
_CODE_WRAP = re.compile(r"\A```[a-z]*\n(.*?)\n```\s*\Z", re.DOTALL)
_HEADING_LINE = re.compile(r"(?m)^#{1,6}\s.*$\n?")
_BLANKS = re.compile(r"\n{3,}")


def sections_of(
    decomposition: Decomposition, template_sections: Sequence[Section]
) -> list[Section]:
    """The report's sections: the template's (with their instructions), or the derived headings."""
    instructions = {s.heading: s.instructions for s in template_sections}
    return [
        Section(heading, instructions.get(heading, ""))
        for heading in decomposition.required_section_headings
    ]


def words_per_section(fmt: FormatRange, sections: int) -> int:
    """The middle of the format's word range, split evenly."""
    low, high = fmt.words
    return max(1, round((low + high) / 2 / max(1, sections)))


def clean_section(text: str) -> str:
    """Model output made fit for a section: no code-fence wrapper, no front matter, no heading
    lines (the structure is code's), no runs of blank lines."""
    result = text.strip()
    wrapped = _CODE_WRAP.match(result)
    if wrapped:
        result = wrapped[1]
    result = _FRONT_MATTER.sub("", result, count=1)
    result = _HEADING_LINE.sub("", result)
    return _BLANKS.sub("\n\n", result).strip()


class Drafter:
    def __init__(
        self,
        service: LLMService,
        packs: PackBuilder,
        keys: EvidenceKeys,
        rules: RunRules,
        events: EventSink,
        *,
        prompt_chars: int,
        condense_chars: int,
    ) -> None:
        self._service = service
        self._packs = packs
        self._keys = keys
        self._rules = rules
        self._events = events
        self._prompt_chars = prompt_chars
        self._condense_chars = condense_chars

    def draft_all(
        self,
        run_dir: Path,
        *,
        title: str,
        questions: Sequence[str],
        sections: Sequence[Section],
        language: str,
        fmt: FormatRange,
        must_read: Sequence[str],
        shim: str,
    ) -> None:
        """Write every section that has no file yet. A model failure propagates; what was saved
        stays saved."""
        words = words_per_section(fmt, len(sections))
        for index, section in enumerate(sections, 1):
            if load_section(run_dir, index) is not None:
                continue
            user = self._user(title, questions, sections, index, language, words, shim)
            text = self._section(section, questions, must_read, user, language)
            save_section(run_dir, index, text)

    def _user(
        self,
        title: str,
        questions: Sequence[str],
        sections: Sequence[Section],
        index: int,
        language: str,
        words: int,
        shim: str,
    ) -> str:
        outline = "\n".join(
            f"{'>' if n == index else '-'} {s.heading}" for n, s in enumerate(sections, 1)
        )
        section = sections[index - 1]
        return prompts.DRAFT_USER.format(
            title=title,
            questions="\n".join(f"{n}. {q}" for n, q in enumerate(questions, 1)),
            language=language_name(language, "en"),
            shim=shim.strip(),
            outline=outline,
            heading=section.heading,
            instructions=section.instructions or prompts.NO_INSTRUCTIONS,
            words=words,
            pack="{pack}",
        )

    def _section(
        self,
        section: Section,
        questions: Sequence[str],
        must_read: Sequence[str],
        user: str,
        language: str,
    ) -> str:
        overhead = len(prompts.DRAFT_SYSTEM) + len(user)
        budget = int(self._prompt_chars * self._rules.pack_context_fraction) - overhead
        pack = self._packs.build(
            section=section.heading,
            query=section_query(section.heading, section.instructions, questions),
            must_read=must_read,
            budget_chars=budget,
            condense_chars=self._condense_chars,
        )
        if not pack.text:
            self._events.emit("section_without_evidence", level="warning", section=section.heading)
            return NO_EVIDENCE.get(language, NO_EVIDENCE["en"])
        messages: list[Message] = [
            {"role": "system", "content": prompts.DRAFT_SYSTEM},
            {"role": "user", "content": user.replace("{pack}", pack.text)},
        ]
        text = clean_section(self._service.text(Role.REASON, messages, think=DRAFT_THINK))
        if text:
            return text
        self._events.emit("section_empty", level="warning", section=section.heading)
        return NO_EVIDENCE.get(language, NO_EVIDENCE["en"])
