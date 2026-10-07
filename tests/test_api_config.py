"""`GET /v1/config` never shows a secret, only whether it is set."""

from pathlib import Path

from api_rig import fake_doctor, make_api_rig
from fastapi.testclient import TestClient
from support import make_settings

from app.adapters.outbound.ledger import MonthLedger
from app.api.facade import Facade
from app.api.rest import build_app


def test_the_config_endpoint_does_not_contain_a_secret(tmp_path: Path) -> None:
    api = make_api_rig(tmp_path)
    settings = make_settings(
        data_dir=api.data_dir, tavily_api_key="tvly-geheim-123", openalex_api_key="oa-geheim-456"
    )
    facade = Facade(
        api.briefs.service,
        api.research.service,
        api.jobs,
        settings,
        api.templates,
        tmp_path / "d.txt",
        keys=api.keys,
        month=MonthLedger(tmp_path / "ledger.json", 1000),
        doctor=fake_doctor,
    )
    client = TestClient(build_app(facade, api.keys))
    response = client.get("/v1/config", headers={"Authorization": f"Bearer {api.owner_text}"})
    assert response.status_code == 200
    assert "geheim" not in response.text
    body = response.json()
    assert (body["tavily_api_key"], body["openalex_api_key"]) == ("set", "set")
    assert body["data_dir"] == str(api.data_dir)
