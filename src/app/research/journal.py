"""A small JSON key/value file per step, so a resumed step finds what it already decided."""

import json
import threading
from pathlib import Path
from typing import Any, cast

from app.artifacts import write_json

_LOCK = threading.Lock()


class Journal:
    def __init__(self, path: Path) -> None:
        self._path = path

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            loaded: object = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{self._path} is not valid JSON") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"{self._path} must hold a JSON object")
        return dict(cast("dict[str, Any]", loaded))

    def data(self) -> dict[str, Any]:
        with _LOCK:
            return self._read()

    def get(self, key: str) -> Any:
        return self.data().get(key)

    def put(self, key: str, value: Any) -> None:
        """Store ``value`` under ``key``; the whole file is rewritten atomically."""
        with _LOCK:
            data = self._read()
            data[key] = value
            write_json(self._path, data)
