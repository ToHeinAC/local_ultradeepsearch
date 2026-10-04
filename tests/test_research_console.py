"""The plan review of `udr run` (PRD M5, A7) on scripted keystrokes against a real service."""

from collections.abc import Iterator
from pathlib import Path

from research_rig import PLAN, ResearchRig, build_research_rig

from app.brief.console import ConsoleIO
from app.research.console import render_plan, run_plan_review


class Keys:
    """Scripted answers; everything the console says is collected."""

    def __init__(self, *answers: str) -> None:
        self._answers: Iterator[str] = iter(answers)
        self.said: list[str] = []
        self.prompts: list[str] = []

    def io(self) -> ConsoleIO:
        return ConsoleIO(ask=self._ask, say=self.said.append, edit=lambda _t: None)

    def _ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return next(self._answers)

    def output(self) -> str:
        return "\n".join(self.said)


def waiting(tmp_path: Path) -> tuple[ResearchRig, str]:
    rig = build_research_rig(tmp_path)
    run_id = rig.new_run()
    rig.service.start(run_id)
    return rig, run_id


def test_the_table_shows_every_query_as_it_would_be_sent(tmp_path: Path) -> None:
    rig, run_id = waiting(tmp_path)
    view = rig.service.edit_query(run_id, "q001", "Firma X Kosten Rückbau")
    lines = render_plan(view)
    assert lines[0] == "Nr | Item | Linse | Kanal | Gesendete Query | Entfernt | Status"
    assert lines[1] == "1 | i01 | breadth | web | Kosten Rückbau | Firma X | bereit"
    assert len(lines) == 1 + len(PLAN)


def test_blocked_rows_are_marked_and_deleted_ones_are_left_out(tmp_path: Path) -> None:
    rig, run_id = waiting(tmp_path)
    rig.service.edit_query(run_id, "q002", "Geheimprojekt Dauer")
    view = rig.service.delete_query(run_id, "q003")
    lines = render_plan(view)
    assert lines[2].endswith("| GESPERRT: denylist")
    assert all(not line.startswith("3 |") for line in lines)


def test_approving_ends_the_review_and_runs_the_sweep(tmp_path: Path) -> None:
    rig, run_id = waiting(tmp_path)
    keys = Keys("f")
    assert run_plan_review(rig.service, keys.io(), run_id) == "approved"
    assert rig.service.get(run_id).status == "done"
    assert "bereit" in keys.output()


def test_quit_leaves_the_plan_waiting(tmp_path: Path) -> None:
    rig, run_id = waiting(tmp_path)
    assert run_plan_review(rig.service, Keys("q").io(), run_id) == "quit"
    assert rig.service.get(run_id).waiting_for == "plan"
    assert rig.gateway.web_calls == []


def test_edit_delete_and_add_then_approve(tmp_path: Path) -> None:
    rig, run_id = waiting(tmp_path)
    keys = Keys(
        "b", "1", "Firma X Kosten Rückbau",  # edit query 1
        "l", "2",  # delete query 2
        "n", "i02", "depth", "Obrigheim Gutachten",  # a new depth query
        "n", "i02", "", "Obrigheim Zeitplan",  # Enter = breadth
        "f",
    )  # fmt: skip
    assert run_plan_review(rig.service, keys.io(), run_id) == "approved"
    rows = {r.query_id: r for r in rig.store.rows(run_id, wave=1)}
    assert (rows["q001"].sent, rows["q001"].removed) == ("Kosten Rückbau", ("Firma X",))
    assert rows["q002"].state == "deleted"
    assert (rows["q010"].lens, rows["q011"].lens) == ("depth", "breadth")
    assert "Kosten Rückbau" in [sent for sent, _, _ in rig.gateway.web_calls]
    assert "Rückbau Dauer Kernkraftwerk" not in [s for s, _, _ in rig.gateway.web_calls]


def test_errors_are_shown_and_the_loop_goes_on(tmp_path: Path) -> None:
    rig, run_id = waiting(tmp_path)
    keys = Keys("b", "abc", "n", "i99", "breadth", "x", "l", "99", "x", "q")
    assert run_plan_review(rig.service, keys.io(), run_id) == "quit"
    output = keys.output()
    assert "'abc' ist keine Nummer" in output
    assert "unknown item" in output
    assert "Bitte f, b, l, n oder q eingeben." in output


def test_a_blocked_plan_is_not_approved_in_the_console(tmp_path: Path) -> None:
    rig, run_id = waiting(tmp_path)
    rig.service.edit_query(run_id, "q001", "Geheimprojekt")
    keys = Keys("f", "q")
    assert run_plan_review(rig.service, keys.io(), run_id) == "quit"
    assert "gesperrte Query" in keys.output()
    assert rig.service.get(run_id).waiting_for == "plan"


def test_a_denylist_hit_at_approval_is_shown(tmp_path: Path) -> None:
    rig, run_id = waiting(tmp_path)
    rig.denylist.add("Obrigheim")
    keys = Keys("f", "q")
    assert run_plan_review(rig.service, keys.io(), run_id) == "quit"
    assert "q003" in keys.output()
    assert "GESPERRT: denylist" in keys.output()


def test_a_run_that_is_not_waiting_ends_the_review_at_once(tmp_path: Path) -> None:
    rig = build_research_rig(tmp_path)
    run_id = rig.new_run()  # queued, never started
    keys = Keys()
    assert run_plan_review(rig.service, keys.io(), run_id) == "quit"
    assert "not waiting" in keys.output()
