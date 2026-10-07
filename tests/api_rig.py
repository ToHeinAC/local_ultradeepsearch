"""A `Facade` over the Phase-1 and Phase-2 rigs: two scripted 'services' that share keys and a
data directory, for the facade, REST and MCP tests."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brief_rig import Rig as BriefRig
from brief_rig import rig as brief_rig
from fastapi import FastAPI
from fastapi.testclient import TestClient
from research_rig import TEMPLATES
from research_run_rig import RunRig, make_rig
from support import make_settings

from app.adapters.outbound.ledger import MonthLedger
from app.api.facade import Facade
from app.api.jobs import SessionJobs
from app.api.keys import ApiKey, KeyStore
from app.api.rest import build_app
from app.store.db import Database
from app.templates import ReportTemplate


@dataclass
class ApiRig:
    facade: Facade
    briefs: BriefRig
    research: RunRig
    jobs: SessionJobs
    keys: KeyStore
    templates: dict[str, ReportTemplate]
    data_dir: Path
    owner: ApiKey  # may approve
    owner_text: str
    agent: ApiKey  # may not approve
    agent_text: str
    app: FastAPI

    def client(self, key_text: str | None = None) -> TestClient:
        """A client that sends ``key_text`` as its bearer key (the owner's by default)."""
        client = TestClient(self.app)
        client.headers["Authorization"] = f"Bearer {key_text or self.owner_text}"
        return client


def fake_doctor() -> dict[str, Any]:
    return {
        "checks": [{"name": "shared_endpoint", "level": "ok", "detail": "reachable"}],
        "roles": [{"role": "reason", "model": "m", "endpoint": "own"}],
    }


def make_api_rig(base: Path) -> ApiRig:
    jobs = SessionJobs(threads=2)
    briefs = brief_rig(base / "brief", background=jobs.submit)
    templates = dict(TEMPLATES)
    research = make_rig(base / "research", templates=templates)
    data_dir = research.base  # the rig's files are the API's data directory
    settings = make_settings(data_dir=data_dir)
    keys = KeyStore(Database(base / "keys.sqlite"))
    facade = Facade(
        briefs.service,
        research.service,
        jobs,
        settings,
        templates,
        data_dir / "denylist.txt",
        keys=keys,
        month=MonthLedger(data_dir / "tavily-ledger.json", settings.tavily_monthly_limit),
        doctor=fake_doctor,
    )
    owner, owner_text = keys.create("owner", self_approve=True)
    agent, agent_text = keys.create("agent", self_approve=False)
    return ApiRig(
        facade,
        briefs,
        research,
        jobs,
        keys,
        templates,
        data_dir,
        owner,
        owner_text,
        agent,
        agent_text,
        build_app(facade, keys, sse_poll_s=0.0, sleep=lambda _s: None),
    )
