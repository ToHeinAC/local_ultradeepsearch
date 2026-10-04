import json
import threading
from pathlib import Path
from typing import Any

import pytest
from brief_rig import (
    DONE,
    FINISHED,
    LIMITS,
    PASTED,
    QUESTION,
    ROUND_ONE,
    TIER,
    Crash,
    Models,
    Rig,
    a_pdf,
    kinds,
    rig,
)
from support import make_docx, make_pdf

from app.brief.errors import (
    InvalidInput,
    NotFound,
    StaleBrief,
    UploadRejected,
    WrongState,
)
from app.brief.render import brief_sha256, canonical_text
from app.brief.service import SessionView
from app.brief.uploads import UploadFile
from app.graphs.brief import BriefRunner
from app.llm.errors import LLMModelMissingError


@pytest.fixture
def r(tmp_path: Path) -> Rig:
    return rig(tmp_path)


def at_decision(r: Rig) -> SessionView:
    view = r.service.start(QUESTION)
    return r.service.answer(view.session_id, kinds("accept", "accept"))


# ---- starting -------------------------------------------------------------------------------


@pytest.mark.parametrize("empty", ["", "   ", "\n\t"])
def test_an_empty_question_is_invalid_and_creates_no_session(r: Rig, empty: str) -> None:
    with pytest.raises(InvalidInput, match="question"):
        r.service.start(empty)
    assert r.service.list_sessions() == []


def test_the_first_view_waits_for_the_answers_to_the_first_questions(r: Rig) -> None:
    view = r.service.start(QUESTION)
    assert view.waiting_for == "questions"
    assert (view.status, view.round, view.max_rounds) == ("interviewing", 0, LIMITS.max_rounds)
    assert list(view.questions) == ROUND_ONE["questions"]
    assert view.checklist["audience"] == "missing"
    assert (view.brief_text, view.run_id, view.error) == (None, None, None)
    assert view.uploads == ()


@pytest.mark.parametrize(
    ("message", "language"),
    [
        ("Wie lange dauert der Rückbau eines Forschungsreaktors in Deutschland?", "de"),
        ("How long does it take to dismantle a research reactor in Germany?", "en"),
        ("Combien de temps faut-il pour démanteler un réacteur de recherche en France ?", "fr"),
        ("KKW costs?", "de"),  # too short to be sure: German
    ],
)
def test_the_interview_language_comes_from_the_first_message(
    tmp_path: Path, message: str, language: str
) -> None:
    """M4 AC7."""
    r = rig(tmp_path)
    view = r.service.start(message)
    assert view.interview_language == language
    named = {"de": "German", "en": "English", "fr": "French"}[language]
    assert f"Interview language: {named}" in r.models.prompts_seen["assess"][0]


def test_uploads_given_at_the_start_appear_in_the_view_with_their_state(r: Rig) -> None:
    view = r.service.start(QUESTION, [a_pdf()])
    (upload,) = view.uploads
    assert (upload.name, upload.kind, upload.stage, upload.warnings) == (
        "a.pdf",
        "pdf",
        "distilled",
        (),
    )
    assert upload.pages == 1


@pytest.mark.parametrize(
    "bad",
    [UploadFile("x.xlsx", b"PK"), UploadFile("secret.pdf", b"nope"), UploadFile("empty.txt", b"")],
)
def test_a_rejected_upload_creates_no_session_and_stores_nothing(r: Rig, bad: UploadFile) -> None:
    """M4 AC8 at service level."""
    with pytest.raises(UploadRejected):
        r.service.start(QUESTION, [a_pdf(), bad])
    assert r.service.list_sessions() == []
    assert r.files() == []
    assert r.models.total() == 0  # no model was asked before the upload was refused


def test_too_many_files_are_rejected_at_the_start(r: Rig) -> None:
    files = [UploadFile(f"{n}.txt", f"Text {n}".encode()) for n in range(LIMITS.max_files + 1)]
    with pytest.raises(UploadRejected, match="at most"):
        r.service.start(QUESTION, files)
    assert r.service.list_sessions() == []


# ---- answering ------------------------------------------------------------------------------


def test_answering_moves_on_to_the_decision(r: Rig) -> None:
    view = at_decision(r)
    assert view.waiting_for == "decision"
    assert view.status == "awaiting_decision"
    assert view.brief_text is not None
    assert view.brief_sha256 == brief_sha256(view.brief_text)
    assert view.recommendation == TIER
    assert view.settings == {
        "report_language": "de",
        "response_format": "structured",
        "template_id": "auto",
    }
    assert view.round == 1


def test_the_wrong_number_of_answers_is_invalid_and_changes_nothing(r: Rig) -> None:
    view = r.service.start(QUESTION)
    with pytest.raises(InvalidInput, match="2 question"):
        r.service.answer(view.session_id, kinds("accept"))
    assert r.service.get(view.session_id).waiting_for == "questions"
    assert r.models.count("assess") == 1


def test_an_answer_is_only_accepted_while_questions_are_open(r: Rig) -> None:
    view = at_decision(r)
    with pytest.raises(WrongState):
        r.service.answer(view.session_id, kinds("accept", "accept"))


def test_unknown_sessions_are_not_found(r: Rig) -> None:
    for call in (
        lambda: r.service.get("s000000000000"),
        lambda: r.service.answer("s000000000000", []),
        lambda: r.service.revise("s000000000000", "x"),
        lambda: r.service.save("s000000000000"),
        lambda: r.service.approve("s000000000000", "0" * 64, "light"),
        lambda: r.service.add_files("s000000000000", []),
    ):
        with pytest.raises(NotFound):
            call()


def test_genug_ends_the_rounds_and_lists_what_is_missing(tmp_path: Path) -> None:
    r = rig(tmp_path, Models(assessments=[ROUND_ONE]))
    view = r.service.start(QUESTION)
    done = r.service.answer(view.session_id, kinds("unknown", "unknown"), genug=True)
    assert done.waiting_for == "decision"
    assert done.brief_text is not None
    assert "- Nicht geklärt: Zielgruppe" in done.brief_text


def test_two_answers_at_once_do_not_interleave(tmp_path: Path) -> None:
    r = rig(tmp_path)
    view = r.service.start(QUESTION)
    outcomes: list[object] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        try:
            outcomes.append(r.service.answer(view.session_id, kinds("accept", "accept")))
        except WrongState as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(type(o).__name__ for o in outcomes) == ["SessionView", "WrongState"]
    assert r.models.count("draft") == 1


# ---- uploads while interviewing -------------------------------------------------------------


def test_files_can_be_added_while_questions_are_open_and_are_read_next_round(
    tmp_path: Path,
) -> None:
    more = {**ROUND_ONE, "questions": [{"item": "goal", "question": "Wofür?", "candidate": "x"}]}
    r = rig(tmp_path, Models(assessments=[ROUND_ONE, more, DONE]))
    view = r.service.start(QUESTION)
    added = r.service.add_files(view.session_id, [a_pdf()])
    assert [u.stage for u in added.uploads] == ["stored"]  # accepted, not yet read
    assert r.models.count("facts") == 0
    after = r.service.answer(view.session_id, kinds("accept", "accept"))
    assert [u.stage for u in after.uploads] == ["distilled"]
    assert "Ein Fakt aus der Datei." in r.models.prompts_seen["assess"][1]


def test_files_added_in_the_last_round_are_read_before_the_draft(r: Rig) -> None:
    view = r.service.start(QUESTION)
    r.service.add_files(view.session_id, [a_pdf()])
    after = r.service.answer(view.session_id, kinds("accept", "accept"), genug=True)
    assert [u.stage for u in after.uploads] == ["distilled"]
    assert "Ein Fakt aus der Datei." in r.models.prompts_seen["draft"][0]
    assert r.models.count("assess") == 1  # "genug" still ends the rounds


def test_files_cannot_be_added_once_the_brief_is_drafted(r: Rig) -> None:
    view = at_decision(r)
    with pytest.raises(WrongState):
        r.service.add_files(view.session_id, [a_pdf()])


def test_a_rejected_added_file_changes_nothing(r: Rig) -> None:
    view = r.service.start(QUESTION)
    with pytest.raises(UploadRejected):
        r.service.add_files(view.session_id, [a_pdf(), UploadFile("x.xlsx", b"PK")])
    assert r.service.get(view.session_id).uploads == ()
    assert r.files() == []


# ---- a pasted finished prompt ---------------------------------------------------------------


def test_a_finished_prompt_is_offered_and_can_be_installed(tmp_path: Path) -> None:
    r = rig(tmp_path, Models(assessments=[FINISHED]))
    view = r.service.start(PASTED)
    assert view.waiting_for == "offer"
    assert view.pasted == PASTED
    done = r.service.choose_offer(view.session_id, strengthen=False)
    assert done.waiting_for == "decision"
    assert done.brief_text is not None
    assert done.brief_text.startswith("Method: Prompt wörtlich übernommen\n\n# Wie teuer")


def test_strengthening_a_pasted_prompt_calls_the_model(tmp_path: Path) -> None:
    r = rig(tmp_path, Models(assessments=[FINISHED]))
    view = r.service.start(PASTED)
    r.service.choose_offer(view.session_id, strengthen=True)
    assert r.models.count("strengthen") == 1


def test_a_prompt_that_cannot_be_installed_must_be_strengthened(tmp_path: Path) -> None:
    r = rig(tmp_path, Models(assessments=[FINISHED]))
    view = r.service.start("Bitte analysiere den Rückbau ohne Titel und ohne Liste.")
    refused = r.service.choose_offer(view.session_id, strengthen=False)
    assert refused.waiting_for == "offer"
    assert "title" in refused.refusal
    with pytest.raises(InvalidInput, match="strengthen"):
        r.service.choose_offer(view.session_id, strengthen=False)  # no point asking again
    assert r.service.choose_offer(view.session_id, strengthen=True).waiting_for == "decision"


def test_the_offer_is_only_valid_while_it_is_open(r: Rig) -> None:
    view = r.service.start(QUESTION)
    with pytest.raises(WrongState):
        r.service.choose_offer(view.session_id, strengthen=True)


# ---- revising, editing, settings, saving ----------------------------------------------------


def test_revise_gives_a_new_hash_and_the_old_one_no_longer_approves(r: Rig) -> None:
    """M4 AC4."""
    view = at_decision(r)
    revised = r.service.revise(view.session_id, "Mehr zu den Kosten")
    assert revised.brief_sha256 != view.brief_sha256
    assert revised.waiting_for == "decision"
    with pytest.raises(StaleBrief):
        r.service.approve(view.session_id, str(view.brief_sha256), "light")
    assert r.parts.runs.run_for_session(view.session_id) is None


@pytest.mark.parametrize("feedback", ["", "   "])
def test_revise_needs_feedback(r: Rig, feedback: str) -> None:
    view = at_decision(r)
    with pytest.raises(InvalidInput):
        r.service.revise(view.session_id, feedback)
    assert r.models.count("revise") == 0


def test_a_hand_edit_replaces_the_text_and_the_hash(r: Rig) -> None:
    view = at_decision(r)
    edited = r.service.edit(
        view.session_id, "# Mein Titel\r\n\r\n## Forschungsfragen\r\n\r\n1. Meine Frage\r\n"
    )
    assert edited.brief_text == "# Mein Titel\n\n## Forschungsfragen\n\n1. Meine Frage\n"
    assert edited.brief_sha256 == brief_sha256(edited.brief_text)


@pytest.mark.parametrize(
    "text",
    ["", "   ", "Kein Titel\n\n1. Frage\n", "# Titel\n\nKeine Fragen.\n"],
)
def test_an_edit_that_could_not_be_used_is_refused_and_changes_nothing(r: Rig, text: str) -> None:
    view = at_decision(r)
    with pytest.raises(InvalidInput):
        r.service.edit(view.session_id, text)
    assert r.service.get(view.session_id).brief_sha256 == view.brief_sha256


def test_settings_change_the_output_section_and_are_reported(r: Rig) -> None:
    view = at_decision(r)
    changed = r.service.set_settings(
        view.session_id,
        report_language="en",
        response_format="short",
        template_id="literaturuebersicht",
    )
    assert changed.settings == {
        "report_language": "en",
        "response_format": "short",
        "template_id": "literaturuebersicht",
    }
    assert changed.brief_text is not None
    assert "**Berichtssprache: Englisch.**" in changed.brief_text
    assert changed.brief_sha256 != view.brief_sha256


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"template_id": "gibt-es-nicht"},
        {"report_language": "deutsch"},
        {"report_language": "D"},
        {"response_format": "essay"},
    ],
)
def test_invalid_settings_are_refused_and_change_nothing(r: Rig, kwargs: dict[str, Any]) -> None:
    view = at_decision(r)
    with pytest.raises(InvalidInput):
        r.service.set_settings(view.session_id, **kwargs)
    assert r.service.get(view.session_id).brief_sha256 == view.brief_sha256


def test_save_parks_the_session_and_any_action_continues_it(r: Rig) -> None:
    view = at_decision(r)
    parked = r.service.save(view.session_id)
    assert parked.status == "saved"
    draft = r.tmp / "data" / "briefs" / "drafts" / f"{view.session_id}.md"
    assert draft.read_text(encoding="utf-8") == view.brief_text
    assert parked.draft_path == str(draft)
    assert view.draft_path is None
    again = r.service.get(view.session_id)
    assert (again.status, again.waiting_for) == ("saved", "decision")
    resumed = r.service.set_settings(view.session_id, report_language="en")
    assert resumed.status == "awaiting_decision"


def test_decisions_need_a_pending_decision(r: Rig) -> None:
    view = r.service.start(QUESTION)  # still asking questions
    for call in (
        lambda: r.service.revise(view.session_id, "x"),
        lambda: r.service.edit(view.session_id, "# T\n\n1. Q\n"),
        lambda: r.service.set_settings(view.session_id, report_language="en"),
        lambda: r.service.save(view.session_id),
        lambda: r.service.approve(view.session_id, "0" * 64, "light"),
    ):
        with pytest.raises(WrongState):
            call()


# ---- approval (M4 AC1, AC2) -----------------------------------------------------------------


def test_approval_creates_the_run_and_archives_the_exact_bytes(r: Rig) -> None:
    view = at_decision(r)
    assert view.brief_sha256 is not None
    done = r.service.approve(
        view.session_id, view.brief_sha256, "full", summarize_model="gemma4:e2b"
    )
    assert (done.status, done.waiting_for) == ("approved", "nothing")
    run = r.parts.runs.run_for_session(view.session_id)
    assert run is not None
    assert (done.run_id, run.tier, run.summarize_model, run.status) == (
        run.run_id,
        "full",
        "gemma4:e2b",
        "queued",
    )
    (archive,) = r.archives()
    assert archive.name == "2026-10-02T09-30-15Z.md"  # named after the service's clock
    assert archive.read_bytes() == str(view.brief_text).encode("utf-8")
    assert done.archive_path == str(archive)
    assert run.brief_sha256 == view.brief_sha256


def test_approval_freezes_the_effective_settings_into_the_run(r: Rig) -> None:
    view = at_decision(r)
    r.service.approve(view.session_id, str(view.brief_sha256), "full", summarize_model="gemma4:e2b")
    run = r.parts.runs.run_for_session(view.session_id)
    assert run is not None
    assert json.loads(str(run.settings_json)) == {
        "report_language": "de",  # the interview language: the owner chose none
        "response_format": "structured",  # the recommendation
        "template_id": "auto",
        "interview_language": "de",
        "tier": "full",
        "summarize_model": "gemma4:e2b",
    }


def test_settings_the_owner_chose_are_the_ones_frozen(r: Rig) -> None:
    view = at_decision(r)
    changed = r.service.set_settings(
        view.session_id,
        report_language="en",
        response_format="short",
        template_id="literaturuebersicht",
    )
    r.service.approve(view.session_id, str(changed.brief_sha256), "light")
    run = r.parts.runs.run_for_session(view.session_id)
    assert run is not None
    settings = json.loads(str(run.settings_json))
    assert (settings["report_language"], settings["response_format"], settings["template_id"]) == (
        "en",
        "short",
        "literaturuebersicht",
    )
    assert settings["interview_language"] == "de"
    assert settings["summarize_model"] is None


def test_tier_auto_applies_the_recommendation(tmp_path: Path) -> None:
    r = rig(tmp_path, Models(tier={**TIER, "tier": "light"}))
    view = at_decision(r)
    r.service.approve(view.session_id, str(view.brief_sha256), "auto")
    run = r.parts.runs.run_for_session(view.session_id)
    assert run is not None
    assert run.tier == "light"


@pytest.mark.parametrize("stale", ["0" * 64, "f" * 64])
def test_a_stale_hash_is_rejected_and_creates_no_run(r: Rig, stale: str) -> None:
    """M4 AC1."""
    view = at_decision(r)
    with pytest.raises(StaleBrief):
        r.service.approve(view.session_id, stale, "light")
    assert r.parts.runs.run_for_session(view.session_id) is None
    assert r.archives() == []
    assert r.service.get(view.session_id).waiting_for == "decision"


def test_a_malformed_hash_is_invalid_input(r: Rig) -> None:
    view = at_decision(r)
    with pytest.raises(InvalidInput):
        r.service.approve(view.session_id, "not-a-hash", "light")


def test_a_tier_must_be_light_full_or_auto(r: Rig) -> None:
    view = at_decision(r)
    with pytest.raises(InvalidInput, match="tier"):
        r.service.approve(view.session_id, str(view.brief_sha256), "dissertation")  # type: ignore[arg-type]


def test_a_second_approval_is_a_conflict(r: Rig) -> None:
    view = at_decision(r)
    r.service.approve(view.session_id, str(view.brief_sha256), "light")
    with pytest.raises(WrongState):
        r.service.approve(view.session_id, str(view.brief_sha256), "light")
    assert r.parts.db.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_concurrent_approvals_make_exactly_one_run(r: Rig) -> None:
    view = at_decision(r)
    sha = str(view.brief_sha256)
    barrier = threading.Barrier(6)
    outcomes: list[str] = []

    def worker() -> None:
        barrier.wait()
        try:
            r.service.approve(view.session_id, sha, "light")
            outcomes.append("approved")
        except WrongState:
            outcomes.append("conflict")

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["approved"] + ["conflict"] * 5
    assert len(r.archives()) == 1
    assert r.parts.db.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_approval_works_after_save_and_hand_edit(r: Rig) -> None:
    view = at_decision(r)
    r.service.save(view.session_id)
    edited = r.service.edit(
        view.session_id, "# Titel mit Ümlaut\n\n1. Frage <think>bleibt</think>\n"
    )
    done = r.service.approve(view.session_id, str(edited.brief_sha256), "light")
    assert done.status == "approved"
    (archive,) = r.archives()
    assert (
        archive.read_bytes()
        == canonical_text("# Titel mit Ümlaut\n\n1. Frage <think>bleibt</think>\n").encode()
    )


# ---- model errors ---------------------------------------------------------------------------


def test_a_model_error_at_the_start_keeps_the_session_and_can_be_retried(tmp_path: Path) -> None:
    models = Models(
        assessments=[ROUND_ONE], errors={"assess": LLMModelMissingError("qwen is not installed")}
    )
    r = rig(tmp_path, models)
    view = r.service.start(QUESTION)
    assert view.waiting_for == "work"
    assert view.error is not None
    assert "LLMModelMissingError" in view.error
    models.errors.clear()  # the owner fixed the model
    healed = r.service.retry(view.session_id)
    assert (healed.waiting_for, healed.error) == ("questions", None)


def test_a_model_error_while_drafting_keeps_the_answers(tmp_path: Path) -> None:
    models = Models(errors={"draft": LLMModelMissingError("gone")})
    r = rig(tmp_path, models)
    view = r.service.start(QUESTION)
    stuck = r.service.answer(view.session_id, kinds("accept", "accept"))
    assert (stuck.waiting_for, stuck.round) == ("work", 1)
    assert stuck.error is not None
    models.errors.clear()
    done = r.service.retry(view.session_id)
    assert done.waiting_for == "decision"
    assert models.count("assess") == 2  # the interview was not repeated


def test_retry_without_an_error_does_not_touch_the_graph(
    r: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = r.service.start(QUESTION)
    touched: list[str] = []
    monkeypatch.setattr(BriefRunner, "proceed", lambda self, sid: touched.append(sid))
    assert r.service.retry(view.session_id).waiting_for == "questions"
    assert touched == []
    assert r.models.count("assess") == 1


# ---- restart and recovery (M4 AC6, AD10) ----------------------------------------------------


def test_a_session_survives_a_restart_at_its_pending_questions(tmp_path: Path) -> None:
    first = rig(tmp_path)
    view = first.service.start(QUESTION)
    second = rig(tmp_path, Models(assessments=[DONE]))  # a new process on the same files
    again = second.service.get(view.session_id)
    assert again.waiting_for == "questions"
    assert list(again.questions) == ROUND_ONE["questions"]
    done = second.service.answer(view.session_id, kinds("accept", "accept"))
    assert done.waiting_for == "decision"


def test_a_session_survives_a_restart_at_its_decision(tmp_path: Path) -> None:
    first = rig(tmp_path)
    view = at_decision(first)
    second = rig(tmp_path, Models())
    done = second.service.approve(view.session_id, str(view.brief_sha256), "light")
    assert done.status == "approved"


def test_recover_finishes_a_crash_between_interrupts_without_repeating_work(tmp_path: Path) -> None:
    first = rig(tmp_path, Models(crash_on={"draft": 1}))
    view = first.service.start(QUESTION)
    with pytest.raises(Crash):
        first.service.answer(view.session_id, kinds("accept", "accept"))

    second = rig(tmp_path, Models())  # a restarted process
    assert second.service.get(view.session_id).waiting_for == "work"
    assert second.service.recover() == [view.session_id]
    done = second.service.get(view.session_id)
    assert done.waiting_for == "decision"
    assert second.models.count("assess") == 0  # the questions were not asked again
    assert second.models.count("draft") == 1


def test_recover_continues_a_crashed_upload_distillation(tmp_path: Path) -> None:
    first = rig(tmp_path, Models(crash_on={"facts": 1}))
    with pytest.raises(Crash):
        first.service.start(QUESTION, [a_pdf()])
    (row,) = first.parts.sessions.list_sessions()
    second = rig(tmp_path, Models())
    assert second.service.recover() == [row.session_id]
    view = second.service.get(row.session_id)
    assert view.waiting_for == "questions"
    assert [u.stage for u in view.uploads] == ["distilled"]
    assert second.models.count("facts") == 1


def test_recover_removes_a_session_that_never_got_going(tmp_path: Path) -> None:
    first = rig(tmp_path)
    orphan = first.parts.sessions.create("de")  # `start` died after creating the row
    first.parts.ingestor.accept(orphan.session_id, [a_pdf()])
    kept = first.service.start(QUESTION).session_id
    assert first.service.recover() == [orphan.session_id]
    assert [s.session_id for s in first.parts.sessions.list_sessions()] == [kept]
    assert len(first.files()) == 0  # the orphan's upload files went with it


def test_recover_leaves_waiting_and_finished_sessions_alone(tmp_path: Path) -> None:
    # assessments in call order: waiting.start, decided.start, decided.answer, approved.start, ...
    r = rig(tmp_path, Models(assessments=[ROUND_ONE, ROUND_ONE, DONE, ROUND_ONE, DONE]))
    waiting = r.service.start(QUESTION).session_id
    decided = at_decision(r).session_id
    approved_view = at_decision(r)
    r.service.approve(approved_view.session_id, str(approved_view.brief_sha256), "light")
    before = r.models.total()
    assert r.service.recover() == []
    assert r.models.total() == before
    assert r.service.get(waiting).waiting_for == "questions"
    assert r.service.get(decided).waiting_for == "decision"


def test_recover_never_touches_an_approved_session_even_without_its_checkpoint(
    tmp_path: Path,
) -> None:
    first = rig(tmp_path)
    view = at_decision(first)
    first.service.approve(view.session_id, str(view.brief_sha256), "light")
    for path in tmp_path.glob("checkpoints.sqlite*"):
        path.unlink()  # the conversation is gone, the run and its brief are not
    second = rig(tmp_path)
    assert second.service.recover() == []
    assert second.parts.sessions.get(view.session_id) is not None
    assert second.parts.runs.run_for_session(view.session_id) is not None


def test_recover_cleans_orphan_upload_files(tmp_path: Path) -> None:
    r = rig(tmp_path)
    view = r.service.start(QUESTION, [a_pdf()])
    folder = tmp_path / "uploads" / view.session_id
    (folder / ("0" * 64 + ".pdf")).write_bytes(b"left by a crashed accept")
    r.service.recover()
    assert len(list(folder.iterdir())) == 1


# ---- listing --------------------------------------------------------------------------------


def test_the_templates_a_session_can_choose_from_are_listed(r: Rig) -> None:
    choices = dict(r.service.templates())
    assert choices["auto"] == "Automatisch"
    assert choices["literaturuebersicht"] == "Literaturübersicht"
    assert len(choices) == 5


def test_sessions_are_listed_newest_first(r: Rig) -> None:
    first = r.service.start(QUESTION).session_id
    second = r.service.start(QUESTION).session_id
    assert [s.session_id for s in r.service.list_sessions()] == [second, first]


# ---- the whole of Phase 1 stays on this machine (M4 AC3) ------------------------------------


def test_a_full_session_with_uploads_makes_no_outbound_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    attempts: list[object] = []

    def record(*args: object, **_kwargs: object) -> None:
        attempts.append(args)
        raise RuntimeError("no network in Phase 1")

    monkeypatch.setattr(socket.socket, "connect", record)
    monkeypatch.setattr(socket.socket, "connect_ex", record)
    import app.adapters.outbound.gateway as gateway

    for name in ("fetch", "search", "search_all", "prepare_queries"):
        if hasattr(gateway.OutboundGateway, name):
            monkeypatch.setattr(
                gateway.OutboundGateway, name, lambda *_a, _n=name, **_k: attempts.append(_n)
            )

    r = rig(tmp_path)
    docx = UploadFile(
        "b.docx", make_docx(["Ein Absatz mit genug Text, damit der Test etwas liest. " * 3])
    )
    scanned = UploadFile(
        "scan.pdf",
        make_pdf(["", "Zweite Seite mit ausreichend Text für den Test, ohne OCR nötig."]),
    )
    view = r.service.start(QUESTION, [a_pdf(), docx, scanned])
    done = r.service.answer(view.session_id, kinds("accept", "accept"))
    revised = r.service.revise(done.session_id, "Mehr Details")
    r.service.save(revised.session_id)
    r.service.approve(revised.session_id, str(revised.brief_sha256), "light")
    assert attempts == []


def test_phase_one_modules_never_load_the_outbound_package() -> None:
    import subprocess
    import sys

    code = (
        "import sys\n"
        "import app.brief.service, app.graphs.brief, app.brief.uploads, app.store.sessions\n"
        "print(sorted(m for m in sys.modules if m.startswith('app.adapters.outbound')))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert (done.returncode, done.stdout.strip()) == (0, "[]"), done.stderr


class RecordingGraph:
    """Records how the runner calls a compiled graph."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, dict[str, Any]]] = []

    def invoke(self, value: object, config: dict[str, Any], **kwargs: Any) -> None:
        self.calls.append((value, {"config": config, **kwargs}))


def test_every_graph_call_persists_its_checkpoints_before_the_next_step() -> None:
    """LangGraph defaults to writing checkpoints in the background; a SIGKILL could then lose the
    last steps. The runner must ask for synchronous durability on every call."""
    graph = RecordingGraph()
    runner = BriefRunner(graph)
    runner.start({"session_id": "s0123456789ab"})
    runner.resume("s0123456789ab", {"strengthen": False})
    runner.proceed("s0123456789ab")
    assert len(graph.calls) == 3
    assert [call[1]["durability"] for call in graph.calls] == ["sync"] * 3
    assert {call[1]["config"]["configurable"]["thread_id"] for call in graph.calls} == {
        "s0123456789ab"
    }


# ---- a real SIGKILL (PRD AD10, M4 AC6) ------------------------------------------------------

CHILD = Path(__file__).parent / "brief_crash_child.py"


def kill_the_child_while_it_drafts(base_dir: Path) -> str:
    """Run the child until its model call hangs in the draft, SIGKILL it, return the session id."""
    import signal
    import subprocess
    import sys

    child = subprocess.Popen(
        [sys.executable, str(CHILD), str(base_dir)], stdout=subprocess.PIPE, text=True
    )
    watchdog = threading.Timer(30, child.kill)  # a stuck child must not hang the suite
    watchdog.start()
    session_id = ""
    try:
        assert child.stdout is not None
        for line in child.stdout:
            if line.startswith("STARTED "):
                session_id = line.split()[1]
            if line.strip() == "HANGING draft":
                break
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=5)
    finally:
        watchdog.cancel()
        child.kill()
        child.wait()
    assert child.returncode == -signal.SIGKILL
    assert session_id
    return session_id


def test_a_sigkilled_process_is_continued_by_a_fresh_one(tmp_path: Path) -> None:
    session_id = kill_the_child_while_it_drafts(tmp_path)

    fresh = rig(tmp_path, Models())  # a new process on the files the dead one left
    stopped = fresh.service.get(session_id)
    assert stopped.waiting_for == "work"  # the answers are saved, the draft is not
    assert stopped.round == 1
    assert fresh.service.recover() == [session_id]

    done = fresh.service.get(session_id)
    assert done.waiting_for == "decision"
    assert done.brief_text is not None
    assert fresh.models.count("assess") == 0  # the questions were not asked again
    assert fresh.models.count("draft") == 1
    approved = fresh.service.approve(session_id, str(done.brief_sha256), "light")
    assert approved.status == "approved"
    (archive,) = fresh.archives()
    assert archive.read_bytes() == done.brief_text.encode("utf-8")
