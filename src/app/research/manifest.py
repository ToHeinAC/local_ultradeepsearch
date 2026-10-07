"""`run.json`: the settings a run was started with and every step transition (PRD M5, AD9, AD10).

It shares the file with the vault statistics (`stats` key), so every write merges instead of
replacing. The database holds the run's status; this file is the readable record.
"""

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from app.pipeline.artifacts import merge_run_json
from app.pipeline.profiles import ResponseFormatName

LIGHT_STEPS = ("0", "1", "2.1", "2", "10", "15", "16", "G", "X")
_STEPS_LOCK = threading.Lock()  # one read-modify-write of the step list at a time


class RunSettings(BaseModel):
    """What the run was approved with; later steps read only this, never the session."""

    model_config = ConfigDict(frozen=True)

    report_language: str = Field(pattern=r"^[a-z]{2}$")
    response_format: ResponseFormatName
    template_id: str = Field(min_length=1)
    interview_language: str = Field(pattern=r"^[a-z]{2}$")
    tier: Literal["light", "full"]
    summarize_model: str | None
    tavily_cap: int | None = Field(default=None, ge=0)  # None: the tier's credit cap


def read_run_json(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "run.json"
    if not path.exists():
        return {}
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} (run.json) must hold a JSON object")
    return cast("dict[str, Any]", loaded)


def write_settings(run_dir: Path, settings: RunSettings) -> None:
    merge_run_json(run_dir, {"settings": settings.model_dump(mode="json")})


def read_settings(run_dir: Path) -> RunSettings:
    data = read_run_json(run_dir)
    if "settings" not in data:
        raise FileNotFoundError(f"{run_dir / 'run.json'} has no settings: step 0 has not run")
    return RunSettings.model_validate(data["settings"])


def _steps(data: dict[str, Any]) -> list[dict[str, str]]:
    return list(data.get("steps", []))


def done_steps(run_dir: Path) -> list[str]:
    return [s["step"] for s in _steps(read_run_json(run_dir)) if s["status"] == "done"]


def begin_step(run_dir: Path, step: str, now: datetime) -> bool:
    """Record that ``step`` runs. False if it already finished: the caller skips it."""
    with _STEPS_LOCK:
        steps = _steps(read_run_json(run_dir))
        if any(s["step"] == step and s["status"] == "done" for s in steps):
            return False
        steps.append({"step": step, "status": "running", "ts": now.isoformat()})
        merge_run_json(run_dir, {"steps": steps})
    return True


def finish_step(run_dir: Path, step: str, now: datetime) -> None:
    with _STEPS_LOCK:
        steps = _steps(read_run_json(run_dir))
        if any(s["step"] == step and s["status"] == "done" for s in steps):
            return
        steps.append({"step": step, "status": "done", "ts": now.isoformat()})
        merge_run_json(run_dir, {"steps": steps})


def record_failure(run_dir: Path, step: str, reason: str, now: datetime) -> None:
    merge_run_json(run_dir, {"failure": {"step": step, "reason": reason, "ts": now.isoformat()}})
