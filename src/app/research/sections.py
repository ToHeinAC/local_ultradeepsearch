"""The sections of the report on disk, and the report rendered from them (PRD AD5, D7).

`temp/sections/<nn>.md` hold the report's text with evidence keys; they are the editable state.
Every step that changes the report changes a section file (through hunks), and `report.md` is
rendered from the files again, so citation numbers, Sources and the appendix are always code's.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path

from app.artifacts import write_text
from app.events import EventSink
from app.research.report import (
    ApprovedBrief,
    RenderedReport,
    ReportSource,
    SectionText,
    render_report,
    source_from_note,
)
from app.store.vault import Vault

SECTIONS_DIR = Path("temp") / "sections"
REPORT_FILE = "report.md"


def section_path(run_dir: Path, index: int) -> Path:
    return run_dir / SECTIONS_DIR / f"{index:02d}.md"


def save_section(run_dir: Path, index: int, text: str) -> None:
    write_text(section_path(run_dir, index), text.strip() + "\n")


def load_section(run_dir: Path, index: int) -> str | None:
    path = section_path(run_dir, index)
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def load_sections(run_dir: Path, headings: Sequence[str]) -> list[SectionText]:
    """The section files in order; a section not written yet is empty."""
    return [
        SectionText(heading, load_section(run_dir, index) or "")
        for index, heading in enumerate(headings, 1)
    ]


def sources_of_keys(vault: Vault, keys: Mapping[str, str]) -> dict[str, ReportSource]:
    """What the Sources list needs for every evidence key of the run."""
    sources: dict[str, ReportSource] = {}
    for key, note_id in keys.items():
        note = vault.get_note(note_id)
        if note is not None:
            sources[key] = source_from_note(note)
    return sources


def write_report(
    run_dir: Path,
    *,
    title: str,
    headings: Sequence[str],
    keys: Mapping[str, str],
    vault: Vault,
    language: str,
    brief: ApprovedBrief,
    events: EventSink,
) -> RenderedReport:
    """Render `report.md` from the section files. Citations of keys that name no evidence are
    dropped and reported; the file keeps the brief's exact bytes in its appendix."""
    rendered = render_report(
        title,
        load_sections(run_dir, headings),
        sources_of_keys(vault, keys),
        language,
        brief,
    )
    if rendered.dropped_keys:
        events.emit("citations_dropped", level="warning", keys=list(rendered.dropped_keys))
    write_text(run_dir / REPORT_FILE, rendered.markdown, scrub=False)
    return rendered
