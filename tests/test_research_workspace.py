"""Run manifest, step journal, workspace files and external briefs (PRD M5, step 0)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from support import make_settings

from app.brief.errors import InvalidInput
from app.brief.parse import parse_brief
from app.pipeline.artifacts import merge_run_json
from app.pipeline.profiles import load_response_formats
from app.research.external import prepare_external_brief
from app.research.journal import Journal
from app.research.manifest import Manifest
from app.research.models import RunSpec
from app.research.workspace import bootstrap_workspace, set_scaffold_section
from app.templates import load_templates

NOW = datetime(2026, 10, 4, 8, 0, 0, tzinfo=UTC)
SETTINGS = make_settings()
TEMPLATES = load_templates([SETTINGS.templates_dir])
FORMATS = load_response_formats(SETTINGS.config_dir)
BRIEF = "# Wie teuer ist der Rückbau?\n\n## Forschungsfragen\n\n1. Kosten <think>x</think>\n"


def spec(**overrides: object) -> RunSpec:
    base: dict[str, object] = {
        "run_id": "r-20261004-080000-abcdef",
        "tier": "light",
        "brief_sha256": "ab" * 32,
        "brief_path": "data/briefs/x.md",
        "template_id": "auto",
        "response_format": "structured",
        "report_language": "de",
        "summarize_model": None,
    }
    return RunSpec(**{**base, **overrides})  # type: ignore[arg-type]


# ---- journal --------------------------------------------------------------------------------


def test_the_journal_keeps_values_across_instances(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "temp" / "j.json")
    assert journal.get("k") is None
    journal.put("k", {"a": [1, 2]})
    journal.put("other", "ü")
    again = Journal(tmp_path / "temp" / "j.json")
    assert again.get("k") == {"a": [1, 2]}
    assert again.data() == {"k": {"a": [1, 2]}, "other": "ü"}


@pytest.mark.parametrize("damage", ["{not json", "[1, 2]"])
def test_a_damaged_journal_is_an_error_not_a_reset(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "j.json"
    path.write_text(damage, encoding="utf-8")
    with pytest.raises(ValueError, match=r"j\.json"):
        Journal(path).get("k")
    assert path.read_text(encoding="utf-8") == damage


# ---- manifest -------------------------------------------------------------------------------


def test_the_manifest_records_the_spec_and_steps_in_order(tmp_path: Path) -> None:
    manifest = Manifest(tmp_path, now=lambda: NOW)
    manifest.init(spec())
    manifest.start_step("0")
    manifest.finish_step("0")
    manifest.start_step("1")
    manifest.start_step("1")  # a re-run after a crash does not add a second entry
    assert manifest.step_ids() == ["0", "1"]
    data = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert data["steps"] == [
        {
            "id": "0",
            "status": "done",
            "started_at": NOW.isoformat(),
            "finished_at": NOW.isoformat(),
        },
        {"id": "1", "status": "running", "started_at": NOW.isoformat(), "finished_at": None},
    ]
    assert (data["run_id"], data["tier"], data["template_id"]) == (spec().run_id, "light", "auto")
    assert (data["response_format"], data["report_language"]) == ("structured", "de")
    assert data["brief_sha256"] == "ab" * 32


def test_init_again_keeps_steps_and_the_stats_of_the_pipeline(tmp_path: Path) -> None:
    manifest = Manifest(tmp_path, now=lambda: NOW)
    manifest.init(spec())
    manifest.start_step("0")
    merge_run_json(tmp_path, {"stats": {"sources": 3}})
    manifest.init(spec())
    data = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert (data["stats"], manifest.step_ids()) == ({"sources": 3}, ["0"])


def test_status_and_exports(tmp_path: Path) -> None:
    manifest = Manifest(tmp_path, now=lambda: NOW)
    manifest.init(spec())
    manifest.set_status("failed", "LLMTimeoutError")
    manifest.set_exports({"docx": "ok"})
    data = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert (data["status"], data["status_reason"], data["exports"]) == (
        "failed",
        "LLMTimeoutError",
        {"docx": "ok"},
    )


def test_finishing_an_unstarted_step_is_an_error(tmp_path: Path) -> None:
    manifest = Manifest(tmp_path, now=lambda: NOW)
    manifest.init(spec())
    with pytest.raises(ValueError, match="step 2 was not started"):
        manifest.finish_step("2")


# ---- workspace ------------------------------------------------------------------------------


def test_query_md_holds_the_exact_brief_bytes(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path, spec(), BRIEF)
    assert (tmp_path / "query.md").read_bytes() == BRIEF.encode("utf-8")  # <think> stays


def test_the_scaffold_holds_the_run_config_and_survives_a_second_bootstrap(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path, spec(), BRIEF)
    set_scaffold_section(tmp_path, "Modality", "synthesize")
    bootstrap_workspace(tmp_path, spec(), BRIEF)
    text = (tmp_path / "scaffold.md").read_text(encoding="utf-8")
    assert text.count("## Run config") == 1
    assert "- Run: r-20261004-080000-abcdef" in text
    assert "- Tier: light" in text
    assert "- Template: auto" in text
    assert "- Format: structured" in text
    assert "- Language: de" in text
    assert "## Modality\n\nsynthesize\n" in text


def test_a_scaffold_section_is_replaced_in_place(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path, spec(), BRIEF)
    set_scaffold_section(tmp_path, "Modality", "collect")
    set_scaffold_section(tmp_path, "Tier rationale", "kurz")
    set_scaffold_section(tmp_path, "Modality", "compare\n\nzwei Absätze")
    text = (tmp_path / "scaffold.md").read_text(encoding="utf-8")
    assert text.index("## Modality") < text.index("## Tier rationale")
    assert "collect" not in text
    assert "## Modality\n\ncompare\n\nzwei Absätze\n\n## Tier rationale\n\nkurz\n" in text


# ---- external briefs ------------------------------------------------------------------------


def external(text: str, language: str = "de", template_id: str = "auto") -> str:
    return prepare_external_brief(
        text,
        template=TEMPLATES[template_id],
        fmt_name="structured",
        fmt=FORMATS.structured,
        language=language,
    )


def test_an_external_brief_gets_a_method_line_and_an_output_section() -> None:
    text = "# Wie teuer ist der Rückbau?\n\nVorrede  mit  Abständen.\n\n1. Kosten\n2. Dauer\n"
    done = external(text)
    lines = done.split("\n")
    assert lines[0] == "# Wie teuer ist der Rückbau?"
    assert lines[2] == "Method: extern geliefert"
    assert "Vorrede  mit  Abständen.\n\n1. Kosten\n2. Dauer\n\n## Ausgabe\n" in done
    assert parse_brief(done).research_questions == ("Kosten", "Dauer")


def test_an_english_external_brief_gets_english_labels() -> None:
    done = external("# How much?\n\n1. Costs\n", language="en")
    assert "Method: externally supplied" in done
    assert "## Output" in done


def test_an_existing_method_line_and_output_section_are_replaced_not_doubled() -> None:
    text = "# Titel\n\nMethod: von Hand\n\n## Forschungsfragen\n\n1. Frage\n\n## Ausgabe\n\nalt\n"
    done = external(text)
    assert done.count("Method:") == 1
    assert "Method: von Hand" in done
    assert done.count("## Ausgabe") == 1
    assert "alt" not in done


def test_an_unparseable_external_brief_is_rejected() -> None:
    with pytest.raises(InvalidInput, match="numbered research questions"):
        external("# Titel\n\nkeine Fragen\n")
