# M5 implementation plan — Lite tier end to end

**Executor:** Sonnet 5.5, effort medium, in `/home/he/ai/dev/langgraph/local_ultradeepsearch`.
- Follow [AGENTS.md](../../AGENTS.md) for every step: red → green → gate → commit. No push unless
  asked. End commit messages with the attribution line from the session's system reminder.
- **Two parts.** Do Part A (steps A1–A8), then **stop**: report the red/green results of every step
  and wait for the owner's review. Start Part B only when the owner says so.
- **How to work at medium effort:**
  - Read only the files each step names under "Read first"; this plan already holds the design.
  - Copy the named patterns instead of inventing new ones (brief graph and runner, brief console,
    `tests/fixtures_corpus.py`, `tests/brief_rig.py`).
  - If the plan contradicts the code or leaves a choice open, stop and ask; do not guess.
  - Split a function before it reaches 50 lines or complexity 10; ruff and `test_code_rules.py`
    fail the gate otherwise.
  - Mutation-check every step before its commit (break the code on purpose, see a test fail,
    restore). The checks to run are listed per step.

## Context

M1–M4 are done. M5 turns an approved brief into a shipped Lite report: the `research` graph runs
steps `0 → 1 → 2.1 (approval interrupt) → 2 → 10 → 15 → 16 → G → X` (PRD §3.6). Requirement
source: `PRD.md` §3.1–3.10 and §4 M5; AD1–AD10 apply everywhere. Upstream originals (MIT,
hyperresearch by Jordan Gibbs) live in `../ultradeep-researcher/`:
- step skills `.claude/skills/hyperresearch-{1-decompose,2-width-sweep,10-triple-draft,15-polish,16-readability-audit}/SKILL.md`;
- agent prompts `.claude/agents/hyperresearch-{polish-auditor,readability-recommender}.md`;
- shim texts `.venv/lib/python3.13/site-packages/hyperresearch/core/levers.py`;
- the gate `.venv/lib/python3.13/site-packages/hyperresearch/core/runs.py` (`verify_run`).

**Owner decisions (2026-10-02), binding:**
1. **CLI.** `udr run <run_id>` starts or resumes a run approved in Phase 1. `udr run --brief <file>
   --tier light --template <id> [--format F] [--language L] [--yes]` creates a run from an
   external brief (code adds `Method: extern geliefert` and renders the Output section), as
   `POST /runs` will in M6. `udr run --list` lists runs.
2. **Plan approval in the CLI:** interactive table; edit, delete, add (edits are re-sanitized),
   approve. `--no-input` stops at `awaiting_plan_approval`; a later `udr run <id>` continues.
3. **Export without pandoc:** DOCX via python-docx, PDF via markdown-it-py → HTML → weasyprint.
   `reference_docx` gives styles through a python-docx base document. Rendering never touches the
   network.
4. **PDF engine weasyprint.** Owner licence exception for its transitive deps `pyphen` (MPL-1.1
   option of its tri-licence) and `pillow` (MIT-CMU).
5. **Plan cut:** Part A (run skeleton, steps 0–2, `udr run` up to the sweep), stop for review,
   then Part B.
6. **Scholarly APIs in Lite** only when a domain assigned in step 1 has `scholarly_first = true`;
   then depth-lens queries go to OpenAlex and Crossref (plus arXiv for the STEM domains
   `tech_standards` and `science_medicine`). Otherwise all queries are web queries.
7. **Live reference run:** the executor builds it, asks the owner before starting it, runs it and
   records the numbers. M5 is `done` only after a passing live run.
8. **Readability:** all 8 upstream categories, applied in upstream order, each with code-checked
   invariants (§ Readability below).
9. **Section word budgets:** step 1 gives one weight per H2; code clamps to 0.5–2.0 and scales the
   budgets so they add up to the middle of the format's word range.
10. **Thinking:** on for step 1 calls and every step-10 section draft (and G12 redrafts); off for
    plan, wave-2 queries, polish, readability and fix hunks.
11. **Blocked runs:** DOCX and PDF start with a notice "Ship-Gate nicht bestanden: G3, G6 – siehe
    gate.json" (English for non-German reports); `report.md` stays untouched.
12. **No lead chasing in M5** (M8). Wikipedia pages are stored but never cited: they are excluded
    from evidence packs.

Adopted defaults (challenge if wrong): the brief's free-text register stays in the brief; the
step-1 lever `register` is classified separately and an explicit directive in the brief wins
(prompt rule). Searches run sequentially (the fetch pipeline already parallelises fetches). Model
writers cite by note id (`[n0012]`); code numbers citations, builds Sources and renumbers after
every change, so numbering is always by first appearance. G6 fixes un-quote in code; G7, G8 and G9
fixes are code-only. Run-level events go to `data/runs/<id>/events.jsonl`; LLM telemetry stays in
`data/events.jsonl`.

## Step 0 — Contract updates (one docs commit, before A1)

1. Keep this plan at `docs/plans/m5-lite.md`. Set the M5 row of `IMPLEMENTATION.md` to
   `in progress`; milestone text "M5: Lite end to end, plan gate, ship gate, export", followed by
   a link to this plan as in the M4 row.
2. **PRD edits** (owner-approved through this plan). `PRD.md` is at 800 lines, its limit: every edit
   below keeps the line count. Replace exactly these lines:
   - §3.3, `  1. Scholarly APIs: OpenAlex, Crossref, and arXiv for STEM.` →
     `  1. Scholarly APIs: OpenAlex, Crossref, arXiv for STEM (Lite: scholarly-first domains only).`
   - §3.4, `    streamlit, pydantic-settings, langdetect, weasyprint, psutil.` →
     `    streamlit, pydantic-settings, langdetect, weasyprint, markdown-it-py, psutil.`
   - §3.4, `  - pandoc (GPL) is used only as an external binary.` →
     `  - No pandoc: DOCX via python-docx, PDF via weasyprint (owner decision 2026-10-02).`
   - M5, `  - **Step 2:** light numbers, coverage check, wave 2 for thin items.` →
     `  - **Step 2:** light numbers, coverage check, wave 2 for thin items; Wikipedia is never cited.`
   - M5, the two Step-10 lines →
     `  - **Step 10:** single draft, section by section (word weights from step 1), from evidence`
     `    packs of the 8–15 most relevant notes.`
   - M5, the two Export lines →
     `  - **Export:** `report.docx` via python-docx (styles from an optional `reference_docx`);`
     `    `report.pdf` via markdown-it-py HTML and weasyprint with a default CSS, no network access.`
   - M5, the CLI line → `  - **CLI (resumable):** `udr run <run_id>`; `udr run --brief <file> --tier light --template <id>`.`
   - M5 AC5 second line → `     and MD/DOCX/PDF stay downloadable; DOCX and PDF open with a "nicht bestanden" notice.`
   - M5 AC7 second line → `     If a renderer fails, that export fails visibly and MD stays available.`
   - M10, `  - System packages `pandoc` and WeasyPrint's pango, installed with the owner's OK.` →
     `  - WeasyPrint's system library pango, installed with the owner's OK (present on this host).`
   - §5, the R14 row → `| R14 | weasyprint pulls in `pyphen` (MPL tri-licence) and `pillow` (MIT-CMU) | Owner exception 2026-10-02 (AGENTS.md §5.5) | Licence policy changes |`
   - Check: `wc -l PRD.md` = 800 and `uv run pytest tests/test_docs.py -q` green.
3. `AGENTS.md` §5.5: extend the accepted list with "`pyphen` (MPL-1.1 option) and `pillow`
   (MIT-CMU), both via weasyprint, on 2026-10-02". §1–4 stay untouched.
4. `THIRD_PARTY_NOTICES.md`: add a `## hyperresearch` entry — used in `src/app/prompts/shims.py`
   (verbatim shim texts from `hyperresearch/core/levers.py`) and `src/app/prompts/research.py`
   (prompts adapted from the step skills and agent prompts); MIT, "Copyright (c) 2026 Jordan Gibbs";
   the MIT terms are already reproduced in the file, reference them.
5. `IMPLEMENTATION.md` §4: one bullet listing the PRD changes above.

Commit: `docs: M5 plan and Lite decisions`.

## Layout (new code)

```
config/profiles.toml           + Lite/Full step-2 and draft numbers, [research], [readability], [gate]
src/app/pipeline/profiles.py   + fields on Profile; ResearchConfig, ReadabilityConfig, GateConfig
src/app/store/db.py            + migration 3
src/app/store/runs.py          + RunRow fields, create_external_run(), set_status(), list_runs()
src/app/store/research.py      ResearchStore: plan_queries rows (wave 1 and 2)
src/app/research/__init__.py
src/app/research/models.py     LIGHT_STEPS, RunStatus, RunSpec, Item, QueryRow, PlanView, RunView
src/app/research/errors.py     ResearchError, StalePlan, PlanBlocked (NotFound, WrongState,
                               InvalidInput are reused from app.brief.errors)
src/app/research/journal.py    Journal: atomic JSON key/value file per step
src/app/research/manifest.py   Manifest: run.json status and step transitions
src/app/research/workspace.py  step 0: query.md, scaffold.md (+ set_scaffold_section)
src/app/research/external.py   prepare_external_brief()
src/app/research/ports.py      ResearchGateway protocol (prepare_query, search_web,
                               search_scholarly, fetch, credits_used)
src/app/research/schemas.py    LLM I/O models (Pydantic)
src/app/research/shims.py      compose_shims(levers)
src/app/research/decompose.py  step 1
src/app/research/plan.py       step 2.1: draft, validate, sanitize, hash, edit operations
src/app/research/sweep.py      step 2: run queries, URL queue, ingest, coverage, wave 2
src/app/research/steps.py      LightSteps: one method per graph node; RunContext factory
src/app/research/service.py    ResearchService (CLI now, REST/MCP in M6)
src/app/research/console.py    interactive plan review (German)
--- Part B ---
src/app/research/report.py     report structure: sections, body, words, appendix, Sources lines
src/app/research/citations.py  note-id and [N] markers, renumber()
src/app/research/hunks.py      patch engine (AD5)
src/app/research/mutation.py   run_mutation(): the resume pattern of steps 15, 16 and fix rounds
src/app/research/evidence.py   must-read notes and per-section evidence packs
src/app/research/draft.py      step 10
src/app/research/polish.py     step 15
src/app/research/readability.py step 16
src/app/research/gate.py       G1–G12, pure
src/app/research/fixes.py      fix rounds
src/app/adapters/export.py     DOCX (python-docx) and PDF (weasyprint), no network
src/app/graphs/research.py     build_research_graph(), ResearchRunner — LangGraph only here
src/app/prompts/research.py    DECOMPOSE_*, COVERAGE_*, PLAN_*, WAVE2_*, DRAFT_*, POLISH_*,
                               READABILITY_*, REPAIR_CITATIONS_*, COMPRESS_*, EXPAND_*, REDRAFT_*
src/app/prompts/shims.py       shim texts ported verbatim from levers.py
src/app/brief/labels.py        + external ("extern geliefert" / "externally supplied"),
                               appendix_heading(), provenance labels
src/app/bootstrap.py           + build_research_service()
src/app/cli.py                 + `udr run`
tests/research_rig.py          FakeGateway, ResearchModels (scripted by prompt), build_research_rig()
tests/research_crash_child.py  child process for the SIGKILL tests
tests/gate_fixtures.py         a passing report + notes; one failing variant per check
```

## Fixed numbers (`config/profiles.toml`, AD3)

`Profile` has `extra="forbid"`: add each key to the model in the same step. Prompts get these
numbers through placeholders; prompt text holds no digits (copy the digit test of
`tests/test_brief_interview.py` to `tests/test_research_prompts.py`).

```toml
[light]                       # existing keys stay
planned_searches = [8, 20]    # wave-1 queries
adversarial_min = 5
results_per_query = 10        # Tavily max_results (PRD 3.3)
candidate_urls = [20, 40]     # hits considered before deduplication
deduped_urls = [15, 30]       # URLs fetched in wave 1
wave2_urls = 10               # extra URLs fetched in wave 2
wave2_queries_per_item = 3
fetch_waves = 2
sources_min = 10
sources_target = [15, 25]
thin_sources = 1              # an item with this many sources or fewer is thin
must_read_notes = [8, 15]
readability_cap = 50

[full]                        # seeded now from PRD 3.8; used from M8
planned_searches = [40, 100]
adversarial_min = 5
results_per_query = 10
candidate_urls = [80, 120]
deduped_urls = [60, 100]
wave2_urls = 40
wave2_queries_per_item = 3
fetch_waves = 3
sources_min = 45
sources_target = [55, 80]
thin_sources = 1
must_read_notes = [20, 50]
readability_cap = 50

[research]
coverage_iterations = 3
section_weight_bounds = [0.5, 2.0]
section_min_words = 80        # no section budget below this
passages_per_note = 2         # FTS-style passages per must-read note and section
passage_chars = 1200
max_citations_per_bracket = 3
hunk_max_old_chars = 1200

[readability]
merge_max_chars = 300         # adjacent paragraphs shorter than this may merge
paragraph_target_chars = [500, 1000]
break_min_chars = 1500
split_min_chars = 150
added_words_max = 5           # words make-list / make-table may add (labels, headers)
connector_words_max = 3       # connector words merge / split may add or drop

[gate]
fix_rounds = 3
length_tolerance = [0.8, 1.2]
citation_density_min = 9      # citations per 1000 body words
quote_min_words = 5
retraction_window_chars = 200
language_sample_chars = 1000
```

## Data model

**Migration 3** (`store/db.py`, appended to `MIGRATIONS`):

```sql
ALTER TABLE runs ADD COLUMN report_language TEXT;
ALTER TABLE runs ADD COLUMN response_format TEXT;
ALTER TABLE runs ADD COLUMN template_id TEXT;
ALTER TABLE runs ADD COLUMN approved_at TEXT;
ALTER TABLE runs ADD COLUMN origin TEXT NOT NULL DEFAULT 'session';   -- 'session' | 'external'
ALTER TABLE runs ADD COLUMN status_reason TEXT NOT NULL DEFAULT '';
UPDATE runs SET
  report_language = (SELECT report_language FROM sessions s WHERE s.session_id = runs.session_id),
  response_format = (SELECT response_format FROM sessions s WHERE s.session_id = runs.session_id),
  template_id = (SELECT template_id FROM sessions s WHERE s.session_id = runs.session_id),
  approved_at = created_at
WHERE session_id IS NOT NULL;
CREATE TABLE plan_queries (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  query_id TEXT NOT NULL,                 -- q001 … per run, allocated inside the write tx
  wave INTEGER NOT NULL,                  -- 1 = the approved plan, 2 = wave 2
  item_id TEXT NOT NULL,                  -- i01 … from step 1
  lens TEXT NOT NULL CHECK (lens IN ('breadth','depth','adversarial','period')),
  channel TEXT NOT NULL CHECK (channel IN ('web','scholarly')),
  original TEXT NOT NULL,
  sent TEXT NOT NULL DEFAULT '',
  removed_json TEXT NOT NULL DEFAULT '[]',
  state TEXT NOT NULL CHECK (state IN ('draft','planned','blocked','deleted','done','failed')),
  reason TEXT NOT NULL DEFAULT '',        -- why blocked or failed
  hits_json TEXT NOT NULL DEFAULT '[]',   -- written together with state 'done'
  PRIMARY KEY (run_id, query_id));
```

- `RunStore.approve` (M4) also copies `report_language`, `response_format`, `template_id` from the
  session and sets `approved_at = created_at` (one test: an approved run carries them).
- `RunStore.create_external_run(sha256, brief_path, tier, template_id, response_format,
  report_language, approved_at)` inserts `origin='external'`, `session_id` NULL, status `queued`.
- `RunStore.set_status(run_id, status, reason="")`; `list_runs()` newest first.
- Run status: `queued → running → awaiting_plan_approval → running → done | blocked | failed`.
  M6 adds `cancelled` and `awaiting_brief_approval`. `failed` runs can be resumed.
- `ResearchStore` methods: `insert_drafts(run_id, wave, drafts) -> list[QueryRow]` (one tx),
  `rows(run_id, *, wave=None)`, `set_sanitized(run_id, qid, sent, removed, state, reason)`,
  `set_result(run_id, qid, state, hits, reason="")`, `edit(run_id, qid, original)` (back to
  `draft`), `delete(run_id, qid)`, `add(run_id, item_id, lens, channel, original)`.

**Files of a run** (`data/runs/<run_id>/`, AD9): `query.md` (the archived brief bytes, nothing
added), `scaffold.md` (private), `run.json`, `events.jsonl`, `outbound.jsonl` (gateway),
`prompt-decomposition.json`, `temp/coverage-matrix.md`, `shims/{research,drafting,critics,polish}.md`,
`search-plan.json`, `temp/coverage-gaps.md`, notes (M3), `temp/sections/NN.md`,
`temp/draft-log.json`, `report.md`, `polish-log.json`, `readability-recommendations.json`,
`readability-decisions.json`, `temp/gate-fix-N.json`, `gate.json`, `report.docx`, `report.pdf`.
No full-only artifact name may appear (AC1).

**`run.json`** (through `pipeline.artifacts.merge_run_json`, so the M3 `stats` key survives):
`run_id, tier, status, status_reason, brief_sha256, template_id, response_format,
report_language, steps: [{id, status: "running"|"done", started_at, finished_at}], exports`.
`Manifest.start_step(id)` appends an entry only if `id` is not yet listed (a re-run after a crash
updates it); `finish_step(id)` sets `done`. AC1 reads the ids in order.

## Shared mechanisms

**RunContext** (`steps.py`): built once per run id by a factory from bootstrap and cached in the
process: `spec`, `run_dir`, `vault`, `gateway`, `pipeline`, `events` (run sink), `llm` (the runtime
service; if `spec.summarize_model` is set, a service whose registry has
`dataclasses.replace(spec, model=…)` for `summarize`), `templates`, `profile`, configs, `journal(name)`.
The gateway's `confidential_context` is the brief text. `Focus` is `parse_brief(brief)` (AD6).

**Prompts** (all in `prompts/research.py`): each system constant starts with a unique first
line, so the test rig can tell the prompt kind from `messages[0]["content"]`. Fetched text enters
prompts only through `fence_untrusted` (AD7) and every such system prompt ends with
`UNTRUSTED_NOTE`. `reason` and `summarize` get the full brief verbatim (AD6). Prompts that write
report text name the report language. The step's shim file is appended verbatim at the end of the
system prompt: research shim → plan and wave-2; drafting shim → draft, expand, redraft; polish shim
→ polish and readability.

**Errors.** A fatal step error (an `LLMError` in step 1, plan drafting or a section draft) stops
the graph at its last checkpoint; the service sets `failed` with the reason and `udr run <id>`
resumes. Non-fatal (event, continue): coverage call, wave-2 drafting, one polish or readability
section, one fix. Never catch `BaseException`.

## Part A — run skeleton, steps 0–2, plan gate, `udr run` to the sweep

### A1 — Numbers, migration 3, stores
- Read first: `config/profiles.toml`, `src/app/pipeline/profiles.py`, `src/app/store/db.py`,
  `src/app/store/runs.py`, `tests/test_brief_store.py`.
- Do: the TOML keys and models above (`load_research_config`, `load_readability_config`,
  `load_gate_config`); migration 3; RunRow fields; RunStore and ResearchStore methods.
- Tests: profiles load and reject a missing key; migration 2 → 3 on an M4 database keeps rows and
  backfills an approved run; `create_external_run`; `set_status`; query ids are sequential and
  unique under two threads; `edit` returns a row to `draft`; deleting a run cascades its queries.
- Mutation: drop the backfill `UPDATE`; allocate query ids outside the transaction.
- Commit: `feat(store): run specs and search-plan queries`.

### A2 — Manifest, journal, workspace, external briefs
- Read first: `src/app/pipeline/artifacts.py`, `src/app/artifacts.py`, `src/app/brief/render.py`,
  `src/app/brief/archive.py`, `src/app/brief/labels.py`.
- `Journal(path)`: `get(key)`, `put(key, value)` (whole file rewritten with `write_json`, one lock),
  `data()`. A damaged file raises `ValueError` (never silently reset).
- `Manifest(run_dir)`: `init(spec)`, `start_step`, `finish_step`, `step_ids()`,
  `set_status(status, reason)`, `set_exports(dict)`.
- `bootstrap_workspace(run_dir, spec)`: `query.md` = brief bytes via `write_text(scrub=False)`;
  `scaffold.md` with `## Run config` (run id, tier, template, format, language). Idempotent.
  `set_scaffold_section(run_dir, heading, text)` replaces or appends one `## heading` section.
- `prepare_external_brief(text, *, template, fmt_name, fmt, language) -> str`: canonical text;
  `parse_brief` must succeed (else `InvalidInput`); insert `Method: <labels.external>` after the
  title line if no line starts with `Method:`; then `replace_output(text, ctx)` with a
  `BriefContext` built like `brief.flow.build_context` (rounds 0, nothing missing, no digest).
- Tests: journal round trip and damaged file; manifest ids in order, a repeated `start_step` does
  not duplicate; `query.md` bytes equal the archive (umlauts, a literal `<think>`); scaffold
  section replace; external brief gets Method and Output, keeps the rest byte for byte, and an
  unparseable brief is rejected.
- Mutation: write `query.md` with scrubbing; let `start_step` append twice.
- Commit: `feat(research): run manifest, workspace and external briefs`.

### A3 — Step 1: decomposition, coverage loop, shims
- Read first: upstream `hyperresearch-1-decompose/SKILL.md` and `levers.py`;
  `src/app/brief/interview.py` (how calls are made), `src/app/templates.py`,
  `src/app/pipeline/strategies.py`.
- Schemas: `Entity{name, type, required_fields}`, `TimePeriod{period, type, primary_source,
  issuer}`, `Levers{register: teach|survey|analyze|advocate, register_confidence: high|low,
  domain_notes, inference_depth: surface|standard|deep}`, `Decomposition{sub_questions, entities,
  required_formats, required_sections, time_horizons, time_periods, scope_conditions, domains
  (subset of the four strategy domains), section_headings, section_weights, tier_recommendation:
  light|full, tier_rationale, modality: collect|synthesize|compare|forecast, levers}`,
  `CoverageRow{phrase, item_ids, scope_ok, gap, note}`, `CoverageMatrix{rows}`.
- Calls (all `reason`, `think=True`): `DECOMPOSE` (brief, template headings or "derive", the
  binding tier, response format, the four domains with one-line descriptions); `COVERAGE` (brief +
  numbered items → matrix); `REVISE_DECOMPOSITION` (decomposition + gap rows → decomposition).
  Loop: decompose, then up to `coverage_iterations` matrix checks with a revision after each check
  that has gaps. Gaps left after the last check go to `scaffold.md` (`## Coverage notes`) and an
  event `coverage_gaps_left`. A coverage call error ends the loop with a warning event.
- Pure helpers: `atomic_items(dec) -> list[Item]` = sub-questions, then entities, then time
  periods, ids `i01…` in that order (the searchable items); `final_headings(template, dec)`: a
  fixed template's headings exactly (event `brief_sections_overridden` if `required_sections` is
  not empty); for `auto` the decomposition's headings, leading `#` stripped, validated with the
  template rules (2–15 unique, none reserved; reuse the checks in `templates.py`), one repair call,
  then the fallback: one heading per sub-question (max 15, at least 2);
  `section_weights(raw, n, bounds)`: wrong length → all 1.0 (event), clamp, then
  `section_budgets(weights, target, min_words)` with target = middle of the format's word range,
  integers that add up to the target.
- `compose_shims(levers)` with the texts of `levers.py` copied verbatim into `prompts/shims.py`;
  four files under `shims/`.
- Writes: `prompt-decomposition.json` (decomposition + `items`, `required_section_headings`,
  `section_weights`, `section_budgets`, `pipeline_tier` = the run's tier, `tier_recommendation`,
  `response_format`, `report_language`, `citation_style: "inline"`), `temp/coverage-matrix.md`
  (the upstream table), scaffold sections `Modality` and `Tier rationale`.
- Tests: think flag per call; the loop stops at no gaps, at the iteration cap (gaps listed), on a
  coverage error; fixed template headings win; auto headings validated, repaired, fallback; budget
  sums and clamps; shims byte-equal to the upstream composition for two lever sets (hard-code the
  expected text of one); prompts carry the brief verbatim; no digits in the new prompts.
- Mutation: let a fixed template take the model's headings; skip the clamp; drop the gap revision.
- Commit: `feat(research): step 1 decomposition, coverage loop and shims`.

### A4 — Step 2.1: search plan
- Read first: upstream `hyperresearch-2-width-sweep/SKILL.md` (2.1); `src/app/adapters/outbound/
  gateway.py` (`prepare_query`), `errors.py`, `denylist.py`.
- `ResearchGateway` protocol in `ports.py` (the real `OutboundGateway` satisfies it unchanged).
- `PLAN` call (`reason`, `think=False`): items with ids and kinds, lens definitions A–D (lens D
  only when time periods exist), the profile's query range and adversarial minimum → schema
  `SearchPlanDraft{queries: [{item_id, lens, query}]}`.
- `validate_plan(drafts, items, profile) -> PlanCheck(kept, problems)`: drop unknown item ids,
  empty and duplicate queries (`normalize_for_match`); problems = fewer adversarial queries than
  the minimum, a searchable item without a query, a period item without a lens-D query, fewer
  queries than the minimum. With problems: one repair call naming them, then proceed with event
  `plan_short` and a scaffold note. Over the maximum: keep round-robin over items, adversarial
  queries kept up to the minimum first.
- `channel_for(lens, scholarly) = "scholarly" if lens == "depth" and scholarly else "web"`;
  `scholarly` = any assigned domain has `scholarly_first`.
- Sanitizing: insert all drafts as `draft` rows in one tx, then per row `gateway.prepare_query(q,
  step="2.1")` → `planned` with sent and removed terms; `DenylistBlocked` → `blocked`
  (`denylist`); `OutboundBlocked` → `blocked` (its reason). Each row is stored before the next is
  sanitized, so a resume only sanitizes `draft` rows.
- `plan_sha256(rows)`: sha256 of `json.dumps([...], sort_keys=True, ensure_ascii=False,
  separators=(",", ":"))` over wave-1 rows not `deleted`, sorted by id, each `{query_id, item_id,
  lens, channel, sent, state}`. `write_plan_file(run_dir, items, rows)` writes `search-plan.json`
  (`plan_sha256`, items, rows incl. `original`, which stays local).
- Edit operations (used by the service): edit text → row back to `draft` → re-sanitize; add
  (item id must exist, lens valid) → sanitize; delete → `deleted`. Each rewrites the plan file.
- Tests: validation table; one repair, then `plan_short`; truncation keeps every item; channel by
  domain; a denylisted query is `blocked` and no fake provider saw it; sanitizer failure blocks;
  hash changes on edit/delete/add and ignores `original`; resume sanitizes only `draft` rows.
- Mutation: hash over `original` instead of `sent`; skip re-sanitizing an edit.
- Commit: `feat(research): step 2.1 search plan`.

### A5 — Step 2: width sweep
- Read first: upstream 2.2–2.5; `src/app/pipeline/fetch.py`, `urls.py`, `store/vault.py`
  (`search`, `notes`, `find_by_canonical`), `tests/fixtures_corpus.py`.
- `run_queries(ctx, wave)`: for each `planned` row of the wave, in id order: rebuild
  `PreparedQuery(original, sent, removed)`; web → `gateway.search_web(prepared, step="2",
  include_domains=<union of the assigned domains' include_domains> if lens in (depth, period) else
  (), max_results=profile.results_per_query)`; scholarly → `search_scholarly` for openalex and
  crossref, plus arxiv for the STEM domains. Store hits (`url, title, snippet, provider`, and the
  `SourceMeta` fields of scholarly records; url = `oa_url or url`) with state `done` in one
  update. `SearchUnavailable` → `failed` (`unavailable`); `OutboundBlocked` → `failed` (its
  reason). A `done` row is never searched again (AD10: no credit spent twice).
- `build_queue(rows, known_keys, *, candidates, cap) -> list[QueueEntry(url, meta, item_ids)]`,
  pure: hits in row order, first `candidates` kept, dedup by `dedup_key`, known keys (stored
  notes or final rejections) dropped; group by item, inside an item sort by host tier weight then
  hit rank; take round-robin over items up to `cap`. A URL found for several items keeps all ids.
- Ingest: `pipeline.resume()`, then `pipeline.ingest_many([(e.url, e.meta) …])`.
- `coverage(items, rows, vault) -> list[ItemCoverage(item_id, sources, status)]`: sources = distinct
  complete, non-derivative source notes whose canonical URL came from a hit of that item's queries;
  status `uncovered` (0), `thin` (≤ `thin_sources`), `adequate` (2–3), `well` (4+).
- Wave 2 (once, if `fetch_waves` ≥ 2 and any item is thin or uncovered): `WAVE2` call (`reason`,
  `think=False`) → `Wave2Draft{queries: [{item_id, query}]}`, capped per item; rows with lens
  `breadth`, channel `web`, wave 2; sanitized without a human (PRD §3.2); executed like wave 1;
  queued with cap `wave2_urls`. Existing wave-2 rows mean "already drafted": never redraft.
- `temp/coverage-gaps.md`: every item with status and count, failed queries, sources found vs
  `sources_min` (fewer: noted, the run proceeds, as upstream).
- Tests (fake gateway + `FakeFetcher`): hits stored once; resume skips `done` rows (count fake
  calls); scholarly only for scholarly-first domains, arXiv only for STEM; include-domain hints
  only for depth/period; queue dedup, caps, round-robin, known URLs skipped; coverage statuses;
  wave 2 only for thin items, sanitized, capped, not redrafted on resume; a failed query is listed.
- Mutation: store hits before setting `done` in a second statement; drop the round-robin.
- Commit: `feat(research): step 2 width sweep`.

### A6 — Graph to the sweep, service, bootstrap
- Read first: `src/app/graphs/brief.py` (runner, `durability="sync"`, interrupt rules),
  `src/app/brief/service.py` (locks, error types), `src/app/bootstrap.py`.
- `LightSteps(context_factory, …)` in `steps.py`: `bootstrap(run_id)` (step 0), `decompose`,
  `plan` (sets `awaiting_plan_approval`), `approve(run_id, sha)` (after the interrupt: verify,
  finish 2.1, set `running`), `sweep`. Each wraps its work in `manifest.start_step`/`finish_step`.
- `graphs/research.py`: `ResearchState{run_id, plan_sha256, gate_round, gate_passed}`; nodes call
  `LightSteps`; `approve_plan` node calls `interrupt({"kind": "plan_approval", "plan_sha256": …})`
  and does nothing before it; Part A edges `START → bootstrap → decompose → plan → approve_plan →
  sweep → finish → END` (`finish` sets `done`; Part B inserts steps 10–X before it).
  `ResearchRunner` mirrors `BriefRunner` (thread id = run id, same checkpointer file).
- `ResearchService` (one lock per run id):

  | Method | Does | Errors |
  |---|---|---|
  | `start(run_id)` | `queued` or `failed`: run the graph until the interrupt or the end | `NotFound`, `WrongState` (no `brief_sha256`, tier not `light`: "Full-Tier ab M8", archive bytes ≠ hash) |
  | `resume(run_id)` | continue from the checkpoint; a pending interrupt → return the view | `NotFound` |
  | `get(run_id)` → `RunView` | status, reason, steps, current step, plan view when waiting, credits, gate summary, paths | `NotFound` |
  | `plan(run_id)` → `PlanView` | items, rows, `plan_sha256`, `approvable` | `WrongState` unless waiting |
  | `edit_query` / `delete_query` / `add_query` | A4 operations | `WrongState`, `InvalidInput` |
  | `approve_plan(run_id, plan_sha256)` | hash = current (else `StalePlan`); no `blocked` row and ≥ 1 `planned` (else `PlanBlocked`); re-check every `sent` against a freshly loaded denylist, block hits and raise `PlanBlocked`; then resume | as listed |
  | `create_external_run(text, tier, template_id, response_format, report_language)` | `prepare_external_brief`, archive, `create_external_run` | `InvalidInput` |
  | `list_runs()` | rows | – |

  Fatal errors in the graph: catch `LLMError` and `ResearchError` around runner calls, set
  `failed` with the reason, return the view.
- `bootstrap.build_research_service(rt, *, now=_utcnow, gateway_factory=None, fetcher=None)`:
  wires stores, templates, profiles, strategies, the context factory (gateway via `build_gateway`
  with the run sink and the brief as confidential context), `Denylist.load` for the approval
  re-check, the checkpointer and the graph.
- Tests (`tests/research_rig.py`: `FakeGateway` with a denylist, call counts and hits that point
  at `fixtures_corpus` URLs; `ResearchModels` answering by prompt kind; `build_research_rig(tmp)`):
  `start` returns at `awaiting_plan_approval` with steps `0,1,2.1` running/done as specified;
  **AC2**: the process can stop there — a new service on the same files shows the same plan;
  approving with a stale hash, with a blocked row, after adding a denylist term → refused;
  approve runs to `done` with steps `0,1,2.1,2`; runs without hash, tier full, or a tampered
  archive are refused; an `LLMError` in step 1 → `failed`, `start` again resumes.
- Mutation: approve without the fresh denylist load; a model call before `interrupt`.
- Commit: `feat(graphs): research graph to the width sweep`.

### A7 — `udr run` (to the sweep)
- Read first: `src/app/cli.py`, `src/app/brief/console.py`, `tests/test_cli_brief.py`.
- `console.run_plan_review(service, io, run_id) -> "approved" | "quit"` (German, `ConsoleIO`):
  table `Nr | Item | Linse | Kanal | Gesendete Query | Entfernt | Status` (blocked rows marked
  `GESPERRT: <Grund>`), menu `[f] Freigeben  [b] Bearbeiten  [l] Löschen  [n] Neu  [q] Beenden`;
  errors are shown and the loop goes on.
- `udr run RUN_ID [--no-input]`; `udr run --brief FILE --tier light --template ID [--format F]
  [--language L] [--yes] [--no-input]` (shows the final brief and its sha256, asks `Freigeben?
  [j/N]` unless `--yes`); `udr run --list`. Defaults: format = the template's
  `default_response_format`, language = the template's language. `--tier full` → exit 2 with "Full-Tier
  ab M8". Exit codes: 0 done or waiting, 1 failed or error, 2 bad input or blocked.
- Tests with `CliRunner` and a fake service: each option path, plan review scripted (edit, delete,
  add, approve, quit), `--no-input` prints the resume hint, invalid combinations exit 2.
- Commit: `feat(cli): udr run up to the width sweep`.

### A8 — Kill and resume, mutation sweep, Part-A docs, stop
- In-process `SimulatedCrash` (from `fixtures_corpus`): (a) after the second sanitized query —
  resume sanitizes only the rest; (b) after the third executed search — resume does not repeat the
  first three (fake call counts) and credits match `outbound.jsonl`; (c) inside wave-2 drafting.
- SIGKILL: `tests/research_crash_child.py` hangs inside step 2 after k searches; the parent kills
  it, then `resume()` finishes; no search runs twice (≤ 15 s, one child start).
- Re-run all mutation checks of A1–A7 once more; fix gaps.
- `/documentation-update`: `IMPLEMENTATION.md` (run table: `udr run`; module map rows for
  `app.research`, `store/research.py`, `graphs/research.py`), no new component doc yet.
- Commit: `test(research): kill and resume up to the sweep` and `docs: Part A of M5`.
- **Stop.** Report red/green results per step and wait for the owner.

## Part B — draft, polish, readability, gate, export

### B1 — Dependencies
- `uv add markdown-it-py weasyprint`. Audit every new transitive licence (`uv tree`, metadata):
  only `pyphen` and `pillow` may fall outside AGENTS.md §5.5 (both accepted). Anything else: stop
  and ask.
- CI (`.github/workflows/ci.yml`): before `uv sync`, `sudo apt-get install -y libpango-1.0-0
  libpangoft2-1.0-0` (weasyprint needs pango).
- Commit: `build: markdown-it-py and weasyprint for exports`.

### B2 — Report structure and citations (pure)
- `report.py`: `split_report(text) -> ReportParts(title, sections: list[Section(heading, body)],
  sources, appendix)`; `body_text(text)` = from after the H1 line to before the Sources heading;
  `body_words(text)` = `len(x.split())` after removing heading lines, citation markers and table
  separator rows; `h2_list(text)`; `render_appendix(brief, approved_at, archive_path, lang)` and
  `extract_appendix(text) -> str | None` (exact round trip: the fence is three backticks + `text`,
  longer if the brief has a backtick run at a line start; content = the brief bytes, which end
  in `\n`; then a blank line and `Freigegeben: <UTC Z> · Archiv: <path>` / `Approved: … ·
  Archive: …`); `assemble(title, sections, sources_lines, appendix)`. Headings: `sources_heading`
  and a new `appendix_heading(lang)` in `labels.py` (`Anhang A — Recherche-Brief` / `Appendix A —
  Research Brief`; spell the em dash `chr(0x2014)` as `pipeline/artifacts.py` does).
- `citations.py`: `NOTE_MARKER` `\[(n\d{4}(?:\s*[,;]\s*n\d{4})*)\]`, `NUM_MARKER`
  `\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\]`; `source_line(n, note, lang)` →
  `[N] Author/Publisher. Title. Year. URL (abgerufen|accessed YYYY-MM-DD)`: authors (≤ 2 joined
  `; `, more → first + `u. a.`/`et al.`), else venue, else the host without `www.`; year or
  `o. J.`/`n.d.`; url = `note.url`; date = `note.created_at[:10]`.
  `renumber(text, notes, lang, max_per_bracket) -> Renumbered(text, dropped)`: map each `[N]` to a
  note id through the current Sources lines; unknown numbers and unknown note ids are dropped
  (reported); merge adjacent brackets, dedupe, keep the first `max_per_bracket`; number by first
  appearance in the body; rebuild Sources from the cited notes only. It is the only code that
  writes citation numbers.
- Tests: split/assemble round trip; body words exclude markers, headings, Sources, appendix;
  appendix round trip incl. a brief with a code fence and umlauts; Sources line variants;
  renumber: first-appearance order, `][` merged, cap 3, mixed `[3]` and `[n0007]`, unknown ids
  dropped, unused entries removed.
- Commit: `feat(research): report structure and citations`.

### B3 — Patch engine (AD5)
- `apply_hunks(text, hunks, rules) -> HunkOutcome(text, applied, rejected)`; a hunk is
  `{id, old, new}`; reject reasons: `empty_old`, `not_found`, `not_unique` (counted in the whole
  current text), `too_long` (> `hunk_max_old_chars`), `crosses_heading` (old or new contains a line
  starting with `#`), `outside_body` (old not entirely in the body), `grows` (rules
  `forbid_growth` and `len(new) > len(old)`), `citations_changed` (multiset of citation markers),
  `numbers_changed` (multiset of `\d+(?:[.,]\d+)*` outside markers). Hunks apply in order to the
  current text.
- Tests: one per reason; order; heading levels, list numbering and table column counts survive an
  applied hunk (fixtures).
- Commit: `feat(research): patch engine`.

### B4 — Step 10: evidence packs and section-wise draft
- Read first: upstream `hyperresearch-10-triple-draft/SKILL.md` (light path),
  `src/app/pipeline/scoring.py`, `src/app/prompts/untrusted.py`.
- `evidence.py`: pool = source notes `complete`, not derivative, not `extract_failed`, host not
  `wikipedia.org` or a subdomain. Per section, `vault.search(heading + section instruction +
  brief title, limit=20)` gives a reciprocal rank `1/(rank+1)` for pool notes. `score = quality
  + best reciprocal rank + 0.25 * min(items_served, 4)` (named constants). Must-read = the top
  `must_read_notes[1]`, at least `[0]` if the pool has them. A section pack lists the must-read
  notes, notes hit for that section first: note id, title, year, publisher, summary, claims (claim
  + quoted support, most section-relevant first), the analysis note body if any, and up to
  `passages_per_note` body paragraphs (≤ `passage_chars`, by word overlap with the section query),
  all fenced. Budget: `(num_ctx - min(num_predict, num_ctx // 2)) * 3` chars minus the fixed prompt;
  whatever does not fit is left out and listed in event `evidence_pack_trimmed` (never silently).
- `draft.py`: per section in order (`reason`, `think=True`, `llm.text`): system `DRAFT_SECTION`
  (report language, cite by note id right after the claim, at most three per bracket, every
  sentence with a number or a quote cites, quotation marks only around text copied from a pack,
  translation in parentheses without quotes, no headings except `###`, no pipeline words, state a
  gap instead of inventing, the drafting shim); user: the full brief, sub-questions, required
  formats, modality, the outline (all headings), the first two sentences of each earlier
  section, this heading, its instruction, its word budget, the pack. An empty pack uses
  `DRAFT_GAP_SECTION` (a short statement of the gap, no citations). Post-process in code: drop
  leading heading lines, demote `##` to `###`, drop unknown note ids (event). Each section is
  stored in `temp/sections/NN.md` before the next starts; existing files are not redrafted.
- Assembly: `# <brief title>` (from `parse_brief`), the sections under the required headings,
  then `renumber` builds Sources, then the appendix. `report.md` is written exactly once
  (`temp/draft-log.json` `assembled: true`; a resumed step 10 never rewrites it).
- Tests: pool exclusions (Wikipedia, derivatives, failed); must-read bounds; pack order and
  trimming event; think flag; budgets in the prompt; one call per section and none on resume for
  stored sections; gap section; assembled H2 list = headings + Sources + Appendix (**AC3**);
  report written once (write counter).
- Mutation: include Wikipedia; redraft stored sections; skip the trimming event.
- Commit: `feat(research): step 10 section-wise draft`.

### B5 — Mutation pattern and step 15 polish
- `mutation.run_mutation(run_dir, name, journal, tasks, apply) -> MutationResult`:
  1. journal `after_sha256` equals the sha of `report.md` → done, nothing runs;
  2. if `temp/<name>-before.md` is missing, copy `report.md` there and store `before_sha256`;
  3. for each `(key, compute)` in `tasks(before_text)`: reuse the journal entry or call `compute()`
     and `put` it at once;
  4. `apply(before_text, entries)` → write `report.md` atomically → store `after_sha256`.
  A crash anywhere replays from the before copy without new model calls for stored keys.
- Polish (`reason`, `think=False`), one task per section: system `POLISH` adapted from the polish
  auditor (hygiene leaks, frontmatter, scaffold words, filler, redundancy, hedges per the register
  in the polish shim; never touch citations, Sources or headings; never add content; escalate
  structural problems) → `HunkList{hunks: [{old, new, reason}], escalations: [str]}`. Apply with
  `forbid_growth`, `preserve citations`, `preserve numbers`. `polish-log.json` is the journal plus
  `applied`, `rejected` (with reasons), `escalations`, `net_char_delta`.
- Tests: **AC6** a growing hunk is rejected and logged; citation-changing hunk rejected; a failed
  section call is skipped with an event; resume after a crash in section 2 makes no new call for
  section 1; replay gives byte-equal output.
- Mutation: allow `len(new) == len(old) + 1`; skip step 1 of the pattern.
- Commit: `feat(research): step 15 polish`.

### B6 — Step 16 readability
- `READABILITY` (`summarize`, `think=False`), one task per section, adapted from the readability
  recommender (thresholds from `[readability]`, cap = `ceil(readability_cap / sections)` per
  section) → `ReadabilityRecs{recommendations: [{category, severity, current, recommended,
  rationale}]}`. Ids `rec-1…` in section order; all stored in `readability-recommendations.json`.
- Code decides per recommendation (order: remove-hr, merge-paragraphs, break-paragraph,
  make-list, make-table, bold-keyterms, split-sentence, add-whitespace): unknown category → skip
  `category_not_allowed`; the hunk rules (old = `current`, new = `recommended`, citations and
  numbers preserved, no heading touched); never the first paragraph of the first section; never an
  existing table (old contains a line starting with `|`); per category (words = casefolded
  `[^\W_]+` tokens):
  - remove-hr: old is only rule lines, new is empty or whitespace;
  - add-whitespace, break-paragraph: non-whitespace characters identical;
  - bold-keyterms: identical after removing `**`;
  - merge-paragraphs, split-sentence: word multisets differ only by connector words, at most
    `connector_words_max` (constant `CONNECTORS`: und, and, aber, but, sowie, jedoch, however,
    zudem, außerdem, furthermore, moreover, daher, therefore, thus, während, while, whereas, wobei);
  - make-list, make-table: no word of old lost, at most `added_words_max` words added.
- `readability-decisions.json`: `total_recommendations`, `applied`, `skipped` [{id, reason}],
  `edit_failures`, `net_char_delta_actual`. Runs through `run_mutation`.
- Tests: **AC6** an unknown category and a recommendation touching an H2 are skipped and logged;
  one passing and one failing case per category; order; mis-anchored `current` → edit failure.
- Mutation: drop the connector cap; allow a heading line in old.
- Commit: `feat(research): step 16 readability`.

### B7 — Ship gate G1–G12 (pure)
- `GateContext(report, required_headings, report_language, fmt, gate_config, notes (url →
  Note lookup also by final URL and canonical key), brief_text, brief_sha256, tier, artifacts
  present)` → `run_gate(ctx) -> GateResult(passed, checks: [GateCheck(id, ok, detail, warnings,
  data)])`. One function per check:
  - G1 non-empty. G2 `h2_list` = headings + Sources + Appendix, exact.
  - G3 `low * 0.8 ≤ body_words ≤ high * 1.2` (tolerance from config); `data` = words per section.
  - G4 citation numbers in the body (`[1, 3, 5]` = 3) × 1000 / body words ≥ the minimum.
  - G5 every body `[N]` has a Sources line whose URL maps to a run note; uncited lines → warnings.
  - G6 spans of ≥ `quote_min_words` words in „…“ “…” "…" «…» »…« ‚…‘ (code points as in
    `text.py`) in the body: the cited notes are the first citation after the closing mark in the
    same paragraph, else the last one before the opening mark; uncited → fail; the span must pass
    `quote_in_text` for at least one cited note's body. `data` = failing spans.
  - G7 over the H1 and the body: front matter at the start of the file, `<think>`, scaffold
    headers (`User Prompt`, `Run config`, `Tier rationale`, `Coverage notes`), `\bLocus \d+`,
    `\bTension \d+`, `comparisons.md`, `\binterim\b`, `cross-locus`, `scaffold`,
    `hyperresearch`, `[[` (case-insensitive). `data` = hits with positions.
  - G8 `extract_appendix` sha256 = the run's `brief_sha256`.
  - G9 every body citation of a note with `meta.is_retracted` has `retract` or `zurückgezogen`
    within ±`retraction_window_chars`.
  - G10 light: `polish-log.json`, `readability-decisions.json`; full: also the four
    `critic-findings-*.json`, `patch-log.json`, `cite-check-{pairs,findings,patch-log}.json`.
  - G11 light: ok ("not applicable"). Full: every finding with `severity == "critical"` in the
    critic and cite-check findings files (`{"findings": [{id, severity, …}]}`) has a resolution in
    `patch-log.json` or `cite-check-patch-log.json` (`{"resolutions": [{finding_id, outcome:
    applied|rejected|escalated, reason}]}`) that is `applied`, or `rejected` with a reason. M9
    writes these schemas.
  - G12 `langdetect` (seed 0) on the plain text (no markers, no markdown) of the first, middle and
    last section, each cut to `language_sample_chars`; samples under 20 characters are skipped;
    every detected language = the report language. `data` = offending section indexes.
- `tests/gate_fixtures.py`: a passing German report with notes; **AC4**: for every check one
  passing and at least one failing fixture (G11 with a full-tier fixture).
- Mutation: count words incl. Sources; check G8 against the brief text without the final newline;
  G6 against the wrong note.
- Commit: `feat(research): ship gate G1–G12`.

### B8 — Fix rounds and the gate loop
- `fixes.py`, one round = one `run_mutation` named `gate-fix-<n>` (tasks keyed `check:section`),
  fixes in this order, then `renumber`:
  - G8: re-render the appendix from the archive (code).
  - G7: remove front matter, `<think>…</think>`, scaffold header lines and `[[`/`]]` (code);
    delete each sentence that holds a vocabulary hit (code deletion hunk).
  - G6: remove the quotation marks around each failing span (code).
  - G9: insert ` (zurückgezogen)` / ` (retracted)` before each failing marker (code).
  - G4/G5: `REPAIR_CITATIONS` (`reason`, `think=False`) per affected section with its pack and the
    current Sources → hunks that add note-id citations or fix dangling ones; growth allowed,
    numbers preserved.
  - G12: `REDRAFT_SECTION` (`reason`, `think=True`) once per offending section, from its step-10
    pack, in the report language.
  - G3 over: `COMPRESS_SECTION` once per over-budget section (> its budget), to its budget; the
    single compression pass of AD5 (journal flag `compressed`). G3 under: `EXPAND_SECTION` once
    per section under 80 % of its budget, from pool notes not yet cited there (journal
    `expanded:<section>`).
  - G1, G2, G10, G11: no fix.
  Rewritten sections keep their heading; their citations go through `renumber`. A fix error
  skips that fix with event `fix_skipped`.
- Graph: `gate` node (runs checks, writes round r into `gate.json` with history) → passed: `export`;
  failed and r < `fix_rounds`: `fix` (r + 1) → `gate`; else `blocked` → `export`. Step `G` starts
  at the first gate and finishes when routing to export.
- Tests: each fix on its fixture; order; a section is never compressed or expanded twice; after
  three failing rounds the run is `blocked` and `gate.json` names the checks (**AC5**); resume in
  the middle of a fix round replays without new model calls.
- Mutation: allow a second expansion; skip `renumber` after fixes.
- Commit: `feat(research): gate fix rounds`.

### B9 — Export adapter
- `adapters/export.py`: `export_docx(markdown, path, *, reference_docx=None, notice=None)` —
  markdown-it-py (`MarkdownIt("commonmark", {"html": False}).enable("table")`) tokens → python-docx:
  H1 `Heading 1`, H2 `Heading 2`, H3 `Heading 3`, paragraphs with bold/italic/code runs, links as
  `text (url)`, bullet/ordered lists (`List Bullet`/`List Number`), tables, the appendix fence as
  monospace paragraphs line by line; a style missing in the base document falls back to the
  default. With `reference_docx` (template front matter, path relative to the template's
  directory): `Document(reference)`, body cleared except its section properties.
  `export_pdf(markdown, path, *, notice=None)`: the same markdown-it HTML, a default CSS constant,
  `weasyprint.HTML(string=…, url_fetcher=_refuse)` — `_refuse` raises for every URL, so rendering
  never reaches the network (`![x](http://…)` in a report renders without the image).
  The notice (decision 11) is the first paragraph.
- Export node: md always; docx and pdf each in `try`; a failure → event `export_failed` and
  `run.json` `exports.<fmt> = {"error": …}`; the run status stays `done`/`blocked`.
- Tests: **AC7** DOCX opens with python-docx, its `Heading 2` texts = the H2 list, tables and the
  appendix text survive; PDF bytes start with `%PDF`; a report with a remote image renders and
  `_refuse` was called (sockets are blocked by `conftest`); a broken renderer (monkeypatched)
  leaves MD and DOCX and records the PDF error; blocked notice present in both.
- Mutation: default url fetcher; drop the notice.
- Commit: `feat(export): DOCX and PDF without pandoc`.

### B10 — Lite end to end
- Graph edges: `… sweep → draft → polish → readability → gate ⟲ fix → export → finish → END`;
  `finish` sets `done` if the gate passed, else `blocked`. CLI prints at the end: status, gate
  result (failed checks), `report.md`, DOCX/PDF path or error, `gate.json`, credits, sources.
- Tests on fakes: **AC1** `run.json` step ids = `0,1,2.1,2,10,15,16,G,X` and no full-only artifact
  exists; **AC3**; **AC5** with a model that keeps producing a too-short report; zero outbound
  calls before plan approval (fake gateway counts); kill and resume: in-process crash in step 10
  after section 2, in step 15, in a fix round; SIGKILL during step 10 (child), then `resume()`
  finishes with each stored section drafted once.
- Commit: `feat(graphs): Lite run end to end`.

### B11 — Mutation sweep and docs
- Mutate and confirm a test fails: step list missing `16`; plan approval skipped; report written
  twice in step 10; polish growth allowed; readability on a heading; G5 ignoring Sources URLs;
  fix rounds unlimited; PDF fetcher not refusing; `query.md` scrubbed.
- `/documentation-update` against the commit before step 0: new `docs/research.md` (flow, steps
  and artifacts, plan gate, evidence packs, citations, patch engine and mutation pattern, gate and
  fixes, export, resume contract, CLI); `docs/architecture.md` (the `research` package, the
  mutation pattern, no network in export); `IMPLEMENTATION.md` (M5 row, module map, run table,
  open issues); README status.
- Commit: `docs: Lite run docs and M5 status`.

### B12 — Live reference run (ask the owner first)
- `tests/live/test_live_lite.py` (marked `live`): an external German brief (propose "Welche
  Pflichten stellt die EU-KI-Verordnung an Anbieter von Hochrisiko-KI-Systemen, und ab wann
  gelten sie?" — public, nothing confidential; the owner may replace it), tier light, template
  `regulatorische-analyse`, format `structured`; prints the sanitized plan, approves it, runs to
  the end; asserts `done`.
- Before starting: show the owner the brief and the expected cost (≤ 60 Tavily credits, our
  Ollama, ~1 h) and wait for "ja". Preconditions: `udr doctor` exit 0, calibration present,
  `TAVILY_API_KEY` in `.env` (else ddgs).
- Record in `IMPLEMENTATION.md`: wall time, credits, sources, `claims_drop_rate`, gate result and
  rounds. If the run ends `blocked`, report the failing checks and ask before tuning prompts (at
  most two tuning rounds, each committed separately). Then set M5 to `done`.
- Afterwards stop our daemon by PID after checking `/proc/<pid>/environ` holds
  `OLLAMA_HOST=127.0.0.1:11436`.
- Commit: `docs: live Lite reference run`.

## Verification

- `uv run pre-commit run --all-files` green after every step: coverage ≥ 85 %, suite ≤ 60 s (now
  ~15 s; the two SIGKILL children each import LangGraph once).
- `uv run pytest tests/test_docs.py -q`: PRD at 800 lines, links resolve.
- `tests/test_layer_rules.py` (LangGraph only in `graphs/`) and `tests/test_egress_guard.py` stay
  green with the new modules; `app.research` imports only types and errors from the outbound
  package.
- Report red and green results per step in the summary (AGENTS.md §5.3).
