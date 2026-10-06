"""Run events: what happened, when, at which level. Never prompts, answers or thinking."""

import json
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from app.artifacts import scrub_think

Level = Literal["info", "warning", "error"]


@dataclass(frozen=True)
class Event:
    ts: str
    type: str
    level: Level
    data: dict[str, Any] = field(default_factory=lambda: {})


class EventSink(Protocol):
    def emit(self, type: str, *, level: Level = "info", **data: Any) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MemoryEventSink:
    """Collects events in a list; used by tests and short-lived commands."""

    def __init__(self, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now
        self.events: list[Event] = []

    def emit(self, type: str, *, level: Level = "info", **data: Any) -> None:
        self.events.append(Event(self._now().isoformat(), type, level, scrub_think(data)))

    def of_type(self, type: str) -> list[Event]:
        return [e for e in self.events if e.type == type]


class JsonlEventSink:
    """Appends one JSON line per event to ``path`` (`events.jsonl`). Thread-safe."""

    def __init__(self, path: Path, now: Callable[[], datetime] = _utcnow) -> None:
        self._path = path
        self._now = now
        self._lock = threading.Lock()

    def emit(self, type: str, *, level: Level = "info", **data: Any) -> None:
        record = {
            "ts": self._now().isoformat(),
            "type": type,
            "level": level,
            "data": scrub_think(data),
        }
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line)


RUN_EVENTS_FILE = "events.jsonl"


class RunScopedEventSink:
    """Wraps the global sink. While a run is bound, every event (the model telemetry included)
    carries the run's id and is also appended to ``<run_dir>/events.jsonl`` (PRD M6).

    One run executes per process, so the binding is a plain attribute under a lock, not a
    context variable: the fetch pipeline emits from a thread pool."""

    def __init__(self, inner: EventSink) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self._bound: tuple[str, JsonlEventSink] | None = None

    @contextmanager
    def bound(self, run_id: str, run_dir: Path) -> Generator[None]:
        with self._lock:
            self._bound = (run_id, JsonlEventSink(run_dir / RUN_EVENTS_FILE))
        try:
            yield
        finally:
            with self._lock:
                self._bound = None

    def emit(self, type: str, *, level: Level = "info", **data: Any) -> None:
        with self._lock:
            bound = self._bound
        if bound is None:
            self._inner.emit(type, level=level, **data)
            return
        run_id, run_sink = bound
        data = {**data, "run_id": run_id}
        self._inner.emit(type, level=level, **data)
        run_sink.emit(type, level=level, **data)


def read_run_events(run_dir: Path, after: int) -> tuple[list[dict[str, Any]], int]:
    """The events of a run after the first ``after`` lines, and the new cursor (the number of
    complete lines read). A half-written last line is left for the next call."""
    path = run_dir / RUN_EVENTS_FILE
    if not path.exists():
        return [], after
    text = path.read_text(encoding="utf-8")
    complete = text.split("\n")[:-1]  # the piece after the last newline is not a whole line
    events = [json.loads(line) for line in complete[after:] if line.strip()]
    return events, max(after, len(complete))
