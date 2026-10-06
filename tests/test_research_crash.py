"""Real SIGKILLs of a Lite run (PRD AD10, M5): during the sweep and during drafting. A fresh
process continues from what the dead one stored and repeats nothing it had finished."""

import signal
import subprocess
import sys
import threading
from pathlib import Path

from research_run_rig import make_rig

from app.research.manifest import LIGHT_STEPS, done_steps

CHILD = Path(__file__).parent / "research_crash_child.py"
CHILD_DEADLINE_S = 20


def kill_child_while_hanging(base_dir: Path, mode: str) -> str:
    """Run the child until its call hangs, SIGKILL it there; returns the run id."""
    child = subprocess.Popen(
        [sys.executable, str(CHILD), str(base_dir), mode], stdout=subprocess.PIPE, text=True
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


def reference(base_dir: Path) -> tuple[list[tuple[str, str]], int]:
    """The searches and section calls of the same run, uninterrupted."""
    rig = make_rig(base_dir)
    run_id = rig.create().run_id
    rig.service.run(run_id)
    rig.service.approve_and_run(run_id, str(rig.service.view(run_id).plan_sha256))
    return rig.searcher.calls, rig.models.count("text")


def test_a_sigkill_in_the_third_search_repeats_none_of_the_stored_searches(tmp_path: Path) -> None:
    all_calls, _ = reference(tmp_path / "reference")
    run_id = kill_child_while_hanging(tmp_path / "killed", "sweep")
    fresh = make_rig(tmp_path / "killed")
    assert fresh.runs.get_run(run_id).status == "running"  # type: ignore[union-attr]
    view = fresh.service.run(run_id)
    assert view.status == "done"
    assert fresh.searcher.calls == all_calls[2:]  # the third again, never the first two
    assert done_steps(fresh.run_dir(run_id)) == list(LIGHT_STEPS)
    assert fresh.models.count("DecompositionDraft") == 0  # step 1 was kept


def test_a_sigkill_in_the_third_section_keeps_the_first_two(tmp_path: Path) -> None:
    _, all_sections = reference(tmp_path / "reference")
    run_id = kill_child_while_hanging(tmp_path / "killed", "draft")
    fresh = make_rig(tmp_path / "killed")
    view = fresh.service.run(run_id)
    assert view.status == "done"
    assert fresh.models.count("text") == all_sections - 2  # sections one and two were saved
    assert fresh.searcher.calls == []  # every search was stored before the kill
    assert done_steps(fresh.run_dir(run_id)) == list(LIGHT_STEPS)
