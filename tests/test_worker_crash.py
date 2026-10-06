"""M6 AC5: a real SIGKILL of a worker process during the sweep. A fresh worker resumes the run
from its last checkpoint and no step that had finished starts again."""

import signal
import subprocess
import sys
import threading
from collections import Counter
from pathlib import Path

from research_run_rig import RunRig, make_rig

from app.research.manifest import LIGHT_STEPS, done_steps, read_run_json
from app.worker import Worker

CHILD = Path(__file__).parent / "worker_crash_child.py"
CHILD_DEADLINE_S = 20


def kill_worker_while_hanging(base_dir: Path) -> str:
    """Run the worker child until its search hangs, SIGKILL it there; returns the run id."""
    child = subprocess.Popen(
        [sys.executable, str(CHILD), str(base_dir)], stdout=subprocess.PIPE, text=True
    )
    watchdog = threading.Timer(CHILD_DEADLINE_S, child.kill)  # a hung child must not hang the suite
    watchdog.start()
    run_id = ""
    try:
        assert child.stdout is not None
        for line in child.stdout:
            if line.startswith("RUN "):
                run_id = line.split()[1]
            if line.startswith("HANGING"):
                break
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=5)
    finally:
        watchdog.cancel()
        child.kill()
        child.wait()
    assert child.returncode == -signal.SIGKILL
    assert run_id
    return run_id


def starts(r: RunRig, run_id: str) -> Counter[str]:
    steps = read_run_json(r.run_dir(run_id)).get("steps", [])
    return Counter(s["step"] for s in steps if s["status"] == "running")


def test_a_worker_killed_in_the_sweep_is_resumed_without_repeating_a_finished_step(
    tmp_path: Path,
) -> None:
    run_id = kill_worker_while_hanging(tmp_path)
    fresh = make_rig(tmp_path)
    finished_before = done_steps(fresh.run_dir(run_id))
    assert finished_before == ["0", "1", "2.1"]
    before = starts(fresh, run_id)
    row = fresh.runs.get_run(run_id)
    assert row is not None
    assert row.status == "running"  # the orphan of the dead worker
    worker = Worker(fresh.service, poll_s=0.0, events=fresh.events)
    assert worker.run_once() == run_id
    assert fresh.runs.get_run(run_id).status == "done"  # type: ignore[union-attr]
    assert done_steps(fresh.run_dir(run_id)) == list(LIGHT_STEPS)
    after = starts(fresh, run_id)
    assert all(after[step] == before[step] for step in finished_before)
    assert after["2"] == before["2"] + 1  # the cut step began again, once
    assert fresh.models.count("DecompositionDraft") == 0
    assert worker.run_once() is None
