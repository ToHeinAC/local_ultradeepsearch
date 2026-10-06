"""The API process (`udr serve`): the composition of the service layer and uvicorn on loopback.
The only module that imports uvicorn."""

import threading
from typing import Any

import uvicorn
from fastapi import FastAPI

from app import bootstrap
from app.api.facade import Facade
from app.api.jobs import SessionJobs
from app.api.keys import KeyStore
from app.api.rest import build_app
from app.config import Settings
from app.events import JsonlEventSink
from app.pipeline.profiles import load_service_limits
from app.store.db import Database

HOST = "127.0.0.1"  # not a setting: the API never listens on another interface


def build_service_app(rt: bootstrap.Runtime, *, recover: bool = False, **research: Any) -> FastAPI:
    """The REST and MCP app over the runtime. Phase-1 work runs in a thread pool of this
    process; research runs are left to the worker. ``research`` goes to the research service
    (tests replace the gateway and pandoc). With ``recover`` the sessions a restart cut off are
    continued in a background thread."""
    settings = rt.settings
    limits = load_service_limits(settings.config_dir)
    jobs = SessionJobs(limits.session_threads)
    templates = bootstrap.load_report_templates(settings)  # one dict: an upload is seen at once
    briefs = bootstrap.build_brief_service(rt, background=jobs.submit, templates=templates)
    service = bootstrap.build_research_service(rt, templates=templates, **research)
    keys = KeyStore(Database(settings.data_dir / bootstrap.VAULT_FILE))
    facade = Facade(
        briefs, service, jobs, settings, templates, settings.data_dir / bootstrap.DENYLIST_FILE
    )
    if recover:
        threading.Thread(target=briefs.recover, name="recover-sessions", daemon=True).start()
    return build_app(facade, keys, sse_poll_s=limits.sse_poll_s)


def serve(settings: Settings) -> None:
    events = JsonlEventSink(settings.data_dir / "events.jsonl")
    rt = bootstrap.build_runtime(settings, events)
    uvicorn.run(build_service_app(rt, recover=True), host=HOST, port=settings.api_port)
