"""One HTTP attempt to a JSON API, with status codes mapped onto the outbound error types."""

from collections.abc import Collection, Mapping
from typing import Any, cast

import httpx

from app.adapters.outbound.errors import (
    PlanLimitError,
    ProviderAuthError,
    ProviderError,
    TransientProviderError,
)
from app.adapters.outbound.types import HttpFactory


def _error_message(response: httpx.Response) -> str:
    try:
        data: object = response.json()
    except ValueError:
        return response.text[:200] or response.reason_phrase
    if isinstance(data, dict):
        body = cast("dict[str, Any]", data)
        detail = body.get("detail")
        if isinstance(detail, dict) and "error" in detail:
            return str(cast("dict[str, Any]", detail)["error"])
        for key in ("error", "message", "detail"):
            if key in body:
                return str(body[key])
    return response.text[:200]


def raise_for_status(
    provider: str, response: httpx.Response, plan_limit_codes: Collection[int] = ()
) -> None:
    status = response.status_code
    if status < 400:
        return
    message = _error_message(response)
    if status in (401, 403):
        raise ProviderAuthError(provider, message, status)
    if status in plan_limit_codes:
        raise PlanLimitError(provider, message, status)
    if status == 429 or status >= 500:
        raise TransientProviderError(provider, message, status)
    raise ProviderError(provider, message, status)


def network_error(provider: str, exc: httpx.HTTPError) -> TransientProviderError:
    kind = "timeout" if isinstance(exc, httpx.TimeoutException) else "network"
    return TransientProviderError(provider, f"{type(exc).__name__}: {exc}", kind=kind)


def send(
    http: HttpFactory,
    timeout_s: float,
    provider: str,
    method: str,
    url: str,
    *,
    params: Mapping[str, str] | None = None,
    json: Any = None,
    headers: Mapping[str, str] | None = None,
    plan_limit_codes: Collection[int] = (),
) -> httpx.Response:
    """Exactly one request; network failures become `TransientProviderError`."""
    try:
        with http(timeout_s) as client:
            response = client.request(method, url, params=params, json=json, headers=headers)
    except httpx.HTTPError as exc:
        raise network_error(provider, exc) from exc
    raise_for_status(provider, response, plan_limit_codes)
    return response


def json_object(provider: str, response: httpx.Response) -> dict[str, Any]:
    """The body as a JSON object; anything else counts as a transient failure."""
    try:
        data: object = response.json()
    except ValueError as exc:
        raise TransientProviderError(provider, "response is not JSON") from exc
    if not isinstance(data, dict):
        raise TransientProviderError(provider, "response is not a JSON object")
    return cast("dict[str, Any]", data)


def as_list(value: object) -> list[Any]:
    return cast("list[Any]", value) if isinstance(value, list) else []


def as_dict(value: object) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}
