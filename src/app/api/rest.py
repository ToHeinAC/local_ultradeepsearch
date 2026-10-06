"""The REST app (PRD M6): `/v1`, bearer API keys on every route. The interactive docs and the
OpenAPI endpoint are off (the schema is still built, `app.openapi()`)."""

import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.errors import HANDLED, to_http
from app.api.facade import Facade
from app.api.keys import ApiKey, KeyStore, bearer_token
from app.api.mcp import KeyMiddleware, build_mcp, current_key
from app.api.routes_admin import admin_router
from app.api.routes_runs import runs_router
from app.api.routes_sessions import sessions_router

UNAUTHORIZED = HTTPException(
    status_code=401,
    detail="a valid API key is required",
    headers={"WWW-Authenticate": "Bearer"},
)


def build_auth(keys: KeyStore) -> Callable[[Request], ApiKey]:
    def current_key(request: Request) -> ApiKey:
        token = bearer_token(request)
        key = keys.verify(token) if token else None
        if key is None:
            raise UNAUTHORIZED
        return key

    return current_key


def _domain_error(request: Request, exc: Exception) -> JSONResponse:
    mapped = to_http(exc)
    assert mapped is not None  # only registered for mapped errors
    status, body = mapped
    return JSONResponse(body, status_code=status)


def build_routers(
    facade: Facade,
    auth: Callable[..., ApiKey],
    *,
    sse_poll_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> list[APIRouter]:
    """Every group of routes, each built over ``auth``; `build_app` includes them all."""
    return [
        sessions_router(facade, auth),
        runs_router(facade, auth, sse_poll_s=sse_poll_s, sleep=sleep),
        admin_router(facade, auth),
    ]


def build_app(
    facade: Facade,
    keys: KeyStore,
    *,
    sse_poll_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> FastAPI:
    mcp = build_mcp(facade, current_key)
    mcp_app = mcp.streamable_http_app(json_response=True, stateless_http=True)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        async with mcp.session_manager.run():  # a mounted app's own lifespan never runs
            yield

    app = FastAPI(
        title="UltraDeepSearch",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    for error_type in HANDLED:
        app.add_exception_handler(error_type, _domain_error)
    auth = build_auth(keys)
    for router in build_routers(facade, auth, sse_poll_s=sse_poll_s, sleep=sleep):
        app.include_router(router, dependencies=[Depends(auth)])
    app.mount("/", KeyMiddleware(mcp_app, keys))  # `/mcp`; after the routes, so they win
    return app
