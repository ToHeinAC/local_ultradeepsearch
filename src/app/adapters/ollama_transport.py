"""Talks to Ollama on loopback: chat (`OllamaTransport`) and status probes (`OllamaAdmin`)."""

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpx
import ollama
from ollama import ChatResponse, ResponseError

from app.config import is_loopback_url
from app.llm.errors import LLMError, LLMModelMissingError, LLMUnavailableError
from app.llm.types import ChatReply, ChatRequest

ADMIN_TIMEOUT_S = 3.0


def _require_loopback(base_url: str) -> None:
    if not is_loopback_url(base_url):
        raise ValueError(f"Ollama must be on loopback, got {base_url!r}")


def normalize_tag(name: str) -> str:
    """`gemma4` → `gemma4:latest`; names that already carry a tag are unchanged."""
    return name if ":" in name.rsplit("/", 1)[-1] else f"{name}:latest"


class ChatClient(Protocol):
    def chat(
        self,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        think: bool,
        format: dict[str, Any] | None,
        options: Mapping[str, Any],
        keep_alive: str,
    ) -> ChatResponse: ...


def _ollama_client(base_url: str, timeout_s: float) -> ChatClient:
    return ollama.Client(host=base_url, timeout=timeout_s)


def _ms(nanoseconds: int | None) -> int:
    return (nanoseconds or 0) // 1_000_000


def _to_reply(response: ChatResponse) -> ChatReply:
    message = response.message
    return ChatReply(
        content=message.content or "",
        thinking=message.thinking,
        done_reason=response.done_reason,
        prompt_tokens=response.prompt_eval_count or 0,
        eval_tokens=response.eval_count or 0,
        total_ms=_ms(response.total_duration),
        load_ms=_ms(response.load_duration),
    )


def _map_response_error(exc: ResponseError) -> LLMError:
    if exc.status_code == 404:
        return LLMModelMissingError(exc.error)
    if exc.status_code >= 500:
        return LLMUnavailableError(f"{exc.error} (status {exc.status_code})")
    return LLMError(f"ollama rejected the request ({exc.status_code}): {exc.error}")


class OllamaTransport:
    """`Transport` backed by the `ollama` client. `think` is always sent explicitly."""

    def __init__(self, client_factory: Callable[[str, float], ChatClient] = _ollama_client) -> None:
        self._factory = client_factory
        self._clients: dict[tuple[str, float], ChatClient] = {}
        self._lock = threading.Lock()

    def _client(self, base_url: str, timeout_s: float) -> ChatClient:
        with self._lock:
            key = (base_url, timeout_s)
            if key not in self._clients:
                self._clients[key] = self._factory(base_url, timeout_s)
            return self._clients[key]

    def chat(self, base_url: str, request: ChatRequest, timeout_s: float) -> ChatReply:
        _require_loopback(base_url)
        client = self._client(base_url, timeout_s)
        try:
            response = client.chat(
                request.model,
                list(request.messages),
                think=request.think,
                format=request.schema,
                options={
                    "num_ctx": request.num_ctx,
                    "num_predict": request.num_predict,
                    "temperature": request.temperature,
                },
                keep_alive=request.keep_alive,
            )
        except ResponseError as exc:
            raise _map_response_error(exc) from exc
        except (ConnectionError, httpx.TransportError) as exc:
            raise LLMUnavailableError(str(exc)) from exc
        return _to_reply(response)


@dataclass(frozen=True)
class LoadedModel:
    name: str
    size: int
    size_vram: int
    context_length: int | None

    @property
    def fully_in_vram(self) -> bool:
        return self.size > 0 and self.size_vram >= self.size


def _default_http(timeout_s: float) -> httpx.Client:
    return httpx.Client(timeout=timeout_s)


class OllamaAdmin:
    """Read-only status probes. `None` always means "could not be reached or understood"."""

    def __init__(
        self,
        http_factory: Callable[[float], httpx.Client] = _default_http,
        timeout_s: float = ADMIN_TIMEOUT_S,
    ) -> None:
        self._http_factory = http_factory
        self._timeout_s = timeout_s

    def _get(self, base_url: str, path: str) -> dict[str, Any] | None:
        _require_loopback(base_url)
        try:
            with self._http_factory(self._timeout_s) as http:
                response = http.get(f"{base_url.rstrip('/')}{path}")
            body = response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None
        return cast("dict[str, Any]", body) if isinstance(body, dict) else None

    def version(self, base_url: str) -> str | None:
        body = self._get(base_url, "/api/version")
        value = body.get("version") if body else None
        return value if isinstance(value, str) else None

    def tags(self, base_url: str) -> frozenset[str] | None:
        models = _models(self._get(base_url, "/api/tags"))
        if models is None:
            return None
        names = (str(m.get("name") or m.get("model") or "") for m in models)
        return frozenset(normalize_tag(n) for n in names if n)

    def ps(self, base_url: str) -> list[LoadedModel] | None:
        models = _models(self._get(base_url, "/api/ps"))
        if models is None:
            return None
        return [
            LoadedModel(
                str(m.get("name") or m.get("model") or ""),
                int(m.get("size") or 0),
                int(m.get("size_vram") or 0),
                m.get("context_length"),
            )
            for m in models
        ]


def _models(body: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    models = body.get("models") if body else None
    return cast("list[dict[str, Any]]", models) if isinstance(models, list) else None
