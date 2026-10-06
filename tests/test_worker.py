"""M6 Step 5 (AC4): the queue worker. One run at a time, first in first out, runs that wait for
an approval do not hold the slot, a run a dead worker left `running` goes first."""

import threading
from pathlib import Path

import pytest
from research_run_rig import RunRig, make_rig

from app.research.worker import WorkerLock
from app.worker import Worker


@pytest.fixture
def rig(tmp_path: Path) -> RunRig:
    return make_rig(tmp_path)


def worker_of(r: RunRig, lines: list[str] | None = None) -> Worker:
    log = lines.append if lines is not None else (lambda _line: None)
    return Worker(r.service, poll_s=0.0, events=r.events, log=log)


def status(r: RunRig, run_id: str) -> str:
    row = r.runs.get_run(run_id)
    assert row is not None
    return row.status


def test_an_idle_worker_does_nothing(rig: RunRig) -> None:
    assert worker_of(rig).run_once() is None


def test_a_second_run_starts_after_the_first_and_the_slot_is_free_while_a_plan_waits(
    rig: RunRig,
) -> None:
    first, second = rig.create().run_id, rig.create().run_id
    worker = worker_of(rig)
    assert worker.run_once() == first
    assert status(rig, first) == "awaiting_plan_approval"
    assert status(rig, second) == "queued"  # one run at a time
    assert worker.run_once() == second  # the waiting plan of the first holds no slot
    assert worker.run_once() is None
    assert status(rig, second) == "awaiting_plan_approval"


def test_an_approved_run_finishes_before_a_younger_queued_one_starts(rig: RunRig) -> None:
    first, second = rig.create().run_id, rig.create().run_id
    worker = worker_of(rig)
    worker.run_once()
    rig.service.approve_plan(first, str(rig.service.view(first).plan_sha256))
    assert worker.run_once() == first
    assert status(rig, first) == "done"
    assert status(rig, second) == "queued"
    assert worker.run_once() == second
    assert status(rig, second) == "awaiting_plan_approval"


def test_a_run_left_running_by_a_dead_worker_goes_before_an_older_queued_run(rig: RunRig) -> None:
    older, orphan = rig.create().run_id, rig.create().run_id
    rig.runs.set_status(orphan, "running")
    assert worker_of(rig).run_once() == orphan
    assert status(rig, older) == "queued"


def test_a_run_held_by_another_process_changes_nothing(rig: RunRig) -> None:
    run_id = rig.create().run_id
    with WorkerLock(rig.base / "worker.lock"):
        assert worker_of(rig).run_once() is None
    assert status(rig, run_id) == "queued"


def test_a_run_that_cannot_run_fails_once_and_is_not_picked_again(rig: RunRig) -> None:
    run_id = rig.create(tier="full").run_id  # the full tier arrives with M8
    worker = worker_of(rig)
    assert worker.run_once() == run_id
    assert status(rig, run_id) == "failed"
    assert worker.run_once() is None
    assert [e.data["run_id"] for e in rig.events.of_type("worker_run_error")] == [run_id]


def test_the_worker_says_when_a_run_starts_and_where_it_stops(rig: RunRig) -> None:
    run_id = rig.create().run_id
    lines: list[str] = []
    worker_of(rig, lines).run_once()
    assert lines == [f"Lauf {run_id}: gestartet", f"Lauf {run_id}: awaiting_plan_approval"]


class StopWhenIdle(threading.Event):
    """An event whose wait ends the loop: the first idle poll is the last."""

    def wait(self, timeout: float | None = None) -> bool:
        self.set()
        return True


def test_run_forever_works_until_idle_then_stops_on_the_event(rig: RunRig) -> None:
    run_id = rig.create().run_id
    worker_of(rig).run_forever(StopWhenIdle())
    assert status(rig, run_id) == "awaiting_plan_approval"


def test_run_forever_does_not_start_a_run_once_stopped(rig: RunRig) -> None:
    run_id = rig.create().run_id
    stop = threading.Event()
    stop.set()
    worker_of(rig).run_forever(stop)
    assert status(rig, run_id) == "queued"
