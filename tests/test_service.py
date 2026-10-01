import json
import threading
import time
from dataclasses import dataclass

import pytest
from pydantic import BaseModel
from support import make_settings

from app.events import MemoryEventSink
from app.llm.errors import (
    LLMModelMissingError,
    LLMOutputError,
    LLMTruncatedError,
    LLMUnavailableError,
    PromptTooLargeError,
)
from app.llm.fakes import ScriptedTransport, reply
from app.llm.roles import build_registry
from app.llm.service import LLMService
from app.llm.structured import estimate_tokens
from app.llm.types import ChatReply, ChatRequest, Endpoint, Message, Role

URLS = {Endpoint.OWN: "http://own", Endpoint.SHARED: "http://shared"}
GOOD = '{"title": "t", "score": 3}'
BAD = '{"title": "t"}'
ASK: tuple[Message, ...] = ({"role": "user", "content": "give me JSON"},)


class Answer(BaseModel):
    title: str
    score: int


@dataclass
class Rig:
    service: LLMService
    transport: ScriptedTransport
    events: MemoryEventSink
    sleeps: list[float]


def rig(script: list[ChatReply | Exception]) -> Rig:
    transport = ScriptedTransport(script)
    events = MemoryEventSink()
    sleeps: list[float] = []
    registry = build_registry(make_settings())
    service = LLMService(registry, URLS, transport, events, timeout_s=5, sleep=sleeps.append)
    return Rig(service, transport, events, sleeps)


def test_structured_returns_a_validated_model_and_sends_the_schema() -> None:
    r = rig([reply(GOOD)])
    assert r.service.structured(Role.REASON, ASK, Answer) == Answer(title="t", score=3)
    base_url, req = r.transport.calls[0]
    assert base_url == "http://own"
    assert req.schema == Answer.model_json_schema()
    assert (req.model, req.num_ctx, req.num_predict) == ("qwen3.8-27b:latest", 16384, 8192)
    assert (req.temperature, req.keep_alive, req.think) == (0.2, "30m", False)
    assert req.messages == ASK


def test_roles_are_routed_to_their_endpoints() -> None:
    r = rig([reply(GOOD), reply(GOOD)])
    r.service.structured(Role.SUMMARIZE, ASK, Answer)
    r.service.structured(Role.EXTRACT, ASK, Answer)
    assert [url for url, _ in r.transport.calls] == ["http://shared", "http://own"]


def test_think_flag_is_passed_through() -> None:
    r = rig([reply(GOOD)])
    r.service.structured(Role.REASON, ASK, Answer, think=True)
    assert r.transport.calls[0][1].think is True


def test_thinking_in_the_content_is_stripped_before_parsing() -> None:
    r = rig([reply(f"<think>let me see</think>\n{GOOD}")])
    assert r.service.structured(Role.REASON, ASK, Answer).score == 3


def test_repair_succeeds_on_the_third_call() -> None:
    r = rig([reply('{"title"'), reply(BAD), reply(GOOD)])
    assert r.service.structured(Role.REASON, ASK, Answer).score == 3
    assert len(r.transport.calls) == 3
    second, third = r.transport.calls[1][1].messages, r.transport.calls[2][1].messages
    assert second[:1] == ASK
    assert second[1] == {"role": "assistant", "content": '{"title"'}
    assert "JSON" in second[2]["content"]
    assert third[1] == {"role": "assistant", "content": BAD}  # based on the original, not stacked
    assert "score" in third[2]["content"]
    assert len(third) == 3
    assert [e.data["attempt"] for e in r.events.of_type("llm_repair")] == [1, 2]


def test_output_error_after_two_repairs_carries_the_raw_output() -> None:
    r = rig([reply("nope"), reply("still no"), reply(BAD)])
    with pytest.raises(LLMOutputError) as err:
        r.service.structured(Role.REASON, ASK, Answer)
    assert err.value.raw == BAD
    assert len(r.transport.calls) == 3


def test_length_retry_doubles_the_budget_capped_by_the_context() -> None:
    r = rig([reply("{", done_reason="length"), reply(GOOD)])
    r.service.structured(Role.REASON, ASK, Answer)
    first, second = (c[1] for c in r.transport.calls)
    assert first.num_predict == 8192
    assert second.num_predict == 16384 - estimate_tokens(ASK)  # doubling to 16384 would not fit
    assert r.events.of_type("llm_length_retry")


def test_length_retry_uses_the_full_doubling_when_there_is_room() -> None:
    r = rig([reply("{", done_reason="length"), reply(GOOD)])
    r.service.structured(Role.EXTRACT, ASK, Answer)
    assert [c[1].num_predict for c in r.transport.calls] == [2048, 4096]


def test_truncated_error_after_a_second_length_stop() -> None:
    r = rig(
        [reply('{"title": "a', done_reason="length"), reply('{"title": "ab', done_reason="length")]
    )
    with pytest.raises(LLMTruncatedError) as err:
        r.service.structured(Role.EXTRACT, ASK, Answer)
    assert err.value.raw == '{"title": "ab'
    assert len(r.transport.calls) == 2


def test_length_without_headroom_raises_without_a_pointless_retry() -> None:
    # extract: num_ctx 8192, num_predict 2048. A prompt of exactly 6144 tokens
    # leaves no room to grow the output budget.
    edge: tuple[Message, ...] = ({"role": "user", "content": "x" * (3 * 6144)},)
    r = rig([reply("{", done_reason="length")])
    with pytest.raises(LLMTruncatedError):
        r.service.structured(Role.EXTRACT, edge, Answer)
    assert len(r.transport.calls) == 1


def test_unavailable_is_retried_with_backoff_then_succeeds() -> None:
    boom = LLMUnavailableError("refused")
    r = rig([boom, boom, boom, reply(GOOD)])
    assert r.service.structured(Role.REASON, ASK, Answer).score == 3
    assert r.sleeps == [1.0, 2.0, 4.0]
    outcomes = [e.data["outcome"] for e in r.events.of_type("llm_call")]
    assert outcomes == ["unavailable", "unavailable", "unavailable", "ok"]


def test_unavailable_gives_up_after_three_retries() -> None:
    boom = LLMUnavailableError("refused")
    r = rig([boom, boom, boom, boom])
    with pytest.raises(LLMUnavailableError):
        r.service.structured(Role.REASON, ASK, Answer)
    assert r.sleeps == [1.0, 2.0, 4.0]
    assert len(r.transport.calls) == 4


def test_missing_model_is_never_retried() -> None:
    r = rig([LLMModelMissingError("no such model")])
    with pytest.raises(LLMModelMissingError):
        r.service.structured(Role.REASON, ASK, Answer)
    assert r.sleeps == []
    assert [e.data["outcome"] for e in r.events.of_type("llm_call")] == ["model_missing"]


def test_prompt_too_large_never_reaches_the_transport() -> None:
    huge: tuple[Message, ...] = ({"role": "user", "content": "x" * (3 * 9000)},)
    r = rig([])
    with pytest.raises(PromptTooLargeError) as err:
        r.service.structured(Role.EXTRACT, huge, Answer)  # extract budget: 8192 - 2048
    assert (err.value.estimate, err.value.limit) == (9000, 6144)
    assert r.transport.calls == []


def test_text_returns_stripped_output() -> None:
    r = rig([reply("<think>hm</think>  The answer.  ")])
    assert r.service.text(Role.SUMMARIZE, ASK) == "The answer."
    assert r.transport.calls[0][1].schema is None


def test_telemetry_has_the_expected_fields_and_no_content() -> None:
    secret_prompt: tuple[Message, ...] = ({"role": "user", "content": "SECRET-PROMPT"},)
    r = rig(
        [
            reply(
                '{"title": "SECRET-REPLY", "score": 1}',
                thinking="SECRET-THOUGHT",
                prompt_tokens=11,
                eval_tokens=7,
                total_ms=900,
                load_ms=40,
            )
        ]
    )
    r.service.structured(Role.REASON, secret_prompt, Answer, think=True)
    (event,) = r.events.of_type("llm_call")
    assert event.data == {
        "role": "reason",
        "model": "qwen3.8-27b:latest",
        "endpoint": "own",
        "attempt": 1,
        "outcome": "ok",
        "prompt_tokens": 11,
        "eval_tokens": 7,
        "total_ms": 900,
        "load_ms": 40,
        "done_reason": "stop",
    }
    dump = json.dumps([e.data for e in r.events.events])
    assert not any(s in dump for s in ("SECRET-PROMPT", "SECRET-REPLY", "SECRET-THOUGHT"))


class SlowTransport:
    """Records how many chats run at the same time."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def chat(self, base_url: str, request: ChatRequest, timeout_s: float) -> ChatReply:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.03)
        with self.lock:
            self.active -= 1
        return reply("ok")


@pytest.mark.parametrize(("role", "limit"), [(Role.REASON, 1), (Role.EXTRACT, 2)])
def test_role_concurrency_is_bounded(role: Role, limit: int) -> None:
    transport = SlowTransport()
    registry = build_registry(make_settings())
    service = LLMService(registry, URLS, transport, MemoryEventSink(), timeout_s=5)
    threads = [threading.Thread(target=service.text, args=(role, ASK)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert 1 <= transport.peak <= limit
