"""The GUI's client of the REST API (PRD M7): typed calls over `httpx`, loopback only.

It is the only thing `src/app/gui/` imports from the application. Answers are plain JSON data
(dicts and lists), so the GUI never touches the domain models. The one piece of logic is the
search plan as a table: `plan_rows` and `rows_to_lines`.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

import httpx

from app.config import is_loopback_url

HttpFactory = Callable[[float], httpx.Client]  # timeout -> client; tests pass a MockTransport
Json = dict[str, Any]
Upload = tuple[str, bytes]  # (file name, content)
TIMEOUT_S = 30.0


class ApiDown(Exception):
    """The API does not answer (not running, wrong port, timeout)."""


class ApiError(Exception):
    """The API answered with an error status; ``detail`` is its message."""

    def __init__(self, status: int, detail: str, run_status: str | None = None) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail
        self.run_status = run_status


def _default_http(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout)


def _detail(body: object) -> tuple[str, str | None]:
    if not isinstance(body, dict):
        return "", None
    data = cast("Json", body)
    detail = data.get("detail")
    if isinstance(detail, list):  # a validation error lists its problems
        items = cast("list[Any]", detail)
        detail = "; ".join(str(cast("Json", i).get("msg", i)) for i in items if isinstance(i, dict))
    status = data.get("status")
    return str(detail or ""), str(status) if status else None


class ApiClient:
    def __init__(self, base_url: str, key: str, http: HttpFactory = _default_http) -> None:
        if not is_loopback_url(base_url):
            raise ValueError(f"the API URL must be a loopback http(s) URL, got {base_url!r}")
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {key}"}
        self._http = http

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            with self._http(TIMEOUT_S) as client:
                response = client.request(
                    method, f"{self._base}{path}", headers=self._headers, **kwargs
                )
        except httpx.HTTPError as exc:
            raise ApiDown(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            try:
                detail, run_status = _detail(response.json())
            except ValueError:
                detail, run_status = response.text[:200], None
            raise ApiError(response.status_code, detail or response.reason_phrase, run_status)
        return response

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._send(method, path, **kwargs).json()

    # ---- sessions (Phase 1) -------------------------------------------------------------

    def list_sessions(self) -> list[Json]:
        return self._json("GET", "/v1/sessions")

    def get_session(self, session_id: str) -> Json:
        return self._json("GET", f"/v1/sessions/{session_id}")

    def start_session(self, question: str, files: Sequence[Upload] = ()) -> Json:
        return self._json(
            "POST", "/v1/sessions", data={"question": question}, files=_multipart(files)
        )

    def add_uploads(self, session_id: str, files: Sequence[Upload]) -> Json:
        return self._json("POST", f"/v1/sessions/{session_id}/uploads", files=_multipart(files))

    def send_message(
        self,
        session_id: str,
        answers: Sequence[Mapping[str, Any]] = (),
        *,
        note: str = "",
        genug: bool = False,
        offer: str | None = None,
    ) -> Json:
        body = {"answers": list(answers), "note": note, "genug": genug, "offer": offer}
        return self._json("POST", f"/v1/sessions/{session_id}/messages", json=body)

    def revise(self, session_id: str, feedback: str) -> Json:
        return self._json("POST", f"/v1/sessions/{session_id}/revise", json={"feedback": feedback})

    def set_settings(self, session_id: str, **choices: str | None) -> Json:
        return self._json("PUT", f"/v1/sessions/{session_id}/settings", json=choices)

    def edit_brief(self, session_id: str, text: str) -> Json:
        return self._json("PUT", f"/v1/sessions/{session_id}/brief", json={"text": text})

    def save(self, session_id: str) -> Json:
        return self._json("POST", f"/v1/sessions/{session_id}/save")

    def retry(self, session_id: str) -> Json:
        return self._json("POST", f"/v1/sessions/{session_id}/retry")

    def approve_session(
        self,
        session_id: str,
        brief_sha256: str,
        tier: str,
        *,
        summarize_model: str | None = None,
        tavily_cap: int | None = None,
    ) -> Json:
        """Approve the brief with this hash; only the choices made are sent."""
        body: Json = {"brief_sha256": brief_sha256, "tier": tier}
        if summarize_model is not None:
            body["summarize_model"] = summarize_model
        if tavily_cap is not None:
            body["tavily_cap"] = tavily_cap
        return self._json("POST", f"/v1/sessions/{session_id}/approve", json=body)

    # ---- runs (Phase 2) -----------------------------------------------------------------

    def run_summaries(self) -> list[Json]:
        return self._json("GET", "/v1/run-summaries")

    def run_summary(self, run_id: str) -> Json:
        return self._json("GET", f"/v1/runs/{run_id}/summary")

    def events(self, run_id: str, after: int = 0) -> Json:
        return self._json("GET", f"/v1/runs/{run_id}/events", params={"after": after})

    def get_plan(self, run_id: str) -> Json:
        return self._json("GET", f"/v1/runs/{run_id}/search-plan")

    def put_plan(self, run_id: str, text: str) -> Json:
        return self._json("PUT", f"/v1/runs/{run_id}/search-plan", json={"text": text})

    def approve_plan(self, run_id: str, plan_sha256: str) -> Json:
        body = {"plan_sha256": plan_sha256}
        return self._json("POST", f"/v1/runs/{run_id}/search-plan/approve", json=body)

    def approve_run(self, run_id: str, brief_sha256: str) -> Json:
        return self._json("POST", f"/v1/runs/{run_id}/approve", json={"brief_sha256": brief_sha256})

    def cancel(self, run_id: str) -> Json:
        return self._json("POST", f"/v1/runs/{run_id}/cancel")

    def resume(self, run_id: str) -> Json:
        return self._json("POST", f"/v1/runs/{run_id}/resume")

    def delete(self, run_id: str) -> None:
        self._send("DELETE", f"/v1/runs/{run_id}")

    def gate(self, run_id: str) -> Json:
        return self._json("GET", f"/v1/runs/{run_id}/gate")

    def outbound(self, run_id: str) -> Json:
        return self._json("GET", f"/v1/runs/{run_id}/outbound")

    def report(self, run_id: str, fmt: str) -> bytes:
        return self._send("GET", f"/v1/runs/{run_id}/report", params={"format": fmt}).content

    # ---- admin --------------------------------------------------------------------------

    def health(self) -> Json:
        return self._json("GET", "/v1/health")

    def doctor(self) -> Json:
        return self._json("GET", "/v1/doctor")

    def templates(self) -> list[Json]:
        return self._json("GET", "/v1/templates")

    def upload_template(self, name: str, content: bytes) -> Json:
        return self._json("POST", "/v1/templates", files=_multipart([(name, content)], "file"))

    def get_denylist(self) -> Json:
        return self._json("GET", "/v1/denylist")

    def put_denylist(self, terms: Sequence[str]) -> Json:
        return self._json("PUT", "/v1/denylist", json={"terms": list(terms)})


def _multipart(
    files: Sequence[Upload], field: str = "files"
) -> list[tuple[str, tuple[str, bytes]]]:
    return [(field, (name, content)) for name, content in files]


# ---- the search plan as a table (PRD M7 D3) ---------------------------------------------------


def plan_rows(plan: Mapping[str, Any]) -> list[Json]:
    """One table row per planned query: what the owner may edit (item, lens, original) and what
    the gateway made of it (sent, removed terms, why it is blocked)."""
    return [
        {
            "id": q["query_id"],
            "item": q["item"],
            "lens": q["lens"],
            "kind": q["kind"],
            "original": q["original"],
            "sent": q["sent"],
            "removed": ", ".join(q.get("removed_terms") or []),
            "blocked": q.get("blocked") or "",
        }
        for q in plan["queries"]
    ]


def rows_to_lines(rows: Sequence[Mapping[str, Any]]) -> str:
    """The edited table in the server's line format (`id | item | lens | query`). A row without
    a query is dropped, one without an id is new."""
    lines: list[str] = []
    for row in rows:
        text = " ".join(str(row.get("original") or "").split())
        if not text:
            continue
        query_id = str(row.get("id") or "").strip() or "-"
        lines.append(f"{query_id} | {row.get('item') or ''} | {row.get('lens') or ''} | {text}")
    return "\n".join(lines) + "\n"
