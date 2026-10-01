"""Run events: what happened, when, at which level. Never prompts, answers or thinking."""

import json
import threading
from collections.abc import Callable
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
