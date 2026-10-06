"""M6 D5, D7: Phase-1 model work in the background. Validation stays synchronous; the graph call
is handed to a job runner and the session is read by polling."""

import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from brief_rig import QUESTION, Models, Rig, kinds, rig

from app.api.jobs import SessionJobs
from app.brief.errors import InvalidInput, WrongState
from app.llm.errors import LLMModelMissingError


class Queue:
    """A job runner that holds the jobs until the test runs them: no threads, no timing."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, Callable[[], str | None]]] = []

    def __call__(self, session_id: str, action: Callable[[], str | None]) -> None:
        self.jobs.append((session_id, action))

    def run_all(self) -> list[str | None]:
        done = [action() for _sid, action in self.jobs]
        self.jobs.clear()
        return done


@pytest.fixture
def queue() -> Queue:
    return Queue()


@pytest.fixture
def r(tmp_path: Path, queue: Queue) -> Rig:
    return rig(tmp_path, background=queue)


def test_start_returns_before_any_model_call_and_the_job_reaches_the_questions(
    r: Rig, queue: Queue
) -> None:
    view = r.service.start(QUESTION)
    assert view.waiting_for == "work"
    assert r.models.total() == 0
    assert [sid for sid, _ in queue.jobs] == [view.session_id]
    queue.run_all()
    after = r.service.get(view.session_id)
    assert after.waiting_for == "questions"
    assert r.models.total() > 0


def test_an_answer_is_validated_at_once_and_the_graph_call_is_a_job(r: Rig, queue: Queue) -> None:
    sid = r.service.start(QUESTION).session_id
    queue.run_all()
    with pytest.raises(InvalidInput, match="answer"):
        r.service.answer(sid, kinds("accept"))  # two questions are open
    assert queue.jobs == []
    calls = r.models.total()
    r.service.answer(sid, kinds("accept", "accept"))
    assert r.models.total() == calls
    assert len(queue.jobs) == 1
    queue.run_all()
    assert r.service.get(sid).waiting_for == "decision"


def test_a_model_error_comes_back_from_the_job_as_its_result(tmp_path: Path, queue: Queue) -> None:
    models = Models(errors={"assess": LLMModelMissingError("gone")})
    r = rig(tmp_path, models, background=queue)
    r.service.start(QUESTION)
    (error,) = queue.run_all()
    assert error is not None
    assert "LLMModelMissingError" in error


def test_retry_in_background_mode_is_a_job_too(tmp_path: Path, queue: Queue) -> None:
    models = Models(errors={"assess": LLMModelMissingError("gone")})
    r = rig(tmp_path, models, background=queue)
    sid = r.service.start(QUESTION).session_id
    queue.run_all()
    models.errors.clear()
    assert r.service.retry(sid).waiting_for == "work"
    assert len(queue.jobs) == 1
    queue.run_all()
    assert r.service.get(sid).waiting_for == "decision"  # the second assessment is the last one


def test_the_approval_is_never_a_job_so_the_caller_gets_the_run_at_once(
    r: Rig, queue: Queue
) -> None:
    sid = r.service.start(QUESTION).session_id
    queue.run_all()
    r.service.answer(sid, kinds("accept", "accept"))
    queue.run_all()
    view = r.service.get(sid)
    assert view.brief_sha256 is not None
    approved = r.service.approve(sid, view.brief_sha256, "light")
    assert approved.run_id is not None
    assert queue.jobs == []


def test_a_summarize_model_outside_the_configured_ones_is_refused(tmp_path: Path) -> None:
    r = rig(tmp_path, summarize_models=("gemma4:e4b", "gemma4:e2b"))
    sid = r.service.start(QUESTION).session_id
    view = r.service.answer(sid, kinds("accept", "accept"))
    assert view.brief_sha256 is not None
    with pytest.raises(InvalidInput, match="summarize_model"):
        r.service.approve(sid, view.brief_sha256, "light", "gemma4:e9b")
    assert r.service.approve(sid, view.brief_sha256, "light", "gemma4:e2b").run_id is not None


# ---- SessionJobs ----------------------------------------------------------------------------


def test_a_job_runs_in_a_thread_and_the_session_is_busy_meanwhile() -> None:
    jobs = SessionJobs(threads=1)
    release = threading.Event()

    def hold() -> str | None:
        release.wait(5)
        return None

    jobs.submit("s-1", hold)
    assert jobs.busy("s-1")
    with pytest.raises(WrongState, match="busy"):
        jobs.submit("s-1", lambda: None)
    assert not jobs.busy("s-2")
    release.set()
    assert jobs.wait_idle()
    assert not jobs.busy("s-1")
    jobs.shutdown()


def test_the_error_of_a_job_is_kept_until_the_next_job_starts() -> None:
    jobs = SessionJobs(threads=1)
    jobs.submit("s-1", lambda: "ModelError: down")
    jobs.wait_idle()
    assert jobs.error("s-1") == "ModelError: down"

    def boom() -> str | None:
        raise RuntimeError("bug")

    jobs.submit("s-1", boom)
    jobs.wait_idle()
    assert jobs.error("s-1") == "RuntimeError: bug"
    jobs.submit("s-1", lambda: None)
    jobs.wait_idle()
    assert jobs.error("s-1") is None
    jobs.shutdown()


def test_two_sessions_work_at_the_same_time() -> None:
    jobs = SessionJobs(threads=2)
    both = threading.Barrier(2, timeout=5)
    results: list[int] = []

    def meet() -> str | None:
        results.append(both.wait())
        return None

    jobs.submit("s-1", meet)
    jobs.submit("s-2", meet)
    assert jobs.wait_idle()
    assert sorted(results) == [0, 1]  # each waited for the other: they ran together
    jobs.shutdown()
