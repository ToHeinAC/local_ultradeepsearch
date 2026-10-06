"""M6 D9: a run applies its own `summarize_model` to the summarize and extract roles."""

import json
from pathlib import Path

import pytest
from research_rig import REGISTRY, ResearchModels, llm
from research_run_rig import RunRig, make_rig

from app.events import MemoryEventSink
from app.llm.types import Role
from app.research.manifest import RunSettings
from app.research.settings import llm_for_run


def run_with_model(base: Path, model: str | None) -> tuple[RunRig, str]:
    """A run whose frozen settings name ``model``, worked by a fresh process (the contexts of the
    first one were built when the run was created)."""
    first = make_rig(base)
    run_id = first.create().run_id
    row = first.runs.get_run(run_id)
    assert row is not None
    assert row.settings_json is not None
    stored = {**json.loads(row.settings_json), "summarize_model": model}
    first.runs.set_settings(run_id, json.dumps(stored))
    r = make_rig(base)
    r.service.run(run_id)
    r.service.approve_and_run(run_id, str(r.service.view(run_id).plan_sha256))
    return r, run_id


def test_a_run_with_its_own_summarize_model_sends_it_for_summarize_calls_only(
    tmp_path: Path,
) -> None:
    r, run_id = run_with_model(tmp_path, "gemma4:e2b")
    assert r.runs.get_run(run_id).status == "done"  # type: ignore[union-attr]
    used = r.models.models_used
    assert used["ReadabilityProposal"] == {"gemma4:e2b"}  # a summarize-role step
    assert used["PlanDraft"] == {REGISTRY[Role.REASON].model}  # reason is untouched
    warnings = r.events.of_type("summarize_model_override")
    assert [(e.level, e.data["model"], e.data["run_id"]) for e in warnings] == [
        ("warning", "gemma4:e2b", run_id)
    ]


def settings_of(model: str | None) -> RunSettings:
    return RunSettings(
        report_language="de",
        response_format="short",
        template_id="auto",
        interview_language="de",
        tier="light",
        summarize_model=model,
    )


@pytest.mark.parametrize("model", [None, REGISTRY[Role.SUMMARIZE].model])
def test_no_choice_or_the_configured_model_keeps_the_service_and_says_nothing(
    model: str | None,
) -> None:
    events = MemoryEventSink()
    base = llm(ResearchModels(), events)
    chosen = llm_for_run(base, settings_of(model), REGISTRY[Role.SUMMARIZE].model, "r-1", events)
    assert chosen is base
    assert events.events == []
