# IMPLEMENTATION

Current state of the code, and the only place for phase status. What and why: [PRD.md](PRD.md).
Rules: [AGENTS.md](AGENTS.md). Design: [docs/architecture.md](docs/architecture.md).

## 1. Run and verify

| Task | Command |
|---|---|
| Install (once per clone) | `uv sync && uv run pre-commit install` |
| Tests (fast loop) | `uv run pytest` or `uv run pytest tests/test_service.py` |
| Full gate | `uv run pre-commit run --all-files` |
| Live tests (real Ollama and services, not in the gate) | `uv run pytest -m live` (outbound only: `uv run pytest -m live tests/live/test_live_outbound.py`) |
| Machine check | `uv run udr doctor` (`--json`; `--calibrate` measures the reason context) |
| Denylist | `uv run udr denylist add <term>...`, `remove <term>...`, `list` |
| Phase 1: clarify and approve a brief | `uv run udr brief "<question>" [--file F]...`, `--list`, `--session <id>` (see [docs/brief.md](docs/brief.md)) |
| Phase 2: start or continue a run, review its plan | `uv run udr run <run_id> [--approve-plan <sha> \| --no-input]`, `uv run udr run --brief F --tier light --template ID` (see [docs/research.md](docs/research.md)) |
| Ingest sources of a run (library call; the CLI and graphs arrive later) | `bootstrap.build_pipeline(rt, run_id, tier="light", focus=...)`, then `pipeline.resume()` and `pipeline.ingest_many(items)` |

## 2. Phase status

One row per PRD milestone. Status: `planned`, `in progress`, `done`.

| Phase | Milestone | Status | Verified by |
|---|---|---|---|
| 0 | Blueprint skeleton (no PRD milestone) | done | full gate green |
| 1 | M1: Foundation and model infrastructure ([plan](docs/plans/m1-foundation.md)) | done | 209 offline tests, 99 % branch coverage; live: `udr doctor --calibrate` and `pytest -m live` (6 passed), see [docs/ollama-runtime.md](docs/ollama-runtime.md) |
| 2 | M2: Outbound gateway and retrieval adapters ([plan](docs/plans/m2-outbound.md)) | done | AC1–AC7 offline: `test_gateway.py`, `test_denylist.py`, `test_guard.py`, `test_outbound_infra.py`, `test_egress_guard.py`; 449 offline tests, 98 % branch coverage; live: `tests/live/test_live_outbound.py` (9 passed), see §4 |
| 3 | M3: Per-run source vault and fetch pipeline ([plan](docs/plans/m3-source-vault.md)) | done | AC1–AC6 offline: `test_fetch_pipeline.py` (20-URL corpus, in-process crashes, real SIGKILL), `test_store.py`, `test_extraction.py`, `test_dedup.py`, `test_scoring.py`; 809 offline tests. Live: `test_live_vault.py` ran, drop rate 0.75 (above R2), see §4 |
| 4 | M4: Phase 1 — clarification, uploads, brief ([plan](docs/plans/m4-brief.md)) | done | AC1–AC8 offline: `test_brief_*.py` (render, store, uploads, digest, interview, graph, service, console), `test_documents.py`, `test_cli_brief.py`, a real SIGKILL in `test_brief_service.py`, zero-outbound in `test_egress_guard.py`; 1396 offline tests, 98 % branch coverage. live: `tests/live/test_live_brief.py` ran, see §4 |
| 5 | M5: Lite end to end, plan gate, templates, ship gate, export ([plan](docs/plans/m5-lite.md)) | done | offline: every step, the gate G1–G12 with fixtures, fix rounds, export, a whole Lite run on fakes, the real composition (`test_bootstrap_research.py`), `udr run` (`test_cli_run.py`) and two real SIGKILLs (`test_research_crash.py`); live AC8: a German Lite run passed the gate in 52 min, see §4 |
| 6 | M6: Service — REST, MCP, worker | planned | route auth table, crash-resume, in-process MCP tests |
| 7 | M7: GUI (Streamlit, German) | planned | import scan, AppTest, safe-exit tests |
| 8 | M8: Full tier — analysis steps 3–9 | planned | invariant tests, investigator caps, schema tests |
| 9 | M9: Full tier — drafting and review, calibration | planned | full step-sequence, patch-engine tests; live Full run |
| 10 | M10: Operations | planned | backup/restore test; manual reboot and bind checks |

## 3. Module map

| Module | Responsibility |
|---|---|
| `src/app/config.py` | `Settings` (`UDR_*` env, `.env`) and `is_loopback_url`. Ollama URLs must be loopback. |
| `src/app/llm/` | Role registry, typed requests, the `LLMService`, structured-output helpers, errors, `ScriptedTransport` fake. See [docs/llm-layer.md](docs/llm-layer.md). |
| `src/app/prompts/` | Prompt strings as named constants: `llm.py` (repair, calibration probe), `outbound.py` (sanitizer), `untrusted.py` (fencing of fetched and uploaded text), `notes.py` (extraction, summary merge, source analysis), `brief.py` (interview, drafting, tier, uploads). A test forbids digits outside placeholders. |
| `src/app/events.py` | `EventSink` protocol, thread-safe `JsonlEventSink`, `MemoryEventSink`. |
| `src/app/artifacts.py` | Atomic, thread-safe `write_text`/`write_json` that strip `<think>`; `scrub=False` for byte-exact files. |
| `src/app/telemetry.py` | Forces LangSmith tracing off; `app.graphs` calls it before LangGraph loads. |
| `src/app/text.py` | `normalize_for_match` and the verbatim-quote check `contains_quote` (also used by the ship gate later). |
| `src/app/calibration.py` | Measures the largest `reason` context that fits VRAM; `calibration.json` I/O. |
| `src/app/doctor.py` | Pure checks over a `DoctorSnapshot`; exit code and rendering. |
| `src/app/bootstrap.py` | Composition root: settings → own instance → wired `Runtime`; doctor snapshot; calibrate; `build_providers` / `build_gateway` for a run (restores the run's spent credits); `run_dir`, `open_vault`, `build_pipeline`; `build_brief_service` and `build_research_service` (the per-run context, the step dependencies from the model registry, the worker lock). |
| `src/app/cli.py` | `udr` command (`doctor`, `denylist`, `brief`, `run`). Entry point `udr = app.cli:main`. |
| `src/app/adapters/ollama_transport.py` | Chat via the `ollama` client and read-only status probes. Loopback only. |
| `src/app/adapters/ollama_instance.py` | Adopt, start or fail open our own Ollama daemon on `:11436`. See [docs/ollama-runtime.md](docs/ollama-runtime.md). |
| `src/app/adapters/system_probe.py` | `nvidia-smi`, free disk, binary lookup, model-store discovery. |
| `src/app/adapters/outbound/` | The only egress: denylist, private-URL guard, sanitizer, outbound log, credit ledgers, throttle, Tavily / ddgs / OpenAlex / Crossref / arXiv clients, HTTP fetch and HTML/PDF extraction, and the run-scoped `OutboundGateway`. See [docs/outbound.md](docs/outbound.md). |
| `src/app/templates.py`, `templates/` | Report templates (front matter plus one H2 per section), validation, the five built-ins. |
| `src/app/documents.py` | PDF text and page images (greyscale PNG), DOCX, text decoding; no network. |
| `src/app/brief/` | Phase 1: render, parse and hash the brief, interview, uploads, digest, `BriefService`, the terminal loop. See [docs/brief.md](docs/brief.md). |
| `src/app/research/` | Phase 2 (M5): manifest and settings, `console.py` (the plan review of `udr run`), report rendering with code-owned citations, ship gate and its fixes, patch engine, decomposition, plan, sweep, evidence, drafting, polish, readability, export, worker lock, steps and service. See [docs/research.md](docs/research.md). |
| `src/app/graphs/research.py`, `src/app/adapters/pandoc.py`, `src/app/research/pdf.py` | The `research` graph and its runner; the only code that starts pandoc (DOCX only); the in-process PDF renderer. See [docs/research.md](docs/research.md). |
| `src/app/graphs/` | The only place that imports LangGraph: the `brief` graph, `BriefRunner` (synchronous checkpoints) and the checkpointer. |
| `src/app/store/` | The run-scoped SQLite vault (migrations, notes, claims, rejections, FTS5 search, stats; see [docs/vault.md](docs/vault.md)), and the Phase-1 `sessions.py` and `runs.py` (sessions, uploads, approved runs). |
| `src/app/pipeline/` | Ingestion: `fetch.py` (`FetchPipeline`, resume), URL canonicalising, junk gates, MinHash near-duplicates, claim extraction, long-source analysis, scoring, note files and run stats, profile and source-strategy loaders. See [docs/vault.md](docs/vault.md). |
| `config/` | `profiles.toml` (per-tier budgets) and `source_strategies.toml` (tier weights, host rules, domain sections). |
| `tests/research_rig.py`, `tests/research_run_rig.py`, `tests/research_crash_child.py` | The Phase-2 fakes (gateway pieces, scripted models, pandoc), a whole Lite run on them, and the child process the Phase-2 SIGKILL tests kill. |
| `tests/brief_rig.py`, `tests/brief_crash_child.py` | The scripted model and the wired Phase-1 session; the child process the Phase-1 SIGKILL test kills. |
| `tests/test_layer_rules.py` | Enforces that LangGraph is imported only in `src/app/graphs/`. |
| `tests/support.py` | `make_settings()` (`Settings` that ignore any developer `.env`), `make_pdf()` (synthetic PDFs) and `make_note()`. |
| `tests/fixtures_corpus.py`, `tests/crash_child.py` | The 20-URL corpus, fake fetcher and fake model; the child process the SIGKILL test kills. |
| `tests/conftest.py` | Autouse fixtures: block sockets (except `live` tests), scrub `UDR_*` env. |
| `tests/live/` | Real-model checks, marked `live`, excluded from the gate. |
| `tests/test_code_rules.py` | Enforces functions ≤ 50 lines in `src/`, `tests/`, `.claude/hooks/`. |
| `tests/test_py311_syntax.py` | Enforces that all code parses as Python 3.11 (CI runs 3.11). |
| `tests/test_egress_guard.py` | Enforces that network-capable modules are imported only in the outbound package and the Ollama transport. |
| `tests/test_docs.py` | Enforces doc size limits and resolvable local links. |
| `.claude/hooks/format_on_edit.py` | PostToolUse hook: ruff-formats each `.py` file Claude edits. |
| `.claude/hooks/stop_gate.py` | Stop hook: runs the gate if `.py` files changed; blocks the stop on failure. |

## 4. Open issues

- M5: the PDF is made in-process with `reportlab` (BSD) and `markdown-it-py` (MIT),
  so no GPL, LGPL or MPL package is involved any more. `reportlab` brings `pillow` (MIT-CMU), an
  exception the owner accepted on 2026-10-04 ([AGENTS.md](AGENTS.md) §5.5). The PDF text uses the Bitstream Vera
  fonts that ship with ReportLab: Latin text is fine, a character outside Vera is shown as `?`.
  `uv add` also rewrote `uv.lock` in lock revision 2 (a newer uv), which is why that diff is large.
  `include_domains` hints of the domain strategies are not passed to Tavily yet: they would
  restrict results, so M5 only ranks authoritative hosts first.

- M5 live Lite run (AC8, 2026-10-05, run `r-20261005-112922-c2e21d`, German brief on heat pumps in
  existing buildings, template `auto`, no Tavily key so search ran on ddgs): the ship gate passed in
  52 min with no fix round and no warning. 62 sources and 6 source analyses, 423 claims kept and
  96 dropped (drop rate 0.18; one batch of 48 had 0.35 and raised `extract_quality_low`), 0 Tavily
  credits, 251 model calls (5 length retries). The report has 3543 words in 4 sections; the PDF is
  made, the DOCX failed because pandoc is not installed on this host. Time: decomposition 21 min,
  search plan 7, sweep 17, the rest 7. Findings:
  1. The section headings are the research questions cut at 80 characters, mid-word ("… in
     deutschen Bestan"): `clean_headings` fell back to them (`decompose.py`).
  2. Step 1 is slow: `reason` spends its whole 8192-token output on thinking, is retried with
     16384 and takes about 3.7 min a call; the coverage loop reported gaps again.
  3. ddgs failed transiently in 17 of 36 searches (3 queries gave nothing; DNS errors for a
     Wikipedia backend). Of 95 fetches, 33 were rejected (12 `http_403`, 15 `empty_text`).
  4. Source quality is thin on the first factual question: commercial heat pump guides carry the
     average annual performance factor, and two of the sources are US (NREL) documents for a
     German question. Tavily and the domain strategies were not in play.
  The prompts of steps 1 to 16 now ran on real output; their quality beyond this one run is
  unmeasured.
- `udr run` does not apply a run's `summarize_model` choice; the `summarize` role uses the registry's
  model for every run.
- Run events go to `data/events.jsonl` with the model telemetry, not to a file per run.
- PRD wording differs from the code in these places and needs the owner's approval to change:
  1. §3.1 and R6 say `UDR_OLLAMA_GPU`; the variable is `UDR_OWN_OLLAMA_GPU`.
  2. R6 says the doctor "warns about foreign processes"; it compares used VRAM with what our own
     instance reports loaded, because our daemon and the summarizer's are both named `ollama`.
  3. M1 lists a "default `think`" per role; `think` is a per-call argument (default `False`).
  4. §3.3 says Tavily Extract runs "only if local extraction fails the junk gates". It also runs
     after network errors and HTTP error codes (bot walls); never for oversized or unsupported
     files. The full junk gates arrive with M3.
  5. §3.4 lists `tavily-python`; Tavily is called over REST with `httpx` instead, so status codes
     432/433 are handled directly.
  6. M3 compares MinHash signatures all-pairs instead of through an LSH index, and `datasketch` is
     pinned to one hash scheme. `Vault` is bound to one run id, and the near-duplicate index holds
     originals only. These are implementation choices inside the PRD's "MinHash, threshold 0.6".
- The live outbound check (`tests/live/test_live_outbound.py`) ran on 2026-10-05, verified by
  Tobias: 9 passed. Tavily is only exercised there with `TAVILY_API_KEY` in this project's `.env`;
  whether that test ran or was skipped was not checked. `OPENALEX_API_KEY` is supported but not yet
  configured (the owner adds it later); OpenAlex works without it at a lower rate budget.
- M3 live check (`tests/live/test_live_vault.py`, 2026-10-05, 2 Wikipedia pages, one run per
  model): `claims_drop_rate` was 0.75 with `lfm2.5-1.2b` (above the 0.30 of PRD risk R2), 0.41 with
  `gemma4:e2b`, 0.34 with `gemma4:e4b` and 0.30 with `gemma4:26b`. Per R2, `extract` now uses the
  `summarize` model on the shared daemon ([docs/ollama-runtime.md](docs/ollama-runtime.md)). Most
  drops are near-verbatim quotes (stitched or reworded passages). Ignoring trailing punctuation and
  `<sup>[n]</sup>` markers would give 0.29 (`e2b`), 0.32 (`e4b`) and 0.25 (`26b`); that loosens
  the verbatim gate (also used by the ship gate), so it needs the owner's decision. Plain-prose
  pages are untested.
- Tavily's provider switch and an interrupted outbound log line are not fully durable across a
  restart; see [docs/vault.md](docs/vault.md) (Resuming).
- M4 builds the template loader and the four built-in templates that PRD M5 lists, because the
  template is chosen in Phase 1 and written into the brief. M5 uses them.
- M4 differs from its plan in three places: the session row also holds the current brief text and
  hash, so an approval is checked in the database; events go to `data/events.jsonl`, not to a file
  per session; and the model errors of a call are returned in the view instead of raised.
- M4 live check (`tests/live/test_live_brief.py`, 2026-10-05, real `reason`, `summarize` and `ocr`;
  a question plus a PDF with one text and one scanned page; 15 min): the session reached the
  decision. Findings:
  1. `deepseek-ocr` with `Free OCR.` reads the page but adds markup tokens and one invented line
     ("Mit freundlichen Grüßen"). The markup made `summarize` ignore the page; `clean_ocr` now strips
     it and the page-2 fact is kept in 3 of 3 runs. The invented line remains. `Extract all text
     from this image.` hallucinated a whole letter in one run, so the prompt stays `Free OCR.`.
  2. The `assess` prompt asked too much: 5, 1, 3, 2 and 1 questions in five rounds (the maximum), the
     checklist flipped between rounds and `scope` came up four times. Now the prompt forbids
     questions about output and depth, about clear or answered items and compound questions, and
     code keeps only questions about `missing` items never asked about before. Measured over 3
     cases × 3 runs (`assess` alone, every candidate accepted): questions 10.6 → 6.2 (prompt) → 5.8
     (`missing` only) → 4.0 (never twice); rounds with questions 4.4 → 4.0 → 3.4 → 2.0; `output`
     questions 22 → 0. A vague answer is no longer followed up: the item stays `missing`, so the
     brief lists it as not clarified.
  3. The drafted proposals held factual slips (spent fuel called "abgereichert", Asse named as a
     destination for it). The test accepted every proposal, so it shows model quality, not a defect
     of the code.
- `udr brief` adds files only at the start; `BriefService.add_files` also works while questions are
  open, for the GUI and the API.
- `orjson` (MPL-2.0 part), a hard dependency of LangGraph, was accepted as a transitive exception
  on 2026-10-02 ([AGENTS.md](AGENTS.md) §5.5).
- The calibration candidates stop at 32768 and that value fits on this host, so a larger window is
  untested. Raise the candidate list only if a PRD change asks for it.
- A daemon started by `udr doctor` outlives the command by design (adopted next time). Until the
  systemd unit of M10 exists it must be stopped by hand; see [docs/ollama-runtime.md](docs/ollama-runtime.md).
