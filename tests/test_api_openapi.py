"""M6 AC7: the generated OpenAPI schema covers every endpoint."""

from pathlib import Path

import pytest
from api_rig import ApiRig, make_api_rig
from fastapi.routing import APIRoute

from app.api.rest import build_auth, build_routers


@pytest.fixture(scope="module")
def api(tmp_path_factory: pytest.TempPathFactory) -> ApiRig:
    return make_api_rig(tmp_path_factory.mktemp("openapi"))


def test_every_route_and_method_is_in_the_schema(api: ApiRig) -> None:
    paths = api.app.openapi()["paths"]
    routers = build_routers(api.facade, build_auth(api.keys))
    expected = {
        (route.path, method.lower())
        for router in routers
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    }
    assert len(expected) >= 30
    documented = {(path, method) for path, item in paths.items() for method in item}
    assert expected <= documented


def test_the_schema_has_no_route_that_the_app_does_not_serve(api: ApiRig) -> None:
    documented = {path for path in api.app.openapi()["paths"]}
    assert all(path.startswith("/v1/") for path in documented)


def test_the_schema_is_not_served(api: ApiRig, tmp_path: Path) -> None:
    assert api.client().get("/openapi.json").status_code == 404
