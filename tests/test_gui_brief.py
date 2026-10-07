"""PRD M7, page "Neue Recherche", AC2 (part 1): Phase 1 renders its questions, and "Freigeben"
sends the hash of the brief that is displayed."""

import hashlib
from typing import Any

from gui_rig import FakeApi, run_app
from streamlit.testing.v1 import AppTest

from app.client import ApiError

BRIEF = "# Wärmepumpen\n\n1. Wie effizient sind sie?\n"
SHA = hashlib.sha256(BRIEF.encode()).hexdigest()
QUESTIONS = (
    {"item": "scope", "question": "Welcher Zeitraum?", "candidate": "2020 bis heute"},
    {"item": "goal", "question": "Wofür der Bericht?", "candidate": ""},
)


def view(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "session_id": "s1",
        "status": "interviewing",
        "interview_language": "de",
        "round": 0,
        "max_rounds": 5,
        "waiting_for": "questions",
        "questions": QUESTIONS,
        "checklist": {"question": "clear", "scope": "missing"},
        "pasted": "",
        "refusal": "",
        "brief_text": None,
        "brief_sha256": None,
        "recommendation": None,
        "settings": None,
        "notices": [],
        "uploads": [],
        "run_id": None,
        "archive_path": None,
        "error": None,
        "busy": False,
    }
    return {**base, **changes}


def decision(**changes: Any) -> dict[str, Any]:
    return view(
        waiting_for="decision",
        questions=[],
        brief_text=BRIEF,
        brief_sha256=SHA,
        recommendation={
            "tier": "full",
            "response_format": "structured",
            "rationale": "Breite Frage",
        },
        settings={"report_language": "de", "response_format": "structured", "template_id": "auto"},
        **changes,
    )


TEMPLATES = [
    {
        "id": "auto",
        "name": "Automatisch",
        "description": "d",
        "language": "de",
        "default_response_format": "structured",
        "sections": ["Einleitung", "Befunde"],
    }
]


def app(api: FakeApi, **query: str) -> AppTest:
    api.data["templates"] = api.data["templates"] or TEMPLATES
    return run_app(api, **{"session": "s1", **query})


def button(at: AppTest, label: str) -> Any:
    return next(b for b in at.button if b.label == label)


# ---- start ----------------------------------------------------------------------------------


def test_starting_a_session_sends_the_question_and_keeps_the_id_in_the_url() -> None:
    api = FakeApi(start_session={"session_id": "s9"}, list_sessions=[], get_session=view())
    at = run_app(api)
    at.text_area[0].set_value("Wie gut sind Wärmepumpen?")
    button(at, "Starten").click().run()
    assert api.called("start_session")[0][0][0] == "Wie gut sind Wärmepumpen?"
    assert at.query_params["session"] == "s9"


def test_an_empty_question_is_not_sent() -> None:
    api = FakeApi(list_sessions=[])
    at = run_app(api)
    button(at, "Starten").click().run()
    assert api.called("start_session") == []
    assert any("Frage" in w.value for w in at.warning)


def test_an_unfinished_session_can_be_resumed() -> None:
    sessions = [
        {"session_id": "s7", "status": "saved", "title": "Alt", "created_by": None},
        {"session_id": "s8", "status": "approved", "title": "Fertig", "created_by": None},
    ]
    api = FakeApi(list_sessions=sessions, get_session=view())
    at = run_app(api)
    assert at.selectbox[0].options == ["s7 - Alt (saved)"]
    button(at, "Fortsetzen").click().run()
    assert at.query_params["session"] == "s7"


def test_a_rejected_start_shows_the_servers_reason_per_file() -> None:
    err = ApiError(422, "scan.pdf: nicht lesbar; notes.txt: leer")
    api = FakeApi(list_sessions=[], start_session=err)
    at = run_app(api)
    at.text_area[0].set_value("Frage")
    button(at, "Starten").click().run()
    assert not at.exception
    assert any("scan.pdf: nicht lesbar" in e.value for e in at.error)


# ---- questions ------------------------------------------------------------------------------


def test_questions_are_shown_with_editable_candidates_and_the_checklist() -> None:
    at = app(FakeApi(get_session=view(round=1)))
    assert not at.exception
    assert [t.value for t in at.text_input if t.key and t.key.startswith("answer-")] == [
        "2020 bis heute",
        "",
    ]
    text = " ".join(m.value for m in at.markdown)
    assert "Welcher Zeitraum?" in text
    assert "scope" in text
    assert any("Runde 2 von 5" in s.value for s in at.subheader)


def test_the_answers_are_accept_text_or_unknown() -> None:
    api = FakeApi(get_session=view(), send_message=view(busy=True))
    at = app(api)
    at.text_input(key="answer-0-1").set_value("Entscheidung")
    button(at, "Antworten senden").click().run()
    ((args, kwargs),) = api.called("send_message")
    assert args[0] == "s1"
    assert args[1] == [{"kind": "accept"}, {"kind": "text", "text": "Entscheidung"}]
    assert kwargs["genug"] is False


def test_a_cleared_candidate_is_unknown_and_genug_is_passed_on() -> None:
    api = FakeApi(get_session=view(), send_message=view(busy=True))
    at = app(api)
    at.text_input(key="answer-0-0").set_value("")
    at.checkbox(key="genug").set_value(True)
    at.text_input(key="note").set_value("Hinweis")
    button(at, "Antworten senden").click().run()
    ((args, kwargs),) = api.called("send_message")
    assert args[1] == [{"kind": "unknown"}, {"kind": "unknown"}]
    assert (kwargs["genug"], kwargs["note"]) == (True, "Hinweis")


def test_a_working_session_shows_progress_and_no_buttons() -> None:
    at = app(FakeApi(get_session=view(busy=True, questions=[])))
    assert any("arbeitet" in i.value for i in at.info)
    assert [b.label for b in at.button if b.label != "Beenden"] == []


def test_a_model_error_can_be_retried() -> None:
    api = FakeApi(get_session=view(error="LLMError: Zeitüberschreitung", questions=[]))
    at = app(api)
    assert any("Zeitüberschreitung" in e.value for e in at.error)
    button(at, "Erneut versuchen").click().run()
    assert api.called("retry")[0][0] == ("s1",)


# ---- a pasted finished prompt ---------------------------------------------------------------


def test_an_offer_can_be_strengthened_or_installed() -> None:
    offer = view(waiting_for="offer", questions=[], pasted="# Mein Prompt")
    api = FakeApi(get_session=offer, send_message=view(busy=True))
    at = app(api)
    button(at, "Unverändert übernehmen").click().run()
    assert api.called("send_message")[0][1]["offer"] == "install"
    button(app(api), "Verstärken").click().run()
    assert api.called("send_message")[1][1]["offer"] == "strengthen"


def test_an_offer_that_cannot_be_installed_only_offers_strengthening() -> None:
    offer = view(waiting_for="offer", questions=[], pasted="x", refusal="zu kurz")
    at = app(FakeApi(get_session=offer))
    assert next(b for b in at.button if b.label == "Unverändert übernehmen").disabled
    assert any("zu kurz" in w.value for w in at.warning)


# ---- the decision (AC2) ---------------------------------------------------------------------


def test_the_brief_is_shown_exactly_with_the_recommendation_and_full_disabled() -> None:
    at = app(FakeApi(get_session=decision()))
    assert at.text_area(key=f"brief-{SHA}").value == BRIEF
    text = " ".join(m.value for m in at.markdown) + " ".join(c.value for c in at.caption)
    assert "Breite Frage" in text
    assert "Full" in text
    assert "ab M8" in text
    assert [r.options for r in at.radio if r.label == "Tiefe"] == [["Lite"]]


def test_freigeben_sends_the_hash_of_the_displayed_brief() -> None:
    api = FakeApi(get_session=decision(), approve_session={"run_id": "r-1"})
    at = app(api)
    shown = str(at.text_area(key=f"brief-{SHA}").value)
    button(at, "Freigeben").click().run()
    ((args, kwargs),) = api.called("approve_session")
    assert args == ("s1", hashlib.sha256(shown.encode()).hexdigest(), "light")
    assert kwargs == {"summarize_model": None, "tavily_cap": None}
    assert (at.query_params["page"], at.query_params["run"]) == ("suchplan", "r-1")


def test_the_choices_are_sent_with_the_approval() -> None:
    api = FakeApi(get_session=decision(), approve_session={"run_id": "r-1"})
    at = app(api)
    at.selectbox(key="summarize_model").set_value("gemma4:e2b")
    at.checkbox(key="cap_on").set_value(True).run()
    at.number_input(key="cap").set_value(10)
    button(at, "Freigeben").click().run()
    assert api.called("approve_session")[0][1] == {
        "summarize_model": "gemma4:e2b",
        "tavily_cap": 10,
    }


def test_an_edited_brief_must_be_taken_over_before_it_can_be_approved() -> None:
    api = FakeApi(get_session=decision(), edit_brief=view(busy=True))
    at = app(api)
    at.text_area(key=f"brief-{SHA}").set_value(BRIEF + "\n2. Neu?\n")
    at.run()
    assert button(at, "Freigeben").disabled
    button(at, "Text übernehmen").click().run()
    assert api.called("edit_brief")[0][0] == ("s1", BRIEF + "\n2. Neu?\n")


def test_a_stale_hash_is_explained_and_the_session_reloaded() -> None:
    api = FakeApi(get_session=decision(), approve_session=ApiError(409, "the hash does not belong"))
    at = app(api)
    button(at, "Freigeben").click().run()
    assert not at.exception
    assert any("geändert" in e.value for e in at.error)


def test_revise_and_save() -> None:
    api = FakeApi(get_session=decision(), revise=view(busy=True), save=view(status="saved"))
    at = app(api)
    at.text_input(key="feedback").set_value("kürzer")
    button(at, "Überarbeiten").click().run()
    assert api.called("revise")[0][0] == ("s1", "kürzer")
    button(app(api), "Speichern").click().run()
    assert api.called("save")[0][0] == ("s1",)


def test_the_settings_are_sent_when_changed() -> None:
    api = FakeApi(get_session=decision(), set_settings=view(busy=True))
    at = app(api)
    at.text_input(key="report_language").set_value("en")
    button(at, "Einstellungen übernehmen").click().run()
    ((args, kwargs),) = api.called("set_settings")
    assert args == ("s1",)
    assert kwargs == {
        "report_language": "en",
        "response_format": "structured",
        "template_id": "auto",
    }


def test_a_template_is_previewed_by_its_sections() -> None:
    at = app(FakeApi(get_session=decision()))
    text = " ".join(m.value for m in at.markdown)
    assert "Einleitung" in text
    assert "Befunde" in text


def test_an_approved_session_points_to_its_run() -> None:
    done = view(status="approved", waiting_for="nothing", questions=[], run_id="r-5")
    at = app(FakeApi(get_session=done))
    button(at, "Zum Suchplan").click().run()
    assert (at.query_params["page"], at.query_params["run"]) == ("suchplan", "r-5")
