"""M6 Step 10: the API process composed from the real runtime, and a worker that sees templates
uploaded after it started."""

from pathlib import Path

from fastapi.testclient import TestClient
from research_rig import FakePandoc
from research_run_rig import RAW_BRIEF, TEMPLATE
from test_bootstrap import FakeHost, admin, settings
from test_bootstrap_research import NOW, AllModels, RunGateway, wired

from app import bootstrap
from app.api.keys import KeyStore
from app.api.server import build_service_app
from app.events import MemoryEventSink
from app.llm.fakes import CallbackTransport
from app.store.db import Database

NEW_TEMPLATE = (
    "---\nid: eigene\nname: Eigene\ndescription: Test\nlanguage: de\n"
    "default_response_format: short\n---\n\n## Eins\n\n## Zwei\n"
)


def runtime(tmp_path: Path) -> bootstrap.Runtime:
    return bootstrap.build_runtime(
        settings(tmp_path),
        MemoryEventSink(),
        probe=FakeHost(),
        admin=admin(),
        transport=CallbackTransport(AllModels()),
        env={},
    )


def test_the_service_app_is_composed_from_the_runtime(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    _key, text = KeyStore(Database(tmp_path / bootstrap.VAULT_FILE)).create("t", self_approve=True)
    gateway = RunGateway()
    app = build_service_app(
        rt, gateway_factory=lambda _d, _b, _p: gateway, pandoc=FakePandoc(), now=lambda: NOW
    )
    client = TestClient(app, headers={"Authorization": f"Bearer {text}"})
    assert client.get("/v1/health").json() == {"api": "ok", "worker_lock": "free", "queued": 0}
    created = client.post(
        "/v1/runs", json={"brief": RAW_BRIEF, "tier": "light", "template_id": TEMPLATE}
    )
    assert created.status_code == 201, created.text
    assert client.get("/v1/runs").json()[0]["status"] == "queued"
    assert client.get("/v1/templates").status_code == 200


def test_a_template_uploaded_through_the_api_is_found_by_a_worker_that_started_earlier(
    tmp_path: Path,
) -> None:
    worker, _gw, _models, _rt = wired(tmp_path, pandoc=FakePandoc())  # started before the upload
    rt = runtime(tmp_path)
    _key, text = KeyStore(Database(tmp_path / bootstrap.VAULT_FILE)).create("t", self_approve=True)
    gateway = RunGateway()
    app = build_service_app(
        rt, gateway_factory=lambda _d, _b, _p: gateway, pandoc=FakePandoc(), now=lambda: NOW
    )
    client = TestClient(app, headers={"Authorization": f"Bearer {text}"})
    files = {"file": ("eigene.md", NEW_TEMPLATE.encode(), "text/markdown")}
    assert client.post("/v1/templates", files=files).status_code == 201
    run_id = client.post(
        "/v1/runs", json={"brief": RAW_BRIEF, "tier": "light", "template_id": "eigene"}
    ).json()["run_id"]
    assert worker.run(run_id).status == "awaiting_plan_approval"
