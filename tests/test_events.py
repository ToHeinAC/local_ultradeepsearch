import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.events import (
    Event,
    JsonlEventSink,
    MemoryEventSink,
    RunScopedEventSink,
    read_run_events,
)

FIXED = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


def read_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_jsonl_sink_writes_one_line_per_event(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "events.jsonl"
    sink = JsonlEventSink(path, now=lambda: FIXED)
    sink.emit("llm_call", role="reason", tokens=12)
    sink.emit("ollama_fail_open", level="warning", reason="timeout")
    assert read_lines(path) == [
        {
            "ts": "2026-10-01T12:00:00+00:00",
            "type": "llm_call",
            "level": "info",
            "data": {"role": "reason", "tokens": 12},
        },
        {
            "ts": "2026-10-01T12:00:00+00:00",
            "type": "ollama_fail_open",
            "level": "warning",
            "data": {"reason": "timeout"},
        },
    ]


def test_jsonl_sink_appends_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    JsonlEventSink(path).emit("a")
    JsonlEventSink(path).emit("b")
    assert [e["type"] for e in read_lines(path)] == ["a", "b"]


def test_jsonl_sink_serialises_unusual_values(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    JsonlEventSink(path).emit("x", where=tmp_path, ratio=0.5, tags=("a", "b"))
    data = read_lines(path)[0]["data"]
    assert data == {"where": str(tmp_path), "ratio": 0.5, "tags": ["a", "b"]}


def test_jsonl_sink_never_persists_thinking(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    JsonlEventSink(path).emit("x", note="<think>secret plan</think>visible")
    text = path.read_text(encoding="utf-8")
    assert "secret plan" not in text
    assert "visible" in text


def test_jsonl_sink_is_thread_safe(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    sink = JsonlEventSink(path)
    threads = [
        threading.Thread(target=sink.emit, args=("tick",), kwargs={"n": i}) for i in range(60)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = read_lines(path)
    assert len(lines) == 60
    assert sorted(e["data"]["n"] for e in lines) == list(range(60))


def test_memory_sink_collects_events() -> None:
    sink = MemoryEventSink(now=lambda: FIXED)
    sink.emit("a", level="warning", k=1)
    sink.emit("b")
    assert sink.events == [
        Event(ts=FIXED.isoformat(), type="a", level="warning", data={"k": 1}),
        Event(ts=FIXED.isoformat(), type="b", level="info", data={}),
    ]
    assert [e.type for e in sink.of_type("a")] == ["a"]


# ---- per-run event files (M6 D6) ----------------------------------------------------------


def test_a_bound_sink_writes_to_both_files_with_the_run_id(tmp_path: Path) -> None:
    global_path = tmp_path / "events.jsonl"
    scoped = RunScopedEventSink(JsonlEventSink(global_path, now=lambda: FIXED))
    run_dir = tmp_path / "runs" / "r-1"
    scoped.emit("before")
    with scoped.bound("r-1", run_dir):
        scoped.emit("inside", n=1)
    scoped.emit("after")
    assert [e["type"] for e in read_lines(global_path)] == ["before", "inside", "after"]
    assert read_lines(global_path)[1]["data"] == {"n": 1, "run_id": "r-1"}
    assert [e["type"] for e in read_lines(run_dir / "events.jsonl")] == ["inside"]
    assert read_lines(run_dir / "events.jsonl")[0]["data"]["run_id"] == "r-1"
    assert "run_id" not in read_lines(global_path)[0]["data"]


def test_an_unbound_sink_makes_no_run_file(tmp_path: Path) -> None:
    scoped = RunScopedEventSink(JsonlEventSink(tmp_path / "events.jsonl"))
    scoped.emit("x")
    assert not (tmp_path / "runs").exists()


def test_the_binding_ends_when_the_block_raises(tmp_path: Path) -> None:
    memory = MemoryEventSink()
    scoped = RunScopedEventSink(memory)
    try:
        with scoped.bound("r-1", tmp_path):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    scoped.emit("after")
    assert memory.events[-1].data == {}


def test_threads_emitting_while_bound_lose_nothing(tmp_path: Path) -> None:
    global_path = tmp_path / "events.jsonl"
    scoped = RunScopedEventSink(JsonlEventSink(global_path))
    run_dir = tmp_path / "r-1"
    with scoped.bound("r-1", run_dir):
        threads = [
            threading.Thread(target=scoped.emit, args=("tick",), kwargs={"n": i}) for i in range(50)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    for path in (global_path, run_dir / "events.jsonl"):
        lines = read_lines(path)
        assert sorted(e["data"]["n"] for e in lines) == list(range(50))
        assert {e["data"]["run_id"] for e in lines} == {"r-1"}


def test_events_are_read_after_a_cursor(tmp_path: Path) -> None:
    sink = JsonlEventSink(tmp_path / "events.jsonl")
    for i in range(3):
        sink.emit("e", i=i)
    events, cursor = read_run_events(tmp_path, 0)
    assert [e["data"]["i"] for e in events] == [0, 1, 2]
    assert cursor == 3
    sink.emit("e", i=3)
    events, cursor = read_run_events(tmp_path, cursor)
    assert [e["data"]["i"] for e in events] == [3]
    assert cursor == 4
    assert read_run_events(tmp_path, cursor) == ([], 4)


def test_a_torn_last_line_is_not_returned_yet(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    JsonlEventSink(path).emit("whole")
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"ts": "x", "ty')
    events, cursor = read_run_events(tmp_path, 0)
    assert [e["type"] for e in events] == ["whole"]
    assert cursor == 1
    with path.open("a", encoding="utf-8") as handle:
        handle.write('pe": "late", "level": "info", "data": {}}\n')
    events, cursor = read_run_events(tmp_path, cursor)
    assert [e["type"] for e in events] == ["late"]
    assert cursor == 2


def test_a_run_without_events_reads_as_empty(tmp_path: Path) -> None:
    assert read_run_events(tmp_path / "missing", 0) == ([], 0)
    assert read_run_events(tmp_path / "missing", 5) == ([], 5)
