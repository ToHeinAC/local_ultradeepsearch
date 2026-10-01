"""Plain data types shared by the LLM service and its transports."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, NotRequired, Protocol, TypedDict


class Role(StrEnum):
    REASON = "reason"
    EXTRACT = "extract"
    SUMMARIZE = "summarize"
    OCR = "ocr"


class Endpoint(StrEnum):
    OWN = "own"
    SHARED = "shared"


@dataclass(frozen=True)
class RoleSpec:
    role: Role
    model: str
    endpoint: Endpoint
    num_ctx: int
    num_predict: int
    temperature: float
    keep_alive: str
    max_concurrency: int


class Message(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str
    images: NotRequired[list[str]]  # base64; used by the ocr role


@dataclass(frozen=True)
class ChatRequest:
    model: str
    messages: tuple[Message, ...]
    schema: dict[str, Any] | None
    think: bool
    num_ctx: int
    num_predict: int
    temperature: float
    keep_alive: str


@dataclass(frozen=True)
class ChatReply:
    content: str
    thinking: str | None
    done_reason: str | None
    prompt_tokens: int
    eval_tokens: int
    total_ms: int
    load_ms: int


class Transport(Protocol):
    """One chat round-trip. Raises `LLMUnavailableError` or `LLMModelMissingError`."""

    def chat(self, base_url: str, request: ChatRequest, timeout_s: float) -> ChatReply: ...
