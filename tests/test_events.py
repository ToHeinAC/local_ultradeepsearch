import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.events import Event, JsonlEventSink, MemoryEventSink

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
