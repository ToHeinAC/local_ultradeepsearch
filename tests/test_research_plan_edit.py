"""The owner edits the plan (PRD §3.2): lines in an editor, new and changed queries re-sanitized."""

import pytest
from research_rig import (
    AUTO_SETTINGS,
    BRIEF,
    LIGHT,
    RULES,
    TEMPLATES,
    FakePreparer,
    ResearchModels,
    llm,
)

from app.adapters.outbound.errors import DenylistBlocked
from app.events import MemoryEventSink
from app.research.decompose import Decomposer
from app.research.errors import InvalidEdit, PlanBlocked
from app.research.plan import Planner, check_approvable, plan_hash
from app.research.plan_edit import EditedQuery, apply_edits, parse_lines, render_lines


def make_plan(rewrite: dict[str, tuple[str, tuple[str, ...]]] | None = None):
    events = MemoryEventSink()
    decomposition = (
        Decomposer(llm(ResearchModels(), events), RULES, events)
        .decompose(brief=BRIEF, settings=AUTO_SETTINGS, template=TEMPLATES["auto"])
        .decomposition
    )
    planner = Planner(llm(ResearchModels(), events), RULES, LIGHT, FakePreparer(rewrite), events)
    return planner.plan(BRIEF, decomposition, "")


PLAN = make_plan({"Rückbau Forschungsreaktor Dauer": ("Rückbau Reaktor Dauer", ("Müller AG",))})
ITEMS = {"Q1", "Q2", "E1"}


def edits_of(text: str) -> list[EditedQuery]:
    return parse_lines(text)


def test_the_plan_renders_as_one_line_per_query_with_the_sanitizer_result_as_comment() -> None:
    text = render_lines(PLAN)
    assert text.splitlines()[0].startswith("#")
    assert "q01 | Q1 | A | Rückbau Forschungsreaktor Dauer" in text
    assert "#   sent: Rückbau Reaktor Dauer (removed: Müller AG)" in text


def test_parsing_what_was_rendered_gives_the_same_queries_back() -> None:
    edits = edits_of(render_lines(PLAN))
    assert [(e.query_id, e.item, e.lens, e.text) for e in edits] == [
        (p.query_id, p.item, p.lens, p.original) for p in PLAN.queries
    ]


def test_comments_and_blank_lines_are_ignored_and_new_lines_have_no_id() -> None:
    edits = edits_of(
        "# a comment\n\n   \nq01 | Q1 | A | eins\n- | Q2 | B | zwei\nnew | E1 | C | drei\n"
    )
    assert [(e.query_id, e.text) for e in edits] == [
        ("q01", "eins"),
        (None, "zwei"),
        (None, "drei"),
    ]


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("q01 | Q1 | A", "line 1: expected"),
        ("q01 | Q1 | X | text", "line 1: lens must be A, B, C or D"),
        ("q01 | Q1 | A |   ", "line 1: the query is empty"),
        ("Q01 | Q1 | A | text", "line 1: the id must be q"),
        ("ok | Q1 | A | text", "line 1: the id must be q"),
    ],
)
def test_malformed_lines_say_which_line_and_why(line: str, message: str) -> None:
    with pytest.raises(InvalidEdit, match=message):
        parse_lines(line)
    with pytest.raises(InvalidEdit, match="line 3"):
        parse_lines("# c\nq01 | Q1 | A | fine\n" + line)


def test_a_query_can_contain_the_separator() -> None:
    (edit,) = parse_lines("q01 | Q1 | A | a | b")
    assert edit.text == "a | b"


def apply(edits: list[EditedQuery], preparer: FakePreparer | None = None):
    prep = preparer or FakePreparer()
    return apply_edits(PLAN, edits, preparer=prep, known_items=ITEMS), prep


def unchanged() -> list[EditedQuery]:
    return parse_lines(render_lines(PLAN))


def test_unchanged_queries_keep_their_sanitizer_result_without_a_new_call() -> None:
    new, prep = apply(unchanged())
    assert new == PLAN
    assert prep.calls == []


def test_a_whitespace_change_is_no_change() -> None:
    edits = unchanged()
    edits[0] = EditedQuery("q01", "Q1", "A", "  Rückbau   Forschungsreaktor Dauer ")
    new, prep = apply(edits)
    assert new == PLAN
    assert prep.calls == []


def test_a_changed_query_is_sanitized_again_and_keeps_its_id() -> None:
    edits = unchanged()
    edits[0] = EditedQuery("q01", "Q1", "A", "Neue Formulierung")
    new, prep = apply(edits, FakePreparer({"Neue Formulierung": ("Neue Form", ("X",))}))
    assert prep.calls == [("Neue Formulierung", "2.1")]
    first = new.queries[0]
    assert (first.query_id, first.original, first.sent, first.removed_terms) == (
        "q01",
        "Neue Formulierung",
        "Neue Form",
        ["X"],
    )
    assert plan_hash(new) != plan_hash(PLAN)


def test_deleted_lines_are_gone_and_new_ones_get_fresh_ids_never_reused() -> None:
    edits = [e for e in unchanged() if e.query_id != "q10"]  # delete the last query
    edits.append(EditedQuery(None, "Q1", "A", "ganz neu"))
    new, prep = apply(edits)
    assert "q10" not in [p.query_id for p in new.queries][:-1]
    assert new.queries[-1].query_id == "q11"  # not q10 again
    assert prep.calls == [("ganz neu", "2.1")]
    assert len(new.queries) == len(PLAN.queries)


def test_the_order_of_the_lines_is_the_order_of_the_plan() -> None:
    edits = unchanged()[::-1]
    new, _ = apply(edits)
    assert [p.query_id for p in new.queries] == [p.query_id for p in PLAN.queries][::-1]


def test_changing_item_or_lens_updates_the_kind_without_a_new_call() -> None:
    edits = unchanged()
    edits[0] = EditedQuery("q01", "Q2", "B", "Rückbau Forschungsreaktor Dauer")
    new, prep = apply(edits)
    assert (new.queries[0].item, new.queries[0].lens, new.queries[0].kind) == (
        "Q2",
        "B",
        "scholarly",
    )
    assert new.queries[0].sent == "Rückbau Reaktor Dauer"
    assert prep.calls == []


def test_an_edit_into_the_denylist_blocks_approval_until_it_is_fixed() -> None:
    edits = unchanged()
    edits[0] = EditedQuery("q01", "Q1", "A", "Müller-Werke Rückbau")
    blocking = FakePreparer(refuse={"Müller-Werke Rückbau": DenylistBlocked("term")})
    blocked, _ = apply(edits, blocking)
    assert (blocked.queries[0].blocked, blocked.queries[0].sent) == ("denylist", "")
    with pytest.raises(PlanBlocked):
        check_approvable(blocked, plan_hash(blocked))
    fixed_edits = [EditedQuery("q01", "Q1", "A", "Rückbau allgemein"), *unchanged()[1:]]
    fixed = apply_edits(blocked, fixed_edits, preparer=FakePreparer(), known_items=ITEMS)
    assert fixed.queries[0].blocked is None
    check_approvable(fixed, plan_hash(fixed))


def test_a_blocked_query_is_checked_again_even_when_its_text_is_unchanged() -> None:
    edits = unchanged()
    edits[0] = EditedQuery("q01", "Q1", "A", "Müller Rückbau")
    blocking = FakePreparer(refuse={"Müller Rückbau": DenylistBlocked("term")})
    blocked, _ = apply(edits, blocking)
    assert blocked.queries[0].blocked == "denylist"
    again = apply_edits(
        blocked,
        parse_lines(render_lines(blocked)),
        preparer=FakePreparer(),  # the term left the denylist meanwhile
        known_items=ITEMS,
    )
    assert (again.queries[0].blocked, again.queries[0].sent) == (None, "Müller Rückbau")


def test_unknown_items_unknown_ids_and_duplicate_ids_are_refused() -> None:
    with pytest.raises(InvalidEdit, match="unknown item Q9"):
        apply([EditedQuery(None, "Q9", "A", "x")])
    with pytest.raises(InvalidEdit, match="unknown query q99"):
        apply([EditedQuery("q99", "Q1", "A", "x")])
    with pytest.raises(InvalidEdit, match="q01 appears twice"):
        apply([EditedQuery("q01", "Q1", "A", "x"), EditedQuery("q01", "Q1", "A", "y")])


def test_a_plan_can_be_edited_down_to_nothing() -> None:
    new, _ = apply([])
    assert new.queries == ()
