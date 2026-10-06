"""`udr serve` and `udr apikey` (PRD M6 D8)."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from app import cli
from app.api import server
from app.api.keys import KeyStore
from app.store.db import Database

runner = CliRunner()


@pytest.fixture
def keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KeyStore:
    store = KeyStore(Database(tmp_path / "udr.sqlite"))
    monkeypatch.setattr(cli, "_key_store", lambda: store)
    return store


def test_create_prints_a_key_that_verifies_once_and_with_a_hint(keys: KeyStore) -> None:
    result = runner.invoke(cli.app, ["apikey", "create", "--name", "gui", "--self-approve"])
    assert result.exit_code == 0, result.output
    text = next(w for w in result.output.split() if w.startswith("udr_"))
    verified = keys.verify(text)
    assert verified is not None
    assert (verified.name, verified.self_approve) == ("gui", True)
    assert "nicht noch einmal" in result.output


def test_a_key_without_the_flag_may_not_approve(keys: KeyStore) -> None:
    result = runner.invoke(cli.app, ["apikey", "create", "--name", "agent"])
    text = next(w for w in result.output.split() if w.startswith("udr_"))
    found = keys.verify(text)
    assert found is not None
    assert not found.self_approve


def test_list_shows_id_name_flag_and_dates_but_never_a_key(keys: KeyStore) -> None:
    _key, text = keys.create("gui", self_approve=True)
    keys.create("agent", self_approve=False)
    result = runner.invoke(cli.app, ["apikey", "list"])
    assert result.exit_code == 0
    assert "gui" in result.output
    assert "agent" in result.output
    assert text not in result.output
    assert "udr_" not in result.output


def test_revoke_stops_the_key_and_an_unknown_id_exits_1(keys: KeyStore) -> None:
    key, text = keys.create("gui", self_approve=True)
    assert runner.invoke(cli.app, ["apikey", "revoke", key.key_id]).exit_code == 0
    assert keys.verify(text) is None
    listed = runner.invoke(cli.app, ["apikey", "list"])
    assert "widerrufen" in listed.output
    missing = runner.invoke(cli.app, ["apikey", "revoke", "k-nope"])
    assert missing.exit_code == 1


def test_serve_starts_the_api_on_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[object] = []
    monkeypatch.setattr(server, "serve", lambda settings: started.append(settings.api_port))
    result = runner.invoke(cli.app, ["serve"])
    assert result.exit_code == 0, result.output
    assert started == [8541]


def test_the_server_binds_to_loopback_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(server.uvicorn, "run", lambda app, **kw: calls.append(kw))
    monkeypatch.setattr(server, "build_service_app", lambda rt, **kw: object())
    monkeypatch.setattr(server.bootstrap, "build_runtime", lambda settings, events: object())
    from support import make_settings

    server.serve(make_settings(api_port=8599, data_dir=tmp_path))
    assert calls == [{"host": "127.0.0.1", "port": 8599}]
