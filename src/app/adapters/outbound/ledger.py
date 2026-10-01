"""Tavily credit accounting (PRD §3.3): prices, a per-run cap and a per-month ledger on disk."""

import fcntl
import json
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

Depth = Literal["basic", "advanced"]
WARN_FRACTION = 0.8


def search_credits(depth: Depth = "basic") -> int:
    return 2 if depth == "advanced" else 1


def extract_credits(successful_urls: int, depth: Depth = "basic") -> int:
    """Tavily charges per started block of 5 successful extractions; failures are free."""
    per_block = 2 if depth == "advanced" else 1
    return math.ceil(successful_urls / 5) * per_block


class RunLedger:
    """Credits spent by one run, against the cap of its profile (light 60, full 300)."""

    def __init__(self, cap: int, used: int = 0) -> None:
        self.cap = cap
        self.used = used  # a resumed run starts with what it has already spent
        self._lock = threading.Lock()

    def can_afford(self, credits: int) -> bool:
        with self._lock:
            return self.used + credits <= self.cap

    def charge(self, credits: int) -> None:
        with self._lock:
            self.used += credits


@dataclass(frozen=True)
class ChargeResult:
    total: int
    crossed_warning: bool  # this charge moved the month from below 80 % to 80 % or more


class MonthLedger:
    """Credits spent this calendar month, shared by every process via ``path`` and a file lock."""

    def __init__(
        self, path: Path, limit: int, now: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._path = path
        self.limit = limit
        self._now = now
        self._lock = threading.Lock()  # flock is per open file; this serialises our own threads

    def _month(self) -> str:
        return self._now().strftime("%Y-%m")

    def _read(self, text: str) -> int:
        """Credits recorded for the current month; 0 for another month or an unreadable file."""
        try:
            data: object = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            return 0
        if not isinstance(data, dict):
            return 0
        record = cast("dict[str, object]", data)
        credits = record.get("credits")
        same_month = record.get("month") == self._month()
        return credits if same_month and isinstance(credits, int) else 0

    def used(self) -> int:
        try:
            return self._read(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 0

    def at_limit(self) -> bool:
        return self.used() >= self.limit

    def charge(self, credits: int) -> ChargeResult:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0)
            before = self._read(handle.read())
            total = before + credits
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({"month": self._month(), "credits": total}))
            handle.flush()
        threshold = self.limit * WARN_FRACTION
        return ChargeResult(total, before < threshold <= total)
