import pytest
from pydantic import ValidationError
from support import make_settings

from app.brief.flow import (
    NOTE_QUESTION,
    build_context,
    final_checklist,
    initial_state,
    missing_items,
    resolve_settings,
    turn_answers,
)
from app.brief.models import Answer, AnswerKind, Checklist, ChecklistItem
from app.brief.protocol import (
    Approve,
    AskReply,
    Edit,
    OfferReply,
    Revise,
    SettingsChange,
    parse_decision,
)
from app.pipeline.profiles import load_response_formats
from app.templates import load_templates

S = make_settings()
TEMPLATES = load_templates([S.templates_dir])
FORMATS = load_response_formats(S.config_dir)


def answer(
    item: ChecklistItem, kind: AnswerKind = "accept", text: str = "", round_: int = 1
) -> Answer:
    return Answer(
        round=round_,
        item=item,
        question=f"Frage {item}?",
        candidate="Vorschlag",
        kind=kind,
        text=text,
    )


# ---- resume values --------------------------------------------------------------------------


def test_an_ask_reply_accepts_all_three_kinds() -> None:
    reply = AskReply.model_validate(
        {
            "answers": [
                {"kind": "accept"},
                {"kind": "text", "text": "Mein Text"},
                {"kind": "unknown"},
            ]
        }
    )
    assert [a.kind for a in reply.answers] == ["accept", "text", "unknown"]
    assert (reply.note, reply.genug) == ("", False)


@pytest.mark.parametrize(
    "bad",
    [
        {"answers": [{"kind": "text", "text": "   "}]},
        {"answers": [{"kind": "text"}]},
        {"answers": [{"kind": "maybe"}]},
        {"answers": "none"},
        {},
    ],
)
def test_a_bad_ask_reply_is_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AskReply.model_validate(bad)


def test_genug_and_a_note_can_come_without_answers() -> None:
    reply = AskReply.model_validate({"answers": [], "genug": True, "note": "Noch etwas."})
    assert (reply.genug, reply.note) == (True, "Noch etwas.")


def test_an_offer_reply_is_a_flag() -> None:
    assert OfferReply.model_validate({"strengthen": True}).strengthen is True
    with pytest.raises(ValidationError):
        OfferReply.model_validate({})


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        (
            {
                "action": "approve",
                "sha256": "ab" * 32,
                "tier": "full",
                "at": "2026-10-02T09:30:15+00:00",
            },
            Approve,
        ),
        ({"action": "revise", "feedback": "Mehr zu Kosten"}, Revise),
        ({"action": "edit", "text": "# T\n\n1. Q\n"}, Edit),
        ({"action": "settings", "response_format": "short"}, SettingsChange),
    ],
)
def test_decisions_parse_by_their_action(value: dict[str, object], kind: type) -> None:
    assert isinstance(parse_decision(value), kind)


def test_save_is_a_decision_too() -> None:
    assert parse_decision({"action": "save"}).action == "save"


@pytest.mark.parametrize(
    "bad",
    [
        {"action": "approve", "tier": "full", "at": "2026-10-02T09:30:15+00:00"},
        {
            "action": "approve",
            "sha256": "nothex",
            "tier": "full",
            "at": "2026-10-02T09:30:15+00:00",
        },
        {
            "action": "approve",
            "sha256": "AB" * 32,
            "tier": "full",
            "at": "2026-10-02T09:30:15+00:00",
        },
        {"action": "approve", "sha256": "ab" * 32, "tier": "full", "at": "2026-10-02T09:30:15"},
        {
            "action": "approve",
            "sha256": "ab" * 32,
            "tier": "auto",
            "at": "2026-10-02T09:30:15+00:00",
        },
        {"action": "approve", "sha256": "ab" * 32, "tier": "full", "at": "not a time"},
        {"action": "revise", "feedback": "  "},
        {"action": "edit", "text": ""},
        {"action": "settings"},
        {"action": "settings", "report_language": "deutsch"},
        {"action": "settings", "response_format": "essay"},
        {"action": "delete"},
        {},
    ],
)
def test_a_bad_decision_is_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        parse_decision(bad)


# ---- checklist helpers ----------------------------------------------------------------------


def test_answered_items_become_clear_but_unknown_ones_do_not() -> None:
    checklist: Checklist = {
        "audience": "missing",
        "scope": "assumed",
        "goal": "missing",
        "context": "missing",
    }
    answers = [
        answer("audience"),
        answer("scope", "text", "Nur Forschungsreaktoren"),
        answer("goal", "unknown"),
    ]
    result = final_checklist(checklist, answers)
    assert result == {
        "audience": "clear",
        "scope": "clear",
        "goal": "missing",
        "context": "missing",
    }
    assert checklist["audience"] == "missing"  # the input is not changed


def test_an_accepted_blank_candidate_clears_nothing() -> None:
    blank = Answer(round=1, item="scope", question="Was?", candidate="  ", kind="accept")
    assert final_checklist({"scope": "missing"}, [blank]) == {"scope": "missing"}


def test_a_note_does_not_clear_the_context_item() -> None:
    checklist: Checklist = {"context": "missing"}
    note = Answer(
        round=1, item="context", question=NOTE_QUESTION, candidate="", kind="text", text="Mehr"
    )
    assert final_checklist(checklist, [note]) == {"context": "missing"}


def test_only_content_items_can_be_open() -> None:
    checklist: Checklist = {
        "question": "missing",
        "context": "missing",
        "goal": "assumed",
        "audience": "missing",
        "scope": "clear",
        "output": "missing",
        "depth": "missing",
    }
    assert missing_items(checklist) == ("context", "audience")  # in checklist order


# ---- settings -------------------------------------------------------------------------------


def settings_state(**chosen: str | None) -> dict[str, str | None]:
    return {"report_language": None, "response_format": None, "template_id": None, **chosen}


def test_defaults_are_the_interview_language_auto_and_the_templates_format() -> None:
    result = resolve_settings(settings_state(), "fr", None, TEMPLATES)
    assert (result.report_language, result.template_id) == ("fr", "auto")
    assert result.response_format == TEMPLATES["auto"].default_response_format


def test_the_recommendation_decides_the_format_when_nothing_was_chosen() -> None:
    assert (
        resolve_settings(settings_state(), "de", "argumentative", TEMPLATES).response_format
        == "argumentative"
    )


def test_a_chosen_format_beats_the_recommendation() -> None:
    result = resolve_settings(
        settings_state(response_format="short"), "de", "argumentative", TEMPLATES
    )
    assert result.response_format == "short"


def test_a_chosen_template_supplies_its_default_format() -> None:
    result = resolve_settings(
        settings_state(template_id="literaturuebersicht"), "de", None, TEMPLATES
    )
    assert result.template_id == "literaturuebersicht"
    assert result.response_format == "argumentative"


def test_chosen_values_win_everywhere() -> None:
    chosen = settings_state(
        report_language="en", response_format="structured", template_id="regulatorische-analyse"
    )
    result = resolve_settings(chosen, "de", "short", TEMPLATES)
    assert (result.report_language, result.response_format, result.template_id) == (
        "en",
        "structured",
        "regulatorische-analyse",
    )


# ---- state and context ----------------------------------------------------------------------


def test_the_initial_state_is_plain_data() -> None:
    state = initial_state("s0123456789ab", "Meine Frage?", "de")
    assert state["session_id"] == "s0123456789ab"
    assert (state["round"], state["answers"], state["pending"], state["genug"]) == (
        0,
        [],
        [],
        False,
    )
    assert state["settings"] == settings_state()
    assert state["recommendation"] is None
    import json

    assert json.loads(json.dumps(state)) == state  # checkpoints store JSON


def test_the_context_collects_what_the_brief_needs() -> None:
    state = initial_state("s0123456789ab", "Frage?", "en")
    state["round"] = 2
    state["digest"] = "- Fakt (a.pdf, p. 1)"
    state["checklist"] = {"audience": "missing", "scope": "assumed", "goal": "missing"}
    state["answers"] = [
        answer("goal", "unknown").model_dump(),
        answer("audience", "accept").model_dump(),
    ]
    state["recommendation"] = {"tier": "full", "response_format": "structured", "rationale": "x"}
    ctx = build_context(state, TEMPLATES, FORMATS)
    assert ctx.interview_language == "en"
    assert ctx.rounds == 2
    assert ctx.missing == ("goal",)  # audience was answered, goal was not known
    assert ctx.unknown_questions == ("Frage goal?",)
    assert ctx.upload_digest == "- Fakt (a.pdf, p. 1)"
    assert ctx.settings.response_format == "structured"
    assert ctx.template is TEMPLATES["auto"]
    assert ctx.fmt is FORMATS.structured


def test_turn_answers_pairs_replies_with_the_pending_questions() -> None:
    pending = [
        {"item": "audience", "question": "Wer liest?", "candidate": "Ingenieure"},
        {"item": "scope", "question": "Was nicht?", "candidate": "Nichts"},
    ]
    reply = AskReply.model_validate(
        {"answers": [{"kind": "accept"}, {"kind": "text", "text": "Kraftwerke"}], "note": "Zusatz"}
    )
    reply.answers[1].text = "  Kraftwerke \n"  # padding is not part of the answer
    result = turn_answers(pending, reply, round_no=3)
    assert [(a.round, a.item, a.kind, a.value) for a in result] == [
        (3, "audience", "accept", "Ingenieure"),
        (3, "scope", "text", "Kraftwerke"),
        (3, "context", "text", "Zusatz"),
    ]
    assert result[2].question == NOTE_QUESTION


def test_a_reply_that_does_not_match_the_questions_is_an_error() -> None:
    pending = [{"item": "audience", "question": "Wer?", "candidate": "x"}]
    with pytest.raises(ValueError, match="1 question"):
        turn_answers(pending, AskReply.model_validate({"answers": []}), round_no=1)


def test_a_reply_without_questions_pending_may_only_carry_a_note() -> None:
    reply = AskReply.model_validate({"answers": [], "note": "Nur eine Notiz", "genug": True})
    assert [a.value for a in turn_answers([], reply, round_no=1)] == ["Nur eine Notiz"]
