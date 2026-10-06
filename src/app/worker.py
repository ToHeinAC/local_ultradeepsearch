"""The queue worker (PRD M6): `udr worker` takes the next run from the `runs` table and executes
it, one at a time, first in first out. A run that waits for an approval is not in the queue; a run
a dead worker left `running` goes first, so a restart resumes it from its last checkpoint."""

import threading
from collections.abc import Callable

from app.events import EventSink
from app.research.errors import WorkerBusy
from app.research.service import ResearchService


class Worker:
    def __init__(
        self,
        research: ResearchService,
        *,
        poll_s: float,
        events: EventSink,
        log: Callable[[str], None] = lambda _line: None,
    ) -> None:
        self._research = research
        self._poll_s = poll_s
        self._events = events
        self._log = log

    def run_once(self) -> str | None:
        """Take the next run and carry it as far as it goes. Returns its id, or `None` when there
        is nothing to do or another process (a `udr run`) holds the worker slot."""
        run_id = self._research.next_runnable()
        if run_id is None:
            return None
        self._log(f"Lauf {run_id}: gestartet")
        try:
            view = self._research.run(run_id)
        except WorkerBusy:
            return None
        except Exception as exc:  # a run that cannot run must not be picked again and again
            self._research.give_up(run_id, f"{type(exc).__name__}: {exc}")
            self._events.emit("worker_run_error", level="error", run_id=run_id, reason=str(exc))
            self._log(f"Lauf {run_id}: failed")
            return run_id
        self._log(f"Lauf {run_id}: {view.status}")
        return run_id

    def run_forever(self, stop: threading.Event) -> None:
        """Work until ``stop`` is set. The run in flight when the process is told to end is left
        `running`; the next start resumes it."""
        while not stop.is_set():
            if self.run_once() is None:
                stop.wait(self._poll_s)
