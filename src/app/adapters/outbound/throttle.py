"""Polite pacing: a minimum interval between two requests to the same host (PRD §3.3)."""

import threading
import time
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

DEFAULT_INTERVAL_S = 1.0
HOST_INTERVALS_S: Mapping[str, float] = {
    "export.arxiv.org": 3.0,  # arXiv API terms: at most one request every three seconds
    "api.openalex.org": 0.1,
    "api.crossref.org": 0.1,
    "api.tavily.com": 0.0,
}


class HostThrottle:
    """Thread-safe: concurrent callers for one host get consecutive time slots."""

    def __init__(
        self,
        intervals: Mapping[str, float] = HOST_INTERVALS_S,
        default_s: float = DEFAULT_INTERVAL_S,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._intervals = intervals
        self._default = default_s
        self._monotonic = monotonic
        self._sleep = sleep
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        host = (urlsplit(url).hostname or url).lower()
        interval = self._intervals.get(host, self._default)
        with self._lock:
            now = self._monotonic()
            start = max(now, self._next.get(host, now))
            self._next[host] = start + interval
        if start > now:
            self._sleep(start - now)
