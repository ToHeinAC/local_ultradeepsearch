"""The outbound log (PRD §3.2): one JSON line per network attempt and per blocked attempt."""

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast


@dataclass(frozen=True)
class OutboundRecord:
    step: str
    provider: str
    status: str  # HTTP code, "ok", "blocked:<reason>" or "error:<kind>"
    original_query: str | None = None  # stays on this machine; shown in the GUI
    sent_query: str | None = None
    removed_terms: tuple[str, ...] = ()
    url: str | None = None
    credits: int = 0


class OutboundLog:
    """Appends records to ``path`` (`outbound.jsonl`). Thread-safe; every field always present."""

    def __init__(self, path: Path, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._path = path
        self._now = now
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def total_credits(self) -> int:
        """Credits recorded so far, so a resumed run keeps its spending. A line cut off by a crash
        and anything that is not a record with an integer `credits` is ignored."""
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return 0
        total = 0
        for line in lines:
            try:
                record: object = json.loads(line)
            except json.JSONDecodeError:
                continue
            credits = (
                cast("dict[str, object]", record).get("credits")
                if isinstance(record, dict)
                else None
            )
            total += credits if isinstance(credits, int) else 0
        return total

    def write(self, record: OutboundRecord) -> None:
        line = {
            "ts": self._now().isoformat(),
            "step": record.step,
            "provider": record.provider,
            "original_query": record.original_query,
            "sent_query": record.sent_query,
            "removed_terms": list(record.removed_terms),
            "url": record.url,
            "status": record.status,
            "credits": record.credits,
        }
        text = json.dumps(line, ensure_ascii=False) + "\n"
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(text)
