# M7 implementation plan — GUI (Streamlit, German) and SearXNG search

**Status:** draft; D1–D11 decided by the owner on 2026-10-07, A1–A8 adopted (challenge if wrong).
**Executor:** Sonnet 5.5. Follow [AGENTS.md](../../AGENTS.md) for every step: red → green → gate →
commit (one commit per step, imperative message). Push only when the owner asks. Report the red and
the green result of every step in its commit message body.

## Context

M1–M6 are done (see [IMPLEMENTATION.md](../../IMPLEMENTATION.md)). The service runs as two
processes, `udr serve` (REST `/v1`, MCP `/mcp` on `127.0.0.1:8541`) and `udr worker`
([docs/api.md](../api.md)). M7 adds a Streamlit GUI on `127.0.0.1:8540` that is a pure client of
that API, and (owner decision D5) SearXNG as the first web search provider. Requirement source:
[PRD.md](../../PRD.md) §4 M7, §3.3.

What exists and must be reused, not rewritten:

| Piece | Where | Note |
|---|---|---|
| Service layer | `api/facade.py` `Facade` | one method per endpoint, D4 approval rule, no HTTP types |
| Routes | `api/routes_sessions.py`, `routes_runs.py`, `routes_admin.py`, `schemas.py` | auth on every router |
| Views | `brief/service.py` `SessionView`, `research/service.py` `RunView` | the API returns them as JSON |
| Plan edit format | `research/plan_edit.py` `render_lines`, `parse_lines`, `HEADER` | `id \| item \| lens \| query` |
| Per-run events | `events.py` `RunScopedEventSink`, `read_run_events` | bound in `ResearchService.run` |
| Web search chain | `adapters/outbound/gateway.py` `web_search`, `_try_tavily`, `_switch` | Tavily → ddgs today |
| Providers | `bootstrap.py` `build_providers`, `build_gateway` (`credit_cap`) | |
| Doctor | `doctor.py` (pure checks), `bootstrap` snapshot | |
| Rigs | `tests/api_rig.py`, `tests/research_run_rig.py`, `tests/brief_rig.py` | owner key and a key without self-approve |
| Layer rules | `tests/test_layer_rules.py`, `tests/test_egress_guard.py` (`ALLOWED`) | |

## Owner decisions (2026-10-07), binding for this plan

- **D1 — Extend the API in M7.** The GUI stays a pure client; what it needs and the API lacks is
  added to the API, test first: `GET /v1/sessions`, `POST /v1/sessions/{id}/retry`,
  `GET /v1/doctor`, and run summary fields (title, created/elapsed, Tavily credits run and month,
  step timeline, creating key).
- **D2 — API client `src/app/client.py`.** A typed `httpx` client that refuses a non-loopback base
  URL. It is added to the egress allowlist (with a violating-input test). `src/app/gui/` imports
  from `app` only `app.client`.
- **D3 — Plan editing as a table** (`st.data_editor`): edit, delete, add rows; denylist hits and
  other blocked rows highlighted; Freigeben disabled while a row is blocked. The client turns the
  table into the existing line format for `PUT …/search-plan`.
- **D4 — Per-run Tavily cap.** An optional `tavily_cap` on session approval and `POST /runs`,
  stored with the run, at most the tier's cap from `config/profiles.toml`; `0` means no Tavily for
  that run. The run's gateway uses it.
- **D5 — SearXNG in M7, first in the web chain:** SearXNG → Tavily → ddgs for lenses A, C, D.
  Scholarly lens B stays OpenAlex/Crossref/arXiv.
- **D6 — Fallback rule.** A query falls through from SearXNG on a network or HTTP error or 0 hits;
  then the existing Tavily rules apply (credits, plan limit, switch), then ddgs.
- **D7 — Deployment.** `deploy/searxng/` holds a compose file with a pinned image and a
  `settings.yml` (JSON format on, limiter off, curated engines), bound to `127.0.0.1:8888`.
  Optional: without `UDR_SEARXNG_URL` the chain is Tavily → ddgs as today. The URL must be
  loopback. `udr doctor` checks it when configured.
- **D8 — Licence.** SearXNG (AGPL-3.0) is accepted as an unmodified external service over HTTP,
  never vendored or imported; recorded in AGENTS.md §5.5.
- **D9 — Fix the events bug in M7.** `GET /runs/{id}/events` returned `[]` for the first 25 min of
  the M6 live run (until step 1 ended). Reproduce with a test first, then fix.
- **D10 — Sessions and runs in the approval list.** A migration adds `sessions.created_by`. Läufe
  lists sessions waiting at the decision and runs in `awaiting_brief_approval` or
  `awaiting_plan_approval` that other keys created, with the key's name.
- **D11 — `udr gui`, Full shown disabled.** `udr gui` starts Streamlit on `127.0.0.1:8540`
  (`UDR_GUI_PORT`); the API URL is `UDR_API_URL` (default `http://127.0.0.1:8541`), the key
  `UDR_GUI_API_KEY`. Full is shown disabled with "ab M8"; the recommendation and its rationale are
  still shown.

Adopted without a separate question (challenge if wrong):
- **A1** Streamlit usage statistics go to the internet by default. `udr gui` passes
  `--browser.gatherUsageStats false` and `--server.address 127.0.0.1`, and the repo carries
  `.streamlit/config.toml` with the same; a test checks both.
- **A2** The GUI polls with `st.fragment(run_every=…)`; the interval is `[gui] poll_s = 5` in
  `config/profiles.toml` (AC4: at most every 5 s). The fragment reads `GET /runs/{id}` and
  `GET …/events?after=N`, not the SSE stream.
- **A3** Query parameters `?session=<id>` and `?run=<id>` and `?page=<name>` are the only state a
  reload needs (AC5); everything else is re-read from the API.
- **A4** Safe exit: a "Beenden" button calls `os.kill(os.getpid(), signal.SIGTERM)` — the Streamlit
  process only, never the API, the worker or a port. `udr gui` runs Streamlit as a child process and
  returns when it ends.
- **A5** The client returns plain typed data (`TypedDict`s / dataclasses in `client.py`), never
  `app.research` or `app.brief` models, so the GUI does not import them indirectly. A contract test
  (in `tests/`, which may import both) checks that the client's line rendering parses with
  `plan_edit.parse_lines` and round-trips.
- **A6** `GET /v1/doctor` runs the read-only doctor snapshot (never starts an Ollama daemon) through
  a callable injected into the facade; the role → model map comes from it.
- **A7** SearXNG calls go through the gateway like every search: sanitized `sent` text, denylist,
  outbound log line (`provider: "searxng_search"`, 0 credits), throttle. The request to the
  configured SearXNG URL is exempt from the private-URL guard (it is our loopback service, like
  Ollama); the result URLs are fetched through the guard as before. Event `search_fallback {from,
  to, reason}` on each fall-through.
- **A8** MCP keeps its 13 tools; D1 and D4 are REST only, plus the optional `tavily_cap` argument
  on `approve_brief` and `start_research`.

## Target design

```
udr gui (Streamlit, 127.0.0.1:8540)          udr serve (8541)              udr worker
  app/gui/*  ──imports──►  app/client.py ──HTTP──►  Facade ─► services ◄── queue ── Worker
                                                                     │
web query (lens A/C/D):  SearXNG (127.0.0.1:8888) ─error/0 hits─► Tavily ─► ddgs
```

Modules (new unless marked):

| Module | Content |
|---|---|
| `src/app/client.py` | `ApiClient(base_url, key, http)`, `ApiDown`, `ApiError(status, detail)`; one method per endpoint the GUI uses; `plan_rows`/`rows_to_lines` (D3) |
| `src/app/gui/app.py` | entry point: navigation, API-down banner, safe exit, query params |
| `src/app/gui/pages/*.py` | `neue_recherche.py`, `suchplan.py`, `laeufe.py`, `bericht.py`, `einstellungen.py` |
| `src/app/gui/texts.py` | German UI strings as constants |
| `src/app/adapters/outbound/searxng.py` | `SearxngApi.search(query, max_results) -> list[SearchHit]` |
| `adapters/outbound/gateway.py` (change) | SearXNG first (D5, D6, A7) |
| `config.py` (change) | `searxng_url`, `api_url`, `gui_port` (loopback validators) |
| `store/db.py` (change) | migration 5: `sessions.created_by`, `runs.tavily_cap` |
| `api/*` (change) | D1 routes and fields, `tavily_cap` (D4) |
| `cli.py` (change) | `udr gui` |
| `deploy/searxng/` | `compose.yaml`, `settings.yml`, `README.md` |
| `.streamlit/config.toml` | A1 |

## Steps

Each step: write the tests first, run them, confirm they fail for the expected reason (red), write
the minimum code (green), run `uv run pre-commit run --all-files`, commit. Function ≤ 50 lines,
complexity ≤ 10, pyright strict, Python 3.11 syntax. Use the existing fakes; no real network,
model, SearXNG or Streamlit server in the gate.

### Step 0 — Contract updates (docs only, one commit)

- PRD (D1–D11 only, keep ≤ 800 lines; check with `tests/test_docs.py`):
  - M7 deliverable: the API additions (D1), the per-run Tavily cap (D4), `udr gui`, Full disabled
    until M8 (D11), sessions in the approval list (D10), SearXNG (D5–D7).
  - §3.3: web search order SearXNG → Tavily → ddgs and the fall-through rule (D6).
  - §3.1 environment list: `UDR_SEARXNG_URL`, `UDR_API_URL`, `UDR_GUI_PORT`.
- AGENTS.md §5.5: the SearXNG acceptance (D8), dated 2026-10-07.
- IMPLEMENTATION.md: phase row 7 → `in progress`, milestone "M7: GUI and SearXNG", link this plan.
- Commit: `docs: M7 contract and plan`.

### Step 1 — The events bug (D9)

- Hypothesis (verify, do not assume): an emitter used during step 1 holds the inner sink instead of
  the `RunScopedEventSink` (e.g. a service built in `_build_run_context` or by
  `LLMService.with_models`), so step 1's model telemetry lands only in `data/events.jsonl`; later
  steps emit through the scoped sink.
- Red: in `tests/test_research_service.py` (or a new `test_run_events.py`), run the Lite rig on the
  real composition (`tests/test_bootstrap_research.py` style) until the plan interrupt and assert
  that `data/runs/<id>/events.jsonl` holds the decomposition step's model events with `run_id`,
  and that `read_run_events(run_dir, 0)` returns them before step 2 begins. Also with a
  `summarize_model` override (the `with_models` path).
- Green: route the emitter through the scoped sink. If the cause is different, record the real one
  in the commit body and in IMPLEMENTATION.md §4 (Step 12 removes the open issue).
- Commit: `fix(events): step 1 events reach the run's event file`.

### Step 2 — SearXNG adapter and deployment (D5, D7, D8)

- `config.py`: `searxng_url: str | None` (`UDR_SEARXNG_URL`), validated with `is_loopback_url`
  (test: a LAN URL raises).
- `adapters/outbound/searxng.py` `SearxngApi(http, base_url, timeout_s)`: `GET
  {base}/search?q=…&format=json` → `SearchHit`s (url, title, snippet) as the other clients produce
  them; malformed JSON or non-200 → the same error types `tavily.py` raises for an outage. Before
  coding, read the SearXNG search API docs (context7 or the project's docs: `format=json`,
  `language`, `pageno`, `categories`).
- `build_providers`: `searxng=SearxngApi(...) if settings.searxng_url else None`.
- `deploy/searxng/compose.yaml` (image pinned by tag and digest, port `127.0.0.1:8888:8080`,
  `restart: unless-stopped`), `settings.yml` (`search.formats: [html, json]`,
  `server.limiter: false`, `server.secret_key` from an env var, curated general engines: google,
  bing, duckduckgo, brave, startpage, qwant, mojeek, wikipedia), `README.md` (start, stop, check
  with curl). No secret in the repo.
- Tests `tests/test_searxng.py` with a fake HTTP factory (as `test_outbound_infra.py` does): hits
  parsed; 0 results → `[]`; 500 / timeout / bad JSON → the outage error; `max_results` honoured.
- Commit: `feat(outbound): SearXNG search client and its compose setup`.

### Step 3 — Gateway: SearXNG first (D6, A7) and doctor

- `gateway.py` `web_search`: if `providers.searxng` is set, try it first through the same
  sanitize → denylist → log → throttle path (`provider="searxng_search"`, pace key = the SearXNG
  URL, 0 credits). Error or 0 hits → event `search_fallback {from: "searxng", to, reason:
  "error"|"empty"}` → the existing Tavily path → ddgs. The SearXNG base URL is not passed through
  `check_url` (A7); add a comment why.
- Doctor: a check `searxng` — not configured → `info`; configured and `GET {base}/healthz` (or
  `/search?q=test&format=json`, per the docs) fails → `warn`. The probe lives in
  `adapters/system_probe.py` or the snapshot builder, never in `doctor.py` (pure).
- Tests (`tests/test_gateway.py`): SearXNG hits → Tavily and ddgs untouched, 0 credits charged;
  SearXNG error → Tavily answers, event raised; SearXNG empty → Tavily; SearXNG and Tavily fail →
  ddgs; no `searxng_url` → today's behaviour (existing tests stay green); the outbound log line
  holds the sanitized text only; a denylisted query never reaches SearXNG. Doctor check
  parametrized over the three states.
- Commit: `feat(outbound): SearXNG first in the web search chain`.

### Step 4 — Store migration 5 and the per-run Tavily cap (D4, D10)

- Migration 5: `ALTER TABLE sessions ADD COLUMN created_by TEXT;`, `ALTER TABLE runs ADD COLUMN
  tavily_cap INTEGER;` (NULL = the tier's cap).
- `RunStore.create_external` / session approval take `tavily_cap`; `SessionStore` records
  `created_by` at start. Validation in the service: `0 ≤ cap ≤ tier cap`, else `InvalidInput`.
- `bootstrap._build_run_context` → `build_gateway(credit_cap=row.tavily_cap if not None else
  profile cap)`.
- Facade/schemas: `tavily_cap: int | None` on `SessionApproveIn` and `RunCreateIn`; MCP tools
  `approve_brief`, `start_research` get the optional argument (A8).
- Tests: cap 0 → no Tavily call, SearXNG/ddgs answer; cap above the tier → 422; cap stored and used
  after a worker restart (resume reads the row); `created_by` set for a session started over the
  API, NULL from the CLI.
- Commit: `feat(store,api): per-run Tavily cap and session creator`.

### Step 5 — API additions (D1, D10)

- `GET /v1/sessions?status=` → `[SessionSummary]` (id, status, question head, created/updated,
  `created_by` key name, `waiting_for`, `run_id`), newest first.
- `POST /v1/sessions/{id}/retry` → 202 (`BriefService.retry` as a background job).
- `GET /v1/doctor` → the doctor checks as JSON plus the role → model map (A6).
- Run view additions (one `RunSummary` returned by `GET /runs` and embedded in `GET /runs/{id}`, so
  `RunView` itself stays as the CLI uses it): `title` (first H1 of the brief), `created_at`,
  `started_at`, `ended_at`, `elapsed_s`, `steps` (from `run.json`: step, status, start, end),
  `credits_run`, `credit_cap`, `credits_month`, `month_limit`, `sources`, `warnings` (count of
  warning events), `created_by` (key name).
- `GET /v1/approvals` is **not** added: the GUI filters `GET /sessions` and `GET /runs` (fewer
  routes). Approving uses the existing endpoints.
- Tests: `test_api_auth.py` picks up the new routes automatically (its route-table check must stay
  green — red first: add the routes to the table); `test_api_rest.py`: list sessions from two keys
  with names; retry after a scripted model error reaches the next interrupt; doctor JSON on a fake
  snapshot; summary fields on a finished rig run (credits from the outbound log, month from the
  ledger); `test_api_openapi.py` stays green.
- Commit: `feat(api): session list, retry, doctor and run summaries`.

### Step 6 — Dependencies, client and the layer rules (D2)

- `uv add streamlit`. List every new package in `uv.lock` with its licence and check it against
  AGENTS.md §5.5. **Any other licence: stop and ask the owner.** Before writing GUI code, read the
  current Streamlit docs through context7: `st.navigation`/`st.Page`, `st.fragment(run_every=)`,
  `st.query_params`, `st.data_editor` (list-of-dict input and output, to keep pandas out of typed
  code), `st.download_button`, `streamlit.testing.v1.AppTest`. Do not code the API from memory.
- `src/app/client.py`: `ApiClient` over an injected `httpx.Client` (tests use
  `httpx.MockTransport`; no socket); bearer header; non-loopback `base_url` → `ValueError`;
  connection error/timeout → `ApiDown`; 4xx/5xx → `ApiError(status, detail, run_status)`;
  `ApiClient.from_env()` reads `Settings` (`api_url`, `gui_api_key`). Methods for every endpoint
  the pages use. `plan_rows(plan)` and `rows_to_lines(rows)` (A5).
- Rules:
  - `tests/test_egress_guard.py`: `ALLOWED` += `app/client.py`; a violating-input test (httpx in
    `app/gui/x.py` is reported).
  - `tests/test_layer_rules.py`: `src/app/gui/**` imports from `app` only `app.client` and
    `app.gui`; `streamlit` is imported only under `src/app/gui/` (AC1). Violating-input tests for
    both.
- Tests `tests/test_client.py`: each method's request (path, method, body, header) on a
  `MockTransport`; error mapping; loopback refusal; the A5 round-trip with `parse_lines`.
- Commit: `feat(client): typed API client for the GUI`.

### Step 7 — GUI shell: navigation, banner, safe exit, reload, `udr gui` (AC3, AC4, AC5)

- `gui/app.py`: pages via `st.navigation`; on start `client.health()`; `ApiDown` → banner "API
  nicht erreichbar" with a "Erneut versuchen" button, no traceback (AC4, edge case). Sidebar
  "Beenden" → `os.kill(os.getpid(), signal.SIGTERM)` (A4). Query parameters restore page, session
  and run (A3).
- `cli.py` `udr gui`: `subprocess.run([sys.executable, "-m", "streamlit", "run", <app.py>,
  "--server.address", "127.0.0.1", "--server.port", str(gui_port), "--server.headless", "true",
  "--browser.gatherUsageStats", "false"])`; refuses to start without `UDR_GUI_API_KEY` (German
  message). `.streamlit/config.toml` (A1).
- Tests `tests/test_gui_shell.py` (AppTest with a fake client injected through a module-level
  factory or `st.session_state`): API down → banner, no exception (AC4); exit button →
  `os.kill(os.getpid(), SIGTERM)` exactly once, mocked (AC3); `?run=r-1&page=laeufe` opens that run
  (AC5); `test_cli_gui.py`: the command line contains the address, port and stats flag (subprocess
  mocked); `.streamlit/config.toml` sets both.
- Commit: `feat(gui): Streamlit shell with safe exit and API banner`.

### Step 8 — Page "Neue Recherche" (AC2 part 1)

- Question and uploads (per-file errors shown, edge case); poll the session while `busy` (fragment,
  A2); questions with editable candidate answers, checklist status, "genug"; offers; settings: tier
  (Lite; Full disabled "ab M8", recommendation and rationale shown), template select with section
  preview and upload, report language, response format, e4b/e2b, Tavily cap (number, max = tier
  cap); the exact brief text with Freigeben / Überarbeiten / Speichern; model error → message and
  "Erneut versuchen" (retry). After approval: `?run=<id>&page=suchplan`.
- Tests `tests/test_gui_brief.py`: questions render; answers are sent as `answers`; Freigeben sends
  the sha256 of the **displayed** text (compute it in the test from the text the page shows, AC2);
  Full disabled; upload error per file; `busy` shows a spinner and no buttons.
- Commit: `feat(gui): new research page`.

### Step 9 — Page "Suchplan" (AC2 part 2, D3)

- Table per atomic item: lens, kind (web/scholarly), original, sent, removed terms, blocked reason;
  blocked rows highlighted. Edit, delete, add → `rows_to_lines` → `PUT …/search-plan`, then the
  re-checked plan is shown. Freigeben sends `plan_sha256` of the plan shown; disabled while any row
  is blocked (AC2).
- Tests `tests/test_gui_plan.py`: a plan with a denylisted query → Freigeben disabled and the row
  marked; edit → the client receives the expected lines; approve sends the shown hash; a 409
  (stale) shows a German message and reloads the plan.
- Commit: `feat(gui): search plan page`.

### Step 10 — Pages "Läufe" and "Bericht"

- Läufe: history (`GET /runs`); selected run: step timeline, current step, sources, credits
  run/month, elapsed, warnings, events (fragment poll ≤ 5 s, cursor in `session_state`), outbound
  log; cancel / resume / delete (delete asks for confirmation); pending approvals from other keys
  (D10) with approve buttons.
- Bericht: rendered Markdown; over 100 000 characters → one `st.expander` per H2 (edge case); gate
  result; MD/PDF/DOCX downloads (DOCX absent → hint "pandoc fehlt", no error).
- Tests `tests/test_gui_runs.py`: the fragment's interval is ≤ 5 s (AC4, read from the config the
  page uses); events appended across two polls without duplicates; cancel calls the client; a
  pending session from another key is listed with its name and approve sends its hash; report
  > 100k → expanders; 404 for DOCX → hint.
- Commit: `feat(gui): runs and report pages`.

### Step 11 — Page "Einstellungen"

- Denylist editor (`GET`/`PUT /denylist`), doctor status (`GET /doctor`, incl. SearXNG), read-only
  role → model map.
- Tests `tests/test_gui_settings.py`: save sends the edited terms; an invalid term shows the 422
  message; doctor `warn` shown.
- Commit: `feat(gui): settings page`.

### Step 12 — Docs

- `/documentation-update`. New `docs/gui.md` (pages, start, safe exit, keys, polling, reload),
  `docs/outbound.md` (the SearXNG chain, fall-through, deployment), `docs/api.md` (new routes,
  `tavily_cap`), README quickstart (`udr gui`, SearXNG compose), IMPLEMENTATION.md module map,
  run/verify rows, phase row 7 with the verifying tests, §4 updates (the events issue closed or
  restated).
- Commit: `docs: M7 GUI and SearXNG`.

### Step 13 — Live check (ask the owner first; ~1 h)

- Start SearXNG (`deploy/searxng/`), `udr serve`, `udr worker`, `udr gui`. Add a SearXNG case to
  `tests/live/test_live_outbound.py` and run it.
- In the browser: a Lite run on the M5 reference brief from the GUI (Phase 1 to report), with the
  plan edited once; note the share of queries SearXNG answered, Tavily credits, wall time.
- The M6 leftovers: a key without self-approve starts a run over curl, the GUI approves it; MCP
  from Claude Code (`claude mcp add …`) lists 13 tools and reads a report.
- `ss -ltnp`: 8540, 8541 and 8888 on 127.0.0.1 only. Stop GUI with "Beenden", API and worker by PID
  (never by port). Record results in IMPLEMENTATION.md §4.

## Verification

- Every step: `uv run pre-commit run --all-files` green (coverage ≥ 85 %, suite ≤ 60 s).
- AC1 `test_layer_rules.py`; AC2 `test_gui_brief.py`, `test_gui_plan.py`; AC3 `test_gui_shell.py`;
  AC4 `test_gui_shell.py`, `test_gui_runs.py`; AC5 `test_gui_shell.py`. D9 Step 1 test; D4–D7
  `test_gateway.py`, `test_searxng.py`, store tests.
- `test_egress_guard.py` extended, green; no test opens a socket.

## Risks and how to handle them

- **AppTest speed.** Each `AppTest.run()` costs ~0.3–1 s. Keep GUI tests few and focused; put
  logic (row conversion, hashing, summaries) in `client.py` or small pure helpers tested directly.
  If the suite nears 60 s, ask before raising the limit.
- **pyright strict and Streamlit/pandas.** Pass and receive lists of dicts to `st.data_editor`;
  keep pandas out of typed code. Narrow `Any` at the client boundary.
- **Fragments in AppTest.** `run_every` does not tick in AppTest; test the poll function directly
  and assert the configured interval.
- **SearXNG engines blocked or rate-limited.** That is a `0 hits`/error → fall-through, never a
  failed run. The live check measures the real share; tune engines in `settings.yml` only.
- **SearXNG image drift.** Pin by digest; the README states how to update it.
- **Scope creep.** No Full tier (M8/M9), no systemd units (M10; note the SearXNG container for
  M10), no GUI auth beyond the API key.
