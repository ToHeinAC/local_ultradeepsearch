import json
import threading
from pathlib import Path
from typing import Any

import pytest

from app.pipeline.artifacts import (
    export_note_files,
    merge_run_json,
    note_path,
    render_note_file,
    write_note_file,
    write_run_stats,
)
from app.store.models import NewClaim, NewSource, SourceMeta
from app.store.vault import Vault


def source(n: int = 1, **overrides: Any) -> NewSource:
    base: dict[str, Any] = {
        "url": f"https://example.org/p{n}",
        "final_url": f"https://example.org/p{n}",
        "canonical_url": f"https://example.org/p{n}",
        "doi": None,
        "title": f"Page {n}",
        "content_type": "text/html",
        "via": "local",
        "body": f"Body of page {n}.\n\nSecond paragraph.",
        "pages": (),
        "word_count": 5,
        "source_tier": "institutional",
        "derivative_of": None,
        "minhash": None,
        "links": (),
        "meta": SourceMeta(),
    }
    return NewSource(**{**base, **overrides})


def claim(text: str = "Dismantling takes years.", quote: str = "takes years") -> NewClaim:
    return NewClaim(
        claim=text,
        stance="supports",
        stance_target="duration",
        evidence_type="empirical",
        scope_conditions="",
        quoted_support=quote,
        numbers=(),
        entities=(),
        time_period=None,
        region=None,
        confidence="high",
    )


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return Vault(tmp_path / "udr.sqlite", "run-a")


def front_matter(text: str) -> dict[str, Any]:
    head = text.split("---\n")[1]
    out: dict[str, Any] = {}
    for line in head.splitlines():
        key, _, value = line.partition(": ")
        out[key] = json.loads(value)
    return out


def completed(vault: Vault, *, finish: bool = True, **overrides: Any) -> tuple[Any, list[Any]]:
    """An extracted note with one claim; ``finish`` also marks it complete (no analysis needed)."""
    note, _ = vault.add_source_note(source(**overrides))
    vault.save_extraction(note.note_id, "A short summary.", [claim()], dropped=2, failed=False)
    if finish:
        vault.mark_complete(note.note_id)
    stored = vault.get_note(note.note_id)
    return stored, vault.claims(note.note_id)


# ---- rendering ------------------------------------------------------------------------------


def test_front_matter_describes_the_note(vault: Vault) -> None:
    note, claims = completed(vault)
    text = render_note_file(note, claims)
    assert text.startswith("---\n")
    assert front_matter(text) == {
        "note_id": "n0001",
        "kind": "source",
        "stage": "complete",
        "url": "https://example.org/p1",
        "final_url": "https://example.org/p1",
        "title": "Page 1",
        "source_tier": "institutional",
        "doi": None,
        "derivative_of": None,
        "analysis_of": None,
        "claims_kept": 1,
        "claims_dropped": 2,
        "extract_failed": False,
    }


def test_awkward_titles_survive_as_valid_front_matter(vault: Vault) -> None:
    title = 'He said: "yes" ' + chr(0x2013) + " # not a comment\nsecond line"
    note, claims = completed(vault, title=title)
    assert front_matter(render_note_file(note, claims))["title"] == title


def test_sections_and_claim_lines(vault: Vault) -> None:
    note, claims = completed(vault)
    text = render_note_file(note, claims)
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert headings == ["## Summary", "## Claims", "## Source text"]
    assert "A short summary." in text
    dash = chr(0x2014)
    assert (
        f'- [c00001] Dismantling takes years. {dash} "takes years" (supports, empirical, high)'
        in text
    )


def test_a_note_without_extraction_or_claims_says_so(vault: Vault) -> None:
    note, _ = vault.add_source_note(source())
    text = render_note_file(note, [])
    assert "(not extracted yet)" in text
    assert "No claims." in text.split("## Claims")[1].split("## Source text")[0]


def test_the_body_is_written_verbatim(vault: Vault) -> None:
    body = "  Leading spaces,  double  spaces <think>kept</think>\n\nüber 100 %  \n\ntrailing  "
    note, claims = completed(vault, body=body)
    text = render_note_file(note, claims)
    assert text.endswith("## Source text\n\n" + body + "\n")


def test_an_analysis_note_is_its_own_markdown(vault: Vault) -> None:
    source_note, _ = completed(vault, finish=False)
    analysis = vault.add_analysis_note("n0001", "Analysis: Page 1", "## Thesis\nIt is slow.\n")
    text = render_note_file(analysis, [])
    assert front_matter(text)["kind"] == "source_analysis"
    assert front_matter(text)["analysis_of"] == "n0001"
    assert text.endswith("## Thesis\nIt is slow.\n")
    assert "## Source text" not in text
    assert source_note.note_id == "n0001"


# ---- writing and exporting ------------------------------------------------------------------


def test_write_note_file_goes_to_the_notes_directory(vault: Vault, tmp_path: Path) -> None:
    note, claims = completed(vault, body="kept <think>as is</think>")
    run_dir = tmp_path / "runs" / "run-a"
    path = write_note_file(run_dir, note, claims)
    assert path == note_path(run_dir, "n0001") == run_dir / "notes" / "n0001.md"
    assert "kept <think>as is</think>" in path.read_text(encoding="utf-8")  # never scrubbed


def test_export_writes_every_note_and_is_idempotent(vault: Vault, tmp_path: Path) -> None:
    completed(vault, finish=False)
    completed(vault, n=2)
    vault.add_analysis_note("n0001", "Analysis", "## Thesis\nx\n")
    run_dir = tmp_path / "run"
    assert export_note_files(vault, run_dir) == 3
    assert sorted(p.name for p in (run_dir / "notes").iterdir()) == [
        "n0001.md",
        "n0002.md",
        "n0003.md",
    ]
    assert export_note_files(vault, run_dir) == 0


def test_export_keeps_the_body_verbatim_too(vault: Vault, tmp_path: Path) -> None:
    body = "A page that talks about <think>tags</think> literally."
    completed(vault, body=body)
    run_dir = tmp_path / "run"
    export_note_files(vault, run_dir)
    assert body in note_path(run_dir, "n0001").read_text(encoding="utf-8")


def test_export_restores_deleted_and_edited_files(vault: Vault, tmp_path: Path) -> None:
    completed(vault)
    completed(vault, n=2)
    run_dir = tmp_path / "run"
    export_note_files(vault, run_dir)
    note_path(run_dir, "n0001").unlink()
    note_path(run_dir, "n0002").write_text("someone edited this", encoding="utf-8")
    assert export_note_files(vault, run_dir) == 2
    assert "Body of page 2." in note_path(run_dir, "n0002").read_text(encoding="utf-8")


def test_export_refreshes_a_file_whose_note_has_moved_on(vault: Vault, tmp_path: Path) -> None:
    note, _ = vault.add_source_note(source())
    run_dir = tmp_path / "run"
    export_note_files(vault, run_dir)
    assert (
        front_matter(note_path(run_dir, "n0001").read_text(encoding="utf-8"))["stage"] == "fetched"
    )
    vault.save_extraction(note.note_id, "Summary.", [claim()], dropped=0, failed=False)
    assert export_note_files(vault, run_dir) == 1
    text = note_path(run_dir, "n0001").read_text(encoding="utf-8")
    assert front_matter(text)["stage"] == "extracted"
    assert "Summary." in text


# ---- run.json -------------------------------------------------------------------------------


def test_run_stats_are_merged_into_run_json_and_keep_other_keys(
    vault: Vault, tmp_path: Path
) -> None:
    completed(vault)
    completed(vault, n=2)
    vault.reject("https://x.org/a", "https://x.org/a", "login_wall")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps({"status": "running", "steps": [1, 2]}), encoding="utf-8"
    )
    write_run_stats(run_dir, vault.stats())
    data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert (data["status"], data["steps"]) == ("running", [1, 2])
    stats = data["stats"]
    assert stats["notes_by_kind"] == {"source": 2}
    assert stats["rejected_by_reason"] == {"login_wall": 1}
    assert (stats["claims_kept"], stats["claims_dropped"]) == (2, 4)
    assert stats["claims_drop_rate"] == pytest.approx(4 / 6)
    assert set(stats) == {
        "notes_by_kind",
        "derivatives",
        "rejected_by_reason",
        "claims_kept",
        "claims_dropped",
        "claims_drop_rate",
        "extract_failed",
        "source_analyses",
    }


def test_run_json_is_created_when_missing_and_stats_are_replaced(
    vault: Vault, tmp_path: Path
) -> None:
    run_dir = tmp_path / "fresh" / "run"
    write_run_stats(run_dir, vault.stats())
    assert (
        json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["stats"]["claims_kept"] == 0
    )
    completed(vault)
    write_run_stats(run_dir, vault.stats())
    assert (
        json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["stats"]["claims_kept"] == 1
    )


def test_concurrent_updates_do_not_lose_keys(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"

    def add(index: int) -> None:
        merge_run_json(run_dir, {f"key{index}": index})

    threads = [threading.Thread(target=add, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert data == {f"key{i}": i for i in range(12)}


def test_a_corrupt_run_json_is_reported_not_overwritten(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match=r"run\.json"):
        merge_run_json(run_dir, {"a": 1})
    assert (run_dir / "run.json").read_text(encoding="utf-8") == "{not json"


def test_a_run_json_that_is_not_an_object_is_reported(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError, match="object"):
        merge_run_json(run_dir, {"a": 1})
