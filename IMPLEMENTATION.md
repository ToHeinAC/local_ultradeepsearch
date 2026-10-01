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

## 2. Phase status

One row per PRD milestone. Status: `planned`, `in progress`, `done`.

| Phase | Milestone | Status | Verified by |
|---|---|---|---|
| 0 | Blueprint skeleton (no PRD milestone) | done | full gate green |
| 1 | M1: Foundation and model infrastructure ([plan](docs/plans/m1-foundation.md)) | done | 209 offline tests, 99 % branch coverage; live: `udr doctor --calibrate` and `pytest -m live` (6 passed), see [docs/ollama-runtime.md](docs/ollama-runtime.md) |
| 2 | M2: Outbound gateway and retrieval adapters ([plan](docs/plans/m2-outbound.md)) | done | AC1–AC7 offline: `test_gateway.py`, `test_denylist.py`, `test_guard.py`, `test_outbound_infra.py`, `test_egress_guard.py`; 449 offline tests, 98 % branch coverage. Live outbound check not yet run, see §4 |
| 3 | M3: Per-run source vault and fetch pipeline | planned | fixture-corpus, quote-verification, isolation tests |
| 4 | M4: Phase 1 — clarification, uploads, brief | planned | brief-graph, hash-approval, zero-outbound tests |
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
| `src/app/prompts/` | Prompt strings as named constants: `llm.py` (repair, calibration probe), `outbound.py` (sanitizer). |
| `src/app/events.py` | `EventSink` protocol, thread-safe `JsonlEventSink`, `MemoryEventSink`. |
| `src/app/artifacts.py` | Atomic `write_text`/`write_json` that strip `<think>`; `scrub=False` for byte-exact files. |
| `src/app/calibration.py` | Measures the largest `reason` context that fits VRAM; `calibration.json` I/O. |
| `src/app/doctor.py` | Pure checks over a `DoctorSnapshot`; exit code and rendering. |
| `src/app/bootstrap.py` | Composition root: settings → own instance → wired `Runtime`; doctor snapshot; calibrate; `build_providers` / `build_gateway` for a run. |
| `src/app/cli.py` | `udr` command (`doctor`, `denylist`). Entry point `udr = app.cli:main`. |
| `src/app/adapters/ollama_transport.py` | Chat via the `ollama` client and read-only status probes. Loopback only. |
| `src/app/adapters/ollama_instance.py` | Adopt, start or fail open our own Ollama daemon on `:11436`. See [docs/ollama-runtime.md](docs/ollama-runtime.md). |
| `src/app/adapters/system_probe.py` | `nvidia-smi`, free disk, binary lookup, model-store discovery. |
| `src/app/adapters/outbound/` | The only egress: denylist, private-URL guard, sanitizer, outbound log, credit ledgers, throttle, Tavily / ddgs / OpenAlex / Crossref / arXiv clients, HTTP fetch and HTML/PDF extraction, and the run-scoped `OutboundGateway`. See [docs/outbound.md](docs/outbound.md). |
| `tests/support.py` | `make_settings()` (`Settings` that ignore any developer `.env`) and `make_pdf()` (synthetic PDFs). |
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
- The live outbound check (`tests/live/test_live_outbound.py`) has not been run: the run was
  declined in the session that built M2. Tavily is only exercised there with `TAVILY_API_KEY` in
  this project's `.env`. `OPENALEX_API_KEY` is supported but not yet configured (the owner adds
  it later); OpenAlex works without it at a lower rate budget.
- The calibration candidates stop at 32768 and that value fits on this host, so a larger window is
  untested. Raise the candidate list only if a PRD change asks for it.
- A daemon started by `udr doctor` outlives the command by design (adopted next time). Until the
  systemd unit of M10 exists it must be stopped by hand; see [docs/ollama-runtime.md](docs/ollama-runtime.md).
