"""Child process of the worker SIGKILL test (tests/test_worker_crash.py).

A worker on the rig of `research_run_rig` in ``argv[1]``: it creates one run, takes it to its plan
gate, approves the plan and works the queue. The fake search hangs in its third call. It prints
`RUN <id>` when the run exists and `HANGING ...` when the hanging call starts, so the parent can
kill it at exactly that point.
"""

import sys
import threading
from pathlib import Path

from research_run_rig import make_rig, make_searcher

from app.worker import Worker


def main(base_dir: Path) -> None:
    rig = make_rig(base_dir, searcher=make_searcher(hang_at=3))
    run_id = rig.create().run_id
    print(f"RUN {run_id}", flush=True)
    worker = Worker(rig.service, poll_s=0.01, events=rig.events)
    worker.run_once()  # to the plan gate
    rig.service.approve_plan(run_id, str(rig.service.view(run_id).plan_sha256))
    worker.run_forever(threading.Event())  # hangs in the third search until the parent kills us


if __name__ == "__main__":
    main(Path(sys.argv[1]))
