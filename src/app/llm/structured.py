"""Pure helpers for structured output: think-stripping, parsing, repair, token estimates."""

import math
import re
from collections.abc import Sequence
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.llm.types import Message
from app.prompts.llm import REPAIR_PROMPT

M = TypeVar("M", bound=BaseModel)  # CI runs 3.11, so no PEP 695 generics

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_ORPHAN_CLOSE = re.compile(r"\A.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_UNCLOSED = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"\A```(?:json)?\s*(.*?)\s*```\Z", re.DOTALL | re.IGNORECASE)

CHARS_PER_TOKEN = 3


class StructuredParseError(Exception):
    """The model output is not valid JSON for the schema. The message is fed back to the model."""


def strip_think(text: str) -> str:
    """Remove `<think>` blocks, an unclosed trailing one, and a lone leading `</think>`."""
    text = _THINK_BLOCK.sub("", text)
    text = _THINK_ORPHAN_CLOSE.sub("", text, count=1)
    text = _THINK_UNCLOSED.sub("", text)
    return text.strip()


def _describe(exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err["loc"])
        if err["type"] == "json_invalid":
            parts.append(f"invalid JSON ({err['msg']})")
        else:
            parts.append(f"{where}: {err['msg']}" if where else err["msg"])
    return "; ".join(parts)


def parse_structured(text: str, schema: type[M]) -> M:
    """Validate `text` (already think-stripped) against `schema`."""
    fenced = _FENCE.match(text.strip())
    payload = fenced.group(1) if fenced else text
    try:
        return schema.model_validate_json(payload)
    except ValidationError as exc:
        raise StructuredParseError(_describe(exc)) from exc


def repair_messages(messages: Sequence[Message], *, raw: str, error: str) -> tuple[Message, ...]:
    """The conversation plus the rejected reply and a correction request."""
    return (
        *messages,
        {"role": "assistant", "content": raw},
        {"role": "user", "content": REPAIR_PROMPT.format(error=error)},
    )


def estimate_tokens(messages: Sequence[Message]) -> int:
    """Rough prompt size: characters / 3, rounded up (PRD §3.1)."""
    return math.ceil(sum(len(m["content"]) for m in messages) / CHARS_PER_TOKEN)
