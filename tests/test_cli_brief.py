from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from brief_rig import QUESTION, Rig, rig
from support import make_pdf
from typer.testing import CliRunner

from app import bootstrap, cli

runner = CliRunner()
# answers to the two questions (Enter = take the proposal), no note, then the decision menu
ANSWERS = "\n\n\n"


@pytest.fixture
def r(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    """A real service on fake models; the command is pointed at it."""
    rg = rig(tmp_path)
    monkeypatch.setattr(cli, "_brief_service", lambda: rg.service)
    return rg


def run(*args: str, input_text: str = "") -> Any:
    return runner.invoke(cli.app, ["brief", *args], input=input_text)


def session_ids(r: Rig) -> list[str]:
    return [s.session_id for s in r.service.list_sessions()]


# ---- starting -------------------------------------------------------------------------------


def test_a_session_from_the_question_to_the_approval(r: Rig) -> None:
    result = run(QUESTION, input_text=ANSWERS + "f\n\n")  # approve, Enter takes the recommendation
    assert result.exit_code == 0, result.output
    assert "Runde 1 von höchstens" in result.output
    assert "Freigegeben" in result.output
    (sid,) = session_ids(r)
    run_row = r.parts.runs.run_for_session(sid)
    assert run_row is not None
    assert run_row.run_id in result.output
    (archive,) = r.archives()
    assert str(archive) in result.output


def test_without_a_question_the_command_asks_for_it(r: Rig) -> None:
    result = run(input_text=QUESTION + "\n" + ANSWERS + "q\n")
    assert result.exit_code == 0, result.output
    assert "Was möchten Sie recherchieren?" in result.output
    (sid,) = session_ids(r)
    assert r.service.get(sid).round == 1


def test_an_empty_question_is_refused(r: Rig) -> None:
    result = run("   ")
    assert result.exit_code == 2
    assert "Frage" in result.output
    assert session_ids(r) == []


def test_files_are_uploaded_with_the_question(r: Rig, tmp_path: Path) -> None:
    pdf = tmp_path / "bericht.pdf"
    pdf.write_bytes(make_pdf(["Diese Seite hat genug Text, damit keine OCR noetig ist."]))
    result = run(QUESTION, "--file", str(pdf), input_text=ANSWERS + "q\n")
    assert result.exit_code == 0, result.output
    (sid,) = session_ids(r)
    assert [(u.name, u.stage) for u in r.service.get(sid).uploads] == [("bericht.pdf", "distilled")]


def test_a_missing_file_is_refused_before_anything_starts(r: Rig, tmp_path: Path) -> None:
    result = run(QUESTION, "-f", str(tmp_path / "gibt-es-nicht.pdf"))
    assert result.exit_code == 2
    assert "gibt-es-nicht.pdf" in result.output
    assert session_ids(r) == []
    assert r.models.total() == 0


def test_a_missing_file_stops_before_the_service_is_even_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_be_built() -> None:
        raise AssertionError("the runtime and the models must not start for a missing file")

    monkeypatch.setattr(cli, "_brief_service", must_not_be_built)
    result = run(QUESTION, "--file", str(tmp_path / "gibt-es-nicht.pdf"))
    assert result.exit_code == 2


def test_a_rejected_upload_is_explained_and_leaves_nothing(r: Rig, tmp_path: Path) -> None:
    bad = tmp_path / "tabelle.xlsx"
    bad.write_bytes(b"PK\x03\x04")
    result = run(QUESTION, "--file", str(bad))
    assert result.exit_code == 2
    assert "tabelle.xlsx" in result.output
    assert session_ids(r) == []


# ---- listing and resuming -------------------------------------------------------------------


def test_list_shows_the_sessions_newest_first(r: Rig) -> None:
    first = r.service.start(QUESTION).session_id
    second = r.service.start(QUESTION).session_id
    result = run("--list")
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert [line.split()[0] for line in lines] == [second, first]
    for line, sid in zip(lines, (second, first), strict=True):
        assert r.service.get(sid).status in line


def test_an_empty_list_says_so(r: Rig) -> None:
    result = run("--list")
    assert result.exit_code == 0
    assert "Keine Sitzungen" in result.output


def test_a_session_can_be_resumed_where_it_waits(r: Rig) -> None:
    sid = r.service.start(QUESTION).session_id
    result = run("--session", sid, input_text=ANSWERS + "f\nl\n")
    assert result.exit_code == 0, result.output
    run_row = r.parts.runs.run_for_session(sid)
    assert run_row is not None
    assert run_row.tier == "light"


def test_an_unknown_session_is_reported(r: Rig) -> None:
    result = run("--session", "s000000000000")
    assert result.exit_code == 1
    assert "s000000000000" in result.output


@pytest.mark.parametrize("extra", [[QUESTION], ["--file", "x.pdf"], ["--list"]])
def test_a_session_cannot_be_combined_with_a_new_question_or_files(
    r: Rig, extra: list[str]
) -> None:
    result = run("--session", "s000000000000", *extra)
    assert result.exit_code == 2
    assert "kombiniert" in result.output


# ---- leaving --------------------------------------------------------------------------------


def test_ending_the_input_leaves_the_session_for_later(r: Rig) -> None:
    result = run(QUESTION, input_text="\n")  # the input ends in the middle of the questions
    assert result.exit_code == 1
    assert "Abgebrochen" in result.output
    (sid,) = session_ids(r)
    assert f"udr brief --session {sid}" in result.output
    assert r.service.get(sid).waiting_for == "questions"


def test_the_editor_is_opened_with_the_brief(r: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_edit(text: str, **kwargs: Any) -> str:
        seen.append(text)
        assert kwargs.get("extension") == ".md"
        return "# Mein Titel\n\n## Forschungsfragen\n\n1. Meine Frage\n"

    monkeypatch.setattr(cli.click, "edit", fake_edit)
    result = run(QUESTION, input_text=ANSWERS + "b\nq\n")
    assert result.exit_code == 0, result.output
    (sid,) = session_ids(r)
    assert (
        r.service.get(sid).brief_text == "# Mein Titel\n\n## Forschungsfragen\n\n1. Meine Frage\n"
    )
    assert seen


# ---- the service the command builds ---------------------------------------------------------


def test_the_command_recovers_unfinished_sessions_before_it_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    order: list[str] = []
    service = SimpleNamespace(recover=lambda: order.append("recover") or [])
    monkeypatch.setenv("UDR_DATA_DIR", str(tmp_path))
    sinks: list[Any] = []

    def build_runtime(_settings: object, events: Any) -> str:
        order.append("runtime")
        sinks.append(events)
        return "rt"

    monkeypatch.setattr(bootstrap, "build_runtime", build_runtime)
    monkeypatch.setattr(
        bootstrap, "build_brief_service", lambda rt, **k: order.append(f"service:{rt}") or service
    )
    assert cli._brief_service() is service
    assert order == ["runtime", "service:rt", "recover"]
    sinks[0].emit("brief_started")  # the events of a session go to the data directory
    assert "brief_started" in (tmp_path / "events.jsonl").read_text(encoding="utf-8")
