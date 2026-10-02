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
| Ingest sources of a run (library call; the CLI and graphs arrive later) | `bootstrap.build_pipeline(rt, run_id, tier="light", focus=...)`, then `pipeline.resume()` and `pipeline.ingest_many(items)` |

## 2. Phase status

One row per PRD milestone. Status: `planned`, `in progress`, `done`.

| Phase | Milestone | Status | Verified by |
|---|---|---|---|
| 0 | Blueprint skeleton (no PRD milestone) | done | full gate green |
| 1 | M1: Foundation and model infrastructure ([plan](docs/plans/m1-foundation.md)) | done | 209 offline tests, 99 % branch coverage; live: `udr doctor --calibrate` and `pytest -m live` (6 passed), see [docs/ollama-runtime.md](docs/ollama-runtime.md) |
| 2 | M2: Outbound gateway and retrieval adapters ([plan](docs/plans/m2-outbound.md)) | done | AC1–AC7 offline: `test_gateway.py`, `test_denylist.py`, `test_guard.py`, `test_outbound_infra.py`, `test_egress_guard.py`; 449 offline tests, 98 % branch coverage. Live outbound check not yet run, see §4 |
| 3 | M3: Per-run source vault and fetch pipeline ([plan](docs/plans/m3-source-vault.md)) | done | AC1–AC6 offline: `test_fetch_pipeline.py` (20-URL corpus, in-process crashes, real SIGKILL), `test_store.py`, `test_extraction.py`, `test_dedup.py`, `test_scoring.py`; 809 offline tests. Live check not yet run, see §4 |
| 4 | M4: Phase 1 — clarification, uploads, brief ([plan](docs/plans/m4-brief.md)) | done | AC1–AC8 offline: `test_brief_*.py` (render, store, uploads, digest, interview, graph, service, console), `test_documents.py`, `test_cli_brief.py`, a real SIGKILL in `test_brief_service.py`, zero-outbound in `test_egress_guard.py`; 1396 offline tests, 98 % branch coverage. Live check not yet run, see §4 |
| 5 | M5: Lite end to end, plan gate, templates, ship gate, export | planned | light step-sequence, G1–G12 fixtures; live Lite run |
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
| `src/app/bootstrap.py` | Composition root: settings → own instance → wired `Runtime`; doctor snapshot; calibrate; `build_providers` / `build_gateway` for a run (restores the run's spent credits); `run_dir`, `open_vault`, `build_pipeline`. |
| `src/app/cli.py` | `udr` command (`doctor`, `denylist`, `brief`). Entry point `udr = app.cli:main`. |
| `src/app/adapters/ollama_transport.py` | Chat via the `ollama` client and read-only status probes. Loopback only. |
| `src/app/adapters/ollama_instance.py` | Adopt, start or fail open our own Ollama daemon on `:11436`. See [docs/ollama-runtime.md](docs/ollama-runtime.md). |
| `src/app/adapters/system_probe.py` | `nvidia-smi`, free disk, binary lookup, model-store discovery. |
| `src/app/adapters/outbound/` | The only egress: denylist, private-URL guard, sanitizer, outbound log, credit ledgers, throttle, Tavily / ddgs / OpenAlex / Crossref / arXiv clients, HTTP fetch and HTML/PDF extraction, and the run-scoped `OutboundGateway`. See [docs/outbound.md](docs/outbound.md). |
| `src/app/templates.py`, `templates/` | Report templates (front matter plus one H2 per section), validation, the five built-ins. |
| `src/app/documents.py` | PDF text and page images (greyscale PNG), DOCX, text decoding; no network. |
| `src/app/brief/` | Phase 1: render, parse and hash the brief, interview, uploads, digest, `BriefService`, the terminal loop. See [docs/brief.md](docs/brief.md). |
| `src/app/graphs/` | The only place that imports LangGraph: the `brief` graph, `BriefRunner` (synchronous checkpoints) and the checkpointer. |
| `src/app/store/` | The run-scoped SQLite vault (migrations, notes, claims, rejections, FTS5 search, stats; see [docs/vault.md](docs/vault.md)), and the Phase-1 `sessions.py` and `runs.py` (sessions, uploads, approved runs). |
| `src/app/pipeline/` | Ingestion: `fetch.py` (`FetchPipeline`, resume), URL canonicalising, junk gates, MinHash near-duplicates, claim extraction, long-source analysis, scoring, note files and run stats, profile and source-strategy loaders. See [docs/vault.md](docs/vault.md). |
| `config/` | `profiles.toml` (per-tier budgets) and `source_strategies.toml` (tier weights, host rules, domain sections). |
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
- The live outbound check (`tests/live/test_live_outbound.py`) has not been run: the run was
  declined in the session that built M2. Tavily is only exercised there with `TAVILY_API_KEY` in
  this project's `.env`. `OPENALEX_API_KEY` is supported but not yet configured (the owner adds
  it later); OpenAlex works without it at a lower rate budget.
- The M3 pipeline has not run against the real `extract` model: `claims_drop_rate` on real pages is
  unmeasured. Above 0.30 is PRD risk R2 (propose `UDR_MODEL_EXTRACT` = a gemma model). The check
  needs the network and our Ollama, so it waits for the owner's go-ahead.
- Tavily's provider switch and an interrupted outbound log line are not fully durable across a
  restart; see [docs/vault.md](docs/vault.md) (Resuming).
- M4 builds the template loader and the four built-in templates that PRD M5 lists, because the
  template is chosen in Phase 1 and written into the brief. M5 uses them.
- M4 differs from its plan in three places: the session row also holds the current brief text and
  hash, so an approval is checked in the database; events go to `data/events.jsonl`, not to a file
  per session; and the model errors of a call are returned in the view instead of raised.
- The Phase-1 pieces have not run against the real models. The check needs our Ollama (`reason`,
  `summarize`, `ocr`) and a PDF with a scanned page; it waits for the owner's go-ahead. The first
  real session decides whether the `assess` prompt asks few enough questions and whether the
  `deepseek-ocr` prompt (`Free OCR.`) is the right one.
- `udr brief` adds files only at the start; `BriefService.add_files` also works while questions are
  open, for the GUI and the API.
- `orjson` (MPL-2.0 part), a hard dependency of LangGraph, was accepted as a transitive exception
  on 2026-10-02 ([AGENTS.md](AGENTS.md) §5.5).
- The calibration candidates stop at 32768 and that value fits on this host, so a larger window is
  untested. Raise the candidate list only if a PRD change asks for it.
- A daemon started by `udr doctor` outlives the command by design (adopted next time). Until the
  systemd unit of M10 exists it must be stopped by hand; see [docs/ollama-runtime.md](docs/ollama-runtime.md).
