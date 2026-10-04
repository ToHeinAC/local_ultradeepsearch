"""The plan review of `udr run` (PRD M5 D3) on scripted keystrokes against a real service."""

from collections.abc import Callable, Iterator
from pathlib import Path

from research_rig import FakePreparer, ResearchModels
from research_run_rig import RunRig, make_rig

from app.adapters.outbound.errors import DenylistBlocked
from app.brief.console import ConsoleIO
from app.research.console import render_plan, run_plan_review

FIRST = "Rückbau Forschungsreaktor Dauer"
SECOND = "decommissioning research reactor duration study"
THIRD = "Rückbau Forschungsreaktor Verzögerungen Kritik"
BLOCKED = "Genehmigung Rückbau Atomgesetz"


class Keys:
    """Scripted answers and editor results; everything the console says is collected."""

    def __init__(
        self, *answers: str, editor: Callable[[str], str | None] = lambda _t: None
    ) -> None:
        self._answers: Iterator[str] = iter(answers)
        self._editor = editor
        self.said: list[str] = []

    def io(self) -> ConsoleIO:
        return ConsoleIO(
            ask=lambda _p: next(self._answers), say=self.said.append, edit=self._editor
        )

    def output(self) -> str:
        return "\n".join(self.said)


def waiting(tmp_path: Path, preparer: FakePreparer | None = None) -> tuple[RunRig, str]:
    rig = make_rig(tmp_path, preparer=preparer)
    run_id = rig.create().run_id
    assert rig.service.run(run_id).status == "awaiting_plan_approval"
    return rig, run_id


def lines(rig: RunRig, run_id: str) -> list[str]:
    plan = rig.service.view(run_id).plan
    assert plan is not None
    return render_plan(plan)


# ---- the table ------------------------------------------------------------------------------


def test_the_table_shows_each_query_as_it_would_be_sent(tmp_path: Path) -> None:
    preparer = FakePreparer(rewrite={FIRST: ("Rückbau Dauer", ("Reaktor GmbH",))})
    rig, run_id = waiting(tmp_path, preparer)
    table = lines(rig, run_id)
    assert table[0] == "Nr | Item | Linse | Kanal | Gesendete Query | Entfernt | Status"
    assert table[1] == "1 | Q1 | A | web | Rückbau Dauer | Reaktor GmbH | bereit"
    assert table[2] == f"2 | Q1 | B | scholarly | {SECOND} | - | bereit"
    assert len(table) == 11


def test_a_blocked_query_is_marked_with_its_reason(tmp_path: Path) -> None:
    preparer = FakePreparer(refuse={BLOCKED: DenylistBlocked("denylist")})
    rig, run_id = waiting(tmp_path, preparer)
    assert lines(rig, run_id)[4].endswith("| GESPERRT: denylist")


# ---- the loop -------------------------------------------------------------------------------


def test_approving_ends_the_review_and_the_run_goes_on_to_its_end(tmp_path: Path) -> None:
    rig, run_id = waiting(tmp_path)
    keys = Keys("f")
    assert run_plan_review(rig.service, keys.io(), run_id) == "approved"
    assert rig.service.view(run_id).status == "done"
    assert "bereit" in keys.output()


def test_quitting_leaves_the_plan_waiting_and_sends_nothing(tmp_path: Path) -> None:
    rig, run_id = waiting(tmp_path)
    assert run_plan_review(rig.service, Keys("q").io(), run_id) == "quit"
    assert rig.service.view(run_id).waiting_for == "plan"
    assert rig.searcher.calls == []


def test_an_edit_in_the_editor_changes_the_plan_and_checks_the_new_text(tmp_path: Path) -> None:
    preparer = FakePreparer(rewrite={"Neue Query Firma X": ("Neue Query", ("Firma X",))})
    rig, run_id = waiting(tmp_path, preparer)
    keys = Keys("b", "q", editor=lambda text: text.replace(FIRST, "Neue Query Firma X"))
    run_plan_review(rig.service, keys.io(), run_id)
    assert lines(rig, run_id)[1] == "1 | Q1 | A | web | Neue Query | Firma X | bereit"


def test_leaving_the_editor_unchanged_changes_nothing(tmp_path: Path) -> None:
    rig, run_id = waiting(tmp_path)
    before = rig.service.view(run_id).plan_sha256
    run_plan_review(rig.service, Keys("b", "q").io(), run_id)
    assert rig.service.view(run_id).plan_sha256 == before


def test_deleting_a_query_removes_its_line(tmp_path: Path) -> None:
    rig, run_id = waiting(tmp_path)
    run_plan_review(rig.service, Keys("l", "2", "q").io(), run_id)
    table = lines(rig, run_id)
    assert len(table) == 10
    assert all(SECOND not in line for line in table)


def test_a_new_query_gets_its_item_lens_and_a_check(tmp_path: Path) -> None:
    rig, run_id = waiting(tmp_path)
    run_plan_review(rig.service, Keys("n", "E1", "c", "Noch eine Gegenposition", "q").io(), run_id)
    assert lines(rig, run_id)[-1].endswith("| E1 | C | web | Noch eine Gegenposition | - | bereit")


def test_a_blocked_plan_cannot_be_approved_and_the_loop_goes_on(tmp_path: Path) -> None:
    preparer = FakePreparer(refuse={BLOCKED: DenylistBlocked("denylist")})
    rig, run_id = waiting(tmp_path, preparer)
    keys = Keys("f", "q")
    assert run_plan_review(rig.service, keys.io(), run_id) == "quit"
    assert "q04" in keys.output()  # the service names the blocked query
    assert rig.service.view(run_id).waiting_for == "plan"


def test_errors_are_shown_and_the_loop_goes_on(tmp_path: Path) -> None:
    rig, run_id = waiting(tmp_path)
    keys = Keys("l", "abc", "l", "99", "n", "X9", "A", "x", "x", "q")
    assert run_plan_review(rig.service, keys.io(), run_id) == "quit"
    output = keys.output()
    assert "'abc' ist keine Nummer" in output
    assert "keine Query mit der Nummer 99" in output
    assert "unknown item X9" in output
    assert "Bitte f, b, l, n oder q eingeben." in output


def test_an_editor_result_that_is_not_a_plan_is_refused(tmp_path: Path) -> None:
    rig, run_id = waiting(tmp_path)
    before = rig.service.view(run_id).plan_sha256
    keys = Keys("b", "q", editor=lambda _t: "das ist keine Zeile")
    run_plan_review(rig.service, keys.io(), run_id)
    assert "expected 'id | item | lens | query'" in keys.output()
    assert rig.service.view(run_id).plan_sha256 == before


def test_a_run_that_is_not_waiting_ends_the_review_at_once(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, models=ResearchModels())
    run_id = rig.create().run_id  # queued
    keys = Keys()
    assert run_plan_review(rig.service, keys.io(), run_id) == "quit"
    assert "wartet nicht" in keys.output()
