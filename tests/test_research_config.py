"""PRD AD3: the numbers of the research steps are data in `config/profiles.toml`."""

from pathlib import Path

import pytest
from pydantic import ValidationError
from support import make_settings

from app.pipeline.profiles import load_research_budget, load_run_rules


def config_dir() -> Path:
    return make_settings().config_dir


def test_light_budget_matches_the_prd() -> None:
    light = load_research_budget("light", config_dir())
    assert light.sources_min == 10
    assert light.planned_searches == (8, 20)
    assert light.adversarial_min == 5
    assert light.deduped_urls == (15, 30)
    assert light.fetch_waves == (1, 2)
    assert light.must_read_notes == (8, 15)
    assert light.readability_cap == 50


def test_full_budget_matches_the_prd_table() -> None:
    full = load_research_budget("full", config_dir())
    assert (full.sources_min, full.planned_searches, full.must_read_notes) == (
        45,
        (40, 100),
        (20, 50),
    )
    assert full.deduped_urls == (60, 100)
    assert full.fetch_waves == (2, 3)


def test_an_unknown_budget_is_an_error() -> None:
    with pytest.raises(ValueError, match="unknown profile 'dissertation'"):
        load_research_budget("dissertation", config_dir())


def test_run_rules_hold_the_gate_numbers() -> None:
    rules = load_run_rules(config_dir())
    assert rules.coverage_matrix_max_iterations == 3
    assert rules.gate_fix_rounds == 3
    assert rules.thin_item_sources == 2
    assert rules.hunk_max_chars == 1200
    assert rules.citation_density_min == 9
    assert rules.quote_min_words == 5
    assert rules.retraction_window_chars == 200
    assert rules.language_samples == 3
    assert rules.length_tolerance == (0.8, 1.2)


def write(tmp_path: Path, research: str) -> Path:
    (tmp_path / "profiles.toml").write_text(research, encoding="utf-8")
    return tmp_path


BUDGET = """
sources_min = 10
planned_searches = {planned}
adversarial_min = 5
deduped_urls = [15, 30]
fetch_waves = [1, 2]
must_read_notes = [8, 15]
readability_cap = 50
"""


def test_a_range_must_be_ordered_and_positive(tmp_path: Path) -> None:
    template = "[research.light]\n" + BUDGET
    for bad in ("[20, 8]", "[0, 8]"):
        directory = write(tmp_path, template.format(planned=bad))
        with pytest.raises(ValidationError, match="expected 0 < low <= high"):
            load_research_budget("light", directory)


def test_unknown_keys_are_refused(tmp_path: Path) -> None:
    text = "[research.light]\n" + BUDGET.format(planned="[8, 20]") + "surprise = 1\n"
    with pytest.raises(ValidationError, match="surprise"):
        load_research_budget("light", write(tmp_path, text))
