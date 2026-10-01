import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.artifacts import scrub_think, write_json, write_text


class Note(BaseModel):
    title: str
    body: str


def test_write_text_creates_parents_and_writes_utf8(tmp_path: Path) -> None:
    target = tmp_path / "runs" / "r1" / "report.md"
    write_text(target, "Größe ß\n")
    assert target.read_bytes() == "Größe ß\n".encode()


def test_write_text_leaves_no_temp_files(tmp_path: Path) -> None:
    write_text(tmp_path / "a.md", "x")
    assert [p.name for p in tmp_path.iterdir()] == ["a.md"]


def test_write_text_never_persists_thinking(tmp_path: Path) -> None:
    target = tmp_path / "t.md"
    write_text(target, "a <think>secret plan</think> b")
    assert target.read_text(encoding="utf-8") == "a  b"  # whitespace is not normalised


def test_write_text_can_skip_scrubbing_for_byte_exact_files(tmp_path: Path) -> None:
    target = tmp_path / "brief.md"
    verbatim = "  keeps <think>this</think> exactly \n\n"
    write_text(target, verbatim, scrub=False)
    assert target.read_text(encoding="utf-8") == verbatim


def test_write_json_scrubs_nested_values(tmp_path: Path) -> None:
    target = tmp_path / "x.json"
    write_json(target, {"a": ["<think>x</think>y", {"b": "<think>z</think>"}], "n": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": ["y", {"b": ""}], "n": 1}


def test_write_json_accepts_pydantic_models_and_keeps_unicode(tmp_path: Path) -> None:
    target = tmp_path / "n.json"
    write_json(target, Note(title="Größe", body="<think>q</think>ok"))
    text = target.read_text(encoding="utf-8")
    assert "Größe" in text
    assert json.loads(text) == {"title": "Größe", "body": "ok"}
    assert text.endswith("\n")


def test_failed_json_write_keeps_the_old_file_and_leaves_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "keep.json"
    write_json(target, {"v": 1})
    with pytest.raises(TypeError):
        write_json(target, {"v": object()})
    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["keep.json"]


def test_scrub_think_leaves_non_strings_alone() -> None:
    assert scrub_think({"a": 1, "b": None, "c": [True, 2.5]}) == {
        "a": 1,
        "b": None,
        "c": [True, 2.5],
    }


def test_failure_during_replace_keeps_the_old_file_and_cleans_the_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "keep.md"
    write_text(target, "old")

    def disk_full(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("app.artifacts.os.replace", disk_full)
    with pytest.raises(OSError, match="disk full"):
        write_text(target, "new")
    monkeypatch.undo()
    assert target.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["keep.md"]
