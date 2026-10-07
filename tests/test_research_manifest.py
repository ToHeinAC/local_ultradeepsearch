"""`run.json`: the run's settings and every step transition (PRD M5 step 0, AD9)."""

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.pipeline.artifacts import write_run_stats
from app.research.manifest import (
    LIGHT_STEPS,
    RunSettings,
    begin_step,
    done_steps,
    finish_step,
    read_settings,
    record_failure,
    write_settings,
)
from app.store.models import RunStats

NOW = datetime(2026, 10, 3, 8, 0, 0, tzinfo=UTC)
SETTINGS = RunSettings(
    report_language="de",
    response_format="structured",
    template_id="auto",
    interview_language="de",
    tier="light",
    summarize_model=None,
)


def test_the_light_sequence_is_the_prd_one() -> None:
    assert LIGHT_STEPS == ("0", "1", "2.1", "2", "10", "15", "16", "G", "X")


def test_settings_round_trip_next_to_other_keys(tmp_path: Path) -> None:
    write_run_stats(
        tmp_path,
        RunStats({}, 0, {}, 0, 0, 0.0, 0, 0),
    )
    write_settings(tmp_path, SETTINGS)
    assert read_settings(tmp_path) == SETTINGS
    assert "stats" in json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))


def test_settings_that_were_never_written_are_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_settings(tmp_path)


def test_the_tavily_cap_defaults_to_the_tiers_and_cannot_be_negative() -> None:
    assert SETTINGS.tavily_cap is None
    with pytest.raises(ValidationError):
        RunSettings(**{**SETTINGS.model_dump(), "tavily_cap": -1})
    assert RunSettings(**{**SETTINGS.model_dump(), "tavily_cap": 0}).tavily_cap == 0


def test_settings_are_validated() -> None:
    with pytest.raises(ValidationError):
        RunSettings(
            report_language="deutsch",
            response_format="structured",
            template_id="auto",
            interview_language="de",
            tier="light",
            summarize_model=None,
        )


def test_steps_are_recorded_as_transitions(tmp_path: Path) -> None:
    assert begin_step(tmp_path, "0", NOW)
    finish_step(tmp_path, "0", NOW)
    assert begin_step(tmp_path, "1", NOW)
    steps = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))["steps"]
    assert [(s["step"], s["status"]) for s in steps] == [
        ("0", "running"),
        ("0", "done"),
        ("1", "running"),
    ]
    assert steps[0]["ts"] == NOW.isoformat()
    assert done_steps(tmp_path) == ["0"]


def test_a_finished_step_is_not_begun_or_recorded_twice(tmp_path: Path) -> None:
    begin_step(tmp_path, "0", NOW)
    finish_step(tmp_path, "0", NOW)
    assert not begin_step(tmp_path, "0", NOW)
    finish_step(tmp_path, "0", NOW)
    assert done_steps(tmp_path) == ["0"]
    assert len(json.loads((tmp_path / "run.json").read_text("utf-8"))["steps"]) == 2


def test_beginning_an_interrupted_step_again_adds_a_running_entry(tmp_path: Path) -> None:
    begin_step(tmp_path, "2", NOW)
    assert begin_step(tmp_path, "2", NOW)
    steps = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))["steps"]
    assert [s["status"] for s in steps] == ["running", "running"]
    assert done_steps(tmp_path) == []


def test_a_failure_is_recorded_with_its_reason(tmp_path: Path) -> None:
    record_failure(tmp_path, "2", "model unavailable", NOW)
    data = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert data["failure"] == {"step": "2", "reason": "model unavailable", "ts": NOW.isoformat()}


def test_parallel_writers_never_lose_a_transition(tmp_path: Path) -> None:
    steps = [f"s{i}" for i in range(12)]
    threads = [threading.Thread(target=begin_step, args=(tmp_path, s, NOW)) for s in steps]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    recorded = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))["steps"]
    assert sorted(s["step"] for s in recorded) == sorted(steps)
