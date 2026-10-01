# M3 implementation plan — Per-run source vault and fetch pipeline

Source of truth: [PRD.md](../../PRD.md) §4 M3. If this plan and the PRD disagree, the PRD wins.
Status: [IMPLEMENTATION.md](../../IMPLEMENTATION.md).

**Executor:** Sonnet 5.5, effort high, in `/home/he/ai/dev/langgraph/local_ultradeepsearch`.
- Follow [AGENTS.md](../../AGENTS.md) for every step: red → green → gate → commit.
- No push unless asked.
- End commit messages with the attribution line from the session's system reminder.

## Context

M1 (LLM layer, own Ollama instance, doctor) and M2 (outbound gateway) are done and pushed; CI is
green. M3 builds the *corpus layer*. Each fetched source becomes a note in a run-scoped SQLite
vault, with:
- its verbatim text;
- a summary;
- claims whose quotes are checked word for word;
- a near-duplicate mark;
- a source tier.

Later milestones (M5 width sweep, M8/M9 analysis and drafting) read only from this vault.
Requirement source: `PRD.md` §4 M3, §3.1, §3.3, §3.5, §3.7.

**Owner decisions (2026-10-01), binding for this plan:**
1. **Note body:** the extractor's text, verbatim (trafilatura, pypdfium2 or Tavily Extract). The
   `extract` model writes only the summary and claims; it never rewrites source text.
2. **Storage:** SQLite is the source of truth. Each source is also written to
   `data/runs/<run_id>/notes/<note_id>.md` so it can be inspected; these files can be regenerated.
3. **Schema:** only the tables M3 uses, with numbered migrations. Sessions, API keys and templates
   come with M4–M6; events stay in `events.jsonl`.
4. **Near-duplicates:** `datasketch` MinHash.
5. **Resumability is mandatory.** When a research run stops for any reason (crash, `kill -9`,
   reboot, cancel), it must continue from that point: no stored source is fetched again, no
   finished LLM work is redone, no credit is spent twice. This becomes PRD AD10 (step 0). M3
   proves it with a real SIGKILL test.

## Step 0 — Contract updates (one docs commit)

1. Save this plan as `docs/plans/m3-source-vault.md`.
2. **PRD edits**, owner-approved through this plan. `PRD.md` must stay ≤ 800 lines; it is 792 now.
   Check with `uv run pytest tests/test_docs.py -q`.
   - **§3.1 role table, extract row:** replace "Cleaning fetched raw content, note summaries,
     claims, lead extraction" with "Note summaries, claims, lead extraction (never rewrites
     source text)".
   - **§3.5, after AD9,** add:
     ```
     - **AD10 — Resumable everywhere.** A run that stops for any reason (crash, kill, reboot,
       cancel) continues from its last checkpoint without repeating finished work or spending
       credits twice: step boundaries via the LangGraph checkpointer, work inside a step persisted
       item by item and idempotently. Every milestone proves it with a kill-and-resume test.
     ```
   - **M3 deliverable, store line:** replace with "**`app.store`:** SQLite tables for runs,
     notes, claims and rejected sources, with numbered migrations (later milestones add their
     own); FTS5 over notes (title 10, summary 5, body 1), filtered by `run_id`. Each source is
     also written to `data/runs/<id>/notes/<note_id>.md`."
   - **M3 step 5:** replace with "`extract` produces a length-scaled summary and claims in the
     original schema: (keep the field list). The note body is the extractor's verbatim text."
   - **M3 acceptance criteria,** add:
     ```
     6. Kill and resume: after a crash at any point of ingestion (including SIGKILL), a fresh
        process continues the run; no stored source is fetched again, no finished note is
        extracted again, and the run's credit count survives.
     ```
3. **AGENTS.md §5.2:** add one bullet: "Every pipeline step is resumable (PRD AD10): persist
   progress per item, idempotently, and add a kill-and-resume test."
4. **IMPLEMENTATION.md:** set the M3 row to `in progress` with a link to the plan.

Commit: `docs: M3 plan and resumability requirement (AD10)`.

## Layout (new code)

```
config/profiles.toml            light/full: source_analysis_cap, long_source_words, credit_cap
config/source_strategies.toml   tier weights, ordered host rules, 4 domain sections
src/app/text.py                 normalize_for_match(), quote_in_text()   (pure; G6 reuses it)
src/app/prompts/untrusted.py    UNTRUSTED_NOTE, fence_untrusted(url, text)
src/app/prompts/notes.py        EXTRACT_*, SUMMARY_MERGE_*, ANALYSIS_MAP_*, ANALYSIS_REDUCE_*
src/app/store/__init__.py
src/app/store/db.py             connect(), MIGRATIONS, migrate()
src/app/store/models.py         SourceMeta, Note, ClaimRecord, Rejection, SearchResult, RunStats
src/app/store/vault.py          Vault (runs, notes, claims, rejections, stats, pending)
src/app/store/search.py         fts_query() (pure), search()
src/app/pipeline/__init__.py
src/app/pipeline/profiles.py    Profile, load_profile()
src/app/pipeline/strategies.py  SourceStrategies, load_strategies(), tier_for()
src/app/pipeline/urls.py        canonicalize(), dedup_key()
src/app/pipeline/links.py       page_links(html, base_url) -> tuple[str, ...]
src/app/pipeline/junk.py        junk_reason()
src/app/pipeline/dedup.py       signature(), NearDupIndex
src/app/pipeline/schemas.py     ClaimDraft, ChunkExtraction, MergedSummary, PartialAnalysis, SourceAnalysis
src/app/pipeline/chunking.py    split_paragraph_chunks(), length_class()
src/app/pipeline/extraction.py  NoteExtractor
src/app/pipeline/analysis.py    SourceAnalyzer
src/app/pipeline/scoring.py     quality(), authority_percentiles(), quality_scores()
src/app/pipeline/artifacts.py   write_note_file(), export_note_files(), write_run_stats()
src/app/pipeline/fetch.py       FetchPipeline (ingest, ingest_many, resume)
src/app/llm/fakes.py            + CallbackTransport (handler(ChatRequest) -> ChatReply)
src/app/adapters/outbound/log.py     + OutboundLog.total_credits()
src/app/adapters/outbound/ledger.py  + RunLedger(cap, used=0)
src/app/bootstrap.py            run_dir(), open_vault(), build_pipeline(); build_gateway restores run credits
tests/fixtures_corpus.py        deterministic synthetic articles and the 20-page corpus
tests/crash_child.py            child script for the SIGKILL test
```

## Fixed numbers

Named constants in code. Profile values live in `config/profiles.toml` (AD3).

| What | Value | Source |
|---|---|---|
| Junk: too short | under 300 chars (all kinds, so a scanned PDF counts as junk) | PRD M3 |
| Junk: login wall | under 1000 chars plus a login marker | PRD M3 |
| Junk: cookie wall | under 1500 chars plus a cookie marker | PRD M3 |
| Junk: binary garbage | more than 5 % garbage characters in the first 2000 chars | PRD M3 |
| MinHash | 128 permutations, seed 1, word 3-gram shingles of `normalize_for_match(body)`, LSH threshold 0.6, confirmed with `jaccard ≥ 0.6` | PRD M3 |
| Length class | short < 1500 words; medium < 5000; long ≥ 5000 | original fetcher |
| Summary length | short: 1–2 sentences; medium: 1–2 paragraphs; long: 3–6 paragraphs | original fetcher |
| Claim cap per note | short 8, medium 15, long 25; at most 8 per chunk | original fetcher |
| Extract chunk | ≤ 12 000 chars, split at blank lines (prompt budget 6144 tokens) | `extract` num_ctx 8192 − 2048 |
| Analysis map chunk | ≤ 28 000 chars | `summarize` num_ctx 16384 − 4096 |
| Quote max length | 500 chars; longer quotes are dropped | original: ≤ 2 sentences |
| Long-source analysis | word_count ≥ `long_source_words` (5000), not a derivative, extraction not failed, run total < `source_analysis_cap` (6) | profile |
| Drop-rate warning | ≥ 20 claims seen and drop rate > 0.30 → `extract_quality_low` | PRD R2 |
| Page links stored | ≤ 300 absolute http(s) links per note | for M5 lead chasing without a refetch |
| Transient fetch retries | reasons `timeout`, `network`, `http_429`, `http_5xx` stay retryable for up to 2 attempts per URL; all others are final | resumability |

**Markers** (casefolded substring match):
- login: `sign in`, `log in`, `login`, `anmelden`, `einloggen`, `password`, `passwort`,
  `create account`, `konto erstellen`, `registrieren`, `subscribe to read`, `jetzt abonnieren`;
- cookie: `cookie`, `accept all`, `alle akzeptieren`, `einwilligung`, `consent`,
  `datenschutzeinstellungen`, `privacy settings`.

**Garbage characters:** U+FFFD, category `Cc` except `\n\r\t`, and category `Co`.

## Database (migration 1)

Connection settings in `store/db.py`:
- `sqlite3.connect(path, check_same_thread=False, isolation_level=None)`;
- `PRAGMA journal_mode=WAL`, `synchronous=FULL`, `foreign_keys=ON`, `busy_timeout=5000`;
- one `threading.RLock` in `Vault` around every operation, and `BEGIN IMMEDIATE` … `COMMIT` for
  every write.

Migrations:
- `MIGRATIONS: list[str]`. Migration *n* runs as one `executescript("BEGIN;" + ddl +
  f"PRAGMA user_version={n};COMMIT;")`.
- `migrate()` applies only those above the current `user_version`, so it is idempotent.

```sql
CREATE TABLE runs (run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, label TEXT NOT NULL DEFAULT '');
CREATE TABLE notes (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  note_id TEXT NOT NULL,                  -- n0001, n0002 … per run
  kind TEXT NOT NULL CHECK (kind IN ('source','source_analysis')),
  stage TEXT NOT NULL CHECK (stage IN ('fetched','extracted','complete')),
  url TEXT NOT NULL, final_url TEXT, canonical_url TEXT NOT NULL, doi TEXT,
  title TEXT NOT NULL, content_type TEXT, via TEXT,
  body TEXT NOT NULL, pages_json TEXT NOT NULL DEFAULT '[]', word_count INTEGER NOT NULL,
  summary TEXT NOT NULL DEFAULT '', meta_json TEXT NOT NULL DEFAULT '{}',
  source_tier TEXT NOT NULL, utility REAL,           -- utility: set in M8
  derivative_of TEXT, analysis_of TEXT, minhash BLOB, links_json TEXT NOT NULL DEFAULT '[]',
  extract_failed INTEGER NOT NULL DEFAULT 0,
  claims_kept INTEGER NOT NULL DEFAULT 0, claims_dropped INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, note_id), UNIQUE (run_id, kind, canonical_url));
CREATE INDEX notes_doi ON notes(run_id, doi);
CREATE TABLE claims (
  run_id TEXT NOT NULL, claim_id TEXT NOT NULL, note_id TEXT NOT NULL,   -- c00001 … per run
  claim TEXT NOT NULL, stance TEXT NOT NULL, stance_target TEXT NOT NULL,
  evidence_type TEXT NOT NULL, scope_conditions TEXT NOT NULL, quoted_support TEXT NOT NULL,
  numbers_json TEXT NOT NULL, entities_json TEXT NOT NULL, time_period TEXT, region TEXT,
  confidence TEXT NOT NULL,
  PRIMARY KEY (run_id, claim_id),
  FOREIGN KEY (run_id, note_id) REFERENCES notes(run_id, note_id) ON DELETE CASCADE);
CREATE TABLE rejected_sources (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  canonical_url TEXT NOT NULL, url TEXT NOT NULL, reason TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 1, retryable INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, canonical_url));
CREATE VIRTUAL TABLE notes_fts USING fts5(run_id UNINDEXED, note_id UNINDEXED, title, summary, body,
  tokenize='unicode61 remove_diacritics 2');
-- triggers keep notes_fts in sync: AFTER INSERT, AFTER UPDATE OF title,summary,body, AFTER DELETE
```

**Stages** (the resumability backbone):
- `fetched`: the body is stored.
- `extracted`: summary and claims are stored, or `extract_failed=1`.
- `complete`: nothing pending (any analysis note is stored).

`save_extraction` writes the claims and moves the stage in **one** transaction.
`add_analysis_note` inserts the analysis note and marks the source `complete` in one transaction.
IDs are allocated inside the write transaction (`MAX(seq)+1`).

**Search** (`store/search.py`):
- `fts_query(text)` turns the text into words (`[^\W_]+`, lowercased) and joins them as
  `"w1" OR "w2" …`; empty input gives no query.
- The search uses `bm25(notes_fts, 0, 0, 10.0, 5.0, 1.0)` with `WHERE notes_fts MATCH ? AND
  run_id = ?`, joined to `notes` for `kind` and `derivative_of`, ordered by score.
- It returns `SearchResult(note_id, kind, title, score, derivative_of)`.

## Pipeline behaviour

**`FetchPipeline.ingest(url, *, meta=None, step="2") -> IngestResult`**

`IngestResult` is `Ingested(note, reused: bool)` or `Rejected(rejection, reused: bool)`.

1. `canonical = canonicalize(url)`.
2. **Reuse:**
   - A note with the same canonical URL, or the same DOI (`meta.doi`), exists → `_continue(note)`.
     That finishes any pending stage and returns `Ingested(reused=True)`. No fetch.
   - A final rejection exists → `Rejected(reused=True)`.
   - A retryable rejection with `attempts ≥ 2` is treated as final.
3. **Fetch:** `gateway.fetch(url, step=step)`. A `FetchFailure` → `vault.reject(...)`, retryable
   if the reason is transient (attempts is incremented on retry).
4. **Junk:** `junk_reason(doc.text)` → reject with that reason (final).
5. **Store:** under the pipeline lock, compute the signature and the near-dup lookup
   (`NearDupIndex`, rebuilt from `vault.minhashes(run)` on construction), then
   `vault.add_source_note(stage='fetched', …)`:
   - `body` is the extractor text, with whitespace normalised to single spaces and `\n\n` between
     paragraphs;
   - `derivative_of` is the original's `note_id` (the lowest id among matches);
   - `source_tier = tier_for(final_url, meta, strategies)`;
   - `links = page_links(doc.html, final_url)` when there is HTML.

   Then write the note file.
6. `_continue(note)`.

**`_continue(note)`:**
- If the stage is `fetched`, run `NoteExtractor.extract(note, focus)`, then
  `vault.save_extraction(...)`.
- If the stage is `extracted`, then if `needs_analysis(note)` run
  `SourceAnalyzer.analyze(note, focus)`, then `vault.add_analysis_note(...)`; otherwise
  `vault.mark_complete`.
- Afterwards rewrite the note file(s), `write_run_stats(run_dir, vault.stats(run))`, and check the
  drop-rate warning.

**`ingest_many(items, max_workers=4)`** runs a `ThreadPoolExecutor` over `ingest` and returns
results in input order. The pipeline handles only `Exception` subclasses it expects (`LLMError`,
`SearchUnavailable`, …) and never catches `BaseException`, so a crash or interrupt propagates.

**`resume()`** calls `_continue` on every `vault.pending_notes(run)`. `build_pipeline` callers call
`resume()` before new work.

**`NoteExtractor.extract(note, focus) -> Extraction(summary, claims, dropped, failed)`:**
1. Chunk `note.body` at blank lines into ≤ 12 000 chars. Hard-split oversize paragraphs at
   sentence ends, or at the limit as a last resort.
2. Per chunk: `llm.structured(Role.EXTRACT, messages, ChunkExtraction, think=False)`.
   - System prompt: `EXTRACT_SYSTEM`, which includes `UNTRUSTED_NOTE`.
   - User prompt: the focus title and questions, the length class, the per-chunk claim limit, and
     `fence_untrusted(note.final_url or note.url, chunk)`.
   - An `LLMError` in a chunk means that chunk contributes nothing.
3. **All chunks failed:** `failed=True`, `summary = lead(note.body)` (first sentences ≤ 400
   chars), no claims.
4. **Verify:** keep a claim only if `quote_in_text(c.quoted_support, note.body)` and
   `len(quote) ≤ 500`. Every other claim counts as dropped.
5. **Merge:** dedupe by `normalize_for_match(claim)`, rank (confidence high > medium > low, then
   has numbers), cap by length class.
6. **Summary:**
   - one chunk: that chunk's summary;
   - several chunks: `llm.structured(Role.SUMMARIZE, …, MergedSummary)` over the chunk summaries,
     at the class length; if that fails, join the chunk summaries.

**Schemas** (`pipeline/schemas.py`, Pydantic; Ollama enforces them through `format`):
- `ClaimDraft`:
  - text fields: `claim`, `stance_target`, `scope_conditions`, `quoted_support`;
  - `stance: Literal["supports","refutes","neutral"]`;
  - `evidence_type: Literal["empirical","theoretical","anecdotal","expert-opinion","statistical","legal","historical"]`;
  - `numbers: list[str]`, `entities: list[str]`;
  - `time_period: str | None`, `region: str | None`;
  - `confidence: Literal["high","medium","low"]`.
- `ChunkExtraction(summary: str, claims: list[ClaimDraft])`
- `MergedSummary(summary: str)`
- `PartialAnalysis(key_points, numbers, quotes: list[str])`
- `SourceAnalysis`:
  - `thesis`, `methodology`, `key_findings: list[str]`, `load_bearing_citations: list[str]`,
    `caveats`, `relevance_to_query`, `quotes: list[str]` (0–10);
  - `relevance: Literal["load-bearing","useful","tangential","not-relevant"]`.

**`SourceAnalyzer.analyze(note, focus) -> SourceAnalysis`:**
1. Map over ≤ 28 000-char chunks with `Role.SUMMARIZE`.
2. Reduce hierarchically: batch partials so each reduce prompt fits the budget, and repeat until
   one batch remains. Then a final structured reduce. Never truncate.
3. Drop quotes not found verbatim.
4. Render the result as markdown with the original headings: Thesis, Methodology, Key findings,
   Load-bearing citations, Caveats, Relevance to the question, Quotes. That is the body of the
   `source_analysis` note.
5. An `LLMError` → no analysis note; mark the source `complete` and emit `source_analysis_failed`.

**Scoring** (`pipeline/scoring.py`, pure; computed on demand because percentiles move as the
corpus grows):
- `tier_for(url, meta, strategies)`: the first host rule that matches (host equals the suffix or
  ends with `.suffix`) wins. Otherwise a DOI or a scholarly provider gives `institutional`.
  Otherwise `unknown`.
- `quality(tier_weight, utility, authority, retracted)`:
  - weights `tier .35`, `utility .20` (value `utility/18`), `authority .25` (a percentile);
  - only the components present count: `Σw·v / Σw(present)`;
  - a retracted source scores `min(score, 0.05)`.
- `authority_percentiles(counts)`: midrank `(n_lower + 0.5·n_equal) / n`.
- `quality_scores(vault, run, strategies) -> dict[note_id, float]`.

**Files** (`pipeline/artifacts.py`):
- `notes/<note_id>.md` has YAML front matter: `note_id, kind, stage, url, final_url, title,
  source_tier, doi, derivative_of, analysis_of, claims_kept, claims_dropped, extract_failed`.
  Then the sections `## Summary`, `## Claims` (`- [c00012] claim — "quote" (stance, evidence,
  confidence)`) and `## Source text` (the body). It is written with
  `app.artifacts.write_text(…, scrub=False)` so the text stays verbatim.
- `export_note_files(vault, run, run_dir)` regenerates missing or stale files from the DB.
- `write_run_stats(run_dir, stats)` merges `{"stats": {...}}` into `run.json` (read, update,
  `write_json`), keeping other keys for M5. The stats:
  - `notes` by kind; `derivatives`; `rejected` by reason;
  - `claims_kept`, `claims_dropped`, `claims_drop_rate`; `extract_failed`; `source_analyses`.

**Resumability of the M2 parts** (fixes in this milestone):
- `OutboundLog.total_credits()` sums the `credits` of an existing `outbound.jsonl`.
- `RunLedger(cap, used=0)`.
- `build_gateway` passes `used=log.total_credits()`, so a resumed run keeps its spending.
- Provider-switch state is not persisted; a resumed run learns it again with at most one Tavily
  request. Document this.

## Work steps (each: tests first, see them fail, implement, gate, commit)

1. **Config files and loaders.**
   - `uv add datasketch`. Audit licences (numpy and scipy are BSD); stop and ask if anything is
     outside AGENTS.md §5.5 plus the accepted `tld`/`certifi`.
   - Add `profiles.toml`, `source_strategies.toml` with the four domains from PRD §3.3, and the
     `profiles.py` / `strategies.py` loaders (`tomllib`, Pydantic, `extra="forbid"`).
   - Tests: load both, an unknown profile raises, `tier_for` table.
   - Commit: `feat(pipeline): profiles and source strategies`.
2. **Pure text helpers.** `text.py`, `urls.py`, `links.py`, `junk.py`, `chunking.py`.
   - `normalize_for_match`: NFKC; quotes `“”„‟«»‹›` → `"`; `’‘‚` → `'`; `–—‑` → `-`; remove soft
     hyphens; collapse whitespace; casefold.
   - `canonicalize`: lowercase scheme and host, drop the default port, drop the fragment, drop the
     tracking parameters `utm_*`, `fbclid`, `gclid`, `mc_cid`, `mc_eid`, `_hsenc`, `_hsmi`; sort
     the remaining query; strip a trailing slash except at the root. `dedup_key` also strips a
     leading `www.`.
   - Table tests for each.
   - Commit: `feat(pipeline): canonical URLs, junk gates, text normalisation`.
3. **Store.** `db.py`, `models.py`, `vault.py`, `search.py`. Tests:
   - migrate twice is a no-op;
   - the pragmas are set;
   - stage transitions;
   - `save_extraction` is atomic (inject a failure inside the transaction and check that nothing
     is half-written);
   - id allocation under threads;
   - FTS with diacritics (`ruckbau` finds `Rückbau`);
   - **AC3:** run A never returns notes of run B;
   - rejection retry bookkeeping;
   - `pending_notes`.

   Commit: `feat(store): run-scoped vault with migrations and FTS`.
4. **Near-dup.** `dedup.py`: signature bytes via `MinHash.hashvalues.tobytes()`, rebuilt with
   `np.frombuffer(..., dtype=np.uint64)`; `NearDupIndex(minhash_rows)`. datasketch and numpy have
   partial stubs; type them through a small Protocol or `cast` as done for pypdfium2 in M2.
   - Tests: an edited copy at 5 % edits matches; distinct articles don't; the index rebuilt from
     stored bytes gives the same answer.
   - Commit: `feat(pipeline): MinHash near-duplicate index`.
5. **Fencing and extraction.**
   - `prompts/untrusted.py`: escape any literal `</untrusted-source>` inside the text.
   - `prompts/notes.py`, `schemas.py`, `extraction.py`, and `CallbackTransport` in
     `llm/fakes.py`.
   - Tests:
     - **AC5:** the fetched text appears only inside the fence, and an injected closing tag
       cannot escape it;
     - **AC2 part:** a claim with a fabricated quote is dropped and counted; a too-long quote is
       dropped;
     - caps per length class; dedupe;
     - multi-chunk summary merge, and its fallback;
     - all chunks failing → `failed`, lead summary.

   Commit: `feat(pipeline): verbatim-checked claim extraction`.
6. **Source analysis.** `analysis.py`. Tests: map/reduce call counts, hierarchical reduce on many
   partials, quote verification, cap and threshold via `needs_analysis`, failure event.
   Commit: `feat(pipeline): long-source analysis notes`.
7. **Scoring.** **AC4** table test: all components; utility missing; authority missing; tier
   only; retracted floor; midrank percentiles with ties. Commit: `feat(pipeline): quality scoring`.
8. **Files and stats.** Tests: note file content, `scrub=False` verbatim body, regeneration after
   deleting files, `run.json` merge keeps foreign keys. Commit: `feat(pipeline): note files and
   run stats`.
9. **FetchPipeline, composition, resume.**
   - `fetch.py`; `bootstrap.run_dir/open_vault/build_pipeline`; RunLedger restore.
   - Tests:
     - **AC1** (`tests/fixtures_corpus.py`, a fake `Fetcher` with 20 URLs):
       - 10 distinct articles;
       - 2 tracking-parameter variants of article 1 (same note);
       - 1 near-duplicate of article 2 (stored, `derivative_of`);
       - 1 DOI duplicate (same note, no fetch);
       - 1 login wall, 1 cookie wall, 1 too short, 1 garbage (rejected with reasons);
       - 1 PDF document (stored, with pages);
       - 1 `FetchFailure("http_404")`.

       Assert the exact survivor set and every rejection reason.
     - **AC2:** `run.json` `claims_drop_rate`.
     - Edge cases: `extract_failed` flag; `extract_quality_low` emitted once; a foreign-language
       source is kept.
     - Retryable `network` rejection: retried once, then final.
     - Concurrency: `ingest_many` with 4 workers gives no duplicate notes for duplicate URLs.
     - **AC6 in-process:** a `SimulatedCrash(BaseException)` raised by the fake LLM on the third
       extraction. A *fresh* `Vault`, pipeline, fetcher and transport on the same files then run
       `resume()` and `ingest_many(same urls)`. Fetch count = only URLs never stored; extraction
       calls = only notes not extracted; claims not duplicated; all notes complete.
     - **AC6 SIGKILL:** `tests/crash_child.py` (run with `sys.executable`) ingests a fake corpus
       with a fake LLM that sleeps and prints `STORED <n>` after each committed note. The parent
       `SIGKILL`s it after the first `STORED`, then resumes in-process and checks the same
       invariants (timeout ≤ 15 s).
     - Credits: `build_gateway` restores `used` from an existing `outbound.jsonl`.

   Commit: `feat(pipeline): resumable fetch pipeline`.
10. **Mutation checks, then docs.**
    - Mutate and confirm a test fails:
      - skip the quote check;
      - stage update outside the transaction;
      - no reuse by canonical URL;
      - LSH threshold 0.9;
      - percentile `n_lower/n`;
      - no `used=` restore.
    - Run `/documentation-update` against the pre-M3 commit. Add a new `docs/vault.md` (pipeline,
      schema, stages, resume contract); update `docs/architecture.md` (resumability design
      decision), `IMPLEMENTATION.md` (M3 row, module map, run/verify, open issues) and the README
      status.
    - Commit: `docs: source vault docs and M3 status`.

## Verification

- `uv run pre-commit run --all-files` is green after every step, with coverage ≥ 85 % and the
  suite ≤ 60 s (the SIGKILL test included).
- `uv run pytest tests/test_docs.py -q`: PRD ≤ 800 lines, links resolve.
- **Live (optional, ask before running; it uses the network and our own Ollama):**
  `tests/live/test_live_vault.py` ingests 2 neutral public URLs with the real `extract` model and
  records `claims_drop_rate` in IMPLEMENTATION.md. Above 0.30 is PRD risk R2: propose switching
  `UDR_MODEL_EXTRACT` to gemma.
- After a live run, stop our Ollama daemon by PID: check `/proc/<pid>/environ` shows
  `OLLAMA_HOST=127.0.0.1:11436`.
