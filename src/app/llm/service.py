"""The LLM service: one place that turns a role + messages into a validated result.

Handles the PRD §4 M1 behaviour: schema-constrained output with two repair attempts, one larger
retry when the model stops at its output limit, backoff when the endpoint is down, a per-role
concurrency limit, and telemetry that never contains prompts, answers or thinking.
"""

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from app.events import EventSink
from app.llm.errors import (
    LLMModelMissingError,
    LLMOutputError,
    LLMTruncatedError,
    LLMUnavailableError,
    PromptTooLargeError,
)
from app.llm.structured import (
    M,
    StructuredParseError,
    estimate_tokens,
    parse_structured,
    repair_messages,
    strip_think,
)
from app.llm.types import ChatReply, ChatRequest, Endpoint, Message, Role, RoleSpec, Transport

BACKOFF_S = (1.0, 2.0, 4.0)  # three retries when the endpoint is unavailable
MAX_REPAIRS = 2


class LLMService:
    def __init__(
        self,
        registry: Mapping[Role, RoleSpec],
        urls: Mapping[Endpoint, str],
        transport: Transport,
        events: EventSink,
        *,
        timeout_s: float,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._registry = registry
        self._urls = urls
        self._transport = transport
        self._events = events
        self._timeout_s = timeout_s
        self._sleep = sleep
        self._limits = {
            role: threading.BoundedSemaphore(spec.max_concurrency)
            for role, spec in registry.items()
        }

    def with_models(self, overrides: Mapping[Role, str]) -> "LLMService":
        """A service like this one that uses other models for some roles (a run's own summarize
        model). It shares the transport, URLs and events; it has its own concurrency limits."""
        registry = {
            role: replace(spec, model=overrides[role]) if role in overrides else spec
            for role, spec in self._registry.items()
        }
        return LLMService(
            registry,
            self._urls,
            self._transport,
            self._events,
            timeout_s=self._timeout_s,
            sleep=self._sleep,
        )

    def structured(
        self,
        role: Role,
        messages: Sequence[Message],
        schema: type[M],
        *,
        think: bool = False,
        num_predict: int | None = None,
    ) -> M:
        """Ask ``role`` for a ``schema`` object; repair invalid output up to twice.

        ``num_predict`` asks for a larger output budget than the role's default, for calls whose
        thinking needs it; it never exceeds half the context."""
        spec = self._registry[role]
        json_schema = schema.model_json_schema()
        convo: Sequence[Message] = tuple(messages)
        raw, error = "", ""
        for attempt in range(MAX_REPAIRS + 1):
            raw = strip_think(self._complete(spec, convo, json_schema, think, num_predict).content)
            try:
                return parse_structured(raw, schema)
            except StructuredParseError as exc:
                error = str(exc)
            if attempt < MAX_REPAIRS:
                self._events.emit(
                    "llm_repair", level="warning", role=role.value, attempt=attempt + 1, error=error
                )
                convo = repair_messages(messages, raw=raw, error=error)
        self._events.emit("llm_output_error", level="error", role=role.value)
        raise LLMOutputError(
            f"{role.value}: output still invalid after {MAX_REPAIRS} repairs: {error}", raw=raw
        )

    def text(self, role: Role, messages: Sequence[Message], *, think: bool = False) -> str:
        """Free-text answer from ``role``, with thinking and surrounding whitespace removed."""
        spec = self._registry[role]
        return strip_think(self._complete(spec, tuple(messages), None, think).content)

    def _complete(
        self,
        spec: RoleSpec,
        messages: Sequence[Message],
        schema: dict[str, Any] | None,
        think: bool,
        num_predict: int | None = None,
    ) -> ChatReply:
        """Budget check, one call, and one larger retry if the model hit its output limit."""
        budget = min(num_predict or spec.num_predict, spec.num_ctx // 2)
        estimate = estimate_tokens(messages)
        limit = spec.num_ctx - budget
        if estimate > limit:
            raise PromptTooLargeError(estimate=estimate, limit=limit)
        request = ChatRequest(
            spec.model,
            tuple(messages),
            schema,
            think,
            spec.num_ctx,
            budget,
            spec.temperature,
            spec.keep_alive,
        )
        result = self._send(spec, request)
        if result.done_reason != "length":
            return result
        grown = min(budget * 2, spec.num_ctx - estimate)
        if grown <= budget:  # no room left to grow: a retry would change nothing
            raise self._truncated(spec, result)
        self._events.emit(
            "llm_length_retry", level="warning", role=spec.role.value, num_predict=grown
        )
        result = self._send(spec, replace(request, num_predict=grown))
        if result.done_reason == "length":
            raise self._truncated(spec, result)
        return result

    def _truncated(self, spec: RoleSpec, result: ChatReply) -> LLMTruncatedError:
        self._events.emit("llm_truncated", level="error", role=spec.role.value)
        return LLMTruncatedError(
            f"{spec.role.value}: output hit the limit even after a larger budget",
            raw=strip_think(result.content),
        )

    def _send(self, spec: RoleSpec, request: ChatRequest) -> ChatReply:
        """One logical call: retried with backoff while the endpoint is unavailable."""
        delays = iter(BACKOFF_S)
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._call_once(spec, request, attempt)
            except LLMUnavailableError:
                delay = next(delays, None)
                if delay is None:
                    raise
                self._sleep(delay)

    def _call_once(self, spec: RoleSpec, request: ChatRequest, attempt: int) -> ChatReply:
        url = self._urls[spec.endpoint]
        with self._limits[spec.role]:
            try:
                result = self._transport.chat(url, request, self._timeout_s)
            except LLMUnavailableError:
                self._log_call(spec, attempt, "unavailable")
                raise
            except LLMModelMissingError:
                self._log_call(spec, attempt, "model_missing")
                raise
        self._log_call(spec, attempt, "ok", result)
        return result

    def _log_call(
        self, spec: RoleSpec, attempt: int, outcome: str, result: ChatReply | None = None
    ) -> None:
        self._events.emit(
            "llm_call",
            role=spec.role.value,
            model=spec.model,
            endpoint=spec.endpoint.value,
            attempt=attempt,
            outcome=outcome,
            prompt_tokens=result.prompt_tokens if result else 0,
            eval_tokens=result.eval_tokens if result else 0,
            total_ms=result.total_ms if result else 0,
            load_ms=result.load_ms if result else 0,
            done_reason=result.done_reason if result else None,
        )
