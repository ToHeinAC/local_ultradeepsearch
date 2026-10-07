"""A fake API for the GUI tests: the methods of `ApiClient` the pages call, over plain data, and
a helper that runs the real app against it with Streamlit's `AppTest`."""

from pathlib import Path
from typing import Any

from streamlit.testing.v1 import AppTest

from app.client import ApiDown

APP = Path(__file__).resolve().parents[1] / "src" / "app" / "gui" / "app.py"


class FakeApi:
    """Records every call as (name, args); answers with ``data[name]`` or an empty dict."""

    def __init__(self, **data: Any) -> None:
        self.data: dict[str, Any] = {"health": {"api": "ok", "worker_lock": "free", "queued": 0}}
        self.data.update(data)
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.down = False

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            if self.down:
                raise ApiDown("refused")
            answer = self.data.get(name, {})
            if isinstance(answer, Exception):
                raise answer
            return answer(*args, **kwargs) if callable(answer) else answer

        return call

    def called(self, name: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        return [(a, k) for n, a, k in self.calls if n == name]


def run_app(api: FakeApi, **query: str) -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=10)
    at.session_state["client"] = api
    for key, value in query.items():
        at.query_params[key] = value
    return at.run()
