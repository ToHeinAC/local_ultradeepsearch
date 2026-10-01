"""Live checks against the real Ollama models. Excluded from the gate; run with `pytest -m live`.

The OCR role is not covered here: `deepseek-ocr` is an image model, so it is exercised with a
real page image in M4.
"""

import json
from pathlib import Path

import pytest
from pydantic import BaseModel
from support import make_settings

from app import bootstrap
from app.adapters.ollama_instance import InstanceState
from app.events import JsonlEventSink
from app.llm.types import Message, Role

pytestmark = pytest.mark.live

SECRET = "Return the JSON object with ok=true and word='hello'."
ASK: tuple[Message, ...] = ({"role": "user", "content": SECRET},)


class Probe(BaseModel):
    ok: bool
    word: str


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory) -> tuple[bootstrap.Runtime, Path]:
    data_dir = tmp_path_factory.mktemp("udr-live")
    settings = make_settings(data_dir=data_dir)
    events_path = data_dir / "events.jsonl"
    return bootstrap.build_runtime(settings, JsonlEventSink(events_path)), events_path


def test_the_own_instance_is_up(live: tuple[bootstrap.Runtime, Path]) -> None:
    rt, _ = live
    assert rt.status.state in (InstanceState.ADOPTED, InstanceState.STARTED), rt.status.reason


@pytest.mark.parametrize("role", [Role.EXTRACT, Role.SUMMARIZE, Role.REASON])
def test_each_role_returns_a_valid_structured_answer(
    live: tuple[bootstrap.Runtime, Path], role: Role
) -> None:
    rt, _ = live
    answer = rt.service.structured(role, ASK, Probe)
    assert answer.ok is True
    assert answer.word.strip()


def test_reason_with_thinking_still_returns_clean_json(
    live: tuple[bootstrap.Runtime, Path],
) -> None:
    rt, _ = live
    answer = rt.service.structured(Role.REASON, ASK, Probe, think=True)
    assert answer.ok is True


def test_telemetry_was_written_without_content(live: tuple[bootstrap.Runtime, Path]) -> None:
    _, events_path = live
    lines = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    calls = [e for e in lines if e["type"] == "llm_call" and e["data"]["outcome"] == "ok"]
    assert {c["data"]["role"] for c in calls} >= {"extract", "summarize", "reason"}
    assert all(c["data"]["eval_tokens"] > 0 for c in calls)
    # neither the prompt nor the model's answer ("hello") may appear in the event log
    assert "hello" not in events_path.read_text(encoding="utf-8").lower()
