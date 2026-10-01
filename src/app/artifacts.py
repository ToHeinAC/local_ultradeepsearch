"""Atomic artifact writes. Thinking text is scrubbed unless a caller needs exact bytes."""

import json
import os
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from app.llm.structured import remove_think


def scrub_think(value: Any) -> Any:
    """Return ``value`` with `<think>` content removed from every nested string."""
    if isinstance(value, str):
        return remove_think(value)
    if isinstance(value, dict):
        return {k: scrub_think(v) for k, v in cast("dict[str, Any]", value).items()}
    if isinstance(value, list | tuple):
        return [scrub_think(v) for v in cast("list[Any]", value)]
    return value


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(data, encoding="utf-8", newline="")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_text(path: Path, text: str, *, scrub: bool = True) -> None:
    """Write ``text`` atomically. ``scrub=False`` keeps the exact bytes (the approved brief)."""
    _atomic_write(path, remove_think(text) if scrub else text)


def write_json(path: Path, obj: Any) -> None:
    """Write ``obj`` (or a pydantic model) as indented UTF-8 JSON, atomically, thinking removed."""
    data = obj.model_dump(mode="json") if isinstance(obj, BaseModel) else obj
    text = json.dumps(scrub_think(data), indent=2, ensure_ascii=False) + "\n"
    _atomic_write(path, text)
