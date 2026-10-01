"""Scriptable fake transport for offline tests of everything that talks to an LLM."""

import threading
from collections import deque
from collections.abc import Callable, Sequence

from app.llm.types import ChatReply, ChatRequest


def reply(
    content: str = "",
    *,
    thinking: str | None = None,
    done_reason: str | None = "stop",
    prompt_tokens: int = 10,
    eval_tokens: int = 5,
    total_ms: int = 100,
    load_ms: int = 0,
) -> ChatReply:
    return ChatReply(content, thinking, done_reason, prompt_tokens, eval_tokens, total_ms, load_ms)


class ScriptedTransport:
    """Plays back `script` in order: a `ChatReply` is returned, an `Exception` is raised.

    Every call is recorded in `calls` as `(base_url, request)`. Running past the end of the
    script is a test bug and raises `AssertionError`.
    """

    def __init__(self, script: Sequence[ChatReply | Exception]) -> None:
        self._script: deque[ChatReply | Exception] = deque(script)
        self._lock = threading.Lock()
        self.calls: list[tuple[str, ChatRequest]] = []

    def chat(self, base_url: str, request: ChatRequest, timeout_s: float) -> ChatReply:
        with self._lock:
            self.calls.append((base_url, request))
            if not self._script:
                raise AssertionError("ScriptedTransport: script exhausted")
            item = self._script.popleft()
        if isinstance(item, Exception):
            raise item
        return item


class CallbackTransport:
    """Answers each request with ``handler(request)``: a `ChatReply` is returned, an `Exception`
    is raised. Use it when the answer depends on the request (which chunk, which model).

    `calls` holds the requests and `urls` the base URLs they were sent to, in call order.
    """

    def __init__(self, handler: Callable[[ChatRequest], ChatReply | Exception]) -> None:
        self._handler = handler
        self._lock = threading.Lock()
        self.calls: list[ChatRequest] = []
        self.urls: list[str] = []

    def chat(self, base_url: str, request: ChatRequest, timeout_s: float) -> ChatReply:
        with self._lock:
            self.calls.append(request)
            self.urls.append(base_url)
        outcome = self._handler(request)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
