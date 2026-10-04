"""Step 1 (PRD M5): decomposition, coverage loop, headings, word budgets and shims."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from support import make_settings

from app.events import MemoryEventSink
from app.llm.errors import LLMModelMissingError
from app.llm.fakes import CallbackTransport, reply
from app.llm.roles import build_registry
from app.llm.service import LLMService
from app.llm.types import ChatReply, ChatRequest, Endpoint
from app.pipeline.profiles import load_research_config, load_response_formats
from app.pipeline.strategies import load_strategies
from app.prompts import research as prompts
from app.research.decompose import (
    StepOneInput,
    atomic_items,
    final_headings,
    run_step_one,
    section_budgets,
    section_weights,
)
from app.research.schemas import Decomposition, Levers
from app.research.shims import compose_shims
from app.templates import load_templates

SETTINGS = make_settings()
TEMPLATES = load_templates([SETTINGS.templates_dir])
FORMATS = load_response_formats(SETTINGS.config_dir)
CONFIG = load_research_config(SETTINGS.config_dir)
DOMAINS = load_strategies(SETTINGS.config_dir).domains
URLS = {Endpoint.OWN: "http://own", Endpoint.SHARED: "http://shared"}
REGISTRY = build_registry(SETTINGS)
BRIEF = (
    "# Wie teuer ist der Rückbau von Kernkraftwerken?\n\nMethod: 2 Klärungsrunden\n\n"
    "## Forschungsfragen\n\n1. Kosten je Anlage\n2. Dauer\n"
)

DECOMPOSITION: dict[str, Any] = {
    "sub_questions": ["Was kostet der Rückbau je Anlage?", "Wie lange dauert er?"],
    "entities": [{"name": "Kernkraftwerk Obrigheim", "type": "plant", "required_fields": []}],
    "time_periods": [{"period": "2005-2025", "type": "range", "primary_source": "", "issuer": ""}],
    "domains": ["regulation_de_eu"],
    "section_headings": ["## Kosten", "## Dauer", "## Einordnung"],
    "section_weights": [2.0, 1.0, 1.0],
    "tier_recommendation": "light",
    "tier_rationale": "Begrenzte Frage.",
    "modality": "synthesize",
    "levers": {"register": "analyze", "inference_depth": "standard", "domain_notes": "Behörden."},
}
GAP = {"phrase": "Kernkraftwerke", "item_ids": [], "scope_ok": False, "gap": True, "note": "eng"}
OK = {"phrase": "Rückbau", "item_ids": ["i01"], "scope_ok": True, "gap": False, "note": ""}


@dataclass
class Models:
    """Answers each step-1 call by its system prompt; ``errors`` raise instead of answering."""

    decomposition: dict[str, Any] = field(default_factory=lambda: dict(DECOMPOSITION))
    revised: dict[str, Any] = field(
        default_factory=lambda: {**DECOMPOSITION, "sub_questions": ["Kosten?", "Dauer?", "Wer?"]}
    )
    matrices: list[list[dict[str, Any]]] = field(default_factory=lambda: [[OK]])
    headings: list[str] = field(default_factory=lambda: ["Kosten", "Dauer"])
    errors: dict[str, Exception] = field(default_factory=lambda: {})
    calls: list[tuple[str, ChatRequest]] = field(default_factory=lambda: [])

    def kind(self, request: ChatRequest) -> str:
        first = request.messages[0]["content"].split("\n", 1)[0]
        kinds = {
            prompts.DECOMPOSE_SYSTEM: "decompose",
            prompts.COVERAGE_SYSTEM: "coverage",
            prompts.REVISE_DECOMPOSITION_SYSTEM: "revise",
            prompts.HEADINGS_SYSTEM: "headings",
        }
        return next(kind for system, kind in kinds.items() if system.split("\n", 1)[0] == first)

    def __call__(self, request: ChatRequest) -> ChatReply | Exception:
        kind = self.kind(request)
        self.calls.append((kind, request))
        if kind in self.errors:
            return self.errors[kind]
        answers: dict[str, Callable[[], object]] = {
            "decompose": lambda: self.decomposition,
            "revise": lambda: self.revised,
            "coverage": lambda: {
                "rows": self.matrices[min(self.count("coverage"), len(self.matrices)) - 1]
            },
            "headings": lambda: {"headings": self.headings},
        }
        return reply(json.dumps(answers[kind]()))

    def count(self, kind: str) -> int:
        return sum(1 for k, _ in self.calls if k == kind)


@dataclass
class Rig:
    models: Models
    events: MemoryEventSink
    llm: LLMService
    run_dir: Path

    def run(self, template_id: str = "auto", fmt: str = "structured", language: str = "de") -> Any:
        inp = StepOneInput(
            brief=BRIEF,
            template=TEMPLATES[template_id],
            tier="light",
            response_format=fmt,
            fmt=FORMATS.named(fmt),
            report_language=language,
            domains=DOMAINS,
        )
        return run_step_one(self.llm, self.events, self.run_dir, inp, CONFIG)


def rig(tmp_path: Path, models: Models | None = None) -> Rig:
    models = models or Models()
    events = MemoryEventSink()
    llm = LLMService(
        REGISTRY, URLS, CallbackTransport(models), events, timeout_s=5, sleep=lambda _s: None
    )
    return Rig(models, events, llm, tmp_path / "run")


def dec(**overrides: Any) -> Decomposition:
    return Decomposition.model_validate({**DECOMPOSITION, **overrides})


# ---- items, headings, weights, budgets (pure) -----------------------------------------------


def test_atomic_items_are_sub_questions_then_entities_then_periods() -> None:
    items = atomic_items(dec())
    assert [(i.item_id, i.kind) for i in items] == [
        ("i01", "sub_question"),
        ("i02", "sub_question"),
        ("i03", "entity"),
        ("i04", "period"),
    ]
    assert items[0].text == "Was kostet der Rückbau je Anlage?"
    assert items[2].text == "Kernkraftwerk Obrigheim"
    assert items[3].text == "2005-2025"


def no_repair(_problem: str) -> list[str]:
    raise AssertionError("no repair expected")


def test_a_fixed_template_keeps_its_headings() -> None:
    events = MemoryEventSink()
    template = TEMPLATES["regulatorische-analyse"]
    got = final_headings(template, dec(), events=events, repair=no_repair, language="de")
    assert got == list(template.headings)
    assert events.of_type("brief_sections_overridden") == []


def test_a_fixed_template_overrides_sections_the_brief_asks_for() -> None:
    events = MemoryEventSink()
    template = TEMPLATES["regulatorische-analyse"]
    asked = dec(required_sections=["## Fazit"])
    assert final_headings(template, asked, events=events, repair=no_repair, language="de") == list(
        template.headings
    )
    assert len(events.of_type("brief_sections_overridden")) == 1


def test_auto_takes_the_derived_headings_without_hashes() -> None:
    got = final_headings(
        TEMPLATES["auto"], dec(), events=MemoryEventSink(), repair=no_repair, language="de"
    )
    assert got == ["Kosten", "Dauer", "Einordnung"]


@pytest.mark.parametrize(
    "bad",
    [["## Nur eine"], ["Kosten", "kosten"], ["Kosten", "Quellen"], ["Kosten", "Anhang A"], []],
)
def test_invalid_auto_headings_are_repaired_once(bad: list[str]) -> None:
    problems: list[str] = []

    def repair(problem: str) -> list[str]:
        problems.append(problem)
        return ["Kosten", "Dauer"]

    got = final_headings(
        TEMPLATES["auto"],
        dec(section_headings=bad),
        events=MemoryEventSink(),
        repair=repair,
        language="de",
    )
    assert got == ["Kosten", "Dauer"]
    assert len(problems) == 1
    assert problems[0]


def test_a_failed_repair_falls_back_to_one_heading_per_sub_question() -> None:
    events = MemoryEventSink()
    got = final_headings(
        TEMPLATES["auto"],
        dec(section_headings=[]),
        events=events,
        repair=lambda _p: ["Quellen"],
        language="de",
    )
    assert got == ["Was kostet der Rückbau je Anlage?", "Wie lange dauert er?"]
    assert len(events.of_type("headings_fallback")) == 1


def test_the_fallback_has_at_least_two_and_at_most_fifteen_headings() -> None:
    one = dec(section_headings=[], sub_questions=["Nur eine Frage?"])
    got = final_headings(
        TEMPLATES["auto"], one, events=MemoryEventSink(), repair=lambda _p: [], language="en"
    )
    assert got == ["Background and context", "Nur eine Frage?"]
    many = dec(section_headings=[], sub_questions=[f"Frage {n}?" for n in range(20)])
    got = final_headings(
        TEMPLATES["auto"], many, events=MemoryEventSink(), repair=lambda _p: [], language="de"
    )
    assert len(got) == 15


def test_a_repair_error_falls_back_too() -> None:
    def repair(_problem: str) -> list[str]:
        raise LLMModelMissingError("gone")

    got = final_headings(
        TEMPLATES["auto"],
        dec(section_headings=[]),
        events=MemoryEventSink(),
        repair=repair,
        language="de",
    )
    assert len(got) == 2


def test_weights_are_clamped_and_a_wrong_count_means_equal_weights() -> None:
    events = MemoryEventSink()
    assert section_weights([3.0, 0.1, 1.2], 3, (0.5, 2.0), events) == [2.0, 0.5, 1.2]
    assert events.of_type("section_weights_reset") == []
    assert section_weights([1.0, 2.0], 3, (0.5, 2.0), events) == [1.0, 1.0, 1.0]
    assert len(events.of_type("section_weights_reset")) == 1


def test_budgets_add_up_to_the_target_in_proportion() -> None:
    budgets = section_budgets([2.0, 1.0, 1.0], 3500, 80)
    assert budgets == [1750, 875, 875]
    assert sum(section_budgets([1.0, 1.0, 1.0], 1000, 80)) == 1000


def test_no_budget_falls_below_the_minimum() -> None:
    budgets = section_budgets([2.0] + [0.5] * 9, 1250, 80)
    assert sum(budgets) == 1250
    assert min(budgets) >= 80
    assert budgets[0] > budgets[1]


def test_too_many_sections_for_the_minimum_split_the_target_evenly() -> None:
    budgets = section_budgets([1.0] * 15, 1000, 80)
    assert sum(budgets) == 1000
    assert max(budgets) - min(budgets) <= 1


# ---- the step -------------------------------------------------------------------------------


def test_a_clean_run_writes_every_artifact(tmp_path: Path) -> None:
    r = rig(tmp_path)
    result = r.run()
    assert [k for k, _ in r.models.calls] == ["decompose", "coverage"]
    assert all(req.think for _, req in r.models.calls)
    data = json.loads((r.run_dir / "prompt-decomposition.json").read_text(encoding="utf-8"))
    assert data["required_section_headings"] == ["Kosten", "Dauer", "Einordnung"]
    assert data["section_weights"] == [2.0, 1.0, 1.0]
    assert data["section_budgets"] == [1750, 875, 875]  # the middle of 2000..5000
    assert (data["pipeline_tier"], data["tier_recommendation"]) == ("light", "light")
    assert (data["response_format"], data["report_language"]) == ("structured", "de")
    assert data["citation_style"] == "inline"
    assert [i["item_id"] for i in data["items"]] == ["i01", "i02", "i03", "i04"]
    assert data["modality"] == "synthesize"
    assert result.headings == ["Kosten", "Dauer", "Einordnung"]
    matrix = (r.run_dir / "temp" / "coverage-matrix.md").read_text(encoding="utf-8")
    assert '| "Rückbau" | i01 | OK | No |' in matrix
    scaffold = (r.run_dir / "scaffold.md").read_text(encoding="utf-8")
    assert "## Modality\n\nsynthesize\n" in scaffold
    assert "## Tier rationale\n\nBegrenzte Frage.\n" in scaffold
    assert "## Coverage notes" not in scaffold
    for role in ("research", "drafting", "critics", "polish"):
        assert (
            (r.run_dir / "shims" / f"{role}.md")
            .read_text(encoding="utf-8")
            .startswith("## Run directives")
        )


def test_the_decompose_prompt_carries_the_brief_and_the_run_settings(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.run(template_id="regulatorische-analyse")
    _, request = r.models.calls[0]
    user = request.messages[-1]["content"]
    assert BRIEF in user
    assert "Run tier: light" in user
    assert "Response format: structured" in user
    for heading in TEMPLATES["regulatorische-analyse"].headings:
        assert heading in user
    for domain in DOMAINS:
        assert domain in user


def test_gaps_lead_to_a_revision_and_a_new_check(tmp_path: Path) -> None:
    r = rig(tmp_path, Models(matrices=[[GAP], [OK]]))
    result = r.run()
    assert [k for k, _ in r.models.calls] == ["decompose", "coverage", "revise", "coverage"]
    assert result.decomposition.sub_questions == ["Kosten?", "Dauer?", "Wer?"]
    assert "Kernkraftwerke" in r.models.calls[2][1].messages[-1]["content"]


def test_gaps_left_after_the_last_check_are_noted(tmp_path: Path) -> None:
    r = rig(tmp_path, Models(matrices=[[GAP]]))
    result = r.run()
    kinds = [k for k, _ in r.models.calls]
    assert kinds.count("coverage") == CONFIG.coverage_iterations
    assert kinds.count("revise") == CONFIG.coverage_iterations - 1
    assert [g.phrase for g in result.gaps_left] == ["Kernkraftwerke"]
    scaffold = (r.run_dir / "scaffold.md").read_text(encoding="utf-8")
    assert '## Coverage notes\n\n- "Kernkraftwerke": eng\n' in scaffold
    assert len(r.events.of_type("coverage_gaps_left")) == 1


def test_a_coverage_error_ends_the_loop_with_a_warning(tmp_path: Path) -> None:
    r = rig(tmp_path, Models(errors={"coverage": LLMModelMissingError("gone")}))
    result = r.run()
    assert [k for k, _ in r.models.calls] == ["decompose", "coverage"]
    assert result.gaps_left == []
    (event,) = r.events.of_type("coverage_check_failed")
    assert event.level == "warning"


def test_a_decomposition_error_is_fatal(tmp_path: Path) -> None:
    r = rig(tmp_path, Models(errors={"decompose": LLMModelMissingError("gone")}))
    with pytest.raises(LLMModelMissingError):
        r.run()


def test_a_second_run_reuses_every_answer(tmp_path: Path) -> None:
    first = rig(tmp_path, Models(matrices=[[GAP], [OK]]))
    first.run()
    again = rig(tmp_path)  # a restarted process on the same run directory
    result = again.run()
    assert again.models.calls == []
    assert result.decomposition.sub_questions == ["Kosten?", "Dauer?", "Wer?"]


def test_invalid_auto_headings_call_the_repair_prompt(tmp_path: Path) -> None:
    models = Models(decomposition={**DECOMPOSITION, "section_headings": ["Quellen"]})
    r = rig(tmp_path, models)
    result = r.run()
    assert result.headings == ["Kosten", "Dauer"]
    assert models.count("headings") == 1
    assert models.calls[-1][1].think


# ---- shims ----------------------------------------------------------------------------------

HEADER = (
    "## Run directives\n\nAuto-selected for this run in step 1; binding wherever they adjust a "
    "default in your prompt. Absent instructions here leave your prompt's defaults untouched.\n\n"
)


def test_the_default_shims_match_the_upstream_composition() -> None:
    shims = compose_shims(
        Levers.model_validate({"register": "analyze", "register_confidence": "low"})
    )
    head = HEADER + "- register: analyze\n- inference depth: standard\n"
    assert shims["research"] == (
        head + "\n### Inference depth\n\nStandard depth (this run confirms your prompt's "
        "defaults). Follow the\nnormal sourcing playbook.\n"
    )
    assert shims["polish"] == (
        head + "\n### Register posture\n\nDefault evaluative posture (this run confirms your "
        "prompt's defaults).\nHedge-striking and the one-kicker-per-section budget apply as\n"
        "written.\n"
    )


def test_a_teach_deep_shim_has_domain_notes_and_the_deep_block() -> None:
    shims = compose_shims(
        Levers.model_validate(
            {
                "register": "teach",
                "inference_depth": "deep",
                "domain_notes": "Primärquellen zuerst.",
            }
        )
    )
    research = shims["research"]
    assert research.startswith(HEADER + "- register: teach\n- inference depth: deep\n")
    assert "\n### Domain notes\n\nPrimärquellen zuerst.\n\n### Inference depth\n\nDEEP" in research
    assert research.endswith("budgets when a lead is genuinely load-bearing.\n")
    assert "### Domain notes" not in shims["critics"]
    assert "### Domain notes" in shims["drafting"]
    assert "TEACH register" in shims["drafting"]


# ---- prompts follow AD3 ---------------------------------------------------------------------


def prompt_constants() -> dict[str, str]:
    return {k: v for k, v in vars(prompts).items() if k.isupper() and isinstance(v, str)}


@pytest.mark.parametrize("name", sorted(prompt_constants()))
def test_prompt_text_contains_no_digits_outside_placeholders(name: str) -> None:
    text = re.sub(r"\{[a-z_]+\}", "", prompt_constants()[name])
    assert not re.search(r"\d", text), f"{name} hardcodes a number"


def test_system_prompts_have_unique_first_lines() -> None:
    systems = [v for k, v in prompt_constants().items() if k.endswith("_SYSTEM")]
    firsts = [s.split("\n", 1)[0] for s in systems]
    assert len(set(firsts)) == len(firsts)
