"""The ship gate with its fix rounds (PRD §3.10, D11, M5 AC5)."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_rig import (
    NOW,
    RULES,
    ResearchModels,
    Wired,
    german_text,
    seed_note,
    wired,
)

from app.pipeline.profiles import FormatRange
from app.research.draft import DraftPlan
from app.research.report import ApprovedBrief
from app.research.sections import load_section, save_section
from app.research.shipgate import GATE_FILE, GateContext, ShipGate, gate_notes, sha256_of
from app.store.models import SourceMeta
from app.templates import Section

OPEN, CLOSE = chr(0x201E), chr(0x201C)
BRIEF = "# Wie lange dauert der Rückbau?\n\n## Forschungsfragen\n\n1. Wie lange?\n"
FMT = FormatRange(words=(400, 600), citations=(1, 2))  # about 167 words per section of three
HEADINGS = ("Eins", "Zwei", "Drei")


@dataclass
class Rig:
    w: Wired
    gate: ShipGate
    ctx: GateContext
    run_dir: Path


def rig(
    tmp_path: Path,
    models: ResearchModels | None = None,
    texts: tuple[str, ...] | None = None,
    fmt: FormatRange = FMT,
    *,
    seed_sections: bool = True,
) -> Rig:
    w = wired(tmp_path, models)
    for note_id in w.ids:
        w.keys.key_for(note_id)  # S1 and S2
    run_dir = tmp_path / "run"
    if seed_sections:
        for index, text in enumerate(texts or tuple(german_text(167, "S1") for _ in HEADINGS), 1):
            save_section(run_dir, index, text)
    (run_dir / "polish-log.json").write_text("{}", encoding="utf-8")
    (run_dir / "readability-decisions.json").write_text("{}", encoding="utf-8")
    plan = DraftPlan(
        title="Wie lange dauert der Rückbau?",
        questions=("Wie lange?",),
        sections=tuple(Section(h, "") for h in HEADINGS),
        language="de",
        fmt=fmt,
        must_read=tuple(w.ids),
        shim="Drafting posture.",
    )
    ctx = GateContext(
        run_dir, "light", ApprovedBrief(BRIEF, NOW, "data/briefs/x.md"), sha256_of(BRIEF), plan
    )
    gate = ShipGate(w.vault, w.keys, RULES, w.events, w.fixes, w.drafter)
    return Rig(w, gate, ctx, run_dir)


def gate_json(r: Rig) -> dict[str, Any]:
    return json.loads((r.run_dir / GATE_FILE).read_text(encoding="utf-8"))


def test_a_good_report_passes_without_a_fix_round(tmp_path: Path) -> None:
    r = rig(tmp_path)
    outcome = r.gate.run(r.ctx)
    assert outcome.passed is True
    assert outcome.rounds == []
    data = gate_json(r)
    assert data["passed"] is True
    assert data["rounds"] == []
    assert [c["id"] for c in data["checks"]] == [f"G{n}" for n in range(1, 13)]
    assert (r.run_dir / "report.md").read_text(encoding="utf-8").startswith("# Wie lange dauert")


def test_a_quote_that_is_not_in_its_source_is_unquoted_and_the_next_judgement_passes(
    tmp_path: Path,
) -> None:
    quote = f"{OPEN}Frei erfundenes Zitat das niemand je sagte{CLOSE}"
    texts = (
        german_text(167, "S1") + f" Er schreibt {quote} [S1].",
        german_text(167),
        german_text(167),
    )
    r = rig(tmp_path, texts=texts)
    outcome = r.gate.run(r.ctx)
    assert outcome.passed
    assert outcome.rounds == [
        {"round": 1, "failed": ["G6"], "fixes": ["G6: removed quotation marks in section 1"]}
    ]
    assert OPEN not in (load_section(r.run_dir, 1) or "")
    assert "Frei erfundenes Zitat das niemand je sagte [S1]." in (load_section(r.run_dir, 1) or "")
    assert OPEN not in (r.run_dir / "report.md").read_text(encoding="utf-8")


def test_a_verbatim_quote_is_left_alone(tmp_path: Path) -> None:
    quote = f"{OPEN}Der Rückbau dauert zehn Jahre{CLOSE}"
    texts = (german_text(167) + f" Es heißt {quote} [S1].", german_text(167), german_text(167))
    r = rig(tmp_path, texts=texts)
    assert r.gate.run(r.ctx).rounds == []
    assert quote in (load_section(r.run_dir, 1) or "")


def test_a_citation_of_a_retracted_source_gets_a_notice(tmp_path: Path) -> None:
    r = rig(tmp_path)
    retracted = seed_note(r.w.vault, 9, meta=SourceMeta(is_retracted=True))
    key = r.w.keys.key_for(retracted.note_id)
    save_section(r.run_dir, 2, german_text(167) + f" Ein Befund dazu [{key}].")
    outcome = r.gate.run(r.ctx)
    assert outcome.passed
    assert [x["failed"] for x in outcome.rounds] == [["G9"]]
    assert outcome.rounds[0]["fixes"] == ["G9: marked retracted sources in section 2"]
    assert f"[{key}] (zurückgezogen)" in (load_section(r.run_dir, 2) or "")


def test_pipeline_vocabulary_is_reworded_by_the_model_and_scaffold_headers_are_removed(
    tmp_path: Path,
) -> None:
    leak = " Das ist nur interim gültig [S1]."
    texts = (
        german_text(167) + leak,
        german_text(167) + "\n\n## Run config\n\nText.",
        german_text(167),
    )
    hunks = {
        "LeakProposal": [
            {"hunks": [{"old": leak.strip(), "new": "Das gilt vorläufig [S1].", "reason": ""}]}
        ]
    }
    r = rig(tmp_path, ResearchModels(answers=hunks), texts)
    outcome = r.gate.run(r.ctx)
    assert outcome.passed, outcome.result
    assert "G7" in outcome.rounds[0]["failed"]
    assert outcome.rounds[0]["fixes"] == [
        "G7: removed front matter or headers in section 2",
        "G7: reworded 1 sentences in section 1",
    ] or sorted(outcome.rounds[0]["fixes"]) == sorted(
        [
            "G7: removed front matter or headers in section 2",
            "G7: reworded 1 sentences in section 1",
        ]
    )
    assert "interim" not in (load_section(r.run_dir, 1) or "")
    assert "Run config" not in (load_section(r.run_dir, 2) or "")


def test_a_report_that_is_too_long_gets_its_long_sections_shortened_once(tmp_path: Path) -> None:
    texts = (german_text(900, "S1"), german_text(167), german_text(167))
    models = ResearchModels(texts={"Eins": german_text(160, "S1")})
    r = rig(tmp_path, models, texts)
    outcome = r.gate.run(r.ctx)
    assert outcome.passed
    assert outcome.rounds[0]["fixes"] == ["G3: shortened section 1"]
    assert models.count("text") == 1
    assert "Shorten this section to about 167 words" in models.prompts["text"][0]


def test_a_report_that_is_too_long_does_not_extend_its_short_sections(tmp_path: Path) -> None:
    texts = (german_text(900, "S1"), german_text(60, "S1"), german_text(167))
    models = ResearchModels(texts={"Eins": german_text(160, "S1")})
    r = rig(tmp_path, models, texts)
    short = load_section(r.run_dir, 2)
    assert r.gate.run(r.ctx).passed
    assert models.count("text") == 1  # the long section only
    assert load_section(r.run_dir, 2) == short


def test_a_report_that_is_too_short_gets_each_short_section_extended_once(tmp_path: Path) -> None:
    texts = tuple(german_text(30, "S1") for _ in HEADINGS)
    models = ResearchModels()  # the default answer is German text of the asked length
    r = rig(tmp_path, models, texts)
    outcome = r.gate.run(r.ctx)
    assert outcome.passed
    assert [f.split(" ")[-1] for f in outcome.rounds[0]["fixes"]] == ["1", "2", "3"]
    assert models.count("text") == 3


def test_a_one_time_length_fix_is_not_repeated_in_a_later_round(tmp_path: Path) -> None:
    quote = f"{OPEN}Frei erfundenes Zitat das niemand je sagte{CLOSE}"
    useless = {"Eins": "Kurz [S1].", "Zwei": "Kurz [S1].", "Drei": "Kurz [S1]."}
    short = tuple(german_text(30, "S1") for _ in HEADINGS)
    texts = (short[0] + f" Er schreibt {quote} [S1].", short[1], short[2])
    models = ResearchModels(texts=useless)
    r = rig(tmp_path, models, texts)
    outcome = r.gate.run(r.ctx)
    assert not outcome.passed
    assert models.count("text") == 3  # round one tried each section; round two did not again
    assert outcome.rounds[0]["fixes"] == ["G6: removed quotation marks in section 1"]

    long = (
        german_text(900, "S1") + f" Er schreibt {quote} [S1].",
        german_text(167),
        german_text(167),
    )
    models2 = ResearchModels(texts={"Eins": german_text(2000, "S1")})
    r2 = rig(tmp_path / "long", models2, long)
    assert not r2.gate.run(r2.ctx).passed
    assert models2.count("text") == 1


def test_a_section_in_the_wrong_language_is_written_again_once(tmp_path: Path) -> None:
    english = (
        "The decommissioning of nuclear facilities takes several years according to the "
        "available information and requires an extensive permit from the competent authority, "
        "which examines the individual work steps very closely [S1]. " * 6
    )
    texts = (german_text(167), english, german_text(167))
    models = ResearchModels(texts={"Zwei": german_text(167, "S2")})
    r = rig(tmp_path, models, texts)
    outcome = r.gate.run(r.ctx)
    assert outcome.passed
    assert outcome.rounds[0]["fixes"] == ["G12: wrote section 2 again in the report language"]
    assert models.count("text") == 1


def test_a_second_attempt_at_the_language_is_not_made(tmp_path: Path) -> None:
    english = (
        "The decommissioning of nuclear facilities takes several years according to the "
        "available information and requires an extensive permit from the competent authority, "
        "which examines the individual work steps very closely [S1]. " * 6
    )
    models = ResearchModels(texts={"Zwei": english})
    r = rig(tmp_path, models, (german_text(167), english, german_text(167)))
    outcome = r.gate.run(r.ctx)
    assert not outcome.passed
    assert models.count("text") == 1  # one redraft, then the gate gives up
    assert [x["fixes"] == [] for x in outcome.rounds] == [False, True]


def test_too_few_citations_are_repaired_by_hunks_that_only_add_known_keys(tmp_path: Path) -> None:
    fmt = FormatRange(words=(100, 1500), citations=(1, 2))
    plain = " ".join(f"Satz {n} sagt etwas Wichtiges über den Rückbau." for n in range(30))
    texts = (plain, "Kurzer Text mit Beleg [S1].", "Kurzer Text mit Beleg [S2].")
    old = [f"Satz {n} sagt etwas Wichtiges über den Rückbau." for n in range(6)]
    hunks = [{"old": o, "new": o[:-1] + " [S1].", "reason": ""} for o in old]
    models = ResearchModels(answers={"CitationProposal": [{"hunks": hunks}]})
    r = rig(tmp_path, models, texts, fmt)
    outcome = r.gate.run(r.ctx)
    assert outcome.passed, outcome.result
    assert outcome.rounds[0]["fixes"] == ["G4: added citations (6 edits) in section 1"]
    assert models.count("CitationProposal") == 1  # sections that cite enough are not sent


def test_a_failure_nothing_can_fix_blocks_after_one_round_and_is_named_in_gate_json(
    tmp_path: Path,
) -> None:
    r = rig(tmp_path)
    (r.run_dir / "readability-decisions.json").unlink()
    outcome = r.gate.run(r.ctx)
    assert outcome.passed is False
    assert outcome.rounds == [{"round": 1, "failed": ["G10"], "fixes": []}]
    data = gate_json(r)
    assert data["passed"] is False
    assert [c["id"] for c in data["checks"] if not c["passed"]] == ["G10"]
    assert "readability-decisions.json" in next(
        c["detail"] for c in data["checks"] if c["id"] == "G10"
    )


def test_a_gate_that_still_fails_after_three_rounds_stops_there(tmp_path: Path) -> None:
    leaks = [f"Siehe Locus {n} dazu." for n in range(1, 5)]
    texts = (german_text(167) + " " + " ".join(leaks), german_text(167), german_text(167))
    one_at_a_time = [{"hunks": [{"old": leak, "new": "", "reason": ""}]} for leak in leaks]
    models = ResearchModels(answers={"LeakProposal": one_at_a_time})
    r = rig(tmp_path, models, texts)
    outcome = r.gate.run(r.ctx)
    assert outcome.passed is False
    assert len(outcome.rounds) == RULES.gate_fix_rounds == 3
    assert gate_json(r)["passed"] is False
    assert len(gate_json(r)["rounds"]) == 3
    assert models.count("LeakProposal") == 3
    assert "Locus 4" in (load_section(r.run_dir, 1) or "")


def test_a_resumed_gate_continues_with_the_rounds_and_never_repeats_a_one_time_fix(
    tmp_path: Path,
) -> None:
    texts = tuple(german_text(30, "S1") for _ in HEADINGS)
    useless = {t: "Kurz [S1]." for t in HEADINGS}
    first = ResearchModels(texts=useless)
    r = rig(tmp_path, first, texts)  # type: ignore[arg-type]
    out = r.gate.run(r.ctx)
    assert not out.passed
    assert first.count("text") == 3  # each section was tried once, none got longer
    second = ResearchModels(texts=useless)
    r2 = rig(tmp_path, second, seed_sections=False)
    again = r2.gate.run(r2.ctx)
    assert second.count("text") == 0
    assert len(again.rounds) == len(out.rounds)


def test_every_judgement_renders_the_report_from_the_section_files(tmp_path: Path) -> None:
    quote = f"{OPEN}Frei erfundenes Zitat das niemand je sagte{CLOSE}"
    texts = (german_text(167) + f" Er schreibt {quote} [S1].", german_text(167), german_text(167))
    r = rig(tmp_path, texts=texts)
    r.gate.run(r.ctx)
    assert quote not in (r.run_dir / "report.md").read_text(encoding="utf-8")


def test_the_gate_sees_every_address_a_source_is_known_by(tmp_path: Path) -> None:
    r = rig(tmp_path)
    note = seed_note(r.w.vault, 8)
    r.w.vault._conn.execute(
        "UPDATE notes SET final_url = ? WHERE note_id = ?",
        ("https://moved.example.org/a", note.note_id),
    )
    seen = gate_notes(r.w.vault)[note.note_id]
    assert seen.urls == ("https://seed8.example.org/a", "https://moved.example.org/a")
