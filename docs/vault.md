# Source vault and fetch pipeline

Requirements: [PRD.md](../PRD.md) M3 and AD10. Plan: [plans/m3-source-vault.md](plans/m3-source-vault.md).
Each fetched source becomes a note in a run-scoped SQLite store with its verbatim text, a summary,
verified claims, a near-duplicate mark and a source tier. Later milestones read only from here.

## Flow

```
URL ─ reuse? ─ fetch ─ junk gates ─ store (stage fetched) ─ extract ─ (stage extracted) ─ analyse? ─ stage complete
        │         │         │
        │         └ FetchFailure ─────┴─> rejected_sources (reason code, attempts)
        └ same canonical URL or DOI: the stored note is returned, nothing is fetched
```

`FetchPipeline.ingest(url, meta=...)` runs one URL through this; `ingest_many` runs many with up
to 4 workers and returns results in input order. Build it with `bootstrap.build_pipeline(rt,
run_id, tier=..., focus=...)`, then call `resume()` before new work.

## Resuming (PRD AD10)

A note's `stage` moves `fetched` → `extracted` → `complete`, and every move is one transaction.
`resume()` regenerates missing note files, then finishes every note that is not `complete`.

| Process stops… | After a restart |
|---|---|
| before the source is stored | `ingest` fetches it again (nothing was stored) |
| after storing, before extraction ends | the note is `fetched`: extraction runs; no claims exist yet |
| after extraction, during the analysis | the note is `extracted`: only the analysis runs |
| anywhere, with Tavily credits spent | `build_gateway` reads `outbound.jsonl`: the credit count survives |

`save_extraction` stores claims and the stage move together, and `add_analysis_note` stores the
analysis note and completes the source together. Splitting either would let a resume extract
twice and duplicate claims. Tests: crash in-process (`SimulatedCrash`) and a real `SIGKILL` of
`tests/crash_child.py`.

Limits: the outbound log line is written after the charge, so a crash between Tavily's answer and
the line undercounts by one request per in-flight worker. The Tavily-to-ddgs switch is not
persisted; a resumed run learns it again with at most one extra request.

## Pipeline rules

- **Reuse:** the dedup key is the canonical URL (lowercase host, no fragment, tracking parameters
  removed, sorted query, no trailing slash, no leading `www.`). A note with that key or the same
  DOI is returned as is. A final rejection is returned as is.
- **Junk gates** (`junk.py`), checked on the extractor's text: under 300 characters (`too_short`);
  under 1000 with a login marker (`login_wall`); under 1500 with a cookie marker (`cookie_wall`);
  over 5 % garbage characters in the first 2000 (`binary_garbage`). All are final.
- **Retries:** `timeout`, `network`, `http_429` and `http_5xx` get two attempts per URL; every
  other failure is final.
- **Body:** the extractor's text with whitespace normalised (single spaces, one blank line between
  paragraphs). The `extract` model never rewrites it.
- **Near-duplicates:** MinHash of word 3-grams (128 permutations, seed 1, pinned `affine32`
  scheme, tagged in every stored signature). A source at Jaccard ≥ 0.6 to an earlier original is
  stored with `derivative_of`. Signatures are compared all-pairs, not through LSH (no false
  negatives near the threshold). Derivatives get no analysis and are not indexed themselves.
- **Tier:** `tier_for` takes the longest matching host rule in `config/source_strategies.toml`,
  else `institutional` for a DOI or scholarly origin, else `unknown`.
- **Extraction:** chunks of at most 12 000 characters (the prompt asks for at most 8 claims per
  chunk); the code caps a note at 8 / 15 / 25 claims (short under 1500 words, medium under 5000, long). A claim survives only if
  its `quoted_support` occurs verbatim in the body (`app.text.contains_quote`, word-boundary rule,
  quote ≤ 500 characters). Everything else counts as dropped. If every chunk fails the note is kept
  with `extract_failed` and a lead summary.
- **Analysis:** a source of at least `long_source_words`, not a derivative and not failed, gets a
  map-reduce analysis note (`summarize` role) until the profile's `source_analysis_cap` is reached.
  Slots are reserved under a lock, so the cap holds with parallel workers.
- **Drop rate:** at 20 or more claims seen and a drop rate above 0.30 the pipeline emits
  `extract_quality_low` once (PRD risk R2: consider a different `extract` model).
- **Untrusted text:** fetched text enters prompts only inside `fence_untrusted` (closing-tag
  variants are neutralised); the system prompts carry `UNTRUSTED_NOTE`.
- **Scoring** (`scoring.py`, on demand): quality is the weighted mean of tier weight (.35), utility
  (.20, set in M8) and authority percentile (.25, midrank); only present components count; a
  retracted source is capped at 0.05.

## Store

`Vault(path, run_id, label=...)` is one run's view of `data/udr.sqlite`; every query filters by
`run_id`, so runs never see each other. `delete_run()` removes one run's rows.

- Connection: WAL, `synchronous=FULL`, `foreign_keys=ON`, `busy_timeout=5000`, one lock per vault
  and `BEGIN IMMEDIATE` for writes.
- Migrations: `MIGRATIONS` in `store/db.py`, applied in order, versioned by `PRAGMA user_version`.
  Tables: `runs`, `notes`, `claims`, `rejected_sources`, and the FTS5 table `notes_fts`.
- Ids: `n0001…` for notes and `c00001…` for claims, allocated inside the write transaction.
- Search: `Vault.search(text)` is FTS5 with bm25 weights title 10, summary 5, body 1; the index
  shares the note's rowid and is kept in sync by triggers. Accents are folded (`ruckbau` finds
  `Rückbau`).
- Kinds: `source` and `source_analysis` (linked by `analysis_of`).

## Files per run

`data/runs/<run_id>/`: `notes/<note_id>.md` (front matter, summary, claims, source text; written
without scrubbing so the text stays verbatim; regenerated from the database), `run.json` (`stats`
key merged in: notes by kind, derivatives, rejections by reason, claims kept and dropped, drop
rate, failed extractions, analyses) and `outbound.jsonl`.

## Configuration

- `config/profiles.toml`: `credit_cap`, `source_analysis_cap`, `long_source_words` per tier.
- `config/source_strategies.toml`: tier weights, ordered host rules, and the four domain sections
  of PRD §3.3.

## Events

`source_stored`, `source_rejected`, `extract_chunk_failed`, `summary_merge_failed`,
`extract_quality_low`, `source_analysis_chunk_failed`, `source_analysis_failed`.

## Tests

`tests/fixtures_corpus.py` has the deterministic 20-URL corpus, a fake fetcher and a fake model
that answers by schema. `tests/test_fetch_pipeline.py` covers AC1, AC2 and AC6;
`tests/test_store.py` covers isolation (AC3) and atomicity with failing triggers.
