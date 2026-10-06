# M6 implementation plan — Service: REST, MCP and worker

**Status:** ready to execute; D1–D11 confirmed by the owner on 2026-10-06.
**Executor:** Sonnet 5.5. Follow [AGENTS.md](../../AGENTS.md) for every step: red → green → gate →
commit (one commit per step, imperative message). Push only when the owner asks. Report the red and
the green result of every step in its commit message body.

## Context

M1–M5 are done (see [IMPLEMENTATION.md](../../IMPLEMENTATION.md)). Phase 1 (`BriefService`,
`src/app/brief/service.py`) and Phase 2 (`ResearchService`, `src/app/research/service.py`) work from
the CLI. M6 puts both behind a FastAPI app on `127.0.0.1:8541` (REST `/v1`, MCP `/mcp`), adds API
keys, and moves Phase-2 execution into a separate worker process. Requirement source:
[PRD.md](../../PRD.md) §3.2 ("API approval"), §3.11 and §4 M6.

What exists and must be reused, not rewritten:

| Piece | Where | Note |
|---|---|---|
| Phase-1 service | `brief/service.py` `BriefService` | `start`, `add_files`, `answer`, `choose_offer`, `revise`, `edit`, `set_settings`, `save`, `approve`, `get`, `list_sessions`, `retry`, `recover`; a lock per session; `_run` wraps every graph call |
| Phase-2 service | `research/service.py` `ResearchService` | `create_external_run`, `run`, `plan_text`, `update_plan`, `approve_plan`, `view`; `_guarded` turns a step exception into `failed` |
| Worker slot | `research/worker.py` `WorkerLock` | `fcntl` lock on `data/worker.lock`; `WorkerBusy` when held |
| Run rows | `store/runs.py` `RunStore`, `RUN_STATUSES`, `_ALLOWED_STATUS` | migrations in `store/db.py` (`MIGRATIONS`, currently 3) |
| Graph runner | `graphs/research.py` `ResearchRunner` | `start`, `resume`, `proceed`, `snapshot`; LangGraph only in `graphs/` |
| Composition | `bootstrap.py` | `build_runtime`, `build_brief_service`, `build_research_service`, `_build_run_context`, `build_pipeline` |
| Fakes | `tests/research_rig.py`, `tests/research_run_rig.py`, `tests/brief_rig.py` | whole Lite run and Phase-1 session on scripted models |

## Owner decisions (2026-10-06), binding for this plan

- **D1 — Worker process.** Phase 2 runs in its own process, `udr worker`. The API process (`udr
  serve`) never executes a research graph. The queue is the `runs` table. M10 gets two systemd
  units. The crash test (AC5) kills a real worker process.
- **D2 — Cancel granularity.** `cancel` takes effect at the next node boundary *and* inside long
  steps: between the queries of the sweep (both waves) and between the sections of the draft.
- **D3 — `udr run` stays direct.** The CLI keeps executing in its own process without a server;
  `WorkerLock` keeps one run active ("Ein anderer Lauf ist aktiv."). No CLI rework beyond D9/D10.
- **D4 — Approval without the GUI.** As PRD AC2: an item created by a key without `self_approve`
  waits until a key *with* `self_approve` approves it. No `udr approve` command.
- **D5 — Phase 1 is asynchronous over the API.** `POST /sessions`, `…/uploads`, `…/messages`,
  `…/revise`, `PUT …/settings`, `PUT …/brief`, `POST …/save`, and the MCP clarification tools
  return at once (HTTP 202 with the session view, `busy: true`). The model work runs in a thread
  pool in the API process (D7). Clients poll `GET /sessions/{id}`.
- **D6 — Run events in a file per run.** `data/runs/<id>/events.jsonl`, in addition to the global
  `data/events.jsonl`. Every event emitted while a run executes (model telemetry included) carries
  the `run_id` and lands in both. Closes the open issue "Run events go to `data/events.jsonl`".
- **D7 — Phase-1 work runs in the API process**, never in the worker, so an interview does not
  wait for a 50-minute run (the `reason` calls of both only share the GPU).
- **D8 — `udr apikey create | list | revoke`.** `list` shows id, name, flag, created and revoked
  dates, never a key. A revoked key gets 401.
- **D9 — A run applies its `summarize_model`.** The `summarize` role *and* the `extract` role of
  that run use it (extract follows the summarize model, PRD R2). A run whose model differs from
  `UDR_MODEL_SUMMARIZE` emits a `summarize_model_override` warning event with the model name.
  Closes the open issue "`udr run` does not apply a run's `summarize_model` choice".
- **D10 — Tiers before M8.** `tier: full` → 422 "Full-Tier ab M8" (as `udr run`); `tier: auto` on
  `POST /runs` → `light`, with a `tier_auto_resolved` event. (A session approval keeps today's rule:
  `auto` takes the session's recommendation; if that is `full`, it is 422 as well.)
- **D11 — Uploads.** REST takes `multipart/form-data`; the MCP tools are text only.

Adopted without a separate question (challenge if wrong):
- **A1** `POST /runs/{id}/approve {brief_sha256}` is added (self-approve keys only). AC2 needs a
  way to approve a run that a non-self-approve key created; the PRD route table has none.
- **A2** Two CLI commands, `udr serve` (API, uvicorn on `127.0.0.1:${UDR_API_PORT:-8541}`) and
  `udr worker`. The host is fixed to `127.0.0.1`; `Settings` rejects anything else.
- **A3** Every route needs a key, `/health` included (AC1 says "every route"). The interactive docs
  (`/docs`, `/redoc`) and `/openapi.json` are switched off; AC7 is tested via `app.openapi()`.
- **A4** `DELETE /runs/{id}` removes the run row, the run's vault rows (`notes`, `notes_fts`,
  `claims`, `rejected_sources`, `searches`), its checkpoint thread, `data/runs/<id>/` and, for a
  run from a session, `data/uploads/<session_id>/` with its `uploads`/`upload_parts` rows. The
  archived brief in `data/briefs/` stays (it is a read-only record).
- **A5** New numbers live in `config/profiles.toml` `[service]` (PRD AD3): `worker_poll_s = 2`,
  `sse_poll_s = 1`, `session_threads = 2`, `summarize_models = ["gemma4:e4b", "gemma4:e2b"]` (the
  choices `summarize_model` accepts; anything else is 422).
- **A6** Errors map to HTTP as: `NotFound` 404; `StaleBrief`, `StalePlan`, `WrongState`,
  `PlanBlocked`, report not ready, concurrent approval 409; `InvalidInput`, `BriefParseError`,
  `TierNotAvailable`, `TemplateError` 422; missing/invalid/revoked key 401; approval without
  `self_approve` 403. Body: `{"detail": "<message>", "status": "<run or session status, if any>"}`.

## Target design

```
udr serve  (API process)                         udr worker  (worker process)
  FastAPI /v1 + FastMCP /mcp                       loop: recover → pick next run → run it
  auth (bearer key)                                 ResearchService.run(run_id) under WorkerLock
  Facade ──► BriefService (+ SessionJobs pool)      cancel flag read between queries/sections
         └─► ResearchService (no graph execution:   per-run events file
             create, view, plan edit, approve →
             status `queued` + pending plan hash)
                        ▲                     ▲
                        └──── data/udr.sqlite (WAL) + data/checkpoints.sqlite ────┘
```

New run statuses and transitions (`store/runs.py`):

| From | To |
|---|---|
| `awaiting_brief_approval` (new) | `queued`, `cancelled` |
| `created` | `queued`, `running`, `failed` |
| `queued` | `running`, `failed`, `cancelled` |
| `running` | `awaiting_plan_approval`, `done`, `blocked`, `failed`, `cancelled`, `queued` (worker death, see Step 5) |
| `awaiting_plan_approval` | `running`, `queued` (plan approved, waiting for the worker), `failed`, `cancelled` |
| `failed` | `running`, `queued` (resume) |
| `cancelled` (new) | `queued` (resume) |
| `done`, `blocked` | nothing |

Modules (new unless marked):

| Module | Content |
|---|---|
| `src/app/api/__init__.py` | empty |
| `src/app/api/keys.py` | `KeyStore` (create, list, revoke, verify) over the `api_keys` table; `ApiKey` dataclass |
| `src/app/api/facade.py` | `Facade`: the service layer REST and MCP share. Only module the routes and tools call. |
| `src/app/api/jobs.py` | `SessionJobs`: thread pool for Phase-1 work (D5, D7) |
| `src/app/api/schemas.py` | Pydantic request/response models (OpenAPI) |
| `src/app/api/errors.py` | exception → HTTP mapping (A6) |
| `src/app/api/rest.py` | `build_app(facade, keys) -> FastAPI`; routers per group; auth dependency |
| `src/app/api/mcp.py` | `build_mcp(facade) -> FastMCP`; the 13 tools |
| `src/app/api/server.py` | `serve(settings)`: composition + uvicorn; the only `uvicorn` import |
| `src/app/worker.py` | `Worker`: queue loop, recovery, one run at a time (D1) |
| `src/app/events.py` (change) | `RunScopedEventSink` (D6) |
| `src/app/store/runs.py` (change) | statuses, cancel flag, pending plan hash, queue queries, delete |
| `src/app/research/service.py` (change) | approval without execution, cancel, resume, delete, report access |
| `src/app/brief/service.py` (change) | optional background executor for `_run` (D5) |
| `src/app/cli.py` (change) | `serve`, `worker`, `apikey create/list/revoke` |
| `tests/test_api_*.py`, `tests/test_worker*.py`, `tests/worker_crash_child.py` | tests |
| `docs/api.md` | the API, MCP, keys and worker for humans |

## Steps

Each step: write the tests first, run them, confirm they fail for the expected reason (red), write
the minimum code (green), run `uv run pre-commit run --all-files`, commit. Function ≤ 50 lines,
complexity ≤ 10, pyright strict, Python 3.11 syntax. Use the existing fakes; no real network or
model in the gate.

### Step 0 — Contract updates (docs only, one commit)

- PRD (owner-approved decisions D1–D11 only, keep ≤ 800 lines; check with `tests/test_docs.py`):
  - M6 deliverable: worker as its own process (`udr worker`), `udr serve`; `udr apikey create |
    list | revoke`; cancel also between sweep queries and draft sections; Phase-1 API calls return
    202 and work in the background; run events in `data/runs/<id>/events.jsonl`; route
    `POST /runs/{id}/approve` (A1); statuses `awaiting_brief_approval` and `cancelled`.
  - §3.1 model table / R2: a run's `summarize_model` also drives `extract`.
  - §3.11 CLI list: add `serve`, `worker`.
- IMPLEMENTATION.md: phase row 6 → `in progress`, link this plan. Commit: `docs: M6 contract and
  plan`.

### Step 1 — Dependencies and the layer rule

- `uv add fastapi uvicorn python-multipart mcp`. Then list every new package in `uv.lock` with its
  licence (`uv run python -c "import importlib.metadata as m; ..."` over the new names) and check
  them against AGENTS.md §5.5 (MIT, BSD, ISC, PSF, Apache-2.0). **Any other licence: stop and ask
  the owner** before continuing; record accepted exceptions in AGENTS.md §5.5.
- Before writing MCP code, read the current `mcp` SDK docs through context7 (`FastMCP`,
  `streamable_http_app`, `stateless_http`, `json_response`, `session_manager.run()` lifespan, the
  in-memory client `mcp.shared.memory.create_connected_server_and_client_session`). Do not code the
  MCP API from memory.
- Red: extend `tests/test_layer_rules.py`: `fastapi`, `starlette`, `uvicorn`, `mcp` may be imported
  only under `src/app/api/`. Add the matching detector test that feeds a violating source string
  (AGENTS.md §5.4: a guard needs a test with a violating input). Green: nothing to change yet (the
  rule holds); the violating-input test is the red one.
- Commit: `build: add FastAPI, uvicorn, python-multipart and mcp`.

### Step 2 — Store: migration 4, statuses, keys

- Migration 4 (`store/db.py`):
  - `ALTER TABLE runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;`
  - `ALTER TABLE runs ADD COLUMN pending_plan_sha256 TEXT;`
  - `CREATE TABLE api_keys (key_id TEXT PRIMARY KEY, name TEXT NOT NULL, key_sha256 TEXT NOT NULL
    UNIQUE, self_approve INTEGER NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT);`
  - `ALTER TABLE runs ADD COLUMN created_by TEXT;` (key id or NULL for CLI)
- `RunStore` (tests next to the existing `RunStore` tests; `grep -ln "RunStore(" tests/`):
  - `RUN_STATUSES` and `_ALLOWED_STATUS` per the table above.
  - `request_cancel(run_id) -> str` (returns the new status): `queued`, `awaiting_plan_approval`,
    `awaiting_brief_approval` → `cancelled` at once; `running` → sets `cancel_requested = 1`, status
    unchanged; `done`/`blocked`/`failed`/`cancelled` → `WrongState`.
  - `cancel_requested(run_id) -> bool`; `clear_cancel(run_id)`.
  - `set_pending_plan(run_id, sha256)`: one conditional `UPDATE … WHERE status =
    'awaiting_plan_approval' AND pending_plan_sha256 IS NULL`; 0 rows → `WrongState` (the second
    of two concurrent approvals gets 409). Sets status `queued` in the same statement.
  - `take_pending_plan(run_id) -> str | None` (read and clear in one transaction).
  - `next_runnable() -> RunRow | None`: first `running` row (an orphan of a dead worker), else the
    oldest `queued` by `created_at` (FIFO).
  - `list_runs(limit=100)` newest first; `delete_run(run_id)` per A4 (DB part only; refuse
    `running` with `WrongState`).
  - `create_external(…, status="queued" | "awaiting_brief_approval", created_by=…)`;
    `approve_external(run_id, brief_sha256)` → `queued`, `StaleBrief` on a wrong hash.
- `api/keys.py` `KeyStore` (tests `tests/test_api_keys.py`): key text `udr_` +
  `secrets.token_urlsafe(32)`; store only `sha256(key)`; `create(name, self_approve) -> (ApiKey,
  key_text)`; `verify(key_text) -> ApiKey | None` (None for unknown or revoked); `list()`;
  `revoke(key_id)` (unknown → `NotFound`). Test that the key text never appears in the database
  file bytes.
- Commit: `feat(store): run statuses for the service, cancel flag, API keys`.

### Step 3 — Per-run events (D6)

- `events.py`: `RunScopedEventSink(global_sink: JsonlEventSink)` with `bind(run_id, run_dir)` and
  `unbind()` (a context manager `bound(run_id, run_dir)` is fine). While bound, `emit` adds
  `run_id` to the data, writes to the global sink and appends the same line to
  `<run_dir>/events.jsonl`. Thread-safe (the fetch pipeline emits from a thread pool, so do **not**
  use contextvars: one run per process, a plain attribute under a lock). Unbound: global only.
- `bootstrap.build_runtime` wraps its sink in `RunScopedEventSink`; `ResearchService.run` and the
  plan-approval resume (Step 4) bind around graph execution.
- `read_run_events(run_dir, after: int) -> tuple[list[dict], int]` (pure helper, cursor = number of
  lines already read; a half-written last line is not returned).
- Tests: two threads emitting while bound → every line in both files, valid JSON, with `run_id`;
  unbound → no per-run file; the cursor skips a torn last line.
- Commit: `feat(events): a per-run event file while a run executes`.

### Step 4 — ResearchService for the worker and the API

Tests extend `tests/test_research_service.py` (whole Lite run on fakes via `research_run_rig.py`).

- `approve_plan(run_id, sha)` no longer executes: it checks (`_require_awaiting`,
  `check_approvable`), then `runs.set_pending_plan(run_id, sha)` → status `queued`. Returns the
  view.
- `run(run_id)`: accepts `queued`, `running` (orphan), `failed`, `created`. Under `WorkerLock` and
  with events bound: if the graph waits at the plan interrupt and `take_pending_plan` gives a hash →
  `runner.resume(run_id, {"action": "approve", "plan_sha256": sha})`; else start or proceed as
  today. Keep `awaiting_plan_approval`, `done`, `blocked`, `cancelled`, `awaiting_brief_approval`
  as no-ops that return the view.
- `udr run` (D3) keeps its behaviour: `_advance` in `cli.py` calls `approve_plan` then `run`.
  Update `tests/test_cli_run.py` only where the call order changes.
- Cancel (D2): `RunCancelled(Exception)` in `research/errors.py`. `RunContext` gets
  `should_stop: Callable[[], bool]` (reads `runs.cancel_requested`). Check it
  - in `ResearchSteps._step` before the work runs (node boundary);
  - in `research/sweep.py` before each query of both loops (lines ~172 and ~254 today) — pass it in
    via `SweepDeps.stop`;
  - in `research/draft.py` `Drafter.draft_all` before each section (loop at line ~107) — a `stop`
    argument.
  `_guarded` catches `RunCancelled` first: status `cancelled`, `clear_cancel`, event
  `run_cancelled {step}`; never `failed`.
- `request_cancel(run_id)`, `resume(run_id)` (`failed`/`cancelled` → `queued`),
  `approve_external(run_id, sha)`, `delete(run_id)` (A4: store part, `runner.delete_thread(run_id)`
  — add that method to `ResearchRunner` in `graphs/research.py`, using the checkpointer's
  `delete_thread` — then the directories), `list()`.
- `report_file(run_id, fmt)`: `md` → `report.md`, `docx`/`pdf` from the exports; status not in
  (`done`, `blocked`) → `ReportNotReady(status)` (409); export missing → `NotFound`.
- `create_external_run(…, approved: bool, created_by)` → status `queued` or
  `awaiting_brief_approval`; tier `auto` → `light` with `tier_auto_resolved` (D10).
- Tests (each red first): approve leaves the graph untouched and the run `queued`; `run` resumes
  with the pending hash and reaches `done`; a second approval → `WrongState`; cancel at a node
  boundary, cancel between two sweep queries (scripted searcher sets the flag after query 1: query
  2 is never searched), cancel between draft sections; resume after cancel finishes the run and no
  completed step runs twice (no second `running` record for it in `run.json` `steps`); `delete` of a running run →
  `WrongState`; `delete` leaves no row, no directory, no checkpoint thread; disk full: a step raising
  `OSError(errno.ENOSPC, …)` → `failed` with the reason in `run.json`.
- Commit: `feat(research): queue-based approval, cancel inside long steps, resume and delete`.

### Step 5 — Worker process (D1)

- `src/app/worker.py` `Worker(research: ResearchService, runs: RunStore, poll_s: float, sleep)`:
  - `run_once() -> str | None`: `next_runnable()`; none → `None`. Else `research.run(run_id)`;
    `WorkerBusy` (a `udr run` holds the slot) → return `None` without changing anything.
  - `run_forever(stop: threading.Event)`: `run_once`, sleep `poll_s` when idle; exits when `stop`
    is set (SIGTERM handler in the CLI sets it; the current run is left to the next start, which
    resumes it).
  - A `running` row with no lock holder is an orphan of a dead worker and is picked first
    (PRD: "runs left `running` auto-resume at startup").
- `bootstrap.build_worker(rt) -> Worker`. `cli.py`: `udr worker` (logs one line per run start and
  end; exits 0 on SIGTERM/SIGINT).
- Tests `tests/test_worker.py` (fakes): AC4 — two queued runs: `run_once` finishes the first, the
  second is still `queued`, the next `run_once` runs it; a `running` orphan goes before an older
  `queued` run; `WorkerBusy` → nothing changes; idle → `None`.
- AC5 `tests/test_worker_crash.py` with `tests/worker_crash_child.py` (model it on
  `research_crash_child.py`): start a worker child on the fakes, SIGKILL it during the sweep (the
  child signals readiness through a file, as the existing crash tests do), start a fresh worker,
  let it finish. Assert `done`, and that no step whose `done` record existed in `run.json` before
  the kill gets a second `running` record afterwards (`begin_step` in `research/steps.py` writes
  one per start).
- Commit: `feat(worker): a queue worker process for research runs`.

### Step 6 — BriefService in the background (D5, D7) and per-run models (D9)

- `brief/service.py`: `ServiceDeps.background: Callable[[str, Callable[[], None]], None] | None =
  None`. `_run` executes directly when `None` (CLI, today's behaviour); otherwise it hands the
  graph call to `background(session_id, action)` and returns at once. Validation stays synchronous
  and still raises (`InvalidInput`, `StaleBrief`, `WrongState`), so the API can answer 4xx.
- `api/jobs.py` `SessionJobs(threads)`: `submit(session_id, action)` → `ThreadPoolExecutor`; at
  most one job per session (a second while busy → `WrongState`, 409); the job takes the service's
  session lock; an exception is stored as the session's last error. `busy(session_id)`,
  `error(session_id)`. `SessionView` gets `busy: bool` (filled by the facade).
- On API start: `BriefService.recover()` runs once as a job (continues sessions a restart cut off).
- D9: `LLMService.with_models(overrides: Mapping[Role, str]) -> LLMService` (same transport, URLs,
  events, timeout; new registry entries, own semaphores). `_build_run_context` builds the run's
  service when `row.summarize_model` is set and differs: overrides for `Role.SUMMARIZE` and
  `Role.EXTRACT`; emits `summarize_model_override {model}` (warning) once. `RunContext.llm` holds
  it (else `rt.service`). `build_pipeline` gets an optional `service`. In `research/steps.py`
  replace every `self._d.service` / `d.service` (decompose, plan, sweep, packs, drafter, fixes,
  polish, readability; about 8 call sites) with `ctx.llm`. `approve` rejects a model outside
  `summarize_models` (A5) with `InvalidInput`.
- Tests: background mode returns before the scripted model is called and the job then reaches
  the next interrupt; a validation error is raised synchronously in background mode; a second job
  for a busy session → `WrongState`; `with_models` sends the override model for summarize and
  extract and the default for reason; a run with `summarize_model = "gemma4:e2b"` sends that model
  for its summarize and extract calls (scripted transport records models) and emits the warning.
- Close the two open issues in IMPLEMENTATION.md §4 in this step's docs.
- Commit: `feat(brief,research): background Phase-1 work and per-run summarize models`.

### Step 7 — Facade and auth

- `api/facade.py` `Facade(briefs: BriefService, research: ResearchService, jobs: SessionJobs,
  keys: KeyStore, settings, templates, denylist_path)`. One method per REST endpoint; every method
  takes the calling `ApiKey` and enforces D4: approve methods (`approve_brief`,
  `approve_plan`, `approve_run`) raise `Forbidden` without `self_approve`; `create_run` by a
  non-self-approve key creates `awaiting_brief_approval`. No HTTP types in the facade.
- `api/errors.py`: the A6 mapping as one function `to_http(exc) -> (status, body)`.
- Tests `tests/test_api_facade.py` on the brief and research rigs: the D4 rules, the error mapping
  table (parametrized), `create_run` statuses per key flag.
- Commit: `feat(api): the service facade shared by REST and MCP`.

### Step 8 — REST routes

- `api/rest.py` `build_app(facade, keys) -> FastAPI` (`docs_url=None`, `redoc_url=None`,
  `openapi_url=None`, A3). Bearer dependency on every router: missing, malformed, unknown or
  revoked key → 401 with `WWW-Authenticate: Bearer`.
- Routes exactly as PRD M6 plus A1; request/response models in `api/schemas.py`:
  - sessions: `POST /v1/sessions` (multipart: `question`, files) → 202; `POST
    /v1/sessions/{id}/uploads` (multipart) → 202; `POST …/messages` (`{answers?, note?, genug?,
    offer?: "strengthen"|"install"}`) → 202; `GET /v1/sessions/{id}`; `POST …/revise
    {feedback}` → 202; `PUT …/settings` → 202; `PUT …/brief {text}` → 202; `POST …/approve
    {brief_sha256, tier, summarize_model?}` → 200 `{run_id}`; `POST …/save` → 202.
  - runs: `POST /v1/runs {brief, tier, template_id, language?, response_format?}` → 201;
    `GET /v1/runs`; `GET /v1/runs/{id}`; `POST /v1/runs/{id}/approve {brief_sha256}` (A1);
    `GET …/events?after=N` → `{events, next}`; `GET …/stream` (SSE: poll the per-run file every
    `sse_poll_s`, `id:` = line number, honour `Last-Event-ID`, end after a final status).
  - plan: `GET …/search-plan` (`{plan, plan_sha256, text}`), `PUT …/search-plan {text}`,
    `POST …/search-plan/approve {plan_sha256}` → 202 (`queued`).
  - output: `GET …/report?format=md|docx|pdf` (file response; blocked runs too), `GET …/gate`,
    `GET …/outbound` (the JSONL lines).
  - control: `POST …/cancel`, `POST …/resume`, `DELETE /v1/runs/{id}` (204).
  - admin: `GET /v1/templates`, `POST /v1/templates` (multipart, validated with the template
    loader, saved to `data/templates/`; existing id → 409), `GET /v1/denylist`, `PUT /v1/denylist
    {terms}`, `GET /v1/health` (`{api: "ok", worker_lock: "free"|"held", queued: n}`), `GET
    /v1/config` (effective settings without any `SecretStr` value).
- Tests (`fastapi.testclient.TestClient` works in-process; the socket block in `conftest.py` stays
  on):
  - `tests/test_api_auth.py` — AC1: parametrize over `app.routes` (every `APIRoute`, every method),
    request without a key, with a wrong key, with a revoked key → 401. A test that fails if a route
    is missing from the parametrization (compare against the route table). AC2: approve endpoints
    with a non-self-approve key → 403 and the object stays `awaiting_*`; then a self-approve key
    approves.
  - `tests/test_api_rest.py` — AC3: stale hash → 409; report before done → 409 with `status`;
    unknown id → 404; a whole session over HTTP on the brief rig (202, poll `GET` until the
    decision, approve → `run_id`); `POST /runs` → run row; plan edit and approve; cancel/resume;
    delete running → 409, delete done → 204 and files gone; events cursor; SSE yields the lines of
    a finished run then ends; upload rejected file → 422 and no rows.
  - `tests/test_api_openapi.py` — AC7: every `APIRoute` path and method appears in
    `app.openapi()["paths"]`.
  - `tests/test_api_config.py`: `/v1/config` with a `TAVILY_API_KEY` set does not contain the
    secret.
- Commit: `feat(api): REST routes with bearer keys`.

### Step 9 — MCP

- `api/mcp.py` `build_mcp(facade, key_resolver) -> FastMCP` (`stateless_http=True`,
  `json_response=True` unless context7 shows a better fit). Tools, each a thin call into the facade
  with the request's key:
  `start_clarification(question)`, `answer_clarification(session_id, answers, note?, genug?)`,
  `get_session(session_id)`, `revise_brief(session_id, feedback)`, `approve_brief(session_id,
  brief_sha256, tier, summarize_model?)`, `start_research(brief, tier, template_id, language?,
  response_format?)`, `get_run_status(run_id)`, `get_search_plan(run_id)`,
  `update_search_plan(run_id, text)`, `approve_search_plan(run_id, plan_sha256)`,
  `get_report(run_id)` (markdown text), `list_templates()`, `cancel_run(run_id)`.
  Facade errors become MCP tool errors with the A6 message.
- Mount the streamable-HTTP app at `/mcp` in `build_app`; run the MCP session manager in the
  FastAPI lifespan (as context7 documents). Bearer auth as an ASGI middleware on the mount: same
  `KeyStore.verify`, 401 otherwise; the verified key is passed to the tools (request state).
- Tests `tests/test_api_mcp.py` — AC6: in-memory client session lists exactly the 13 tools;
  `start_research` (self-approve key) → the worker's `run_once` (in-process, fakes) runs it to the
  plan → `approve_search_plan` → `run_once` again → `get_report` returns markdown starting with
  `# `. Plus: `/mcp` over `TestClient` without a key → 401.
- Commit: `feat(api): MCP tools at /mcp`.

### Step 10 — Server and CLI

- `api/server.py` `serve(settings)`: `build_runtime`, `build_brief_service(background=jobs.submit)`,
  `build_research_service`, `KeyStore`, `Facade`, `build_app`; `uvicorn.run(app, host="127.0.0.1",
  port=settings.api_port)`. `Settings.api_port` (`UDR_API_PORT`, default 8541).
- `cli.py`: `udr serve`, `udr worker`, `udr apikey create --name N [--self-approve]` (prints the key
  once, with a German hint that it is not shown again), `udr apikey list`, `udr apikey revoke ID`.
- Tests `tests/test_cli_service.py` (CliRunner): create prints a key that `verify` accepts and is
  not stored in clear; list never shows a key; revoke then verify → `None`; `serve` and `worker`
  call their entry points (monkeypatch `uvicorn.run` / `Worker.run_forever`).
- Commit: `feat(cli): serve, worker and apikey commands`.

### Step 11 — Docs

- `/documentation-update`. New `docs/api.md` (≤ 800 lines): processes and how to start them, keys
  and the self-approve rule, every endpoint with one curl example per group, the async Phase-1
  pattern (202 + poll), events and SSE, cancel semantics (D2), MCP setup for Claude Code
  (`claude mcp add --transport http udr http://127.0.0.1:8541/mcp --header "Authorization: Bearer
  …"`), SSH tunnel note. `docs/architecture.md`: the two processes and the queue. README:
  quickstart lines for `udr serve`, `udr worker`, `udr apikey create`. IMPLEMENTATION.md: module
  map, run/verify rows, phase row 6 `done` with the verifying tests, §4 updates.
- Commit: `docs: M6 service, API and worker`.

### Step 12 — Live check (ask the owner first; ~1 h, real models, ddgs or Tavily)

- `udr apikey create --name live --self-approve`; start `udr serve` and `udr worker` in the
  background; a curl script (`scripts/` is not needed: put it in `docs/api.md` and run it from the
  shell) does a whole Lite run through REST: `POST /v1/runs` with the M5 reference brief, poll,
  approve the plan, poll to `done`, download `md` and `pdf`. Record wall time and statuses in
  IMPLEMENTATION.md §4. Stop both processes by PID afterwards (never kill by port).
- MCP from Claude Code and the non-self-approve path through the GUI belong to the PRD definition
  of done and are checked after M7.

## Verification

- Every step: `uv run pre-commit run --all-files` green (coverage ≥ 85 %, suite ≤ 60 s — keep the
  worker crash test fast: the fakes, a short `poll_s`, a readiness file instead of sleeps).
- AC1 `test_api_auth.py`, AC2 `test_api_auth.py`/`test_api_facade.py`, AC3 `test_api_rest.py`,
  AC4 `test_worker.py`, AC5 `test_worker_crash.py`, AC6 `test_api_mcp.py`, AC7
  `test_api_openapi.py`. Edge cases: cancel (Step 4), delete running → 409, disk full → `failed`,
  concurrent approvals → 409 (`set_pending_plan`, `RunStore.approve`).
- `tests/test_egress_guard.py` unchanged and green; `tests/test_layer_rules.py` extended (Step 1).
- `ss -ltnp` during the live check shows 8541 on `127.0.0.1` only.

## Risks and how to handle them

- **Two processes on one SQLite file.** `store/db.py` already sets WAL and a 5 s busy timeout. Check
  that `graphs/brief.py` `open_checkpointer` uses the same `connect` (it does) so the checkpoint
  file is WAL too. Add one test that a `RunStore` in a second connection sees a status written by
  the first. If `database is locked` appears in the crash test, raise the busy timeout, do not add
  retries around single statements.
- **MCP SDK API drift.** Only code against what context7 shows for the installed version (Step 1).
- **Thread pool and the session lock.** A job must take the same per-session lock as the
  synchronous path; never hold the lock while waiting for the pool.
- **Long tests.** The crash test and the SSE test must not sleep in real time; inject `sleep` and
  poll intervals.
- **Scope creep.** No GUI work (M7), no systemd units (M10), no Full tier (M8/M9).
