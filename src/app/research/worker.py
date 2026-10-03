"""The worker slot (PRD A3): one active research run at a time.

A lock file held only while a graph executes. A run that waits for the owner's approval of its
search plan does not hold it. The operating system frees the lock when its holder dies, so a
killed run never blocks the next one. M6's worker takes the same lock over.
"""

import fcntl
from pathlib import Path
from types import TracebackType
from typing import TextIO

from app.research.errors import WorkerBusy


class WorkerLock:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._handle: TextIO | None = None

    def __enter__(self) -> "WorkerLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise WorkerBusy("another run is active") from exc
        self._handle = handle
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle, fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None
