from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx
import pytest
from ollama import ChatResponse, Message, ResponseError

from app.adapters.ollama_transport import (
    LoadedModel,
    OllamaAdmin,
    OllamaTransport,
    normalize_tag,
)
from app.llm.errors import LLMError, LLMModelMissingError, LLMUnavailableError
from app.llm.types import ChatRequest

OWN = "http://127.0.0.1:11436"
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


def request(**overrides: Any) -> ChatRequest:
    base: dict[str, Any] = {
        "model": "m:1",
        "messages": ({"role": "user", "content": "hi"},),
        "schema": SCHEMA,
        "think": False,
        "num_ctx": 4096,
        "num_predict": 256,
        "temperature": 0.2,
        "keep_alive": "5m",
    }
    return ChatRequest(**{**base, **overrides})


class FakeClient:
    def __init__(self, outcome: ChatResponse | Exception) -> None:
        self.outcome = outcome
        self.kwargs: dict[str, Any] = {}

    def chat(
        self,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        think: bool,
        format: dict[str, Any] | None,
        options: Mapping[str, Any],
        keep_alive: str,
    ) -> ChatResponse:
        self.kwargs = {
            "model": model,
            "messages": list(messages),
            "think": think,
            "format": format,
            "options": dict(options),
            "keep_alive": keep_alive,
        }
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def transport_for(
    outcome: ChatResponse | Exception,
) -> tuple[OllamaTransport, FakeClient, list[tuple[str, float]]]:
    client = FakeClient(outcome)
    built: list[tuple[str, float]] = []

    def factory(base_url: str, timeout_s: float) -> FakeClient:
        built.append((base_url, timeout_s))
        return client

    return OllamaTransport(factory), client, built


def response(**overrides: Any) -> ChatResponse:
    fields: dict[str, Any] = {
        "model": "m:1",
        "message": Message(role="assistant", content='{"ok": true}', thinking="hmm"),
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 21,
        "eval_count": 8,
        "total_duration": 2_500_000_000,
        "load_duration": 40_000_000,
    }
    return ChatResponse(**{**fields, **overrides})


def test_reply_fields_are_mapped_and_nanoseconds_become_milliseconds() -> None:
    transport, _, _ = transport_for(response())
    out = transport.chat(OWN, request(), 30)
    assert (out.content, out.thinking, out.done_reason) == ('{"ok": true}', "hmm", "stop")
    assert (out.prompt_tokens, out.eval_tokens) == (21, 8)
    assert (out.total_ms, out.load_ms) == (2500, 40)


def test_missing_counters_default_to_zero() -> None:
    bare = response(
        message=Message(role="assistant", content=None),
        prompt_eval_count=None,
        eval_count=None,
        total_duration=None,
        load_duration=None,
        done_reason=None,
    )
    out = OllamaTransport(lambda *_: FakeClient(bare)).chat(OWN, request(), 5)
    assert (out.content, out.thinking, out.done_reason) == ("", None, None)
    assert (out.prompt_tokens, out.eval_tokens, out.total_ms, out.load_ms) == (0, 0, 0, 0)


def test_request_is_translated_with_an_explicit_think_flag() -> None:
    transport, client, _ = transport_for(response())
    transport.chat(OWN, request(think=False), 30)
    assert client.kwargs == {
        "model": "m:1",
        "messages": [{"role": "user", "content": "hi"}],
        "think": False,  # never omitted: gemma4 would otherwise burn its budget thinking
        "format": SCHEMA,
        "options": {"num_ctx": 4096, "num_predict": 256, "temperature": 0.2},
        "keep_alive": "5m",
    }


def test_plain_text_requests_send_no_format() -> None:
    transport, client, _ = transport_for(response())
    transport.chat(OWN, request(schema=None, think=True), 30)
    assert client.kwargs["format"] is None
    assert client.kwargs["think"] is True


def test_images_are_forwarded_for_the_ocr_role() -> None:
    transport, client, _ = transport_for(response())
    msg = {"role": "user", "content": "read", "images": ["QUJD"]}
    transport.chat(OWN, request(messages=(msg,)), 30)
    assert client.kwargs["messages"] == [msg]


def test_clients_are_cached_per_base_url_and_timeout() -> None:
    transport, _, built = transport_for(response())
    shared = "http://127.0.0.1:11434"
    for url in (OWN, OWN, shared, OWN):
        transport.chat(url, request(), 30)
    assert built == [(OWN, 30), (shared, 30)]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ResponseError("model 'x' not found", 404), LLMModelMissingError),
        (ResponseError("server busy", 503), LLMUnavailableError),
        (ResponseError("model failed to load", 500), LLMUnavailableError),
        (ConnectionError("Failed to connect to Ollama"), LLMUnavailableError),
        (httpx.ReadTimeout("slow"), LLMUnavailableError),
        (httpx.RemoteProtocolError("dropped"), LLMUnavailableError),
    ],
)
def test_errors_are_mapped_to_the_llm_hierarchy(error: Exception, expected: type) -> None:
    transport, _, _ = transport_for(error)
    with pytest.raises(expected):
        transport.chat(OWN, request(), 30)


def test_other_client_errors_are_not_retryable() -> None:
    transport, _, _ = transport_for(ResponseError("does not support thinking", 400))
    with pytest.raises(LLMError) as err:
        transport.chat(OWN, request(), 30)
    assert not isinstance(err.value, LLMUnavailableError | LLMModelMissingError)
    assert "does not support thinking" in str(err.value)


@pytest.mark.parametrize("url", ["http://10.0.0.5:11434", "http://example.com", "127.0.0.1:11434"])
def test_the_transport_only_talks_to_loopback(url: str) -> None:
    transport, client, built = transport_for(response())
    with pytest.raises(ValueError, match="loopback"):
        transport.chat(url, request(), 30)
    assert built == []
    assert client.kwargs == {}


# ---- admin client ---------------------------------------------------------------------------


def admin_for(handler: Callable[[httpx.Request], httpx.Response]) -> OllamaAdmin:
    return OllamaAdmin(
        lambda timeout: httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)
    )


def unreachable(_: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("gemma4", "gemma4:latest"),
        ("gemma4:e4b", "gemma4:e4b"),
        ("LiquidAI/lfm2.5-1.2b-instruct:latest", "LiquidAI/lfm2.5-1.2b-instruct:latest"),
        ("hf.co/user/model", "hf.co/user/model:latest"),
        ("registry:5000/team/model", "registry:5000/team/model:latest"),
    ],
)
def test_normalize_tag(name: str, expected: str) -> None:
    assert normalize_tag(name) == expected


def test_version() -> None:
    admin = admin_for(lambda r: httpx.Response(200, json={"version": "0.31.1"}))
    assert admin.version(OWN) == "0.31.1"


def test_tags_are_normalised() -> None:
    body = {
        "models": [{"name": "gemma4", "model": "gemma4"}, {"name": "qwen3:8b", "model": "qwen3:8b"}]
    }
    admin = admin_for(lambda r: httpx.Response(200, json=body))
    assert admin.tags(OWN) == frozenset({"gemma4:latest", "qwen3:8b"})


def test_ps_reports_vram_use() -> None:
    body = {
        "models": [
            {
                "name": "qwen3.8-27b:latest",
                "model": "qwen3.8-27b:latest",
                "size": 20_000,
                "size_vram": 20_000,
                "context_length": 32768,
            },
            {"name": "x:1", "model": "x:1", "size": 10, "size_vram": 4},
        ]
    }
    admin = admin_for(lambda r: httpx.Response(200, json=body))
    loaded = admin.ps(OWN)
    assert loaded == [
        LoadedModel("qwen3.8-27b:latest", 20_000, 20_000, 32768),
        LoadedModel("x:1", 10, 4, None),
    ]
    assert loaded is not None
    assert loaded[0].fully_in_vram
    assert not loaded[1].fully_in_vram


def test_admin_paths() -> None:
    seen: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r.url.path)
        return httpx.Response(200, json={"version": "1", "models": []})

    admin = admin_for(handler)
    admin.version(OWN)
    admin.tags(OWN)
    admin.ps(OWN)
    assert seen == ["/api/version", "/api/tags", "/api/ps"]


def test_unreachable_endpoints_report_none_not_empty() -> None:
    admin = admin_for(unreachable)
    assert admin.version(OWN) is None
    assert admin.tags(OWN) is None
    assert admin.ps(OWN) is None


def test_error_statuses_and_garbage_count_as_unreachable() -> None:
    assert admin_for(lambda r: httpx.Response(500)).tags(OWN) is None
    assert admin_for(lambda r: httpx.Response(200, text="<html>")).version(OWN) is None
    assert admin_for(lambda r: httpx.Response(200, json={"unexpected": 1})).tags(OWN) is None


def test_admin_refuses_non_loopback_urls() -> None:
    called: list[str] = []
    admin = admin_for(lambda r: called.append("x") or httpx.Response(200, json={}))
    with pytest.raises(ValueError, match="loopback"):
        admin.version("http://example.com")
    assert called == []
