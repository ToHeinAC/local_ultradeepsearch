from dataclasses import dataclass, field
from pathlib import Path

import pytest
from brief_rig import (
    DONE,
    FINISHED,
    PASTED,
    QUESTION,
    ROUND_ONE,
    TIER,
    Models,
    Rig,
    kinds,
    rig,
)

from app.brief.console import ConsoleIO, run_session
from app.llm.errors import LLMModelMissingError

SAME = "<the editor returns what it was shown>"


@dataclass
class Script:
    """A scripted terminal: it feeds the typed lines and records everything said and asked."""

    lines: list[str]
    edited: str | None = None
    said: list[str] = field(default_factory=lambda: [])
    asked: list[str] = field(default_factory=lambda: [])
    editor_saw: list[str] = field(default_factory=lambda: [])

    def io(self) -> ConsoleIO:
        return ConsoleIO(ask=self._ask, say=self.said.append, edit=self._edit)

    def _ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        assert self.lines, f"the script ran out of input at: {prompt!r}"
        return self.lines.pop(0)

    def _edit(self, text: str) -> str | None:
        self.editor_saw.append(text)
        return text if self.edited == SAME else self.edited

    @property
    def output(self) -> str:
        return "\n".join(self.said)


def started(tmp: Path, models: Models | None = None, question: str = QUESTION) -> tuple[Rig, str]:
    r = rig(tmp, models)
    return r, r.service.start(question).session_id


def play(r: Rig, session_id: str, *lines: str, edited: str | None = None) -> tuple[str, Script]:
    script = Script(list(lines), edited)
    outcome = run_session(r.service, script.io(), session_id)
    assert script.lines == [], f"unused input: {script.lines}"
    return outcome, script


# ---- the question rounds --------------------------------------------------------------------


def test_a_full_session_ends_with_an_approval(tmp_path: Path) -> None:
    r, sid = started(tmp_path)
    outcome, script = play(r, sid, "", "Kraftwerke", "", "f", "")  # accept, typed, no note; approve
    assert outcome == "approved"
    run = r.parts.runs.run_for_session(sid)
    assert run is not None
    assert run.tier == TIER["tier"]  # Enter takes the recommendation
    (archive,) = r.archives()
    assert str(archive) in script.output
    assert str(run.brief_sha256) in script.output
    assert run.run_id in script.output


def test_each_question_is_shown_with_its_proposal(tmp_path: Path) -> None:
    r, sid = started(tmp_path)
    _, script = play(r, sid, "", "", "", "q")
    assert "Wer liest den Bericht?" in script.output
    assert "Vorschlag: Ingenieure" in script.output
    assert "Runde 1 von höchstens" in script.output


def test_a_question_mark_means_the_owner_does_not_know(tmp_path: Path) -> None:
    r, sid = started(tmp_path)
    play(r, sid, "?", "?", "", "q")
    kinds = [
        a["kind"]
        for a in r.parts.graph.get_state({"configurable": {"thread_id": sid}}).values["answers"]
    ]
    assert kinds == ["unknown", "unknown"]


def test_typed_answers_and_proposals_are_told_apart(tmp_path: Path) -> None:
    r, sid = started(tmp_path)
    play(r, sid, "", "Eigene Antwort", "", "q")
    answers = r.parts.graph.get_state({"configurable": {"thread_id": sid}}).values["answers"]
    assert [(a["kind"], a["text"]) for a in answers] == [("accept", ""), ("text", "Eigene Antwort")]


def test_genug_ends_the_rounds_and_leaves_the_rest_unknown(tmp_path: Path) -> None:
    r, sid = started(tmp_path, Models(assessments=[ROUND_ONE]))
    outcome, _ = play(r, sid, "", "genug", "", "q")  # first question answered, then enough
    assert outcome == "quit"
    view = r.service.get(sid)
    assert view.waiting_for == "decision"
    assert r.models.count("assess") == 1  # no second round
    kinds = [
        a["kind"]
        for a in r.parts.graph.get_state({"configurable": {"thread_id": sid}}).values["answers"]
    ]
    assert kinds == ["accept", "unknown"]


@pytest.mark.parametrize("word", ["genug", "GENUG", " Genug "])
def test_genug_is_recognised_in_any_case(tmp_path: Path, word: str) -> None:
    r, sid = started(tmp_path, Models(assessments=[ROUND_ONE]))
    play(r, sid, word, "", "q")  # on the first question: the second is not even asked
    kinds = [
        a["kind"]
        for a in r.parts.graph.get_state({"configurable": {"thread_id": sid}}).values["answers"]
    ]
    assert kinds == ["unknown", "unknown"]
    assert r.service.get(sid).waiting_for == "decision"


def test_a_blank_reply_to_a_question_without_proposal_is_unknown(tmp_path: Path) -> None:
    blank = {
        **ROUND_ONE,
        "questions": [{"item": "scope", "question": "Was nicht?", "candidate": ""}],
    }
    r, sid = started(tmp_path, Models(assessments=[blank, DONE]))
    play(r, sid, "", "", "q")
    answers = r.parts.graph.get_state({"configurable": {"thread_id": sid}}).values["answers"]
    assert [a["kind"] for a in answers] == ["unknown"]


def test_a_note_after_the_questions_is_passed_on(tmp_path: Path) -> None:
    r, sid = started(tmp_path)
    play(r, sid, "", "", "Der Reaktor ist von 1962.", "q")
    answers = r.parts.graph.get_state({"configurable": {"thread_id": sid}}).values["answers"]
    assert "Der Reaktor ist von 1962." in [a["text"] for a in answers]


# ---- a pasted finished prompt ---------------------------------------------------------------


def test_a_finished_prompt_can_be_installed_unchanged(tmp_path: Path) -> None:
    r, sid = started(tmp_path, Models(assessments=[FINISHED]), PASTED)
    _, script = play(r, sid, "u", "q")
    assert "Fertiger Prompt" in script.output
    assert r.service.get(sid).brief_text is not None
    assert r.models.count("strengthen") == 0


def test_a_finished_prompt_can_be_strengthened(tmp_path: Path) -> None:
    r, sid = started(tmp_path, Models(assessments=[FINISHED]), PASTED)
    play(r, sid, "v", "q")
    assert r.models.count("strengthen") == 1


def test_a_prompt_that_cannot_be_installed_is_strengthened_after_an_explanation(
    tmp_path: Path,
) -> None:
    r, sid = started(tmp_path, Models(assessments=[FINISHED]), "Bitte analysiere den Rückbau.")
    _, script = play(r, sid, "u", "", "q")  # asks to install; then confirms the strengthening
    assert "kann nicht unverändert übernommen werden" in script.output
    assert r.models.count("strengthen") == 1


# ---- the decision ---------------------------------------------------------------------------


def test_the_decision_shows_the_brief_the_recommendation_and_the_settings(tmp_path: Path) -> None:
    r, sid = started(tmp_path)
    _, script = play(r, sid, "", "", "", "q")
    brief = r.service.get(sid).brief_text
    assert brief is not None
    assert brief in script.output
    assert "Empfehlung: Full" in script.output
    assert TIER["rationale"] in script.output
    assert "Berichtssprache: de" in script.output


@pytest.mark.parametrize(
    ("tier_input", "expected"), [("l", "light"), ("f", "full"), ("Lite", "light"), ("FULL", "full")]
)
def test_the_tier_can_be_chosen_explicitly(tmp_path: Path, tier_input: str, expected: str) -> None:
    r, sid = started(tmp_path)
    play(r, sid, "", "", "", "f", tier_input)
    run = r.parts.runs.run_for_session(sid)
    assert run is not None
    assert run.tier == expected


def test_enter_takes_the_lite_recommendation_too(tmp_path: Path) -> None:
    light = {**TIER, "tier": "light", "response_format": "short"}
    r, sid = started(tmp_path, Models(tier=light))
    _, script = play(r, sid, "", "", "", "f", "")
    run = r.parts.runs.run_for_session(sid)
    assert run is not None
    assert run.tier == "light"
    assert "Empfehlung: Lite" in script.output
    assert "Enter = Empfehlung: Lite" in "\n".join(script.asked)


def test_an_unknown_tier_is_asked_again(tmp_path: Path) -> None:
    r, sid = started(tmp_path)
    _, script = play(r, sid, "", "", "", "f", "mittel", "l")
    assert "Bitte l oder f eingeben" in script.output
    assert r.parts.runs.run_for_session(sid) is not None


def test_revise_asks_for_feedback_and_shows_the_new_brief(tmp_path: Path) -> None:
    r, sid = started(tmp_path)
    _, script = play(r, sid, "", "", "", "u", "Mehr zu den Kosten", "q")
    assert r.models.count("revise") == 1
    assert "Überarbeitet." in script.output


def test_blank_feedback_changes_nothing(tmp_path: Path) -> None:
    r, sid = started(tmp_path)
    _, script = play(r, sid, "", "", "", "u", "", "q")
    assert r.models.count("revise") == 0
    assert "Das ging nicht" not in script.output  # nothing was even sent


def test_edit_opens_the_editor_with_the_current_brief(tmp_path: Path) -> None:
    r, sid = started(tmp_path)
    edited = "# Mein Titel\n\n## Forschungsfragen\n\n1. Meine Frage\n"
    outcome, script = play(r, sid, "", "", "", "b", "q", edited=edited)
    assert outcome == "quit"
    (shown,) = script.editor_saw
    assert shown.startswith("# ")
    assert "## Forschungsfragen" in shown
    assert shown != edited
    assert r.service.get(sid).brief_text == edited


def test_an_editor_that_returns_the_same_text_is_no_edit(tmp_path: Path) -> None:
    r, sid = started(tmp_path)
    before = r.service.answer(sid, kinds("accept", "accept")).brief_sha256
    _, script = play(r, sid, "b", "q", edited=SAME)
    assert "Keine Änderung" in script.output
    assert r.service.get(sid).brief_sha256 == before
    assert "Das ging nicht" not in script.output


def test_an_unchanged_editor_result_is_no_edit(tmp_path: Path) -> None:
    r, sid = started(tmp_path)
    _, script = play(r, sid, "", "", "", "b", "q", edited=None)  # the editor was closed unchanged
    assert "Keine Änderung" in script.output
    assert r.service.get(sid).brief_text == script.editor_saw[0]


def test_an_edit_that_cannot_be_used_is_explained_and_the_loop_goes_on(tmp_path: Path) -> None:
    r, sid = started(tmp_path)
    _, script = play(r, sid, "", "", "", "b", "q", edited="Kein Titel und keine Fragen")
    assert "Das ging nicht" in script.output
    assert r.service.get(sid).brief_text == script.editor_saw[0]  # the brief is unchanged


def test_settings_are_asked_one_by_one_and_enter_keeps_the_current_value(tmp_path: Path) -> None:
    r, sid = started(tmp_path)
    play(r, sid, "", "", "", "e", "en", "", "literaturuebersicht", "q")
    view = r.service.get(sid)
    assert view.settings == {
        "report_language": "en",
        "response_format": "structured",
        "template_id": "literaturuebersicht",
    }


def test_the_available_templates_are_listed(tmp_path: Path) -> None:
    r, sid = started(tmp_path)
    _, script = play(r, sid, "", "", "", "e", "", "", "", "q")
    for template_id in ("auto", "literaturuebersicht", "technische-stellungnahme"):
        assert template_id in script.output
    assert "Keine Änderung" in script.output  # three blank answers change nothing
    assert "Das ging nicht" not in script.output


def test_a_single_changed_setting_leaves_the_others_alone(tmp_path: Path) -> None:
    r, sid = started(tmp_path)
    play(r, sid, "", "", "", "e", "", "", "literaturuebersicht", "q")
    view = r.service.get(sid)
    assert view.settings == {
        "report_language": "de",
        "response_format": "structured",  # the recommendation, not the template's own default
        "template_id": "literaturuebersicht",
    }


def test_an_invalid_setting_is_explained_and_nothing_changes(tmp_path: Path) -> None:
    r, sid = started(tmp_path)
    before = r.service.answer(sid, kinds("accept", "accept")).brief_sha256
    _, script = play(r, sid, "e", "", "", "gibt-es-nicht", "q")
    assert "Das ging nicht" in script.output
    assert r.service.get(sid).brief_sha256 == before


def test_save_parks_the_session_and_tells_where_the_draft_is(tmp_path: Path) -> None:
    r, sid = started(tmp_path)
    outcome, script = play(r, sid, "", "", "", "s")
    assert outcome == "saved"
    assert r.service.get(sid).status == "saved"
    assert f"{sid}.md" in script.output
    assert f"udr brief --session {sid}" in script.output


def test_quitting_says_how_to_continue(tmp_path: Path) -> None:
    r, sid = started(tmp_path)
    outcome, script = play(r, sid, "", "", "", "q")
    assert outcome == "quit"
    assert f"udr brief --session {sid}" in script.output


def test_an_unknown_menu_choice_is_asked_again(tmp_path: Path) -> None:
    r, sid = started(tmp_path)
    _, script = play(r, sid, "", "", "", "x", "q")
    assert "Bitte einen der Buchstaben" in script.output


@pytest.mark.parametrize(
    ("typed", "outcome"),
    [
        ("freigeben", "approved"),
        ("F", "approved"),
        ("speichern", "saved"),
        ("S", "saved"),
        ("Q", "quit"),
        ("beenden", "quit"),
    ],
)
def test_menu_choices_accept_letters_words_and_either_case(
    tmp_path: Path, typed: str, outcome: str
) -> None:
    r, sid = started(tmp_path)
    extra = ["l"] if outcome == "approved" else []
    result, _ = play(r, sid, "", "", "", typed, *extra)
    assert result == outcome


# ---- model errors and resuming --------------------------------------------------------------


def test_a_stopped_session_offers_a_retry(tmp_path: Path) -> None:
    models = Models(
        assessments=[ROUND_ONE, ROUND_ONE, DONE],  # the failed call used the first
        errors={"assess": LLMModelMissingError("qwen fehlt")},
    )
    r = rig(tmp_path, models)
    sid = r.service.start(QUESTION).session_id
    models.errors.clear()  # the owner fixed the model
    outcome, script = play(r, sid, "j", "", "", "", "q")  # retry, two answers, no note, leave
    assert "unterbrochen" in script.output
    assert outcome == "quit"
    assert r.service.get(sid).waiting_for == "decision"


def test_a_retry_that_fails_again_shows_the_error(tmp_path: Path) -> None:
    models = Models(assessments=[ROUND_ONE], errors={"assess": LLMModelMissingError("qwen fehlt")})
    r = rig(tmp_path, models)
    sid = r.service.start(QUESTION).session_id
    outcome, script = play(r, sid, "j", "n")
    assert "LLMModelMissingError" in script.output
    assert "qwen fehlt" in script.output
    assert outcome == "quit"


def test_declining_a_retry_leaves_the_session_for_later(tmp_path: Path) -> None:
    models = Models(assessments=[ROUND_ONE], errors={"assess": LLMModelMissingError("weg")})
    r = rig(tmp_path, models)
    sid = r.service.start(QUESTION).session_id
    outcome, script = play(r, sid, "n")
    assert outcome == "quit"
    assert f"udr brief --session {sid}" in script.output


def test_a_resumed_session_continues_where_it_waits(tmp_path: Path) -> None:
    r, sid = started(tmp_path)
    play(r, sid, "", "", "", "q")  # answers the questions, then leaves at the decision
    outcome, script = play(r, sid, "f", "l")  # a new run of the command goes straight to the menu
    assert outcome == "approved"
    assert "Runde" not in script.output  # the questions are not asked again


def test_an_approved_session_just_reports_its_state(tmp_path: Path) -> None:
    r, sid = started(tmp_path)
    play(r, sid, "", "", "", "f", "l")
    outcome, script = play(r, sid)
    assert outcome == "approved"
    assert "bereits freigegeben" in script.output
