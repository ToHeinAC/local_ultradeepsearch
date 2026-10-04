"""`run.json` of a research run: its spec, status and the steps in the order they ran (AC1).

Writes go through `merge_run_json`, so the `stats` key of the fetch pipeline survives."""

from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from app.pipeline.artifacts import merge_run_json, read_run_json
from app.research.models import RunSpec


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Manifest:
    def __init__(self, run_dir: Path, now: Callable[[], datetime] = _utcnow) -> None:
        self._dir = run_dir
        self._now = now

    def _steps(self) -> list[dict[str, Any]]:
        return list(cast("list[dict[str, Any]]", read_run_json(self._dir).get("steps", [])))

    def init(self, spec: RunSpec) -> None:
        """Write the spec; steps, status and exports already recorded are kept."""
        data = read_run_json(self._dir)
        fields = {k: v for k, v in asdict(spec).items() if k not in ("brief_path",)}
        defaults: dict[str, object] = {
            "status": "queued",
            "status_reason": "",
            "steps": [],
            "exports": {},
        }
        merge_run_json(
            self._dir, {**defaults, **{k: data[k] for k in defaults if k in data}, **fields}
        )

    def start_step(self, step_id: str) -> None:
        """Mark ``step_id`` running; a step already listed (a re-run) keeps its place."""
        steps = self._steps()
        entry = next((s for s in steps if s["id"] == step_id), None)
        if entry is None:
            steps.append(
                {
                    "id": step_id,
                    "status": "running",
                    "started_at": self._stamp(),
                    "finished_at": None,
                }
            )
        else:
            entry["status"] = "running"
        merge_run_json(self._dir, {"steps": steps})

    def finish_step(self, step_id: str) -> None:
        steps = self._steps()
        entry = next((s for s in steps if s["id"] == step_id), None)
        if entry is None:
            raise ValueError(f"step {step_id} was not started")
        entry.update(status="done", finished_at=self._stamp())
        merge_run_json(self._dir, {"steps": steps})

    def step_ids(self) -> list[str]:
        return [str(s["id"]) for s in self._steps()]

    def set_status(self, status: str, reason: str = "") -> None:
        merge_run_json(self._dir, {"status": status, "status_reason": reason})

    def set_exports(self, exports: Mapping[str, object]) -> None:
        merge_run_json(self._dir, {"exports": dict(exports)})

    def _stamp(self) -> str:
        return self._now().isoformat()
