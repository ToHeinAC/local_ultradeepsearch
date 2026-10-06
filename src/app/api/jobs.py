"""Phase-1 model work in the API process (PRD M6 D5, D7): a small thread pool, at most one job
per session. The API call returns at once and the caller polls the session."""

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from app.brief.errors import WrongState


class SessionJobs:
    def __init__(self, threads: int) -> None:
        self._pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="session-job")
        self._cond = threading.Condition()
        self._busy: set[str] = set()
        self._errors: dict[str, str] = {}

    def submit(self, session_id: str, action: Callable[[], str | None]) -> None:
        """Run ``action`` in the background. It returns the error text of a model failure, or
        `None`. A second job for a session that is busy is `WrongState`."""
        with self._cond:
            if session_id in self._busy:
                raise WrongState("the session is busy with an earlier request")
            self._busy.add(session_id)
            self._errors.pop(session_id, None)
        self._pool.submit(self._work, session_id, action)

    def _work(self, session_id: str, action: Callable[[], str | None]) -> None:
        error: str | None
        try:
            error = action()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._cond:
                self._busy.discard(session_id)
                self._cond.notify_all()
        if error is not None:
            with self._cond:
                self._errors[session_id] = error

    def busy(self, session_id: str) -> bool:
        with self._cond:
            return session_id in self._busy

    def error(self, session_id: str) -> str | None:
        with self._cond:
            return self._errors.get(session_id)

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Wait until no job runs (tests, shutdown). False on a timeout."""
        with self._cond:
            return self._cond.wait_for(lambda: not self._busy, timeout)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)
