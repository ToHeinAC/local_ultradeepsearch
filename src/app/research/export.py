"""Step X (PRD M5): `report.docx` and `report.pdf` from `report.md` through pandoc.

The Markdown stays the report; the exports are conveniences and are checked after the fact: a
DOCX must hold every H2 of the report, a PDF must be a PDF. Anything else is refused and removed,
and the failure is an event, never silent. A missing pandoc fails both formats and nothing else.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from docx import Document

from app.events import EventSink
from app.research.markdown import h2_list

NOT_FOUND = 127
FIRST_LINE = 200


@dataclass(frozen=True)
class PandocResult:
    code: int
    stderr: str


class PandocRunner(Protocol):
    def run(self, args: Sequence[str], *, cwd: Path) -> PandocResult: ...


@dataclass(frozen=True)
class ExportResult:
    docx: str  # "ok", or why not
    pdf: str

    @property
    def ok(self) -> bool:
        return self.docx == "ok" and self.pdf == "ok"


def docx_args(report: Path, out: Path, reference_docx: Path | None) -> list[str]:
    args = [str(report), "--from=markdown", "-o", str(out)]
    return [*args, f"--reference-doc={reference_docx}"] if reference_docx else args


def pdf_args(report: Path, out: Path, css: Path) -> list[str]:
    return [
        str(report),
        "--from=markdown",
        "-o",
        str(out),
        "--pdf-engine=weasyprint",
        f"--css={css}",
    ]


def docx_headings(path: Path) -> list[str]:
    """The text of the level-2 headings of a DOCX, in order."""
    document = Document(str(path))
    return [
        p.text for p in document.paragraphs if p.style is not None and p.style.name == "Heading 2"
    ]


def _verify(path: Path, fmt: str, headings: Sequence[str]) -> str | None:
    if not path.exists() or path.stat().st_size == 0:
        return "no file"
    if fmt == "pdf":
        return None if path.read_bytes().startswith(b"%PDF") else "not a PDF"
    try:
        found = docx_headings(path)
    except Exception:  # any failure to open the file means it is not a DOCX
        return "not a DOCX"
    missing = [h for h in headings if h not in found]
    return f"missing headings {', '.join(missing)}" if missing else None


def _make(
    runner: PandocRunner, run_dir: Path, fmt: str, args: list[str], headings: Sequence[str]
) -> str:
    out = run_dir / f"report.{fmt}"
    out.unlink(missing_ok=True)  # an export of an earlier attempt must never pass for this one
    result = runner.run(args, cwd=run_dir)
    if result.code == NOT_FOUND:
        return "pandoc_missing"
    if result.code != 0:
        first = (result.stderr.splitlines() or [""])[0][:FIRST_LINE]
        return f"pandoc_failed: {first}"
    problem = _verify(out, fmt, headings)
    if problem is not None:
        out.unlink(missing_ok=True)
        return f"invalid_output: {problem}"
    return "ok"


def export_report(
    run_dir: Path,
    runner: PandocRunner,
    *,
    css: Path,
    reference_docx: Path | None,
    events: EventSink,
) -> ExportResult:
    """Make both exports; each outcome is `ok` or the reason it failed, and an event."""
    report = run_dir / "report.md"
    if not report.exists():
        return ExportResult("no_report", "no_report")
    headings = h2_list(report.read_text(encoding="utf-8"))
    md, docx, pdf = Path("report.md"), Path("report.docx"), Path("report.pdf")
    outcomes = {
        "docx": _make(runner, run_dir, "docx", docx_args(md, docx, reference_docx), headings),
        "pdf": _make(runner, run_dir, "pdf", pdf_args(md, pdf, css), headings),
    }
    for fmt, outcome in outcomes.items():
        if outcome == "ok":
            events.emit("export_ok", format=fmt)
        else:
            events.emit("export_failed", level="warning", format=fmt, reason=outcome)
    return ExportResult(outcomes["docx"], outcomes["pdf"])
