# M5 implementation plan — Lite end to end, plan gate, ship gate, export

**Status:** ready to execute; D1–D12 confirmed by the owner on 2026-10-03.
**Executor:** Opus 5.5, effort high. Follow [AGENTS.md](../../AGENTS.md) for every step:
red → green → mutation check → gate → commit. No push unless asked.

## Progress (2026-10-03, paused here on the owner's request)

Done and committed, each with tests written first and checked by deliberate code breaks:
steps 0 to 11 (config and manifest, report rendering, gate G1-G12, patch engine, decomposition, search
plan and its edits, sweep, evidence packs and drafting, polish and readability, fix rounds and the
blocked outcome, export), the worker lock, and the service, graph and steps of step 12, which run a
whole Lite run on fakes (`tests/test_research_service.py`).

Still to do, in this order:
1. Step 12 rest: two real SIGKILL tests (during the sweep and during drafting) with a child process
   built on `tests/research_run_rig.py`; close four surviving mutants of `steps.py` and
   `service.py` (the second plan-hash check in `confirm_plan`, the stored must-read set, the
   re-render after polish, the "interrupt pending" early return).
2. `bootstrap.build_research_service(rt)`: the real `RunContext` factory (gateway as searcher,
   preparer and fetcher, `build_pipeline`, `resolve_run_settings` with the session fallback), the
   step dependencies from the registry, `SubprocessPandoc`, and a wiring smoke test.
3. Step 13: `udr run <run_id>` and `udr run --brief` (D1, D3): plan table, `$EDITOR` loop,
   `--approve-plan`, exit codes.
4. Step 14: `docs/research.md`, `architecture.md`, IMPLEMENTATION.md status.
5. Step 15 on the server: M2/M3/M4 live checks, then the German Lite reference run (AC8).

Changes against this plan: no `candidates` table (candidates are derived from the stored searches);
the effective settings are frozen into `runs.settings_json` when a brief is approved (the session row
does not hold defaults); retracted sources are never offered as evidence.

## Context

M1–M4 are done. M4 leaves a `queued` run row with an archived, approved brief. M5 turns that row
into a shipped (or honestly `blocked`) Lite report. Requirement source: `PRD.md` §3.2 (plan gate),
§3.3, §3.5 (AD1–AD10), §3.6–§3.10, §4 M5. Upstream originals in
`../ultradeep-researcher/.claude/skills/`: `hyperresearch` (bootstrap, manifest, ship gate),
`-1-decompose`, `-2-width-sweep`, `-10-triple-draft` (light path), `-15-polish`,
`-16-readability-audit`.

Already in place: templates and their loader (M4), the outbound gateway with `prepare_query`,
`search_web`, `search_scholarly`, `fetch` (M2), `FetchPipeline` with resume, notes, claims, FTS,
scoring (M3), the brief parser `parse_brief` and `BriefRunner`'s checkpoint pattern (M4).

Not available on the dev Mac: GPUs, pandoc. Everything offline runs here; the live AC8 run happens
on the server.

## Owner decisions (2026-10-03), binding for this plan

- **D1 — Entry points.** `udr run <run_id>` starts or resumes an approved run. `udr run --brief F
  --tier light --template T [--language L] [--format F]` (PRD wording) is the *external brief* path
  of M6 `POST /runs`: the file must parse (`parse_brief`), code adds the Method line
  "extern übergeben" and the Output section, archives the bytes and creates the run. Running it from
  the owner's shell counts as approval. `--language` and `--format` default to the template's
  `language` and `default_response_format`.
- **D2 — Where run settings come from.** Step 0 copies report language, response format, template
  and interview language from the session row (or the external-brief arguments) into `run.json`.
  Later steps read only `run.json`. If the brief's Output section no longer matches a fresh render
  (hand edit), step 0 emits `brief_output_mismatch`; the settings win.
- **D3 — Plan approval.** `search-plan.json` has a `plan_sha256` over its canonical JSON; approval
  takes that hash (stale → rejected), like the brief. The CLI shows the plan as a table and offers
  approve / edit (`$EDITOR` on one query per line, with item and lens) / delete / add / quit.
  `udr run <id> --approve-plan <sha>` approves non-interactively. Quitting leaves the run
  `awaiting_plan_approval`.
- **D4 — Worker slot in M5.** A file lock `data/worker.lock` (fcntl) is held only while a graph
  executes. It is released at the plan interrupt. A second `udr run` while the lock is held exits
  with "ein anderer Lauf ist aktiv". M6's worker replaces the CLI as the lock holder.
- **D5 — Scholarly search in Lite.** Upstream light skips academic APIs; PRD §3.3 puts them first.
  Lens-B queries go to OpenAlex and Crossref (arXiv when a domain is `science_medicine` or
  `tech_standards`); they cost no credits. All other lenses use `search_web`.
- **D6 — Coverage counting (2.5).** Deterministic by provenance: a complete, non-derivative,
  non-failed note counts for every atomic item whose query produced its URL. Thin = fewer than 2,
  uncovered = 0. Wave 2 asks `reason` for 2–3 queries per thin item (auto-sanitized, no human),
  at most one extra wave.
- **D7 — Citations owned by code.** Drafting prompts show evidence as `[S<k>]` keys. Code maps keys
  to notes, renumbers to `[N]` by first appearance, splits brackets over 3, drops unknown keys
  (logged), and writes the Sources list from note metadata. The model never writes `## Sources`
  or the appendix.
- **D8 — Sources metadata.** Web notes lack author and year. The fetch extractor also keeps
  trafilatura's `author`, `sitename` and `date`; fallback publisher is the host, year "o. J." /
  "n.d.". Scholarly notes use authors and year from search metadata.
- **D9 — Must-read set and evidence packs.** Must-read = 8–15 notes, picked round-robin over the
  atomic items (provenance, D6) by quality score (scoring.py), originals only; the analysis notes
  of picked long sources come on top. A section's pack = summaries of all
  must-read notes + their claims ranked by FTS relevance to the section (heading, instructions,
  mapped items) + top FTS passages from the whole run, fenced as untrusted. Over the token budget,
  the lowest-ranked claims and passages go through a `summarize` map-reduce with an
  `evidence_condensed` event; nothing is cut silently.
- **D10 — Edit safety (polish and readability).** Every hunk: `old` occurs once, ≤ 1200 chars,
  inside one body section (never a heading, Sources or appendix), and keeps every `[N]` marker and
  every number. Polish: net delta ≤ 0 per hunk. Readability categories applied, in the original
  order: `remove-hr`, `merge-paragraphs`, `break-paragraph`, `make-list`, `make-table`,
  `bold-keyterms`, `add-whitespace`; `split-sentence` is skipped. Merge, break, bold and whitespace
  must keep the word sequence; list and table must keep the word multiset. A polish hunk that
  removes a `[N]` or a number is rejected, so cuts hit only uncited filler.
- **D11 — Fix rounds in code where possible.** G6: code removes the quotation marks (never
  invents text). G7: code removes front matter, `<think>` and scaffold headers; vocabulary hits get
  a `reason` deletion hunk. G8: code re-attaches the appendix. G3, G4/G5, G9, G12: `reason`, as in
  PRD §3.10. A blocked run keeps `report.md` and exports; "nicht bestanden" lives in `run.json`
  status and `gate.json`, not in the files (G8 forbids touching the appendix).
- **D12 — Live order.** Before the M5 live run, run the pending M3/M4 live checks on the server
  (the `extract` drop rate decides R2, which shapes claims in every pack). M5 is built offline
  first; AC8 runs last.

Adopted without a separate question (challenge if wrong): code seeds step 1's `sub_questions`
with the brief's numbered research questions verbatim; a section's word budget is the middle of
the format range split evenly over the H2s; `tier_recommendation` = full on a Lite run only emits
an event.

## Step 0 — Contract updates (one docs commit)

1. Set the M5 row in IMPLEMENTATION.md to `in progress`, link this plan.
2. PRD edits (net zero lines, PRD is at 800): M5 CLI line per D1; §3.11 CLI list unchanged.
3. `config/profiles.toml` additions (AD3), see below.

## Fixed numbers (`config/profiles.toml`)

```toml
[light]
sources_min = 10
sources_target = [15, 25]
planned_searches = [8, 20]
adversarial_min = 5
candidate_urls = [20, 40]
deduped_urls = [15, 30]
fetch_waves = [1, 2]
must_read_notes = [8, 15]
readability_cap = 50
utility_scoring = false
redundancy_audit = false

[run]                       # both tiers
coverage_matrix_max_iterations = 3
gate_fix_rounds = 3
thin_item_sources = 2       # fewer counts as thin
wave2_queries_per_item = [2, 3]
hunk_max_chars = 1200
citation_density_min = 9    # per 1000 body words
quote_min_words = 5         # G6
retraction_window_chars = 200
language_samples = 3        # G12
length_tolerance = [0.8, 1.2]
```

The full-tier keys arrive with M8; `Profile` gains optional fields so `full` still loads.

## Layout (new code)

| Path | Holds |
|---|---|
| `src/app/research/models.py` | Pydantic schemas: `Decomposition`, `CoverageMatrix`, `SearchPlan`, `PlannedQuery`, `DraftSection`, `Hunk`, `Recommendation`, `GateResult` |
| `src/app/research/manifest.py` | `run.json`: settings, step transitions, status; atomic writes |
| `src/app/research/decompose.py` | Step 1: decomposition, coverage-matrix loop, shims, scaffold |
| `src/app/research/plan.py` | Step 2.1: lenses A–D, sanitizing, plan hash, edits, approval check |
| `src/app/research/sweep.py` | Step 2: search per query (persisted), candidate selection, waves, coverage report |
| `src/app/research/evidence.py` | Must-read set, section packs, `[S<k>]` keys |
| `src/app/research/report.py` | Assembly: title, sections, citation renumbering, Sources, appendix; body/section split |
| `src/app/research/hunks.py` | Hunk validation and application (AD5) |
| `src/app/research/draft.py` | Step 10 section by section |
| `src/app/research/polish.py`, `readability.py` | Steps 15 and 16 |
| `src/app/research/gate.py` | G1–G12, pure functions; `fixes.py` the fix rounds |
| `src/app/research/export.py` | Pure command building; the pandoc call sits in `adapters/pandoc.py` |
| `src/app/research/service.py` | `ResearchService`: start, resume, plan view/edit/approve, status |
| `src/app/prompts/research.py` | All Phase-2 prompts (no digits outside placeholders) |
| `src/app/graphs/research.py` | The `research` graph and `ResearchRunner` |
| `src/app/store/research.py` | Migration 3 tables (below) |
| `templates/report.css` | Default PDF CSS |

## Data model (migration 3)

- `runs.status` values: `queued`, `running`, `awaiting_plan_approval`, `done`, `blocked`,
  `failed` (M6 adds `cancelled`, `awaiting_brief_approval`).
- `searches(run_id, query_id, provider, sent_query, results_json, credits, created_at)`: one row
  per executed query, written before its results are used. A resumed step 2 never sends a stored
  query again (no double credits).
- `candidates(run_id, url, canonical_url, item_ids_json, lens, query_id, rank, wave)`: provenance
  for D6.
- Step 10 sections, polish and readability progress live as files (`temp/sections/<nn>.md`,
  `polish-log.json`, `readability-decisions.json`) written atomically per section, so a resume
  continues at the first missing section.

## Graph (`graphs/research.py`, thread = run_id, `durability="sync"`)

```
bootstrap(0) → decompose(1) → plan(2.1) → approve_plan (interrupt) ↺ edit
            → sweep(2) → draft(10) → polish(15) → readability(16) → gate(G) → export(X) → END
```

- Edges are fixed per tier (AD1); the light step list is a constant checked against `run.json`.
- `approve_plan` does no model work and writes nothing before the interrupt (M4 rule). Edits are
  validated and re-sanitized in the service before the graph resumes.
- Each node records `running` / `done` in `run.json` and is idempotent (resumes from persisted
  items).

## Step semantics

- **0 Bootstrap:** `query.md` (brief bytes, verbatim), `scaffold.md` (run config, modality),
  `run.json` (settings per D2, profile, step list).
- **1 Decompose:** upstream schema minus `citation_style` (always inline), plus `domains`,
  `tier_recommendation` (recorded, never applied), `required_section_headings`, `modality`,
  `levers`. Code seeds `sub_questions` with the brief's numbered research questions verbatim and
  sets `response_format` from `run.json`; the brief's register line wins over the classified one.
  Headings: the template's, or derived for `auto` and validated with the template rules (2–15,
  unique, none reserved). If the brief names its own sections and a template is set:
  `template_overrides_brief` warning. Coverage matrix: `reason` maps query phrases to items; code
  checks every item id exists; gaps → re-decompose with the gap rows, at most 3 times, leftovers
  listed in the scaffold. Shims `shims/{research,drafting,polish}.md` rendered by code.
- **2.1 Plan:** `reason` writes queries per item and lens (A breadth, B scholarly, C adversarial,
  D period-pinned when `time_periods` exist); code enforces the profile range, ≥ 5 adversarial,
  every item covered; each query goes through `gateway.prepare_query`; blocked queries stay in the
  plan marked `blocked` and make approval impossible until edited or deleted.
- **2 Sweep:** queries run scholarly first (D5); results persisted per query; candidates deduped by
  canonical URL, ranked (strategy tier, authoritative domains first), capped at `deduped_urls`
  high; `FetchPipeline.ingest_many`; coverage per D6; wave 2 for thin items; `coverage-gaps.md`
  always written, with a shortfall below `sources_min` stated.
- **10 Draft:** one `reason` call per H2 (AD4), word budget = middle of the format range split
  evenly, evidence pack per D9, section instructions from the template, `drafting` shim. A section
  without evidence states the gap. Assembly per D7 writes `report.md`.
- **15 Polish:** `reason` proposes cut-only hunks per section; code applies per D10; log
  `polish-log.json {applied, rejected, escalations}`.
- **16 Readability:** `summarize` recommends ≤ 50 per report (per section calls); code applies per
  D10; `readability-recommendations.json`, `readability-decisions.json`.
- **G Gate:** G1–G12 per PRD §3.10, body = text before the Sources heading; G10 light needs polish
  log and readability decisions; G11 passes in light. Up to 3 fix rounds per D11; `gate.json` holds
  every round.
- **X Export:** `pandoc report.md -o report.docx [--reference-doc]`; `pandoc --pdf-engine=weasyprint
  --css templates/report.css -o report.pdf`. Missing pandoc → `export_failed` event, status still
  reflects the gate, MD stays.

## Dependencies

`uv add weasyprint` (BSD-3; needs Pango on the server: `apt install libpango-1.0-0`). pandoc is an
external binary (GPL, never linked, R14). `langdetect` and `python-docx` are present.

## Work steps (each: tests first, see them fail, implement, mutation check, gate, commit)

1. Profiles, manifest, migration 3, run statuses.
2. Report model: body split, citation renumbering, Sources, appendix fence (G8-safe for briefs
   containing backticks).
3. Gate G1–G12 with one passing and ≥ 1 failing fixture each (AC4).
4. Hunks: validation and application, polish and readability rules (AC6).
5. Decompose (step 1) on a scripted model.
6. Plan (2.1): lenses, sanitizing, hash, edits, denylist block (AC2).
7. Sweep (2): persisted searches, candidates, waves, coverage; kill-and-resume without double
   credits.
8. Evidence packs and draft (10).
9. Polish (15), readability (16).
10. Fix rounds and `blocked` (AC5).
11. Export with a fake pandoc runner; real pandoc test marked `live` (AC7).
12. Graph, runner, service, worker lock; light step sequence on fakes (AC1, AC3); real SIGKILL
    during step 2 and step 10.
13. CLI `udr run` (D1, D3).
14. Docs: `docs/research.md`, IMPLEMENTATION.md, architecture.md.
15. Live on the server (D12): M3/M4 checks, then the German Lite reference run (AC8).

## Verification

- AC1–AC7 offline in the gate; AC8 live, results recorded in IMPLEMENTATION.md (wall time,
  credits, sources, drop rate, gate rounds).
- Egress: `research/` imports the gateway only through `bootstrap`; `test_egress_guard.py`
  unchanged.
