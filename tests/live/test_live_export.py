"""Live check of the export with the real pandoc (the DOCX). Run with `pytest -m live`.

Skipped where pandoc is not installed. Nothing here touches the network or a model.
"""

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.adapters.pandoc import SubprocessPandoc
from app.events import MemoryEventSink
from app.research.export import docx_headings, export_report
from app.research.markdown import h2_list
from app.research.report import ApprovedBrief, ReportSource, SectionText, render_report

pytestmark = pytest.mark.live


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc is not installed")
def test_a_rendered_report_becomes_a_docx_with_every_heading_and_a_pdf(tmp_path: Path) -> None:
    source = ReportSource(
        "n1", "https://a.example.org/x", "Rückbau", ("A. Autor",), None, 2020, "2026-10-01", False
    )
    brief = ApprovedBrief(
        "# Frage\n\n## Forschungsfragen\n\n1. Wie lange?\n", datetime.now(UTC), "x"
    )
    sections = [
        SectionText("Eins", "Ein Satz [S1].\n\n| a | b |\n|---|---|\n| 1 | 2 |"),
        SectionText("Zwei", "Noch einer [S1]."),
    ]
    report = render_report("Titel", sections, {"S1": source}, "de", brief).markdown
    (tmp_path / "report.md").write_text(report, encoding="utf-8")
    result = export_report(
        tmp_path,
        SubprocessPandoc(),
        reference_docx=None,
        events=MemoryEventSink(),
    )
    assert result.ok, result
    assert docx_headings(tmp_path / "report.docx") == h2_list(report)
    assert (tmp_path / "report.pdf").read_bytes().startswith(b"%PDF")
