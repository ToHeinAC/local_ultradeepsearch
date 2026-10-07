"""PRD M7, page "Einstellungen": the denylist editor, the doctor and the role to model map."""

from typing import Any

from gui_rig import FakeApi, run_app
from streamlit.testing.v1 import AppTest

from app.client import ApiError

DOCTOR = {
    "checks": [
        {"name": "shared_endpoint", "level": "ok", "detail": "reachable"},
        {
            "name": "searxng",
            "level": "warning",
            "detail": "no JSON answer from http://127.0.0.1:8888",
        },
        {"name": "model:reason", "level": "error", "detail": "missing"},
    ],
    "roles": [
        {"role": "reason", "model": "qwen3.8-27b:latest", "endpoint": "own"},
        {"role": "summarize", "model": "gemma4:e4b", "endpoint": "shared"},
    ],
}


def api(**more: Any) -> FakeApi:
    return FakeApi(get_denylist={"terms": ["Müller", "Projekt X"]}, doctor=DOCTOR, **more)


def open_settings(fake: FakeApi) -> AppTest:
    return run_app(fake, page="einstellungen")


def test_the_denylist_is_shown_one_term_per_line() -> None:
    at = open_settings(api())
    assert not at.exception
    assert at.text_area(key="denylist").value == "Müller\nProjekt X"


def test_saving_sends_the_edited_terms_without_blank_lines() -> None:
    fake = api(put_denylist={"terms": ["Müller", "Neu"]})
    at = open_settings(fake)
    at.text_area(key="denylist").set_value("Müller\n\n  Neu  \n")
    next(b for b in at.button if b.label == "Speichern").click().run()
    assert fake.called("put_denylist") == [((["Müller", "Neu"],), {})]
    assert any("gespeichert" in s.value for s in at.success)


def test_a_rejected_term_shows_the_servers_message() -> None:
    fake = api(put_denylist=ApiError(422, "term too short"))
    at = open_settings(fake)
    next(b for b in at.button if b.label == "Speichern").click().run()
    assert not at.exception
    assert any("term too short" in e.value for e in at.error)


def test_the_doctor_checks_are_listed_with_their_level() -> None:
    at = open_settings(api())
    text = " ".join(m.value for m in at.markdown)
    assert "shared_endpoint" in text
    assert "WARN" in text
    assert "ERROR" in text
    assert "searxng" in text


def test_the_role_model_map_is_read_only() -> None:
    at = open_settings(api())
    rows = at.dataframe[0].value
    assert list(rows["Rolle"]) == ["reason", "summarize"]
    assert list(rows["Modell"]) == ["qwen3.8-27b:latest", "gemma4:e4b"]
    assert not at.text_input
