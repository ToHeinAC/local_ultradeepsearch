"""PRD M7 AC3, AC4, AC5: the shell of the GUI: navigation, the API banner, the safe exit and
the state a reload restores from the query parameters."""

import os
import signal

import pytest
from gui_rig import FakeApi, run_app

from app.gui.state import POLL_SECONDS


def titles(at: object) -> list[str]:
    return [h.value for h in at.header]  # type: ignore[attr-defined]


def test_the_default_page_is_new_research() -> None:
    at = run_app(FakeApi())
    assert not at.exception
    assert titles(at) == ["Neue Recherche"]


@pytest.mark.parametrize(
    ("page", "title"),
    [
        ("suchplan", "Suchplan"),
        ("laeufe", "Läufe"),
        ("bericht", "Bericht"),
        ("einstellungen", "Einstellungen"),
    ],
)
def test_a_reload_restores_the_page_from_the_query_parameters(page: str, title: str) -> None:
    at = run_app(FakeApi(), page=page, run="r-1")
    assert not at.exception
    assert titles(at) == [title]
    assert at.query_params["run"] == "r-1"  # still in the URL after the run


def test_choosing_a_page_puts_it_into_the_url() -> None:
    at = run_app(FakeApi())
    at.sidebar.radio[0].set_value("einstellungen").run()
    assert titles(at) == ["Einstellungen"]
    assert at.query_params["page"] == "einstellungen"


def test_an_unknown_page_falls_back_to_the_first() -> None:
    assert titles(run_app(FakeApi(), page="nope")) == ["Neue Recherche"]


def test_an_unreachable_api_shows_a_banner_and_a_retry_and_nothing_crashes() -> None:
    api = FakeApi()
    api.down = True
    at = run_app(api)
    assert not at.exception
    assert any("API nicht erreichbar" in e.value for e in at.error)
    retry = [b for b in at.button if b.label == "Erneut versuchen"]
    assert retry
    api.down = False
    retry[0].click().run()
    assert not at.error
    assert titles(at) == ["Neue Recherche"]


def test_an_api_error_is_shown_not_raised() -> None:
    from app.client import ApiError

    at = run_app(FakeApi(health=ApiError(401, "ungültiger Schlüssel")))
    assert not at.exception
    assert any("ungültiger Schlüssel" in e.value for e in at.error)


def test_the_exit_button_signals_only_this_process(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    api = FakeApi()
    at = run_app(api)
    next(b for b in at.sidebar.button if b.label == "Beenden").click().run()
    assert sent == [(os.getpid(), signal.SIGTERM)]
    assert [n for n, _a, _k in api.calls if n not in ("health",)] == []  # the API is left alone


def test_a_missing_key_is_explained_in_german(monkeypatch: pytest.MonkeyPatch) -> None:
    from streamlit.testing.v1 import AppTest

    from app.client import ApiClient, MissingApiKey

    def refuse() -> ApiClient:
        raise MissingApiKey("UDR_GUI_API_KEY fehlt")

    monkeypatch.setattr(ApiClient, "from_env", staticmethod(refuse))
    from gui_rig import APP

    at = AppTest.from_file(str(APP), default_timeout=10).run()
    assert not at.exception
    assert any("UDR_GUI_API_KEY" in e.value for e in at.error)


def test_progress_is_polled_at_most_every_five_seconds() -> None:
    assert 1 <= POLL_SECONDS <= 5
