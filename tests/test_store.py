import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.store.db import MIGRATIONS, connect, migrate
from app.store.models import NewClaim, NewSource, SourceMeta, normalize_doi
from app.store.vault import Vault

NOW = datetime(2026, 10, 2, 8, 0, tzinfo=UTC)


def source(n: int, **overrides: Any) -> NewSource:
    base: dict[str, Any] = {
        "url": f"https://example.org/p{n}?utm_source=x",
        "final_url": f"https://example.org/p{n}",
        "canonical_url": f"https://example.org/p{n}",
        "doi": None,
        "title": f"Page {n}",
        "content_type": "text/html",
        "via": "local",
        "body": f"Body of page {n} about reactor decommissioning.",
        "pages": (),
        "word_count": 7,
        "source_tier": "unknown",
        "derivative_of": None,
        "minhash": None,
        "links": (),
        "meta": SourceMeta(),
    }
    return NewSource(**{**base, **overrides})


def claim(text: str = "Dismantling takes years.", **overrides: Any) -> NewClaim:
    base: dict[str, Any] = {
        "claim": text,
        "stance": "supports",
        "stance_target": "duration",
        "evidence_type": "empirical",
        "scope_conditions": "research reactors",
        "quoted_support": "takes years",
        "numbers": ("12 years",),
        "entities": ("IAEA",),
        "time_period": "2020s",
        "region": "EU",
        "confidence": "high",
    }
    return NewClaim(**{**base, **overrides})


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "data" / "udr.sqlite"


def open_vault(db: Path, run_id: str = "run-a") -> Vault:
    return Vault(db, run_id, now=lambda: NOW)


# ---- database -------------------------------------------------------------------------------


def test_migrate_is_idempotent_and_records_the_version(db: Path) -> None:
    conn = connect(db)
    migrate(conn)
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"runs", "notes", "claims", "rejected_sources", "notes_fts"} <= tables


def test_connection_pragmas(db: Path) -> None:
    conn = connect(db)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_a_failing_migration_rolls_back_completely(db: Path) -> None:
    conn = connect(db)
    migrations = [
        "CREATE TABLE one (x INTEGER);",
        "CREATE TABLE two (x INTEGER); CREATE TABLE one (y INTEGER);",
    ]
    with pytest.raises(sqlite3.OperationalError):
        migrate(conn, migrations)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "one" in names
    assert "two" not in names


# ---- notes ----------------------------------------------------------------------------------


def test_a_new_source_becomes_a_fetched_note_with_sequential_ids(db: Path) -> None:
    vault = open_vault(db)
    first, created = vault.add_source_note(source(1))
    second, _ = vault.add_source_note(source(2))
    assert (first.note_id, first.stage, first.kind, created) == ("n0001", "fetched", "source", True)
    assert second.note_id == "n0002"
    assert first.created_at == NOW.isoformat()


def test_a_known_canonical_url_returns_the_existing_note(db: Path) -> None:
    vault = open_vault(db)
    first, _ = vault.add_source_note(source(1))
    again, created = vault.add_source_note(source(1, title="other title"))
    assert (again.note_id, created) == (first.note_id, False)
    assert len(vault.notes()) == 1
    assert again.title == "Page 1"


def test_every_field_round_trips(db: Path) -> None:
    vault = open_vault(db)
    meta = SourceMeta(
        doi="10.1234/abc",
        scholarly=True,
        year=2021,
        authors=("A. Author",),
        venue="Journal",
        cited_by_count=42,
        is_retracted=False,
        oa_url="https://oa.example/x.pdf",
    )
    original, _ = vault.add_source_note(source(1))
    note, _ = vault.add_source_note(
        source(
            2,
            doi="10.1234/abc",
            pages=("page one", "page two"),
            links=("https://a.org/", "https://b.org/"),
            meta=meta,
            derivative_of=original.note_id,
            source_tier="institutional",
            via="tavily_extract",
            content_type="application/pdf",
        )
    )
    loaded = vault.get_note(note.note_id)
    assert loaded is not None
    assert loaded.pages == ("page one", "page two")
    assert loaded.links == ("https://a.org/", "https://b.org/")
    assert loaded.derivative_of == "n0001"
    assert (loaded.doi, loaded.source_tier, loaded.via) == (
        "10.1234/abc",
        "institutional",
        "tavily_extract",
    )
    assert loaded.meta["cited_by_count"] == 42
    assert loaded.meta["authors"] == ["A. Author"]
    assert loaded.meta["is_retracted"] is False
    assert (loaded.summary, loaded.extract_failed, loaded.claims_kept) == ("", False, 0)
    assert loaded.run_id == "run-a"


def test_lookup_by_canonical_url_and_normalised_doi(db: Path) -> None:
    vault = open_vault(db)
    vault.add_source_note(source(1, doi="10.1000/xyz"))
    found = vault.find_by_canonical("https://example.org/p1")
    assert found is not None
    assert found.note_id == "n0001"
    assert vault.find_by_canonical("https://example.org/none") is None
    for variant in ("10.1000/XYZ", "https://doi.org/10.1000/xyz", " doi:10.1000/xyz "):
        by_doi = vault.find_by_doi(variant)
        assert by_doi is not None
        assert by_doi.note_id == "n0001"
    assert vault.find_by_doi("10.9/other") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10.1000/XYZ", "10.1000/xyz"),
        ("https://doi.org/10.1000/xyz", "10.1000/xyz"),
        ("http://dx.doi.org/10.1000/xyz", "10.1000/xyz"),
        ("doi:10.1000/xyz", "10.1000/xyz"),
        ("  10.1000/xyz  ", "10.1000/xyz"),
        ("", None),
        (None, None),
        ("not a doi", None),
    ],
)
def test_normalize_doi(raw: str | None, expected: str | None) -> None:
    assert normalize_doi(raw) == expected


def test_unknown_note_is_none(db: Path) -> None:
    assert open_vault(db).get_note("n9999") is None


# ---- stages ---------------------------------------------------------------------------------


def test_stage_transitions_and_pending_notes(db: Path) -> None:
    vault = open_vault(db)
    a, _ = vault.add_source_note(source(1))
    b, _ = vault.add_source_note(source(2))
    assert [n.note_id for n in vault.pending_notes()] == ["n0001", "n0002"]
    assert (
        vault.save_extraction(a.note_id, "A summary.", [claim()], dropped=1, failed=False) is True
    )
    stored = vault.get_note(a.note_id)
    assert stored is not None
    assert (stored.stage, stored.summary, stored.claims_kept, stored.claims_dropped) == (
        "extracted",
        "A summary.",
        1,
        1,
    )
    assert [n.note_id for n in vault.pending_notes()] == [
        "n0001",
        "n0002",
    ]  # extracted is not complete yet
    vault.mark_complete(a.note_id)
    assert [n.note_id for n in vault.pending_notes()] == ["n0002"]
    vault.mark_complete(b.note_id)  # still 'fetched': completing early is refused
    assert [n.note_id for n in vault.pending_notes()] == ["n0002"]


def test_save_extraction_is_idempotent(db: Path) -> None:
    vault = open_vault(db)
    note, _ = vault.add_source_note(source(1))
    assert vault.save_extraction(note.note_id, "first", [claim("c1")], dropped=0, failed=False)
    assert not vault.save_extraction(note.note_id, "second", [claim("c2")], dropped=0, failed=False)
    assert [c.claim for c in vault.claims()] == ["c1"]
    stored = vault.get_note(note.note_id)
    assert stored is not None
    assert stored.summary == "first"


def test_save_extraction_is_all_or_nothing(db: Path) -> None:
    vault = open_vault(db)
    note, _ = vault.add_source_note(source(1))
    broken: Any = claim("never stored", stance=None)  # NOT NULL violation on the second claim
    with pytest.raises(sqlite3.IntegrityError):
        vault.save_extraction(
            note.note_id, "summary", [claim("first ok"), broken], dropped=0, failed=False
        )
    stored = vault.get_note(note.note_id)
    assert stored is not None
    assert (stored.stage, stored.summary, stored.claims_kept) == ("fetched", "", 0)
    assert vault.claims() == []
    assert vault.save_extraction(note.note_id, "retry", [claim("ok")], dropped=0, failed=False)


def test_save_extraction_for_an_unknown_note_raises(db: Path) -> None:
    with pytest.raises(KeyError):
        open_vault(db).save_extraction("n9999", "x", [], dropped=0, failed=False)


def test_a_failed_extraction_is_flagged_and_still_advances(db: Path) -> None:
    vault = open_vault(db)
    note, _ = vault.add_source_note(source(1))
    vault.save_extraction(note.note_id, "lead sentence", [], dropped=0, failed=True)
    stored = vault.get_note(note.note_id)
    assert stored is not None
    assert (stored.stage, stored.extract_failed) == ("extracted", True)
    assert vault.stats().extract_failed == 1


def test_claims_round_trip_with_sequential_ids(db: Path) -> None:
    vault = open_vault(db)
    a, _ = vault.add_source_note(source(1))
    b, _ = vault.add_source_note(source(2))
    vault.save_extraction(
        a.note_id,
        "s",
        [claim("one"), claim("two", numbers=(), time_period=None)],
        dropped=0,
        failed=False,
    )
    vault.save_extraction(b.note_id, "s", [claim("three")], dropped=0, failed=False)
    claims = vault.claims()
    assert [(c.claim_id, c.note_id, c.claim) for c in claims] == [
        ("c00001", "n0001", "one"),
        ("c00002", "n0001", "two"),
        ("c00003", "n0002", "three"),
    ]
    assert claims[0].numbers == ("12 years",)
    assert claims[0].entities == ("IAEA",)
    assert (claims[1].numbers, claims[1].time_period) == ((), None)
    assert [c.claim for c in vault.claims(note_id="n0002")] == ["three"]


def test_an_analysis_note_completes_its_source(db: Path) -> None:
    vault = open_vault(db)
    note, _ = vault.add_source_note(source(1, source_tier="ground_truth"))
    vault.save_extraction(note.note_id, "s", [], dropped=0, failed=False)
    analysis = vault.add_analysis_note(note.note_id, "Analysis: Page 1", "## Thesis\nText.")
    assert (analysis.kind, analysis.stage, analysis.analysis_of) == (
        "source_analysis",
        "complete",
        "n0001",
    )
    assert analysis.source_tier == "ground_truth"
    assert analysis.note_id == "n0002"
    source_note = vault.get_note(note.note_id)
    assert source_note is not None
    assert source_note.stage == "complete"
    assert vault.pending_notes() == []
    again = vault.add_analysis_note(note.note_id, "ignored", "ignored")
    assert again.note_id == "n0002"
    assert len(vault.notes(kind="source_analysis")) == 1


def test_the_analysis_of_a_source_can_be_looked_up(db: Path) -> None:
    vault = open_vault(db)
    note, _ = vault.add_source_note(source(1))
    assert vault.find_analysis(note.note_id) is None
    vault.save_extraction(note.note_id, "s", [], dropped=0, failed=False)
    created = vault.add_analysis_note(note.note_id, "Analysis", "body")
    found = vault.find_analysis(note.note_id)
    assert found is not None
    assert found.note_id == created.note_id
    assert vault.find_analysis("n9999") is None


def test_an_analysis_note_needs_an_extracted_source(db: Path) -> None:
    vault = open_vault(db)
    note, _ = vault.add_source_note(source(1))
    with pytest.raises(ValueError, match="extracted"):
        vault.add_analysis_note(note.note_id, "t", "b")


def test_notes_can_be_filtered_by_kind_and_stage(db: Path) -> None:
    vault = open_vault(db)
    a, _ = vault.add_source_note(source(1))
    vault.add_source_note(source(2))
    vault.save_extraction(a.note_id, "s", [], dropped=0, failed=False)
    assert [n.note_id for n in vault.notes(stage="extracted")] == ["n0001"]
    assert [n.note_id for n in vault.notes(kind="source")] == ["n0001", "n0002"]
    assert vault.notes(kind="source_analysis") == []


# ---- concurrency and run isolation ----------------------------------------------------------


def test_ids_stay_unique_across_threads_and_connections(db: Path) -> None:
    vaults = [open_vault(db), open_vault(db)]  # two connections, same run
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            for i in range(10):
                vaults[index % 2].add_source_note(source(index * 100 + i))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    ids = [n.note_id for n in vaults[0].notes()]
    assert len(ids) == 60
    assert len(set(ids)) == 60
    assert sorted(ids) == [f"n{i:04d}" for i in range(1, 61)]


def test_runs_are_isolated_but_may_share_urls(db: Path) -> None:
    a, b = open_vault(db, "run-a"), open_vault(db, "run-b")
    a.add_source_note(source(1, body="reactor decommissioning in run A"))
    note_b, created = b.add_source_note(source(1, body="reactor decommissioning in run B"))
    assert created is True
    assert note_b.note_id == "n0001"  # ids are per run
    assert [n.run_id for n in a.notes()] == ["run-a"]
    assert [n.run_id for n in b.notes()] == ["run-b"]
    assert [r.note_id for r in a.search("decommissioning")] == ["n0001"]
    assert [r.note_id for r in b.search("decommissioning")] == ["n0001"]
    a.add_source_note(source(2, body="exclusive rhinoceros content"))
    assert a.search("rhinoceros") != []
    assert (
        b.search("rhinoceros") == []
    )  # AC3: a query for run A never returns run B's notes, and vice versa


def test_reopening_a_run_keeps_its_data(db: Path) -> None:
    open_vault(db).add_source_note(source(1))
    again = open_vault(db)
    assert [n.note_id for n in again.notes()] == ["n0001"]
    assert again.add_source_note(source(2))[0].note_id == "n0002"


# ---- full-text search -----------------------------------------------------------------------


def test_search_folds_diacritics_and_case(db: Path) -> None:
    vault = open_vault(db)
    vault.add_source_note(
        source(1, title="Rückbau von Anlagen", body="Der Rückbau kerntechnischer Anlagen.")
    )
    for query in ("ruckbau", "Rückbau", "RÜCKBAU", "kerntechnischer"):
        assert [r.note_id for r in vault.search(query)] == ["n0001"], query


def test_search_ranks_title_over_body(db: Path) -> None:
    vault = open_vault(db)
    vault.add_source_note(
        source(1, title="Overview", body="reactor " * 5 + "plain filler text here")
    )
    vault.add_source_note(
        source(2, title="Reactor safety", body="filler words and more filler words")
    )
    assert [r.note_id for r in vault.search("reactor")] == ["n0002", "n0001"]


def test_search_covers_summaries_and_reports_flags(db: Path) -> None:
    vault = open_vault(db)
    original, _ = vault.add_source_note(source(1, title="Original"))
    vault.add_source_note(source(2, title="Copy", derivative_of=original.note_id))
    vault.save_extraction(
        original.note_id, "A summary mentioning zirconium cladding.", [], dropped=0, failed=False
    )
    (hit,) = vault.search("zirconium")
    assert (hit.note_id, hit.kind, hit.derivative_of, hit.title) == (
        "n0001",
        "source",
        None,
        "Original",
    )
    assert hit.score > 0
    copy = vault.search("copy")[0]
    assert copy.derivative_of == "n0001"


def test_search_degenerate_queries(db: Path) -> None:
    vault = open_vault(db)
    vault.add_source_note(source(1))
    assert vault.search("") == []
    assert vault.search("!!! ??? --- ***") == []
    assert vault.search('"unbalanced quote AND OR NOT (') == []
    assert vault.search("decommissioning", limit=1)[0].note_id == "n0001"


def test_search_limit(db: Path) -> None:
    vault = open_vault(db)
    for i in range(1, 8):
        vault.add_source_note(source(i, body="common term everywhere"))
    assert len(vault.search("common", limit=3)) == 3


# ---- rejections -----------------------------------------------------------------------------


def test_rejection_bookkeeping(db: Path) -> None:
    vault = open_vault(db)
    first = vault.reject(
        "https://x.org/a?utm_source=1", "https://x.org/a", "network", "refused", retryable=True
    )
    assert (first.attempts, first.retryable, first.reason) == (1, True, "network")
    second = vault.reject(
        "https://x.org/a", "https://x.org/a", "network", "refused again", retryable=True
    )
    assert (second.attempts, second.detail) == (2, "refused again")
    final = vault.reject("https://x.org/a", "https://x.org/a", "http_404", retryable=False)
    assert (final.attempts, final.retryable, final.reason) == (3, False, "http_404")
    loaded = vault.get_rejection("https://x.org/a")
    assert loaded is not None
    assert loaded.attempts == 3
    vault.reject("https://x.org/b", "https://x.org/b", "too_short")
    assert [r.canonical_url for r in vault.rejections()] == ["https://x.org/a", "https://x.org/b"]
    vault.clear_rejection("https://x.org/a")
    assert vault.get_rejection("https://x.org/a") is None
    assert vault.get_rejection("https://x.org/never") is None


# ---- near-duplicate signatures, stats, deletion ----------------------------------------------


def test_minhashes_lists_originals_only(db: Path) -> None:
    vault = open_vault(db)
    original, _ = vault.add_source_note(source(1, minhash=b"\x01" * 16))
    vault.add_source_note(source(2, minhash=b"\x02" * 16, derivative_of=original.note_id))
    vault.add_source_note(source(3, minhash=None))
    assert vault.minhashes() == [("n0001", b"\x01" * 16)]


def test_stats(db: Path) -> None:
    vault = open_vault(db)
    a, _ = vault.add_source_note(source(1))
    b, _ = vault.add_source_note(source(2, derivative_of="n0001"))
    vault.save_extraction(
        a.note_id, "s", [claim("1"), claim("2"), claim("3")], dropped=1, failed=False
    )
    vault.save_extraction(b.note_id, "s", [], dropped=0, failed=True)
    vault.add_analysis_note(a.note_id, "A", "body")
    vault.reject("https://x.org/a", "https://x.org/a", "login_wall")
    vault.reject("https://x.org/b", "https://x.org/b", "login_wall")
    vault.reject("https://x.org/c", "https://x.org/c", "http_404")
    stats = vault.stats()
    assert stats.notes_by_kind == {"source": 2, "source_analysis": 1}
    assert stats.derivatives == 1
    assert stats.rejected_by_reason == {"http_404": 1, "login_wall": 2}
    assert (stats.claims_kept, stats.claims_dropped) == (3, 1)
    assert stats.claims_drop_rate == pytest.approx(0.25)
    assert (stats.extract_failed, stats.source_analyses) == (1, 1)


def test_stats_of_an_empty_run(db: Path) -> None:
    stats = open_vault(db).stats()
    assert stats.claims_drop_rate == 0.0
    assert stats.notes_by_kind == {}


def test_deleting_a_run_removes_everything_of_that_run_only(db: Path) -> None:
    a, b = open_vault(db, "run-a"), open_vault(db, "run-b")
    for v in (a, b):
        note, _ = v.add_source_note(source(1, body="shared words about reactors"))
        v.save_extraction(note.note_id, "s", [claim()], dropped=0, failed=False)
        v.reject("https://x.org/r", "https://x.org/r", "too_short")
    a.delete_run()
    assert a.notes() == []
    assert a.claims() == []
    assert a.rejections() == []
    assert a.search("reactors") == []
    assert [n.run_id for n in b.notes()] == ["run-b"]
    assert len(b.claims()) == 1
    assert [r.note_id for r in b.search("reactors")] == ["n0001"]
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM notes_fts WHERE run_id='run-a'").fetchone()[0] == 0


def test_opening_a_fresh_database_from_several_threads_at_once(db: Path) -> None:
    errors: list[BaseException] = []

    def opener() -> None:
        try:
            open_vault(db).add_source_note(source(1))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=opener) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert [n.note_id for n in open_vault(db).notes()] == ["n0001"]
