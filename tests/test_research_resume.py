"""Kill and resume up to the width sweep (PRD AD10, M5 A8): in-process crashes and a real SIGKILL.
Nothing is sanitized, searched, fetched or drafted twice."""

import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fixtures_corpus import SimulatedCrash
from research_rig import (
    PLAN,
    FakeGateway,
    ResearchModels,
    ResearchRig,
    build_research_rig,
    corpus_hits,
)

QUERIES = [q["query"] for q in PLAN]
CHILD = Path(__file__).parent / "research_crash_child.py"
CHILD_DEADLINE_S = 15


def crash_then_restart(
    tmp_path: Path, models: ResearchModels, gateway: FakeGateway, *, approve: bool
) -> tuple[ResearchRig, str]:
    """Run until the simulated crash, then build a fresh process on the same files."""
    first = build_research_rig(tmp_path, models=models, gateway=gateway)
    run_id = first.new_run()

    def drive() -> None:
        view = first.service.start(run_id)
        if approve and view.plan is not None:
            first.service.approve_plan(run_id, view.plan.plan_sha256)

    with pytest.raises(SimulatedCrash):
        drive()
    return build_research_rig(tmp_path), run_id


def approve(rig: ResearchRig, run_id: str) -> None:
    plan = rig.service.plan(run_id)
    rig.service.approve_plan(run_id, plan.plan_sha256)


# ---- a crash while sanitizing ---------------------------------------------------------------


def test_a_crash_after_the_second_sanitized_query_sanitizes_only_the_rest(tmp_path: Path) -> None:
    gateway = FakeGateway(web_hits=corpus_hits(), crash_prepare_on=3)
    fresh, run_id = crash_then_restart(tmp_path, ResearchModels(), gateway, approve=False)
    assert fresh.service.resume(run_id).waiting_for == "plan"
    assert fresh.models.count("plan") == 0  # the drafted plan was kept
    assert fresh.models.count("decompose") == 0  # and so was step 1
    assert fresh.gateway.prepared == QUERIES[2:]  # the first two were already sanitized
    rows = fresh.service.plan(run_id).rows
    assert [r.state for r in rows] == ["planned"] * len(PLAN)


# ---- a crash while searching ----------------------------------------------------------------


def test_a_crash_after_the_third_search_does_not_repeat_the_first_three(tmp_path: Path) -> None:
    gateway = FakeGateway(web_hits=corpus_hits(), crash_search_on=4)
    fresh, run_id = crash_then_restart(tmp_path, ResearchModels(), gateway, approve=True)
    assert fresh.service.get(run_id).status == "running"
    done = fresh.service.resume(run_id)
    assert done.status == "done"
    assert [sent for sent, _, _ in fresh.gateway.web_calls] == QUERIES[3:]
    assert gateway.credits + fresh.gateway.credits == len(PLAN)  # one credit per query, no more
    assert [r.state for r in fresh.store.rows(run_id, wave=1)] == ["done"] * len(PLAN)


# ---- a crash while drafting wave 2 ----------------------------------------------------------


def test_a_crash_in_wave_two_drafting_repeats_neither_searches_nor_fetches(
    tmp_path: Path,
) -> None:
    hits = corpus_hits()
    hits[QUERIES[2]] = hits[QUERIES[2]][:1]  # item i03 finds one source only: thin
    models = ResearchModels(crash_on={"wave2": 1})
    gateway = FakeGateway(web_hits=hits)
    fresh, run_id = crash_then_restart(tmp_path, models, gateway, approve=True)
    assert len(gateway.web_calls) == len(PLAN)  # wave 1 was complete before the crash
    done = fresh.service.resume(run_id)
    assert done.status == "done"
    assert fresh.gateway.web_calls == []
    assert fresh.builts[run_id].fetcher.calls == []  # the kept queue: every source is stored
    assert fresh.models.count("wave2") == 1  # drafted now, as no wave-2 row existed


# ---- a real SIGKILL -------------------------------------------------------------------------


def kill_child_in_the_fourth_search(base_dir: Path) -> list[str]:
    child = subprocess.Popen(
        [sys.executable, str(CHILD), str(base_dir)], stdout=subprocess.PIPE, text=True
    )
    watchdog = threading.Timer(CHILD_DEADLINE_S, child.kill)  # a hung child must not hang the suite
    watchdog.start()
    printed: list[str] = []
    try:
        assert child.stdout is not None
        for line in child.stdout:
            printed.append(line.strip())
            if line.startswith("SEARCH") and sum(x.startswith("SEARCH") for x in printed) == 4:
                break
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=5)
    finally:
        watchdog.cancel()
        child.kill()
        child.wait()
    assert child.returncode == -signal.SIGKILL
    return printed


def test_a_sigkilled_run_is_resumed_without_repeating_a_search(tmp_path: Path) -> None:
    printed = kill_child_in_the_fourth_search(tmp_path)
    run_id = printed[0].removeprefix("RUN ")
    assert [p.removeprefix("SEARCH ") for p in printed[1:]] == QUERIES[:4]

    fresh = build_research_rig(tmp_path)
    left = fresh.service.get(run_id)
    assert (left.status, left.waiting_for) == ("running", "work")
    states = [r.state for r in fresh.store.rows(run_id, wave=1)]
    assert states == ["done"] * 3 + ["planned"] * (len(PLAN) - 3)
    done = fresh.service.resume(run_id)
    assert done.status == "done"
    assert [sent for sent, _, _ in fresh.gateway.web_calls] == QUERIES[3:]  # the 4th again, no more
    assert fresh.models.calls == []  # not one model call of steps 1 and 2.1 again
