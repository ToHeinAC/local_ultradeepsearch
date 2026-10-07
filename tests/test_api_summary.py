"""PRD M7 (D1): what the GUI shows about a run, as pure functions of the files behind it."""

from app.api.summary import StepSpan, elapsed_seconds, step_spans, title_of

T0, T1, T2, T3 = (f"2026-10-07T09:0{i}:00+00:00" for i in range(4))


def test_a_finished_and_a_running_step_become_spans() -> None:
    steps = [
        {"step": "0", "status": "running", "ts": T0},
        {"step": "0", "status": "done", "ts": T1},
        {"step": "1", "status": "running", "ts": T2},
    ]
    assert step_spans(steps) == (
        StepSpan("0", "done", T0, T1),
        StepSpan("1", "running", T2, None),
    )


def test_a_step_that_restarted_keeps_its_first_start_and_its_end() -> None:
    steps = [
        {"step": "2", "status": "running", "ts": T0},
        {"step": "2", "status": "running", "ts": T1},  # a worker died and it began again
        {"step": "2", "status": "done", "ts": T2},
    ]
    assert step_spans(steps) == (StepSpan("2", "done", T0, T2),)


def test_no_steps_no_spans() -> None:
    assert step_spans([]) == ()


def test_elapsed_is_the_distance_between_two_stamps() -> None:
    assert elapsed_seconds(T0, T3) == 180.0
    assert elapsed_seconds(T0, None) is None
    assert elapsed_seconds(None, T3) is None


def test_the_title_is_the_first_heading_of_the_brief() -> None:
    assert title_of("# Wärmepumpen\n\n1. Frage\n") == "Wärmepumpen"
    assert title_of("no heading\n## second level") is None
    assert title_of(None) is None
