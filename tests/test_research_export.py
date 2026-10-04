"""Step X (PRD M5 AC7): DOCX and PDF through pandoc, checked after the fact; MD always stays."""

import stat
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from research_rig import FakePandoc

from app.adapters.pandoc import PandocResult, SubprocessPandoc
from app.events import MemoryEventSink
from app.research.export import (
    ExportResult,
    docx_args,
    docx_headings,
    export_report,
)

REPORT = (
    "# Titel\n\n## Eins\n\nText [1].\n\n## Zwei\n\nMehr.\n\n## Quellen\n\n[1] A. B. 2020. "
    "https://a.example.org/x (abgerufen 2026-10-01)\n\n## Anhang A — Recherche-Brief\n\n"
    "```text\n# Brief\n\n## Ausgabe\n```\n"
)
H2 = ["Eins", "Zwei", "Quellen", "Anhang A — Recherche-Brief"]


def run_export(
    tmp_path: Path, fake: FakePandoc, reference: Path | None = None
) -> tuple[ExportResult, MemoryEventSink]:
    (tmp_path / "report.md").write_text(REPORT, encoding="utf-8")
    events = MemoryEventSink()
    return export_report(tmp_path, fake, reference_docx=reference, events=events), events


def test_both_formats_are_made_and_checked(tmp_path: Path) -> None:
    fake = FakePandoc()
    result, events = run_export(tmp_path, fake)
    assert (result.docx, result.pdf) == ("ok", "ok")
    assert result.ok is True
    assert (tmp_path / "report.docx").exists()
    assert (tmp_path / "report.pdf").read_bytes().startswith(b"%PDF")
    assert [e.data["format"] for e in events.of_type("export_ok")] == ["docx", "pdf"]
    assert docx_headings(tmp_path / "report.docx") == H2


def test_pandoc_makes_the_docx_only_and_the_pdf_never_goes_through_it(tmp_path: Path) -> None:
    ref = tmp_path / "ref.docx"
    ref.write_bytes(b"x")
    fake = FakePandoc()
    run_export(tmp_path, fake, ref)
    ((docx_call, docx_cwd),) = fake.calls
    assert docx_call == docx_args(Path("report.md"), Path("report.docx"), ref)
    assert docx_call[:2] == ["report.md", "--from=markdown"]
    assert f"--reference-doc={ref}" in docx_call
    assert docx_cwd == tmp_path


def test_without_a_reference_document_the_default_styles_apply(tmp_path: Path) -> None:
    assert not any("reference-doc" in a for a in docx_args(Path("a.md"), Path("a.docx"), None))


def test_a_missing_pandoc_fails_the_docx_visibly_and_leaves_markdown_and_pdf(
    tmp_path: Path,
) -> None:
    result, events = run_export(tmp_path, FakePandoc(code=127))
    assert (result.docx, result.pdf) == ("pandoc_missing", "ok")
    assert result.ok is False
    assert (tmp_path / "report.md").read_text(encoding="utf-8") == REPORT
    assert not (tmp_path / "report.docx").exists()
    assert (tmp_path / "report.pdf").read_bytes().startswith(b"%PDF")
    (failed,) = events.of_type("export_failed")
    assert (failed.level, failed.data) == (
        "warning",
        {"format": "docx", "reason": "pandoc_missing"},
    )


def test_a_pandoc_error_names_the_first_line_of_what_it_said(tmp_path: Path) -> None:
    result, _ = run_export(tmp_path, FakePandoc(code=65, stderr="docx failed\nmore detail"))
    assert result.docx == "pandoc_failed: docx failed"
    assert result.pdf == "ok"


def test_a_docx_without_every_heading_of_the_report_is_refused_and_removed(tmp_path: Path) -> None:
    result, events = run_export(tmp_path, FakePandoc(docx_headings=["Eins", "Zwei"]))
    assert result.docx == "invalid_output: missing headings Quellen, Anhang A — Recherche-Brief"
    assert not (tmp_path / "report.docx").exists()
    assert result.pdf == "ok"
    assert any(e.data["format"] == "docx" for e in events.of_type("export_failed"))


def test_a_pdf_that_cannot_be_laid_out_is_a_visible_failure_and_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_markdown: str, _out: Path) -> None:
        raise RuntimeError("layout exploded\nsecond line")

    monkeypatch.setattr("app.research.export.render_pdf", broken)
    result, events = run_export(tmp_path, FakePandoc())
    assert result.pdf == "render_failed: RuntimeError: layout exploded"
    assert not (tmp_path / "report.pdf").exists()
    assert result.docx == "ok"
    assert any(e.data["format"] == "pdf" for e in events.of_type("export_failed"))


def test_a_pdf_without_the_pdf_signature_is_refused_and_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.research.export.render_pdf", lambda _m, out: out.write_bytes(b"<html>")
    )
    result, _ = run_export(tmp_path, FakePandoc())
    assert result.pdf == "invalid_output: not a PDF"
    assert not (tmp_path / "report.pdf").exists()


def test_a_failed_layout_does_not_leave_the_pdf_of_an_earlier_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "report.pdf").write_bytes(b"%PDF-old")

    def broken(_markdown: str, _out: Path) -> None:
        raise RuntimeError("layout exploded")

    monkeypatch.setattr("app.research.export.render_pdf", broken)
    run_export(tmp_path, FakePandoc())
    assert not (tmp_path / "report.pdf").exists()


def test_the_pdf_has_the_reports_text(tmp_path: Path) -> None:
    import pypdfium2 as pdfium  # pyright: ignore[reportMissingTypeStubs]

    run_export(tmp_path, FakePandoc())
    document = pdfium.PdfDocument(str(tmp_path / "report.pdf"))
    text = "\n".join(document[i].get_textpage().get_text_range() for i in range(len(document)))
    document.close()
    for expected in ("Titel", "Eins", "Zwei", "Quellen", "Anhang A — Recherche-Brief"):
        assert expected in text


def test_a_docx_pandoc_did_not_write_is_refused(tmp_path: Path) -> None:
    result, _ = run_export(tmp_path, FakePandoc(write=False))
    assert (result.docx, result.pdf) == ("invalid_output: no file", "ok")


def test_stale_exports_of_an_earlier_run_are_removed_before_they_are_made(tmp_path: Path) -> None:
    (tmp_path / "report.docx").write_bytes(b"old")
    (tmp_path / "report.pdf").write_bytes(b"%PDF-old")
    result, _ = run_export(tmp_path, FakePandoc(write=False))
    assert result.docx.startswith("invalid_output")
    assert not (tmp_path / "report.docx").exists()
    assert (tmp_path / "report.pdf").read_bytes() != b"%PDF-old"  # made again, not kept


def test_an_empty_output_file_is_refused(tmp_path: Path) -> None:
    class Empty(FakePandoc):
        def run(self, args: Sequence[str], *, cwd: Path) -> PandocResult:
            (cwd / args[args.index("-o") + 1]).write_bytes(b"")
            return PandocResult(0, "")

    result, _ = run_export(tmp_path, Empty())
    assert (result.docx, result.pdf) == ("invalid_output: no file", "ok")


def test_a_missing_report_cannot_be_exported(tmp_path: Path) -> None:
    result = export_report(tmp_path, FakePandoc(), reference_docx=None, events=MemoryEventSink())
    assert (result.docx, result.pdf) == ("no_report", "no_report")


# ---- the real adapter on a stand-in executable ---------------------------------------------


def script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "fake-pandoc"
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_the_adapter_runs_the_binary_in_the_directory_and_reports_its_exit_code(
    tmp_path: Path,
) -> None:
    binary = script(
        tmp_path,
        "import sys, pathlib\n"
        "pathlib.Path('seen.txt').write_text(' '.join(sys.argv[1:]))\n"
        "sys.stderr.write('warning: x')\n"
        "sys.exit(3)\n",
    )
    result = SubprocessPandoc(binary).run(["a.md", "-o", "a.docx"], cwd=tmp_path)
    assert result == PandocResult(3, "warning: x")
    assert (tmp_path / "seen.txt").read_text(encoding="utf-8") == "a.md -o a.docx"


def test_a_binary_that_does_not_exist_is_exit_code_127(tmp_path: Path) -> None:
    assert SubprocessPandoc(str(tmp_path / "nope")).run([], cwd=tmp_path).code == 127


def test_a_binary_that_hangs_is_stopped(tmp_path: Path) -> None:
    binary = script(tmp_path, "import time\ntime.sleep(30)\n")
    result = SubprocessPandoc(binary, timeout_s=0.3).run([], cwd=tmp_path)
    assert result.code == 124
    assert "timed out" in result.stderr
