"""The report as text (PRD §3.9, D7). Code owns the citation numbers, the Sources list and the
appendix: sections hold evidence keys like `[S3]`, and rendering turns them into `[N]` numbered by
first appearance. The sections are the editable state; `report.md` is always rendered from them.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from app.brief.labels import sources_heading
from app.store.models import Note

EM_DASH = chr(0x2014)
MAX_PER_BRACKET = 3
NO_SOURCES_EN = "No sources were cited."
_BRACKET = r"\[\s*S\d+(?:\s*[,;]\s*S\d+)*\s*\]"
CITATION_GROUP = re.compile(rf"( ?)((?:{_BRACKET})(?:\s*{_BRACKET})*)")
_KEY = re.compile(r"S\d+")
_BACKTICKS = re.compile(r"`+")


@dataclass(frozen=True)
class ReportSource:
    """What the Sources list needs to know about one note."""

    note_id: str
    url: str
    title: str
    authors: tuple[str, ...]
    publisher: str | None
    year: int | None
    retrieved: str  # YYYY-MM-DD
    retracted: bool


@dataclass(frozen=True)
class ApprovedBrief:
    text: str  # the archived bytes: canonical, ends with a newline
    approved_at: datetime
    archive_path: str


@dataclass(frozen=True)
class SectionText:
    heading: str
    text: str  # Markdown with evidence keys, no heading


@dataclass(frozen=True)
class RenderedReport:
    markdown: str
    cited: tuple[tuple[int, str], ...]  # (citation number, evidence key), in numbering order
    dropped_keys: tuple[str, ...]  # keys the model cited that name no evidence


def source_from_note(note: Note) -> ReportSource:
    meta = note.meta
    year = meta.get("year")
    return ReportSource(
        note_id=note.note_id,
        url=note.url,
        title=" ".join(note.title.split()),
        authors=tuple(str(a) for a in meta.get("authors") or ()),
        publisher=str(meta["venue"]) if meta.get("venue") else None,
        year=year if isinstance(year, int) else None,
        retrieved=note.created_at[:10],
        retracted=meta.get("is_retracted") is True,
    )


def cited_keys(text: str) -> list[str]:
    """Every evidence key cited in ``text`` (inside brackets), in order, repeats included."""
    return [key for match in CITATION_GROUP.finditer(text) for key in _KEY.findall(match[2])]


def appendix_heading(language: str) -> str:
    return (
        f"Anhang A {EM_DASH} Recherche-Brief"
        if language == "de"
        else (f"Appendix A {EM_DASH} Research Brief")
    )


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def _sentence(text: str) -> str:
    return text if text.endswith((".", "!", "?")) else f"{text}."


def format_source(number: int, source: ReportSource, language: str) -> str:
    """`[N] Author/Publisher. Title. Year. URL (abgerufen YYYY-MM-DD)`."""
    german = language == "de"
    if source.authors:
        who = "; ".join(source.authors[:MAX_PER_BRACKET])
        if len(source.authors) > MAX_PER_BRACKET:
            who += " u. a." if german else " et al."
    else:
        who = source.publisher or _host(source.url)
    title = " ".join(source.title.split())  # an entry is one line
    year = str(source.year) if source.year is not None else ("o. J." if german else "n.d.")
    label = "abgerufen" if german else "accessed"
    return (
        f"[{number}] {_sentence(who)} {_sentence(title)} {_sentence(year)} "
        f"{source.url} ({label} {source.retrieved})"
    )


class _Numbering:
    """Citation numbers by first appearance, shared by all sections of one report."""

    def __init__(self, keys: Mapping[str, ReportSource]) -> None:
        self._keys = keys
        self.numbers: dict[str, int] = {}
        self.dropped: dict[str, None] = {}

    def convert(self, text: str) -> str:
        return CITATION_GROUP.sub(lambda match: self._group(match), text)

    def _group(self, match: re.Match[str]) -> str:
        found = list(dict.fromkeys(_KEY.findall(match[2])))
        known = [key for key in found if key in self._keys]
        self.dropped.update(dict.fromkeys(key for key in found if key not in self._keys))
        if not known:
            return ""  # the space before the marker goes with it
        for key in known:
            self.numbers.setdefault(key, len(self.numbers) + 1)
        ordered = sorted(self.numbers[key] for key in known)
        chunks = [ordered[i : i + MAX_PER_BRACKET] for i in range(0, len(ordered), MAX_PER_BRACKET)]
        return match[1] + " ".join("[" + ", ".join(map(str, chunk)) + "]" for chunk in chunks)


def fence_for(text: str) -> str:
    """A code fence longer than any run of backticks in ``text``."""
    longest = max((len(run) for run in _BACKTICKS.findall(text)), default=0)
    return "`" * max(3, longest + 1)


def _appendix(language: str, brief: ApprovedBrief) -> str:
    fence = fence_for(brief.text)
    stamp = brief.approved_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    provenance = (
        f"Freigegeben am {stamp} UTC. Archiviert unter {brief.archive_path}."
        if language == "de"
        else f"Approved on {stamp} UTC. Archived at {brief.archive_path}."
    )
    return f"## {appendix_heading(language)}\n\n{fence}text\n{brief.text}{fence}\n\n{provenance}\n"


def render_report(
    title: str,
    sections: Sequence[SectionText],
    keys: Mapping[str, ReportSource],
    language: str,
    brief: ApprovedBrief,
) -> RenderedReport:
    """The whole report: title, the sections with their citations numbered, Sources, appendix."""
    numbering = _Numbering(keys)
    blocks = [f"# {' '.join(title.split())}\n"]
    for section in sections:
        text = numbering.convert(section.text.strip())
        blocks.append(f"## {section.heading}\n\n{text}\n")
    cited = tuple(sorted(((n, key) for key, n in numbering.numbers.items())))
    if cited:
        entries = [format_source(n, keys[key], language) for n, key in cited]
        listing = "\n\n".join(entries)
    else:
        listing = "Es wurden keine Quellen zitiert." if language == "de" else NO_SOURCES_EN
    blocks.append(f"## {sources_heading(language)}\n\n{listing}\n")
    blocks.append(_appendix(language, brief))
    return RenderedReport("\n".join(blocks), cited, tuple(numbering.dropped))
