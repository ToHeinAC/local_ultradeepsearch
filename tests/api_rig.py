"""A `Facade` over the Phase-1 and Phase-2 rigs: two scripted 'services' that share keys and a
data directory, for the facade, REST and MCP tests."""

from dataclasses import dataclass
from pathlib import Path

from brief_rig import Rig as BriefRig
from brief_rig import rig as brief_rig
from research_rig import TEMPLATES
from research_run_rig import RunRig, make_rig
from support import make_settings

from app.api.facade import Facade
from app.api.jobs import SessionJobs
from app.api.keys import ApiKey, KeyStore
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


def make_api_rig(base: Path) -> ApiRig:
    jobs = SessionJobs(threads=2)
    briefs = brief_rig(base / "brief", background=jobs.submit)
    templates = dict(TEMPLATES)
    research = make_rig(base / "research", templates=templates)
    data_dir = research.base  # the rig's files are the API's data directory
    settings = make_settings(data_dir=data_dir)
    facade = Facade(
        briefs.service, research.service, jobs, settings, templates, data_dir / "denylist.txt"
    )
    keys = KeyStore(Database(base / "keys.sqlite"))
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
    )
