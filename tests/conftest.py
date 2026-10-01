"""Shared fixtures. The suite is offline: any socket connect raises, except for `live` tests."""

import os
import socket

import pytest

pytest_plugins = ["pytester"]


@pytest.fixture(autouse=True)
def _clean_udr_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's UDR_* variables must never leak into a test."""
    for name in [n for n in os.environ if n.startswith("UDR_")]:
        monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def _block_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("live"):
        return

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("tests must stay offline; use synthetic fixtures")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
