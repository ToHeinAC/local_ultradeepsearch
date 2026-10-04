# PRD — local-ultradeepsearch (local UltraDeep Researcher)

Status: approved for implementation planning on 2026-09-30, after five clarification rounds with
the owner. Items marked *(adopted)* were not asked explicitly and are listed in §5 so they can be
challenged. Changes to this file need the owner's approval ([AGENTS.md](AGENTS.md) §5.1). Phase
status lives only in [IMPLEMENTATION.md](IMPLEMENTATION.md).

## 1. Problem & goal

Research pipelines fail quietly. A vague question produces a fluent, well-cited report that answers
something adjacent, and nothing errors, so nothing can be caught.

[ultradeep-researcher](https://github.com/ToHeinAC/ultradeep-researcher) fixes this with a
two-stage contract:
1. A human-approved brief that every step treats as gospel.
2. An automatic, tier-adaptive (light/full) pipeline with adversarial review and a verification
   gate.

But it runs only inside Claude Code on cloud models, sends the whole research context to cloud
services, and no other application can call it.

**Solved means:** on this server, with *all* language work done by local Ollama models, the owner
(via GUI) or another application (via REST or MCP) can:
- **Phase 1 (HITL):**
  - clarify the question until it is understood and has enough context;
  - fix the output (language, format, length, report template) and the depth (Lite/Full);
  - approve the exact brief text, then the sanitized search plan.
- **Phase 2 (automatic):**
  - run Lite or Full with exactly the original step sets;
  - ship a report (MD/DOCX/PDF) only if it passes the automatic ship gate, which checks that
    citations resolve, quotes are verbatim, structure, length and language are right, and the
    brief is attached verbatim.

Confidential context never leaves the machine except as sanitized, denylist-checked, logged
search queries.

**Terms:**
- **brief:** the approved Phase-1 text, gospel for every step.
- **run:** one Phase-2 execution, with an isolated corpus.
- **tier:** `light` (shown as "Lite") or `full`.
- **profile:** a tier's numeric budgets (`config/profiles.toml`).
- **role:** a model slot (`reason`, `extract`, `summarize`, `ocr`).
- **gateway:** the only egress module.
- **ship gate:** G1–G12 (§3.10).

## 2. Non-goals

- **Models:** no cloud LLMs of any kind.
- **Tiers:** no `dissertation` tier and no `premier` gear. The profile file stays extensible so
  they can be added later.
- **Upstream features not ported:**
  - the Chrome browser-fetcher lane and CAPTCHA/login handling;
  - PageRank;
  - EDGAR, FRED, CORE, DOAB, ClinicalTrials and Unpaywall;
  - wikilink citations;
  - a vault shared across runs;
  - embeddings and vector search (FTS5 + MinHash, as in the original).
- **Inputs:** uploads are never cited, and Phase 1 makes no outbound requests.
- **Interaction:** no intervention points after search-plan approval.
- **Access:** no GUI login, nginx or internet exposure, multi-user support, or English UI.
- **Delivery:** no webhooks, notifications, token streaming, in-GUI report editing, or DOCX
  corporate-template filling (`reference.docx` provides styles only).
- **Process:** no 8-gate first-principles interview and no human quality rubric; the ship gate is
  the quality bar.

## 3. Constraints

### 3.1 Platform and models

- **Host.** This server: 2× RTX 4090 (24 GB each), 124 GB RAM, 32 cores, Ubuntu, Python via `uv`.
- **Network exposure.** GUI, API, MCP and our own Ollama instance bind to `127.0.0.1` only. Remote
  access goes through an SSH tunnel.
- **Model roles.** Tag, endpoint, `num_ctx`, temperature and `keep_alive` can be set per role via
  env.

| Role | Default model | Endpoint | Used for |
|---|---|---|---|
| `reason` | `qwen3.8-27b:latest` | own instance `127.0.0.1:11436`, pinned to one GPU | Phase-1 dialogue, tier recommendation, planning, next-action choice, analysis, drafting, synthesis, critics, patch hunks, cite verdicts, polish, query sanitizer |
| `extract` | `LiquidAI/lfm2.5-1.2b-instruct:latest` | own instance | Note summaries, claims, lead extraction (never rewrites source text) |
| `summarize` | `gemma4:e4b` (`gemma4:e2b` selectable per run) | shared daemon `127.0.0.1:11434` | Upload distillation, long-source map-reduce, intermediate summaries, utility scoring, evidence-digest grouping, readability recommendations |
| `ocr` | `deepseek-ocr:3b` | shared daemon | Upload pages without a text layer |

- **Own instance.** `ollama serve` with `OLLAMA_HOST=127.0.0.1:11436`,
  `CUDA_VISIBLE_DEVICES=<UDR_OLLAMA_GPU>`, `CUDA_DEVICE_ORDER=PCI_BUS_ID`, `OLLAMA_VULKAN=0`,
  `OLLAMA_NUM_PARALLEL=1` and `OLLAMA_MAX_LOADED_MODELS=2`. It uses the system model store, so
  nothing is downloaded twice, and an Ollama already serving the port is adopted.
  **Fail-open:** if the instance is not up within 30 s, its roles use the shared daemon and a
  `warning` event is emitted. Prior art: `../local_summarizer/src/ollama_server.py`,
  `gpu_placement.py`.
- **Context budgets.**
  - `reason` gets the largest `num_ctx` in {32768, 24576, 16384, 12288} that loads 100 % into
    VRAM, i.e. `/api/ps` reports `size_vram == size`. `udr doctor --calibrate` measures it and
    stores it in `data/calibration.json`.
  - `extract` gets 8192, `summarize` 16384.
  - Prompts are assembled against `num_ctx` minus the output reserve, estimating tokens as
    chars/3.
  - Overflow goes through map-reduce (`summarize`), and every chunk is processed. Evidence is
    never truncated silently.
- **Thinking.** `think` is set per call site. Thinking text never reaches an artifact.

### 3.2 Confidentiality and outbound traffic

- **One egress.** Only `src/app/adapters/outbound/` may reach non-loopback hosts. The Ollama
  adapter accepts loopback URLs only. An AST test enforces both rules.
- **Every outbound query** (Tavily, DuckDuckGo, OpenAlex, Crossref, arXiv) goes through four
  steps:
  1. Denylist check; a hit blocks the query.
  2. `reason` sanitizer: removes personal, client, company and project names and internal
     identifiers taken from the brief and uploads. Returns
     `{sanitized_query, removed_terms[]}`.
  3. Denylist check again.
  4. Log entry.

  The denylist is the only hard guarantee; the sanitizer is best-effort.
- **Denylist.** `data/denylist.txt`, one term per line, editable via GUI, API or CLI. Matching is
  case-insensitive and NFKC-normalized. Umlauts and diacritics are folded (ä→ae, é→e), and
  hyphen/space variants count as equal.
- **Private URLs are never fetched or sent to Tavily.** This covers private, loopback and
  link-local IPs, single-label hosts, and `UDR_INTERNAL_DOMAINS`, re-checked after every redirect.
  **Phase 1 makes zero outbound requests.**
- **Search-plan approval gate.** After brief approval, steps 1 and 2.1 run, then the run pauses as
  `awaiting_plan_approval`.
  - The owner can edit, delete or add queries; edits are re-sanitized.
  - A plan containing a denylisted query cannot be approved.
  - Later queries (wave 2, investigators, gap fills) pass the four steps above without a human.
- **Outbound log.** `data/runs/<run_id>/outbound.jsonl`, one line per request, shown in the GUI.
  - Fields: `ts, step, provider, original_query, sent_query, removed_terms, url, status, credits`.
  - `original_query` stays local.
- **API approval.** Each API key has a `self_approve` flag.
  - With the flag, the caller may approve briefs and plans via API.
  - Without it, the item waits for the owner in the GUI.

### 3.3 Search, fetch and budget

- **Search order per atomic item:**
  1. Scholarly APIs: OpenAlex, Crossref, arXiv for STEM (Lite: scholarly-first domains only).
  2. Tavily Search (`basic`, `max_results=10`).
  3. `ddgs` as fallback.
- **Fetch order** *(adopted)*:
  1. Local first: `httpx` with a 30 s timeout, 10 MB HTML / 25 MB PDF caps, 1 req/s per host, and
     a User-Agent without a URL. trafilatura extracts HTML, pypdfium2 extracts PDFs.
  2. Tavily Extract (`basic`) only if local extraction fails the junk gates.
- **Credits.** Search costs 1 credit; extract costs `ceil(successful_urls/5)` per call.
  - Two ledgers: per run (profile cap: light 60, full 300) and per calendar month
    (`UDR_TAVILY_MONTHLY_LIMIT=1000`, warning at 80 %).
  - When the run cap or month limit is reached, or on HTTP 432/433, search switches to `ddgs` for
    the rest of the run. This emits `provider_switched`; the run never fails because of it.
- **Scholarly metadata.** OpenAlex `is_retracted`, `cited_by_count` and `open_access.oa_url` feed
  scoring and G9. `OPENALEX_MAILTO` is the polite-pool contact for OpenAlex and Crossref.
- **Domain strategies** (`config/source_strategies.toml`) cover four domains: `tech_standards`,
  `regulation_de_eu`, `science_medicine` and `business_markets`.
  - Each domain has preferred and authoritative domains (e.g. gesetze-im-internet.de,
    eur-lex.europa.eu, `*.bund.de`, iso.org, din.de, iaea.org, sec.gov), a scholarly-first flag,
    and Tavily `include_domains` hints.
  - Tier weights: ground_truth 1.0, institutional .85, practitioner .7, commentary .4,
    unknown .6.
  - Step 1 assigns one or more domains.
- **Run isolation.** Notes, claims, FTS rows and artifacts are keyed by `run_id`. No run reads
  another run's corpus.

### 3.4 Repository and code conventions

- **Template rules** ([AGENTS.md](AGENTS.md)): uv; Python 3.12 (≥ 3.11); `src/app/`, `tests/`;
  pyright strict; ruff; functions ≤ 50 lines; complexity ≤ 10; branch coverage ≥ 85 %. The
  **offline** suite runs in ≤ 60 s with sockets blocked.
- **Tests.** Every model and network call sits behind an adapter with a scriptable fake. Live
  tests carry the `live` marker, stay outside the gate, run manually, and have their results
  recorded in IMPLEMENTATION.md.
- **Config.** Env via pydantic-settings. Secrets only in `.env`: `TAVILY_API_KEY`,
  `OPENALEX_MAILTO`, optional `OPENALEX_API_KEY`, `UDR_GUI_API_KEY`. `.env.example` lists the keys.
- **Runtime data** lives in `data/` (gitignored):
  - `udr.sqlite`, `checkpoints.sqlite`, `calibration.json`, `denylist.txt`;
  - `runs/<run_id>/`, `briefs/`, `uploads/<session_id>/`, `templates/`, `backups/`.
- **Boundaries.**
  - Prompts are named constants in `src/app/prompts/`.
  - LangGraph is imported only in `src/app/graphs/`.
  - Network and LLM calls happen only in `src/app/adapters/`.
  - `src/app/gui/` imports only the API client.
- **Licences** must be compatible with Apache-2.0.
  - Planned: langgraph, langgraph-checkpoint-sqlite, ollama, tavily-python, ddgs, httpx,
    trafilatura, pypdfium2, python-docx, datasketch, fastapi, uvicorn, sse-starlette, mcp,
    streamlit, pydantic-settings, langdetect, weasyprint, markdown-it-py, psutil.
  - pymupdf (AGPL) and hypothesis (MPL) are **not** allowed.
  - No pandoc: DOCX via python-docx, PDF via weasyprint (owner decision 2026-10-02).
- **Prior art** (the owner's projects; copying is fine):
  - `../KB_BS_local-hybrid-researcher/src/services/{ollama_client,tavily_client}.py`
  - `../KB_BS_local-deep-researcher-he/src/utils.py` (Ollama `format=` schema)
  - `../local_summarizer/src/{templates,export}.py`
  - `../deepagents_ollama/showroom/app.py` (`safe_exit_app`)
  - `../open_deep_research_he/src/open_deep_research_he/graph.py` (`interrupt()`)

### 3.5 Binding architecture decisions

- **AD1 — Deterministic macro graph.** The tier's step list is encoded as graph edges; no LLM
  picks the next pipeline step.
  - `reason` picks the next *tool* only inside capped micro-loops: Phase-1 questioning,
    width-sweep follow-up waves, depth investigators, corpus-critic and gap fills.
  - Each choice is a structured `NextAction` (a discriminated union).
  - Hard caps apply to actions, fetches and credits.
- **AD2 — Typed LLM I/O.** Every LLM output that code consumes is a Pydantic model, requested via
  Ollama `format` with its JSON schema. Invalid output gets ≤ 2 repair retries, then a typed error
  that carries the raw output.
- **AD3 — Numbers are data.** All budgets come from `config/profiles.toml` and are injected into
  prompts; prompt text contains no hardcoded numbers.
- **AD4 — Section-wise generation.** Text over ~1500 words (drafts, synthesis, expansion) is
  generated one H2 section at a time, with word budgets that add up to the target.
- **AD5 — Patch, never regenerate.** After the first write, the report changes only through
  `{old, new}` hunks applied by code.
  - `old` must occur exactly once, be ≤ 1200 chars and not cross an H2.
  - Polish hunks never add characters.
  - Readability hunks are limited to the allowed categories.
  - Exceptions: the single compression pass, and the gate's one-time expansion per section.
- **AD6 — Brief is gospel.**
  - `reason` and `summarize` get the full brief verbatim.
  - `extract` gets its title and research questions, parsed deterministically.
  - Wrapper settings (template, tier, paths) never edit the brief.
- **AD7 — Untrusted content.** Fetched text enters prompts only inside
  `<untrusted-source url="…">` fences marked as data. No tool call is ever derived from it.
- **AD8 — One service process.** FastAPI hosts REST, MCP and the run worker. The graphs `brief`
  (thread = session_id) and `research` (thread = run_id) use `SqliteSaver` on
  `data/checkpoints.sqlite`. The GUI is a pure API client.
- **AD9 — Inspectable artifacts.** `data/runs/<run_id>/` holds the original artifact names
  (§3.6), plus `run.json` (manifest), `events.jsonl`, `outbound.jsonl`, `search-plan.json` and
  `gate.json`.
- **AD10 — Resumable everywhere.** A run that stops for any reason (crash, kill, reboot, cancel)
  continues from its last checkpoint without repeating finished work or spending credits twice:
  step boundaries via the LangGraph checkpointer, work inside a step persisted item by item and
  idempotently. Every milestone proves it with a kill-and-resume test.

### 3.6 Pipeline steps and tiers (exact)

Step semantics, artifacts, invariants and subagent contracts follow the original step skills
(`.claude/skills/hyperresearch-*` upstream). The only exceptions are the adaptations in §3.7.

| Step | Name | Tier | Main artifacts (run dir) | Roles |
|---|---|---|---|---|
| 0 | Bootstrap | both | `query.md` (verbatim brief), `scaffold.md`, `run.json` | – |
| 1 | Decompose | both | `prompt-decomposition.json`, `temp/coverage-matrix.md`, `shims/*.md` | reason |
| 2.1 | Search plan + **approval gate** | both | `search-plan.json` | reason |
| 2 | Width sweep (2.3 utility scoring, 2.6 redundancy audit: full only) | both | notes, claims, `temp/coverage-gaps.md` | extract, summarize, reason |
| 3 | Contradiction graph | full | `temp/contradiction-graph.json`, `temp/consensus-claims.json` | reason |
| 4 | Loci analysis (2 analysts, merge) | full | `loci.json` | reason |
| 5 | Depth investigation (≤ 6 investigators) | full | interim notes | reason, extract |
| 6 | Cross-locus reconcile | full | `comparisons.md` | reason |
| 7 | Source tensions | full | `temp/source-tensions.json` | summarize, reason |
| 8 | Corpus critic + gap fill | full | `corpus-critic-gaps.json`, `temp/corpus-critic-results.md` | reason, extract |
| 9 | Evidence digest | full | `temp/evidence-digest.md` | summarize |
| 10 | Draft: light = single draft; full = 3 angle drafts | both | light `report.md`; full `temp/draft-{a,b,c}.md` | reason |
| 11 | Synthesize (two-pass) | full | `temp/synthesis-{conflicts,plan,outline,pass1}.md`, `report.md` | reason |
| 12 | Critics: dialectic, depth, width, instruction | full | `critic-findings-*.json` | reason |
| 13 | Gap fetch | full | `temp/post-critic-fetch-log.md` | reason, extract |
| 14 | Patcher | full | `patch-log.json` | reason |
| 14.5 | Cite-check + second patch pass | full | `cite-check-{pairs,findings,patch-log}.json` | reason |
| 15 | Polish (cut-only) | both | `polish-log.json` | reason |
| 16 | Readability audit | both | `readability-{recommendations,decisions}.json` | summarize |
| G | Ship gate + ≤ 3 fix rounds | both | `gate.json` | reason |
| X | Export | both | `report.docx`, `report.pdf` | – |

- **light:** 0 → 1 → 2.1 → 2 → 10 → 15 → 16 → G → X.
- **full:** 0 → 1 → 2.1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12 → 13 → 14 → 14.5 → 15 →
  16 → G → X.

**Tier choice.**
- In the GUI the owner always picks Lite or Full. Phase 1 shows a recommendation with a 2–3
  sentence rationale, based on the original step-1 rules (when uncertain: full).
- The API also accepts `auto`, which applies the same rules. An explicit choice is binding; step 1
  never overrides it.

### 3.7 Deliberate adaptations versus the original

- **Subagents → LangGraph `Send` fan-out** with per-endpoint semaphores: reason 1, extract 2,
  summarize 2, network 4.
- **Fetcher agents → deterministic fetch pipeline plus `extract`.** For lead chasing, `extract`
  proposes up to 8 primary links per batch. Only links that actually occur in the page qualify,
  and they are fetched within budget.
- **Large full-text batch reads (8–50 notes) → map-reduce digests plus per-section evidence
  packs.** A pack holds the summaries and claims of all must-read notes, plus FTS-retrieved
  passages.
- **Read/Edit tool locks** (patcher, polish, readability) → code-applied hunks (AD5).
- **Citations:** inline `[N]` only.
- **Locus flavor enum:** unified to `dialectical|synthesis|technical`.
- **Gate measurement:** length and citation density count the **body** only, i.e. the text before
  the Sources heading. This fixes an original flaw: Sources and the appendix inflated both numbers.
- **Additions:**
  - search-plan approval gate and outbound gateway (§3.2);
  - explicit tier choice (§3.6).

### 3.8 Profile numbers (`config/profiles.toml`)

Seeded from the original `profiles.py` (light and full gear). Full is recalibrated in M9 to about
4 h per run.

| Key | light | full |
|---|---|---|
| sources min / target | 10 / 15–25 | 45 / 55–80 |
| planned searches; adversarial minimum | 8–20; 5 | 40–100; 5 |
| candidate → deduped URLs; fetch waves | 20–40 → 15–30; 1–2 | 80–120 → 60–100; 2–3 |
| utility scoring / redundancy audit | no / no | yes / yes |
| long-source analyses (> 5000 words) cap | 6 | 6 |
| loci analysts / max loci / depth budget / max investigators / investigator max actions | – | 2 / 6 / 40 / 6 / 12 |
| comparisons tensions / source tensions / corpus-critic gaps / gap-fetch cap | – | 3–5 / 3–7 / 3–8 / 5 |
| evidence-digest claims cap / min | – | 80–120 / 30 |
| drafts; must-read notes per draft | 1; 8–15 | 3; 20–50 |
| critic caps: dialectic / depth / width / instruction | – | 12 / 12 / 10 / 15 |
| readability recommendations cap; Tavily credit cap per run | 50; 60 | 50; 300 |

Word and citation targets by `response_format` (same for both tiers):
- short: 500–2000 words, 15–30 citations;
- structured: 2000–5000 words, 40–80 citations;
- argumentative: 5000–10000 words, 80–150 citations.

Citation density must be ≥ 9 per 1000 body words.

### 3.9 Report contract

- **Structure, in order:**
  1. `# <research question>`.
  2. The template's H2 sections. For template `auto`, these are the `required_section_headings`
     from step 1.
  3. `## Quellen` (report language `de`) or `## Sources` (all other languages).
  4. `## Anhang A — Recherche-Brief` (`de`) or `## Appendix A — Research Brief`. It holds the
     approved brief byte-for-byte in a fenced `text` block, followed by one provenance line:
     approval time in UTC and the archive path.
- **Citations.** `[N]`, numbered by first appearance, at most 3 per bracket (`[1, 3, 5]`), never
  `][`. Every sentence with a number or a quote carries its own citation.
- **Sources entries.** `[N] Author/Publisher. Title. Year. URL (abgerufen/accessed YYYY-MM-DD)`.
  The URL must match the note's source URL.
- **Quotation marks** only around verbatim source text; a translation follows in parentheses
  without quotation marks.
- **Language.** The report uses the brief's report language; sources may be in any language. The
  appendix is never translated.
- **Excluded:** YAML front matter, scaffold headers, pipeline vocabulary, thinking text.

### 3.10 Ship gate (G1–G12)

| ID | Check | Pass rule |
|---|---|---|
| G1 | report-exists | `report.md` exists and is non-empty |
| G2 | headings | Ordered H2 list = required headings + Sources + Appendix (exact text) |
| G3 | length | 0.8 × low ≤ body words ≤ 1.2 × high of the response-format range |
| G4 | citation-density | Citation numbers in body ≥ 9 per 1000 body words |
| G5 | citations-resolve | Every body `[N]` has a Sources entry that maps to a note of this run; unused entries only warn |
| G6 | quote-integrity | Every quoted span of ≥ 5 words in „…“ “…” "…" «…» »…« ‚…‘ occurs in the cited note's text (case- and whitespace-normalized) |
| G7 | no-leakage | No front matter, scaffold headers, `<think>`, or pipeline vocabulary (`Locus N`, `Tension N`, `comparisons.md`, `interim`, `cross-locus`, `scaffold`, `hyperresearch`, `[[`) |
| G8 | appendix-verbatim | sha256 of the appendix fence = sha256 of the approved brief |
| G9 | retractions | Each citation of a note with `is_retracted` has "retract"/"zurückgezogen" within ±200 chars |
| G10 | tier-artifacts | light: polish log + readability decisions; full: also 4 critic files, `patch-log.json`, cite-check files |
| G11 | criticals-resolved | full: every critical critic or cite-check finding is applied or rejected with a reason |
| G12 | language | langdetect on 3 body samples = the brief's report language |

**Fix rounds.** At most 3, each targeting only the failed checks:

| Failed check | Fix |
|---|---|
| G3 over / under | compress over-budget sections / expand each under-budget section once from unused evidence |
| G4, G5 | citation-repair hunks |
| G6 | un-quote, or restore the verbatim text |
| G7 | deletion hunks |
| G8 | re-attach the appendix from the archive (in code) |
| G9 | acknowledgement hunk |
| G12 | redraft each offending section once |

If the gate still fails after that, the run becomes `blocked`. `gate.json` lists the failures, and
the report and exports stay downloadable, marked "nicht bestanden".

### 3.11 Interfaces

- **REST:** `127.0.0.1:8541/v1`, bearer API key (M6).
- **MCP:** streamable HTTP at `127.0.0.1:8541/mcp`, same keys (M6).
- **GUI:** Streamlit at `127.0.0.1:8540`, German (M7).
- **CLI `udr`:** `doctor`, `brief`, `run`, `apikey`, `denylist`, `backup`.

## 4. Milestones

- M1–M7 deliver a usable Lite product through every interface.
- M8–M9 add the Full tier.
- M10 can run right after M7.

### M1 — Foundation and model infrastructure
- **Deliverable:**
  - Dependencies in `pyproject.toml`; `app.config` (pydantic-settings).
  - Role registry: model, endpoint, `num_ctx`, temperature, `keep_alive`, default `think`.
  - Ollama adapter: chat with a Pydantic schema via `format`, the `think` flag and timeouts;
    telemetry covers tokens, durations and `done_reason`.
  - Own-instance manager for `:11436`: start, adopt, fail-open.
  - Scriptable fake LLM; `events.jsonl` writer; `udr doctor [--calibrate]`.
  - A project README that replaces the template's.
- **Acceptance criteria:**
  1. `udr doctor` exits 0 when all role models exist on their endpoints, the own instance answers
     and ≥ 20 GB disk is free. Otherwise it exits 1 and names every missing model or endpoint.
  2. `--calibrate` writes the largest `reason` `num_ctx` that loads 100 % into VRAM to
     `data/calibration.json`.
  3. Structured calls: valid JSON yields a validated model. Invalid JSON or a schema violation
     gets 2 repair attempts with the error fed back, then raises `LLMOutputError` carrying the raw
     output.
  4. `done_reason == "length"` gets one retry with doubled `num_predict` (capped by `num_ctx`),
     then `LLMTruncatedError`.
  5. `<think>…</think>` is stripped before validation. An artifact-writer test proves thinking text
     is never persisted.
  6. If the own instance does not answer within 30 s, its roles use the shared daemon and an
     `ollama_fail_open` warning event is written.
  7. A non-loopback Ollama base URL fails config validation.
- **Edge cases:**
  - Ollama down: 3 retries with backoff, then `LLMUnavailableError`.
  - Model evicted mid-run: reloaded transparently.
  - Port already served by Ollama: adopted after a `/api/version` check.
  - Invalid GPU index: doctor error.
  - Too many concurrent calls: a client-side semaphore per endpoint.
- **Dependencies:** template bootstrap (phase 0).

### M2 — Outbound gateway and retrieval adapters
- **Deliverable:** `src/app/adapters/outbound/`, the only egress, containing:
  - denylist store and normalizer; `reason` sanitizer; private-URL guard; outbound log;
  - Tavily search/extract; `ddgs` search; OpenAlex, Crossref and arXiv clients;
  - an HTTP GET fetcher with trafilatura and pypdfium2 extractors;
  - run and month ledgers with provider switching (§3.3);
  - `udr denylist add|remove|list`.
- **Acceptance criteria:**
  1. A parametrized adversarial table varies denylist terms by case, umlaut, diacritic, hyphen and
     spacing. For every variant, no provider fake receives a payload containing the term, the call
     raises `DenylistBlocked`, and a `blocked` line is logged.
  2. A sanitized query that still matches the denylist is blocked.
  3. The guard never requests these (tested as a table): 10/8, 172.16/12, 192.168/16, 127/8, ::1,
     link-local, single-label hosts, `UDR_INTERNAL_DOMAINS`, and redirects into any of them.
  4. 10 basic searches plus one extract call with 12 successful URLs cost 13 credits.
  5. The run cap, HTTP 432, or a month ledger at or over its limit at run start each switch search
     to `ddgs` and emit `provider_switched`.
  6. An AST test allows `httpx`, `requests`, `urllib.request`, `socket`, `tavily` and `ddgs`
     imports only in the allowed adapter modules. A second test feeds it a violating module.
  7. Every outbound request writes exactly one complete `outbound.jsonl` line (§3.2).
- **Edge cases:**
  - ddgs blocked: 3 tries, then `SearchUnavailable`; the query becomes a documented coverage gap
    and the run continues. The same applies when all providers are down.
  - Tavily timeout: 2 retries.
  - Oversized HTML or PDF: skipped with a reason code.
  - Undeclared charset: detected.
- **Dependencies:** M1.

### M3 — Per-run source vault and fetch pipeline
- **Deliverable:**
  - **`app.store`:** SQLite tables for runs, notes, claims and rejected sources, with numbered
    migrations (later milestones add their own). FTS5 over notes (title 10, summary 5, body 1),
    filtered by `run_id`. Each source is also written to `data/runs/<id>/notes/<note_id>.md`.
  - **`app.pipeline.fetch`,** in order:
    1. Canonicalize the URL (drop tracking params and fragment, lowercase host); exact dedup.
    2. Fetch via the gateway.
    3. Junk gates: under 300 content chars; login wall (< 1000 chars plus markers); cookie wall
       (< 1500 chars plus markers); binary-garbage ratio > 5 %.
    4. MinHash near-dup (128 permutations, threshold 0.6) marks `derivative_of`.
    5. `extract` produces a length-scaled summary and claims in the original schema: claim,
       stance, stance_target, evidence_type, scope_conditions, quoted_support, numbers, entities,
       time_period, region, confidence. The note body is the extractor's verbatim text.
    6. Verbatim check of every `quoted_support`.
    7. Sources over 5000 words (up to the profile cap) get a `summarize` map-reduce
       source-analysis note.
    8. Source tier and quality score.
- **Acceptance criteria:**
  1. A 20-page synthetic fixture corpus has duplicates, near-duplicates, login and cookie walls and
     a PDF. Exactly the expected notes survive, and each rejection has a reason code.
  2. A claim whose `quoted_support` is not in the normalized source text is dropped and counted.
     `claims_drop_rate` goes to `run.json`.
  3. An FTS query for run A never returns notes of run B.
  4. The quality score is the original weighted sum without centrality (tier .35, utility .20,
     authority .25), renormalized, and is covered by a deterministic table test.
  5. A prompt-builder test shows fetched text appears only inside `<untrusted-source>` fences.
  6. Kill and resume: after a crash at any point of ingestion (including SIGKILL), a fresh process
     continues the run; no stored source is fetched again, no finished note is extracted again,
     and the run's credit count survives.
- **Edge cases:**
  - `extract` fails after retries: the note is kept without claims and flagged `extract_failed`.
  - Drop rate > 30 %: event `extract_quality_low` (R2).
  - Foreign-language sources are kept.
  - A scanned PDF in Phase 2 counts as junk; OCR is for uploads only.
- **Dependencies:** M1, M2.

### M4 — Phase 1: clarification, uploads, brief
- **Deliverable:**
  - **The `brief` graph:** `ingest_uploads → assess → ask (interrupt) ↺ → draft_brief →
    decide (interrupt: approve | revise | edit | settings | save) → finalize`.
  - **Checklist.** Seven items, each `clear | assumed | missing`: question, context,
    goal/decision, audience, scope, output (language, format/length, template), depth (tier).
  - **Rounds.**
    - Up to 5 questions per round, each with a drafted candidate answer the owner accepts, edits
      or replaces. The system never answers on the owner's behalf.
    - At most 5 rounds; "genug" ends early. Content items still open are listed as "not
      clarified"; output items take shown defaults (interview language, §3.6 rules, `auto`).
    - Unknowns become numbered research questions. Tier recommendation per §3.6.
  - **Uploads.**
    - PDF via pypdfium2 (pages with fewer than 50 text chars are OCRed with `deepseek-ocr` at
      200 dpi); DOCX via python-docx; MD/TXT as-is.
    - `summarize` distils each file into facts with file and page provenance. The brief carries
      a digest of at most 500 words.
  - **Brief template.** Labels are in the interview language. Sections, in order:
    1. `# question`, then a `Method:` line (rounds, skipped items), then audience/decision.
    2. Background and context; context from uploads (only if any); goal.
    3. Numbered research questions; scope (in / out / non-negotiables); assumptions; what a good
       answer looks like.
    4. Output: report language; sources in any language; quotation policy; register;
       `response_format` and target words; inline citations; template name and headings.
  - **Settings and edits.** Report language, format and template are session settings rendered
    into Output by code (new hash); a direct edit must keep the title and questions parseable.
  - **Approval and storage.** Approval is by sha256. The approved brief is archived immutable at
    `data/briefs/<UTC>.md`, with no front matter; "save" parks the session and writes
    `data/briefs/drafts/<session_id>.md`. `udr brief` is the interactive CLI.
- **Acceptance criteria:**
  1. No research run can be created unless `approve(session_id, sha256)` matches the current
     brief; a stale hash is rejected.
  2. The archived bytes equal the approved bytes.
  3. A gateway fake records **zero** outbound calls over a full Phase-1 session with uploads.
  4. `revise(feedback)` yields a new draft and hash; the old hash no longer approves.
  5. "genug" after round 1 lists every `missing` item under assumptions.
  6. A session survives a service restart and resumes at its pending interrupt.
  7. Question prompts carry the interview language taken from the owner's first message.
  8. Limits: at most 10 files, 50 MB each, 500 pages in total. Violations get a clear error, and
     nothing is partially stored.
- **Edge cases:**
  - Empty question: 422. Encrypted PDF: rejected. Oversized context: map-reduce with a notice.
  - A pasted finished prompt: one strengthening pass is offered; it may be installed verbatim
    (code adds the `Method` line and the Output section).
  - OCR model missing: scanned pages are skipped with a warning.
  - "weiß nicht" becomes a research question.
- **Dependencies:** M1. M2's gateway fake is needed for criterion 3.

### M5 — Lite tier end to end, search-plan gate, templates, ship gate, export
- **Deliverable:** the `research` graph for light (§3.6).
  - **Step 0:** workspace and `run.json` manifest, recording every step transition.
  - **Step 1:** the original decomposition schema plus `domains`, `tier_recommendation` and
    `required_section_headings` (from the template, or derived for `auto`). Shims are rendered
    deterministically. The coverage-matrix loop runs until there are no gaps, at most 3 times.
  - **Step 2.1:** plan with lenses A–D (breadth, depth/scholarly, adversarial, period-pinned),
    sanitize, then the approval interrupt.
  - **Step 2:** light numbers, coverage check, wave 2 for thin items; Wikipedia is never cited.
  - **Step 10:** single draft, section by section (word weights from step 1), from evidence
    packs of the 8–15 most relevant notes.
  - **Step 15:** polish hunks.
  - **Step 16:** `summarize` recommends; code applies the allowed categories in the original
    order.
  - **Gate:** G1–G12 with fix rounds.
  - **Export:** `report.docx` via python-docx (styles from an optional `reference_docx`);
    `report.pdf` via markdown-it-py HTML and weasyprint with a default CSS, no network access.
  - **CLI (resumable):** `udr run <run_id>`; `udr run --brief <file> --tier light --template <id>`.
  - **Templates** live in `templates/` (built-in) and `data/templates/` (uploads).
    - Front matter: `id`, `name`, `description`, `language`, `default_response_format`, optional
      `reference_docx`.
    - Body: H2s, each followed by `<!-- section instructions -->`.
    - Validation: 2–15 unique H2s, none reserved (Quellen, Sources, Anhang, Appendix).
  - **Built-in templates:**

    | Template | H2 sections |
    |---|---|
    | `auto` | derived as in the original |
    | `technische-stellungnahme` | Management Summary · Fragestellung und Abgrenzung · Stand der Technik und Normenlage · Technische Bewertung · Empfehlungen · Offene Punkte und Unsicherheiten |
    | `regulatorische-analyse` | Management Summary · Rechtsrahmen (DE/EU) · Anforderungen im Einzelnen · Auslegung und Behördenpraxis · Lücken, Konflikte und Risiken · Handlungsbedarf |
    | `literaturuebersicht` | Kurzantwort · Methodik der Recherche · Befundlage je Forschungsfrage · Widersprüche und Evidenzqualität · Forschungslücken · Fazit |
    | `markt-unternehmensanalyse` | Kurzantwort · Ausgangslage · Kennzahlen und Entwicklung · Treiber und Risiken · Szenarien · Bewertung und Empfehlung |

- **Acceptance criteria:**
  1. On fakes, a light run records exactly `0,1,2.1,2,10,15,16,G,X` in `run.json`, and no
     full-only artifact exists.
  2. At 2.1 the run is `awaiting_plan_approval` and does not hold the worker slot. A plan
     containing a denylisted query cannot be approved.
  3. The report's H2 list = template headings + Sources + Appendix, in order.
  4. Every check G1–G12 has one passing fixture and at least one failing fixture.
  5. A gate that still fails after 3 rounds leaves the run `blocked`. `gate.json` names the checks,
     and MD/DOCX/PDF stay downloadable; DOCX and PDF open with a "nicht bestanden" notice.
  6. Code rejects polish hunks with a net positive char delta. Readability recommendations outside
     the allowed categories, or touching an H2, are skipped and logged.
  7. Exports: the DOCX opens with python-docx and has every H2; the PDF starts with `%PDF`.
     If a renderer fails, that export fails visibly and MD stays available.
  8. **Live:** a German Lite reference run passes the gate, with a target of ≤ 60 min. Wall time,
     credits, sources and drop rate are recorded in IMPLEMENTATION.md.
- **Edge cases:**
  - Coverage gaps remain after 3 iterations: proceed, and list them in the scaffold.
  - Fewer than `source_min` sources after 2 waves: proceed, documented in `coverage-gaps.md` (as
    in the original).
  - A section without evidence states the gap and invents nothing.
  - Template language differs from the report language: warning in Phase 1.
  - The brief lists its own sections and a template is chosen: the template wins, with a warning.
- **Dependencies:** M3, M4.

### M6 — Service: REST, MCP and worker
- **Deliverable:**
  - FastAPI on `127.0.0.1:8541`, with a service layer shared by REST and MCP.
  - `udr apikey create --name <n> [--self-approve]` prints the key once and stores only its
    sha256.
  - **Worker:**
    - one active run at a time, FIFO queue; runs awaiting approval do not hold the slot;
    - runs left `running` auto-resume at startup;
    - cancel takes effect at the next node boundary.
  - **REST endpoints** (`/v1`):

    | Group | Endpoints |
    |---|---|
    | sessions | `POST /sessions`; `POST /sessions/{id}/uploads`; `POST …/messages` (answers, text, `genug`); `GET /sessions/{id}`; `POST …/revise`; `PUT …/settings`; `PUT …/brief` (direct edit); `POST …/approve {brief_sha256, tier, summarize_model?}` → `run_id`; `POST …/save` |
    | runs | `POST /runs {brief, tier: light\|full\|auto, template_id, …}` (external brief; its Method line records "externally supplied"); `GET /runs`; `GET /runs/{id}`; `GET …/events?after=`; `GET …/stream` (SSE) |
    | plan | `GET /runs/{id}/search-plan`; `PUT …/search-plan`; `POST …/search-plan/approve {plan_sha256}` |
    | output | `GET …/report?format=md\|docx\|pdf`; `GET …/gate`; `GET …/outbound` |
    | control | `POST …/cancel`; `POST …/resume`; `DELETE /runs/{id}` |
    | admin | `GET/POST /templates`; `GET/PUT /denylist`; `GET /health`; `GET /config` |

  - **MCP** at `/mcp` (streamable HTTP, same keys). Tools:
    - clarification: `start_clarification`, `answer_clarification`, `get_session`,
      `revise_brief`, `approve_brief`;
    - research: `start_research`, `get_run_status`, `get_search_plan`, `update_search_plan`,
      `approve_search_plan`, `get_report` (markdown), `list_templates`, `cancel_run`.
- **Acceptance criteria:**
  1. Every route returns 401 without a valid key; the test is parametrized over the route table.
  2. A key without `self_approve` gets 403 on approve endpoints; the object stays `awaiting_*`
     until a self-approve key approves it. Its `POST /runs` creates a run in state
     `awaiting_brief_approval`.
  3. Stale hash → 409. Report before done → 409 with the current status. Unknown ID → 404.
  4. A second run is `queued` and starts automatically after the first.
  5. Crash test on fakes: kill the worker mid-step and restart it. The run resumes from the last
     checkpoint, and no completed step re-executes (event count).
  6. An in-process MCP client lists all tools, and `start_research` → `get_report` returns
     markdown.
  7. The generated OpenAPI schema covers every endpoint.
- **Edge cases:**
  - Cancel during an LLM call: `cancelled` at the next node boundary.
  - `DELETE` on a running run: 409. Otherwise it removes the DB rows, the run directory and the
    upload directory.
  - Disk full: `failed`, with a reason.
  - Concurrent approvals: the first wins, the second gets 409.
- **Dependencies:** M4, M5.

### M7 — GUI (Streamlit, German)
- **Deliverable:** Streamlit on `127.0.0.1:8540` as a pure API client, using `UDR_GUI_API_KEY`
  (a self-approve key). Pages:
  - **Neue Recherche:**
    - question, uploads, rounds with editable candidate answers, checklist status, "genug";
    - settings: Lite/Full with recommendation and rationale; template select/preview/upload;
      report language; response format; e4b/e2b; Tavily run budget;
    - the exact brief text, with Freigeben / Überarbeiten / Speichern.
  - **Suchplan:** query table per atomic item (provider, sanitized text, removed terms). Queries
    can be edited, deleted or added; denylist hits are highlighted; Freigeben.
  - **Läufe:** history; the tier's step timeline (current step, sources, credits run/month,
    elapsed time, warnings); outbound log; cancel/resume/delete; pending approvals from other keys.
  - **Bericht:** rendered report, gate result, MD/DOCX/PDF downloads.
  - **Einstellungen:** denylist editor, doctor status, read-only role→model map.
  - **Safe exit:** SIGTERM to its own PID only (`safe_exit_app` pattern). It never touches the API
    or the worker.
- **Acceptance criteria:**
  1. An import-scan test proves that `src/app/gui/**` imports no graph, adapter, pipeline or
     store module.
  2. Streamlit AppTest with a fake API: Phase 1 renders questions; "Freigeben" sends the sha256 of
     the displayed text; the plan page blocks approval while a denylisted query exists.
  3. The exit handler signals only `os.getpid()` (`os.kill` mocked).
  4. Progress polls at most every 5 s. If the fake API is down, a banner appears; nothing crashes.
  5. A browser reload restores the session or run from the query parameters.
- **Edge cases:**
  - API unreachable at start: banner plus retry.
  - Report over 100k chars: collapsible sections.
  - Upload errors: shown per file.
- **Dependencies:** M6.

### M8 — Full tier: analysis steps 3–9 (plus 2.3 and 2.6)
- **Deliverable:**
  - **Full width sweep:**
    - `summarize` scores utility on 6 dimensions, 0–3 each.
    - Redundancy audit: MinHash plus claim overlap over 60 % marks `derivative_of`.
    - Wave 3 runs when an item has fewer than 2 independent sources.
  - **Step 3:** deterministic candidate pairing (opposite stance on the same target, same entities
    with opposite conclusions, differing numbers), then `reason` clusters and ranks. Consensus
    requires an independence sum ≥ 3.0.
  - **Step 4:** two `reason` analysts with distinct prompts, then a deterministic merge. At most 6
    loci, with the original budget brackets summing to 40. `inference_depth` is re-evaluated.
  - **Step 5:** up to 6 investigators, run sequentially on `reason`.
    - Each is a `NextAction` loop over `vault_search`, `web_search`, `scholar_search`,
      `fetch_url`, `read_note` and `commit_position`.
    - Caps: `source_budget` fetches and 12 actions.
    - Output: interim notes with the original sections.
  - **Step 6:** 3–5 tensions.
  - **Step 7:** 3–7 source tensions from `summarize` digests of the top 8–12 sources.
  - **Step 8:** period-pinned preflight, then the `reason` corpus critic, then gap fill.
  - **Step 9:** deterministic claim filter (80–120) plus `summarize` grouping.
- **Acceptance criteria:**
  1. Each invariant below is enforced in code and has a failing test:
     - ≥ 1 dialectical locus or a justified `skip_loci`; otherwise the analysts re-run once, then
       the run becomes `blocked`;
     - every interim note ends with `## Committed position` (confidence, boundary conditions,
       what would change it);
     - `comparisons.md` exists exactly when loci ≥ 1;
     - more than 50 % investigator failures → `failed`.
  2. A scripted fake that always chooses `fetch_url` never exceeds `source_budget` or 12 actions.
  3. `loci.json`, the contradiction graph, `source-tensions.json` and `corpus-critic-gaps.json`
     validate against Pydantic schemas that mirror the original fields.
  4. On fakes, a full run up to step 9 produces every artifact named in §3.6.
- **Edge cases:**
  - Fewer than 10 notes before step 4: loci analysis is skipped with a reason, and step 6 writes
    one distilled position.
  - No contradictions: empty arrays.
  - No `time_periods`: the preflight is skipped.
- **Dependencies:** M5.

### M9 — Full tier: drafting and adversarial review (10, 11, 12, 13, 14, 14.5) and calibration
- **Deliverable:**
  - **Step 10:** 3 angle drafts following the original angle rules (tension-based A/B/C, or
    breadth/depth/practitioner). Each has 20–50 must-read notes and evidence packs (§3.7), and is
    drafted section-wise.
  - **Step 11:** conflicts, plan and outline files. Pass 1 targets +15–20 %; pass 2 brings the text
    into range. Both passes are section-wise, and `report.md` is written once.
  - **Step 12:** 4 critics with the finding schema and caps, including instruction-critic
    readability checks R1–R6.
  - **Step 13:** gap fetch, at most 5 gaps.
  - **Step 14:** findings are merged and sorted, turned into hunks, and applied by code.
    - A critical finding is skipped only with a logged resolution: rejected as invalid, escalated
      to the log, or fixed by a restructure hunk.
  - **Step 14.5:**
    - Mechanical triage with the original rules: every number of 2+ digits appears in the cited
      note's claims, or, for sentences without numbers, a 6-word run does.
    - `reason` gives verdicts on sampled pairs: 100 % of strong sentences, 50 % of weak ones.
    - A dangling citation is critical. A second patch pass follows.
- **Acceptance criteria:**
  1. On fakes, the full run executes exactly
     `0,1,2.1,2,3,4,5,6,7,8,9,10,11,12,13,14,14.5,15,16,G,X`.
  2. In full tier step 10 never writes `report.md`, and step 11 writes it exactly once (write
     counter).
  3. Patch engine: a hunk applies only if `old` occurs exactly once. A hunk over 1200 chars or
     crossing an H2 is rejected and logged `escalated`. Heading levels, list numbering and table
     column counts are preserved (fixtures).
  4. An unresolved critical finding fails G11.
  5. A dangling-citation fixture yields a critical cite-check finding.
  6. **Live:** a German Full reference run passes the gate in about 4 h (≤ 4.5 h). If it does
     not, recalibrate `profiles.toml`, record the numbers and the reason in IMPLEMENTATION.md,
     and repeat the run.
- **Edge cases:**
  - A draft under 50 % of its target: that angle is re-drafted once.
  - A finding cites a non-existent note: skipped (as in the original).
  - Synthesis over the ceiling: one compression pass.
  - A critic returns invalid JSON: retried once. The instruction critic is mandatory; any other
    missing critic is logged.
- **Dependencies:** M8.

### M10 — Operations
- **Deliverable:**
  - systemd **user** units `udr-ollama`, `udr-api` and `udr-gui`: loopback only,
    `Restart=on-failure`, `EnvironmentFile=.env`, `loginctl enable-linger`.
  - WeasyPrint's system library pango, installed with the owner's OK (present on this host).
  - `udr backup`: SQLite online backup of both DBs, plus an archive of
    `data/{runs,briefs,templates,denylist.txt}` into `data/backups/<ts>/`. Keeps the last 7; the
    restore procedure is documented.
  - README: SSH tunnel (`ssh -L 8540:127.0.0.1:8540 -L 8541:127.0.0.1:8541 <host>`), MCP client
    snippet for Claude Code, start/stop/logs.
  - An updated [docs/architecture.md](docs/architecture.md).
- **Acceptance criteria:**
  1. After `systemctl --user restart udr-*`, all three units are active and `/v1/health` returns
     200 with green checks.
  2. After a host reboot, the units are up without a login (manual check, recorded).
  3. An automated test restores a backup into a temp dir and gets the same run list.
  4. `ss -ltnp` shows 8540, 8541 and 11436 bound to `127.0.0.1` only.
- **Edge cases:**
  - `udr-ollama` fails: the API still starts and health reports `degraded`.
  - Backup during a run: kept consistent by the online backup API.
  - Free disk under 10 GB: a health warning.
- **Dependencies:** M6, M7.

## 5. Open risks & assumptions

| # | Risk | Mitigation | Revisit when |
|---|---|---|---|
| R1 | Local 27B is far below Opus in drafting and critique | AD1–AD5 put the structure in code; ship gate; section-wise generation | Reference runs stay `blocked` after 3 fix rounds |
| R2 | LFM-1.2B produces weak or unfaithful claims | Verbatim quote check; drop counter | `claims_drop_rate` > 30 % → switch `extract` to the `summarize` model |
| R3 | Sanitizer misses a confidential term | Denylist (hard), plan review, outbound log | Any confidential term appears in `outbound.jsonl` |
| R4 | DuckDuckGo blocks the unofficial API | Backoff; gaps documented; run continues | > 20 % of fallback queries fail |
| R5 | Tavily free plan (1000/month) covers only ~3 Full runs | Local-first fetch; per-run cap; automatic ddgs switch | Limit reached before day 20, twice |
| R6 | The summarizer's auto-pinned daemon lands on our GPU | `UDR_OLLAMA_GPU`; doctor warns about foreign processes | Slowdown visible in step telemetry |
| R7 | qwen does not fit 32k context on 24 GB | Measured calibration (§3.1) | Calibrated `num_ctx` < 16384 |
| R8 | A Full run exceeds 4 h | Profile recalibration (M9) | Reference run > 4.5 h |
| R9 | Output drifts to English | Language in the brief; G12; section redraft | G12 failures on reference runs |
| R10 | Prompt injection via fetched pages | AD7 fencing; no tool calls from fetched text | A review finding |
| R11 | JS-heavy pages burn extract credits | Tavily Extract only as fallback | Extract > 30 % of a run's credits |
| R12 | OpenAlex/Crossref rate limits | Polite pool (`mailto`); 1 req/s per host | HTTP 429 in the logs |
| R13 | `qwen3.8-27b` is a custom tag | Tags live only in env | Doctor reports the tag missing |
| R14 | weasyprint pulls in `pyphen` (MPL tri-licence) and `pillow` (MIT-CMU) | Owner exception 2026-10-02 (AGENTS.md §5.5) | Licence policy changes |

**Adopted without an explicit owner decision:**
- A1: The GUI is Streamlit and German only (house style, global Streamlit rules).
- A2: Ports: GUI 8540, API/MCP 8541, own Ollama 11436. All are free; 8540 is reserved for a new app
  in `local_app-orchestrator`.
- A3–A9:
  - one active Phase-2 run at a time;
  - the GUI gets its own self-approve key;
  - inline `[N]` citations only;
  - local-first fetch;
  - `reason` doubles as the sanitizer;
  - built-in template headings are German;
  - this PRD is in English.

## 6. Definition of done

- [ ] M1–M10 `done` in [IMPLEMENTATION.md](IMPLEMENTATION.md); `pre-commit run --all-files` green.
- [ ] German **Lite** and **Full** reference runs pass the ship gate, with Full at about 4 h.
      Wall time, credits, sources and drop rate are recorded.
- [ ] A Lite run completes via REST (curl script) and via MCP (Claude Code). This holds both with
      a self-approve key and with a non-self-approve key approved in the GUI.
- [ ] No reference run's `outbound.jsonl` contains a denylisted term, and Phase 1 logs zero
      outbound requests.
- [ ] Every report carries the brief byte-identical (G8), and all its citations resolve (G5).
- [ ] Services come back after a reboot, bound to loopback only. Backup and restore are verified.
- [ ] README, IMPLEMENTATION.md and docs/ describe the actual system.
