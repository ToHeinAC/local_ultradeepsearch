import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from brief_rig import (
    DONE,
    FINISHED,
    LIMITS,
    NOW,
    PASTED,
    QUESTION,
    ROUND_ONE,
    TIER,
    Crash,
    Models,
    a_pdf,
    answers,
    build_parts,
    checklist,
    q,
)
from langgraph.types import Command

from app.brief.errors import StaleBrief
from app.brief.flow import initial_state
from app.brief.render import brief_sha256, canonical_text
from app.brief.uploads import UploadFile, UploadIngestor
from app.pipeline.profiles import Phase1Limits
from app.store.db import Database
from app.store.runs import RunStore
from app.store.sessions import SessionStore


@dataclass
class Harness:
    tmp: Path
    models: Models
    limits: Phase1Limits
    db: Database
    sessions: SessionStore
    runs: RunStore
    ingestor: UploadIngestor
    graph: Any
    session_id: str = ""
    config: dict[str, Any] = field(default_factory=lambda: {})

    @property
    def briefs(self) -> Path:
        return self.tmp / "data" / "briefs"

    def start(
        self, question: str = QUESTION, language: str = "de", files: tuple[UploadFile, ...] = ()
    ) -> dict[str, Any]:
        self.session_id = self.sessions.create(language).session_id
        self.config = {"configurable": {"thread_id": self.session_id}}
        self.ingestor.accept(self.session_id, list(files))
        return self.graph.invoke(initial_state(self.session_id, question, language), self.config)

    def resume(self, value: dict[str, Any]) -> dict[str, Any]:
        return self.graph.invoke(Command(resume=value), self.config)

    def continue_after_crash(self) -> dict[str, Any]:
        return self.graph.invoke(None, self.config)

    def pending(self) -> dict[str, Any] | None:
        interrupts = [
            i.value for task in self.graph.get_state(self.config).tasks for i in task.interrupts
        ]
        return interrupts[0] if interrupts else None

    def values(self) -> dict[str, Any]:
        return dict(self.graph.get_state(self.config).values)

    def next(self) -> tuple[str, ...]:
        return tuple(self.graph.get_state(self.config).next)

    def approval(self, tier: str = "light", sha: str | None = None) -> dict[str, Any]:
        return {
            "action": "approve",
            "sha256": sha or self.values()["brief_sha"],
            "tier": tier,
            "at": NOW.isoformat(),
        }

    def session(self) -> Any:
        row = self.sessions.get(self.session_id)
        assert row is not None
        return row

    def archive_files(self) -> list[Path]:
        return sorted(self.briefs.glob("*.md")) if self.briefs.exists() else []


def harness(
    tmp: Path,
    models: Models | None = None,
    *,
    limits: Phase1Limits = LIMITS,
    again: Harness | None = None,
) -> Harness:
    """One 'process'. ``again`` builds a restarted one on the same files and the same session."""
    models = models or Models()
    p = build_parts(tmp, models, limits)
    h = Harness(tmp, models, limits, p.db, p.sessions, p.runs, p.ingestor, p.graph)
    if again is not None:
        h.session_id, h.config = again.session_id, again.config
    return h


def brief_of(h: Harness) -> str:
    return str(h.values()["brief_text"])


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return harness(tmp_path)


def at_decision(h: Harness) -> Harness:
    """A session that finished its interview and waits for the owner's decision."""
    h.start()
    h.resume(answers("accept", "accept"))
    assert (h.pending() or {})["type"] == "decision"
    return h


# ---- the interview rounds -------------------------------------------------------------------


def test_the_first_interrupt_asks_the_assessed_questions(h: Harness) -> None:
    result = h.start()
    assert [i.value["type"] for i in result["__interrupt__"]] == ["questions"]
    payload = h.pending()
    assert payload is not None
    assert payload["round"] == 1
    assert payload["questions"] == ROUND_ONE["questions"]
    assert h.models.count("assess") == 1
    assert h.next() == ("ask",)
    assert h.session().status == "interviewing"


def test_answers_are_recorded_and_the_next_round_is_assessed(tmp_path: Path) -> None:
    more = {**ROUND_ONE, "questions": [q("goal", "Wofür?")]}
    h = harness(tmp_path, Models(assessments=[ROUND_ONE, more, DONE]))
    h.start()
    h.resume(answers("accept", "text"))
    payload = h.pending()
    assert payload is not None
    assert (payload["type"], payload["round"]) == ("questions", 2)
    values = h.values()
    assert values["round"] == 1
    assert [(a["round"], a["kind"]) for a in values["answers"]] == [(1, "accept"), (1, "text")]
    second_prompt = h.models.prompts_seen["assess"][1]
    assert "Ingenieure" in second_prompt  # the accepted candidate
    assert "Mein Text" in second_prompt


def test_answers_accumulate_over_the_rounds_and_each_prompt_knows_its_round(tmp_path: Path) -> None:
    more = {**ROUND_ONE, "questions": [q("goal", "Wofür?")]}
    h = harness(tmp_path, Models(assessments=[ROUND_ONE, more, DONE]))
    h.start()
    h.resume(answers("accept", "text"))
    h.resume(answers("unknown"))
    assert [(a["round"], a["kind"]) for a in h.values()["answers"]] == [
        (1, "accept"),
        (1, "text"),
        (2, "unknown"),
    ]
    first, second, third = h.models.prompts_seen["assess"]
    assert "Round 1 of at most" in first
    assert "Round 2 of at most" in second
    assert "Round 3 of at most" in third
    assert "Ingenieure" in third
    assert "Wofür?" in third  # the second round's question is in the transcript, too


def test_only_the_first_assessment_can_offer_a_finished_prompt(tmp_path: Path) -> None:
    later = {**DONE, "finished_prompt": True}
    h = harness(tmp_path, Models(assessments=[ROUND_ONE, later]))
    h.start()
    h.resume(answers("accept", "accept"))
    assert (h.pending() or {})["type"] == "decision"  # not offered as a pasted prompt


def test_the_draft_sees_the_checklist_with_the_answered_items_clear(tmp_path: Path) -> None:
    h = harness(tmp_path, Models(assessments=[ROUND_ONE]))
    h.start()
    h.resume(answers("accept", "unknown", genug=True))
    prompt = h.models.prompts_seen["draft"][0]
    assert "audience: clear" in prompt  # answered
    assert "scope: missing" in prompt  # "don't know" clears nothing
    assert "goal: missing" in prompt  # never asked about


def test_the_interview_ends_when_there_is_nothing_left_to_ask(h: Harness) -> None:
    h.start()
    result = h.resume(answers("accept", "accept"))
    assert [i.value["type"] for i in result["__interrupt__"]] == ["decision"]
    payload = h.pending()
    assert payload is not None
    assert payload["brief"] == brief_of(h)
    assert payload["sha256"] == brief_sha256(brief_of(h))
    assert payload["recommendation"] == TIER
    assert (h.models.count("draft"), h.models.count("tier")) == (1, 1)
    row = h.session()
    assert (row.status, row.brief_text, row.brief_sha256) == (
        "awaiting_decision",
        brief_of(h),
        payload["sha256"],
    )


def test_genug_after_round_one_lists_every_missing_item(tmp_path: Path) -> None:
    """M4 AC5: no further assessment, and what is still missing is stated, never invented."""
    h = harness(tmp_path, Models(assessments=[ROUND_ONE]))
    h.start()
    h.resume(answers("unknown", "unknown", genug=True))
    assert h.models.count("assess") == 1
    text = brief_of(h)
    assert "nicht geklärt: Ziel, Zielgruppe" in text.splitlines()[2]
    assumptions = text.split("## Annahmen")[1].split("\n## ")[0]
    assert "- Nicht geklärt: Ziel" in assumptions
    assert "- Nicht geklärt: Zielgruppe" in assumptions
    assert "- Nicht geklärt: Umfang" in assumptions  # "don't know" clears nothing
    assert "Wer liest den Bericht?" in text  # the unknown answers became research questions
    assert "Was nicht?" in text


def test_an_assumed_item_is_not_asked_and_not_listed_as_not_clarified(tmp_path: Path) -> None:
    round_one = {
        **ROUND_ONE,
        "checklist": checklist(audience="missing", scope="assumed", goal="missing"),
        "questions": [q("scope", "Was nicht?"), q("audience", "Wer liest den Bericht?")],
    }
    h = harness(tmp_path, Models(assessments=[round_one]))
    h.start()
    payload = h.pending()
    assert payload is not None
    assert [x["item"] for x in payload["questions"]] == ["audience"]  # the assumed one is dropped
    h.resume(answers("unknown", genug=True))
    assumptions = brief_of(h).split("## Annahmen")[1].split("\n## ")[0]
    assert "Nicht geklärt: Umfang" not in assumptions


def test_answered_items_are_not_listed_as_missing(tmp_path: Path) -> None:
    h = harness(tmp_path, Models(assessments=[ROUND_ONE]))
    h.start()
    h.resume(answers("accept", "unknown", genug=True))  # audience answered, scope not known
    assert "nicht geklärt" in brief_of(h)
    assert "Zielgruppe" not in brief_of(h).split("## Annahmen")[1].split("\n## ")[0]


def test_the_round_limit_ends_the_interview(tmp_path: Path) -> None:
    limits = LIMITS.model_copy(update={"max_rounds": 2})
    round_two = {**ROUND_ONE, "questions": [q("goal", "Wofür?")]}  # still asks, about a new item
    h = harness(tmp_path, Models(assessments=[ROUND_ONE, round_two]), limits=limits)
    h.start()
    h.resume(answers("accept", "accept"))
    assert (h.pending() or {})["round"] == 2
    h.resume(answers("accept"))
    assert (h.pending() or {})["type"] == "decision"
    assert h.models.count("assess") == 2  # no third assessment
    assert h.values()["round"] == 2


def test_a_free_note_is_kept_as_an_answer(h: Harness) -> None:
    h.start()
    h.resume(answers("accept", "accept", note="Der Reaktor ist von 1962."))
    assert "Der Reaktor ist von 1962." in [a["text"] for a in h.values()["answers"]]


def test_the_brief_carries_the_defaults_and_the_recommended_format(h: Harness) -> None:
    at_decision(h)
    output = brief_of(h).split("## Ausgabe")[1]
    assert "**Berichtssprache: Deutsch.**" in output
    assert "structured, 2000 bis 5000 Wörter" in output  # from the recommendation
    assert "Automatisch (auto)" in output
    assert "**Register:** Fachlich" in output


def test_the_recommended_format_goes_into_the_brief(tmp_path: Path) -> None:
    short = {**TIER, "tier": "light", "response_format": "short"}
    h = harness(tmp_path, Models(tier=short))
    at_decision(h)
    assert "short, 500 bis 2000 Wörter" in brief_of(h)
    assert (h.pending() or {})["recommendation"]["tier"] == "light"


def test_the_interview_language_reaches_every_prompt_and_the_labels(tmp_path: Path) -> None:
    """M4 AC7."""
    h = harness(tmp_path)
    h.start(language="fr")
    h.resume(answers("accept", "accept"))
    assert all("Interview language: French" in p for p in h.models.prompts_seen["assess"])
    assert "Interview language: French" in h.models.prompts_seen["draft"][0]
    assert "## Research questions" in brief_of(h)  # other languages use the English labels


# ---- interrupts do no model work ------------------------------------------------------------


def test_resuming_a_question_round_costs_exactly_one_assessment(h: Harness) -> None:
    h.start()
    before = h.models.count("assess")
    h.resume(answers("accept", "accept"))
    assert (
        h.models.count("assess") == before + 1
    )  # the interrupted node did not call the model again


def test_decisions_that_only_move_state_make_no_model_calls(h: Harness) -> None:
    at_decision(h)
    before = h.models.total()
    h.resume({"action": "save"})
    h.resume({"action": "edit", "text": brief_of(h) + "\n## Eigener Abschnitt\n\nText.\n"})
    h.resume({"action": "settings", "report_language": "en"})
    assert h.models.total() == before


# ---- uploads --------------------------------------------------------------------------------


def test_uploads_at_the_start_reach_the_assessment_and_the_brief(tmp_path: Path) -> None:
    h = harness(tmp_path)
    h.start(files=(a_pdf(),))
    assert h.models.count("facts") == 1
    assert "Ein Fakt aus der Datei. (a.pdf, S. 1)" in h.models.prompts_seen["assess"][0]
    h.resume(answers("accept", "accept"))
    text = brief_of(h)
    assert "## Kontext aus Unterlagen\n\n- Ein Fakt aus der Datei. (a.pdf, S. 1)" in text
    assert h.values()["digest"] == "- Ein Fakt aus der Datei. (a.pdf, S. 1)"


def test_a_file_added_while_waiting_is_read_before_the_next_assessment(tmp_path: Path) -> None:
    more = {**ROUND_ONE, "questions": [q("goal", "Wofür?")]}
    h = harness(tmp_path, Models(assessments=[ROUND_ONE, more, DONE]))
    h.start()
    assert h.models.count("facts") == 0
    h.ingestor.accept(h.session_id, [a_pdf()])  # the owner uploads while a round is open
    h.resume(answers("accept", "accept"))
    assert h.models.count("facts") == 1
    assert "Ein Fakt aus der Datei. (a.pdf, S. 1)" in h.models.prompts_seen["assess"][1]
    assert "Ein Fakt aus der Datei." not in h.models.prompts_seen["assess"][0]  # not in round 1


# ---- a finished prompt ----------------------------------------------------------------------


def test_a_finished_prompt_is_offered_before_any_question(tmp_path: Path) -> None:
    h = harness(tmp_path, Models(assessments=[FINISHED]))
    h.start(question=PASTED)
    payload = h.pending()
    assert payload is not None
    assert payload == {"type": "finished_prompt", "pasted": PASTED, "refusal": ""}
    assert h.values()["round"] == 0
    assert (h.models.count("draft"), h.models.count("strengthen")) == (0, 0)


def test_declining_to_strengthen_installs_the_prompt_verbatim(tmp_path: Path) -> None:
    h = harness(tmp_path, Models(assessments=[FINISHED]))
    h.start(question=PASTED)
    h.resume({"strengthen": False})
    text = brief_of(h)
    assert text.startswith("Method: Prompt wörtlich übernommen\n\n# Wie teuer ist der Rückbau?\n")
    assert canonical_text(PASTED).rstrip("\n") in text
    assert "## Ausgabe" in text
    assert (h.models.count("draft"), h.models.count("strengthen"), h.models.count("tier")) == (
        0,
        0,
        1,
    )
    assert (h.pending() or {})["type"] == "decision"


def test_strengthening_calls_the_model_once(tmp_path: Path) -> None:
    h = harness(tmp_path, Models(assessments=[FINISHED]))
    h.start(question=PASTED)
    h.resume({"strengthen": True})
    assert h.models.count("strengthen") == 1
    assert "Prompt wörtlich übernommen" not in brief_of(h)
    assert "## Forschungsfragen" in brief_of(h)
    assert (h.pending() or {})["type"] == "decision"


def test_a_prompt_that_cannot_be_installed_is_offered_again_with_the_reason(tmp_path: Path) -> None:
    h = harness(tmp_path, Models(assessments=[FINISHED]))
    h.start(question="Bitte analysiere den Rückbau ohne Titel und ohne Liste.")
    h.resume({"strengthen": False})
    payload = h.pending()
    assert payload is not None
    assert payload["type"] == "finished_prompt"
    assert "title" in payload["refusal"]
    h.resume({"strengthen": True})  # strengthening is the way out
    assert (h.pending() or {})["type"] == "decision"
    assert h.values()["refusal"] == ""


# ---- the decision ---------------------------------------------------------------------------


def test_revise_gives_a_new_draft_and_the_old_hash_no_longer_approves(h: Harness) -> None:
    at_decision(h)
    old = h.values()["brief_sha"]
    h.resume({"action": "revise", "feedback": "Mehr zu den Kosten"})
    assert h.models.count("revise") == 1
    assert "Mehr zu den Kosten" in h.models.prompts_seen["revise"][0]
    assert "Überarbeitet." in brief_of(h)
    new = h.values()["brief_sha"]
    assert new != old
    assert h.session().brief_sha256 == new
    with pytest.raises(StaleBrief):
        h.runs.approve(h.session_id, sha256=old, brief_path="x", tier="light", summarize_model=None)
    assert (h.pending() or {})["sha256"] == new  # and the owner decides again


def test_a_direct_edit_replaces_the_text_and_the_hash(h: Harness) -> None:
    at_decision(h)
    edited = "# Mein Titel\r\n\r\n## Forschungsfragen\r\n\r\n1. Meine Frage\r\n"
    h.resume({"action": "edit", "text": edited})
    assert brief_of(h) == canonical_text(edited)
    assert h.values()["brief_sha"] == brief_sha256(edited)
    row = h.session()
    assert (row.brief_text, row.brief_sha256) == (canonical_text(edited), brief_sha256(edited))
    assert h.models.count("draft") == 1  # no model involved


def test_settings_re_render_only_the_output_section(h: Harness) -> None:
    at_decision(h)
    edited = brief_of(h).replace("Wie lange dauert der Rückbau?", "Meine eigene Frage?")
    h.resume({"action": "edit", "text": edited})
    before = brief_of(h)
    h.resume({"action": "settings", "report_language": "en", "response_format": "short"})
    after = brief_of(h)
    assert after.split("## Ausgabe")[0] == before.split("## Ausgabe")[0]  # the hand edit survives
    assert "**Berichtssprache: Englisch.**" in after
    assert "short, 500 bis 2000 Wörter" in after
    assert "**Register:** Fachlich" in after
    row = h.session()
    assert (row.report_language, row.response_format, row.template_id) == ("en", "short", "auto")
    assert row.brief_sha256 == brief_sha256(after) == h.values()["brief_sha"]


def test_a_register_the_owner_edited_survives_a_settings_change(h: Harness) -> None:
    at_decision(h)
    h.resume(
        {
            "action": "edit",
            "text": brief_of(h).replace("**Register:** Fachlich", "**Register:** Kurz und knapp"),
        }
    )
    h.resume({"action": "settings", "report_language": "en"})
    assert "**Register:** Kurz und knapp" in brief_of(h)
    assert "**Register:** Fachlich" not in brief_of(h)


def test_a_template_change_lists_its_headings(h: Harness) -> None:
    at_decision(h)
    h.resume({"action": "settings", "template_id": "literaturuebersicht"})
    assert "Gliederung: Kurzantwort; Methodik der Recherche;" in brief_of(h)


def test_save_parks_the_session_and_writes_the_draft(h: Harness) -> None:
    at_decision(h)
    h.resume({"action": "save"})
    row = h.session()
    assert row.status == "saved"
    draft = h.tmp / "data" / "briefs" / "drafts" / f"{h.session_id}.md"
    assert draft.read_text(encoding="utf-8") == brief_of(h)
    payload = h.pending()
    assert payload is not None
    assert (payload["type"], payload["status"]) == ("decision", "saved")


def test_a_parked_session_continues_with_any_action(h: Harness) -> None:
    at_decision(h)
    h.resume({"action": "save"})
    h.resume({"action": "settings", "report_language": "en"})
    assert h.session().status == "awaiting_decision"
    assert (h.pending() or {})["status"] == "awaiting_decision"


# ---- approval (M4 AC1, AC2) -----------------------------------------------------------------


def test_approval_archives_the_exact_bytes_and_creates_the_queued_run(h: Harness) -> None:
    at_decision(h)
    sha = h.values()["brief_sha"]
    result = h.resume(h.approval("full"))
    assert "__interrupt__" not in result
    assert h.next() == ()
    (archive,) = h.archive_files()
    assert archive.name == "2026-10-02T09-30-15Z.md"
    assert archive.read_bytes() == brief_of(h).encode("utf-8")
    run = h.runs.run_for_session(h.session_id)
    assert run is not None
    assert (run.brief_sha256, run.tier, run.status, run.brief_path) == (
        sha,
        "full",
        "queued",
        str(archive),
    )
    final = h.values()["result"]
    assert final == {"run_id": run.run_id, "archive_path": str(archive), "sha256": sha}
    row = h.session()
    assert (row.status, row.run_id, row.approved_sha256) == ("approved", run.run_id, sha)


def test_the_archive_matches_what_the_owner_edited_and_saw(h: Harness) -> None:
    at_decision(h)
    h.resume(
        {"action": "edit", "text": "# Titel mit Ümlaut\r\n\r\n1. Frage <think>bleibt</think>\r\n"}
    )
    h.resume(h.approval())
    (archive,) = h.archive_files()
    assert archive.read_bytes() == "# Titel mit Ümlaut\n\n1. Frage <think>bleibt</think>\n".encode()


def test_a_stale_hash_creates_neither_a_run_nor_an_archive(h: Harness) -> None:
    at_decision(h)
    stale = h.values()["brief_sha"]
    h.resume({"action": "edit", "text": "# Anderer Text\n\n1. Frage\n"})
    with pytest.raises(StaleBrief):
        h.resume(h.approval(sha=stale))
    assert h.archive_files() == []
    assert h.runs.run_for_session(h.session_id) is None
    assert h.session().status == "awaiting_decision"


def test_approving_without_a_pending_decision_changes_nothing(h: Harness) -> None:
    h.start()
    assert h.runs.run_for_session(h.session_id) is None  # nothing but the service can make a run


# ---- resuming (PRD AD10, M4 AC6) ------------------------------------------------------------


def test_a_restart_keeps_the_pending_questions_and_continues(tmp_path: Path) -> None:
    first = harness(tmp_path)
    first.start()
    expected = first.pending()
    second = harness(tmp_path, Models(assessments=[DONE]), again=first)  # new objects, same files
    assert second.pending() == expected
    assert second.next() == ("ask",)
    second.resume(answers("accept", "accept"))
    assert (second.pending() or {})["type"] == "decision"
    assert second.models.count("assess") == 1  # only the assessment after the answers


def test_a_restart_keeps_a_pending_decision(tmp_path: Path) -> None:
    first = at_decision(harness(tmp_path))
    expected = first.pending()
    second = harness(tmp_path, Models(), again=first)
    assert second.pending() == expected
    second.resume(second.approval())
    assert second.runs.run_for_session(second.session_id) is not None


def test_a_crash_while_drafting_resumes_without_asking_again(tmp_path: Path) -> None:
    first = harness(tmp_path, Models(crash_on={"draft": 1}))
    first.start()
    with pytest.raises(Crash):
        first.resume(answers("accept", "accept"))
    assert first.next() == ("draft_brief",)
    assert first.values()["round"] == 1  # the answers survived

    second = harness(tmp_path, Models(), again=first)
    second.continue_after_crash()
    assert (second.pending() or {})["type"] == "decision"
    assert second.models.count("assess") == 0  # not asked again
    assert second.models.count("draft") == 1


def test_a_crash_right_after_the_status_change_does_not_break_the_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = harness(tmp_path)
    first.start()
    real = first.sessions.set_status
    state = {"crashed": False}

    def change_then_crash(session_id: str, status: Any) -> None:
        real(session_id, status)
        if not state["crashed"]:
            state["crashed"] = True
            raise Crash("after the status change")

    monkeypatch.setattr(first.sessions, "set_status", change_then_crash)
    with pytest.raises(Crash):
        first.resume(answers("accept", "accept"))
    assert first.session().status == "awaiting_decision"  # the change itself was committed
    assert first.next() == ("recommend",)
    first.continue_after_crash()  # the retry must not try the same transition twice
    assert (first.pending() or {})["type"] == "decision"


def test_a_crash_while_recommending_does_not_draft_again(tmp_path: Path) -> None:
    first = harness(tmp_path, Models(crash_on={"tier": 1}))
    first.start()
    with pytest.raises(Crash):
        first.resume(answers("accept", "accept"))
    assert first.next() == ("recommend",)
    second = harness(tmp_path, Models(), again=first)
    second.continue_after_crash()
    assert second.models.count("draft") == 0
    assert second.models.count("tier") == 1
    assert (second.pending() or {})["type"] == "decision"


def test_finalize_that_runs_twice_makes_one_run_and_one_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = at_decision(harness(tmp_path))
    real = first.runs.approve
    state = {"crashed": False}

    def crash_once(*args: Any, **kwargs: Any) -> Any:
        if not state["crashed"]:
            state["crashed"] = True
            raise Crash("after the archive, before the run")
        return real(*args, **kwargs)

    monkeypatch.setattr(first.runs, "approve", crash_once)
    with pytest.raises(Crash):
        first.resume(first.approval())
    assert len(first.archive_files()) == 1
    assert first.runs.run_for_session(first.session_id) is None
    assert first.next() == ("finalize",)

    first.continue_after_crash()
    assert len(first.archive_files()) == 1  # the same file, not a second one
    run = first.runs.run_for_session(first.session_id)
    assert run is not None
    assert first.db.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert first.values()["result"]["run_id"] == run.run_id


def test_a_crash_while_reading_uploads_resumes_at_the_same_node(tmp_path: Path) -> None:
    first = harness(tmp_path, Models(crash_on={"facts": 1}))
    with pytest.raises(Crash):
        first.start(files=(a_pdf(),))
    assert first.next() == ("ingest_uploads",)
    second = harness(tmp_path, Models(), again=first)
    second.continue_after_crash()
    assert (second.pending() or {})["type"] == "questions"
    assert second.models.count("facts") == 1
    assert re.search(r"a\.pdf", second.session().upload_digest)
