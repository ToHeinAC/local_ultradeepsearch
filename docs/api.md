# Service: API, MCP, keys and worker

Two processes share `data/udr.sqlite` (WAL) and `data/checkpoints.sqlite`. Requirements:
[PRD.md](../PRD.md) §4 M6. The run itself: [research.md](research.md).

```
udr serve   REST /v1 + MCP /mcp on 127.0.0.1:8541     udr worker   loop: next run -> execute it
  Facade -> BriefService (+ SessionJobs thread pool)    ResearchService.run under data/worker.lock
         -> ResearchService (never runs a graph)        cancel flag read between steps
```

The GUI ([gui.md](gui.md)) is a client of this API. The API never executes a research graph. Approving a plan only sets the run `queued` (and stores
the approved plan hash); the worker takes `queued` runs first in first out, a run left `running`
by a dead worker first of all, and carries the approval through. A run waiting for an approval is
not in the queue. `udr run` still executes in its own process; the worker lock keeps one run
active ("Ein anderer Lauf ist aktiv.").

## Start

| Task | Command |
|---|---|
| API | `uv run udr serve` (host fixed to `127.0.0.1`, port `UDR_API_PORT`, default 8541) |
| Worker | `uv run udr worker` (stop with SIGTERM or Ctrl-C: the run in flight stays `running` and the next start resumes it) |
| Keys | `uv run udr apikey create --name N [--self-approve]` (shown once, only its sha256 is stored), `list`, `revoke ID` |

Reach it from another machine through an SSH tunnel (`ssh -L 8541:127.0.0.1:8541 host`); the API
does not listen on another interface. Numbers (`worker_poll_s`, `sse_poll_s`, `session_threads`,
`summarize_models`) are in `config/profiles.toml` `[service]`.

## Keys and approval

Every route, `/v1/health` included, needs `Authorization: Bearer <key>`; a missing, malformed,
unknown or revoked key gets 401 with `WWW-Authenticate: Bearer`. An item created by a key without
`--self-approve` waits until a key with it approves it: a session brief (`POST
/sessions/{id}/approve`), a run created by `POST /runs` (`awaiting_brief_approval`, approved by
`POST /runs/{id}/approve {brief_sha256}`) and a search plan. Without the right: 403, and the
object stays as it was. The full tier is refused with 422 "Full-Tier ab M8"; tier `auto` on
`POST /runs` becomes `light` (event `tier_auto_resolved`).

## REST (`/v1`)

| Group | Endpoints |
|---|---|
| sessions (answer 202, the session view has `busy`) | `POST /sessions` (multipart `question`, `files`); `POST /sessions/{id}/uploads`; `POST …/messages {answers, note, genug, offer}`; `GET /sessions/{id}`; `POST …/revise {feedback}`; `PUT …/settings`; `PUT …/brief {text}`; `POST …/approve {brief_sha256, tier, summarize_model?, tavily_cap?}` (200 `{run_id}`); `POST …/save` |
| runs | `POST /runs {brief, tier, template_id, language?, response_format?, tavily_cap?}` (201); `GET /runs`; `GET /runs/{id}`; `POST …/approve`; `GET …/events?after=N` (`{events, next}`); `GET …/stream` (SSE) |
| plan | `GET`/`PUT /runs/{id}/search-plan` (`{plan, plan_sha256, text}`); `POST …/search-plan/approve {plan_sha256}` (202, `queued`) |
| overview | `GET /sessions` (id, status, title, creating key); `POST /sessions/{id}/retry` (202); `GET /run-summaries`, `GET /runs/{id}/summary` (title, creator, step spans, Tavily credits of run and month, sources, warnings); `GET /doctor` (checks and role to model map) |
| output | `GET …/report?format=md\|docx\|pdf` (also for `blocked` runs); `GET …/gate`; `GET …/outbound` |
| control | `POST …/cancel`; `POST …/resume` (202); `DELETE /runs/{id}` (204) |
| admin | `GET`/`POST /templates` (multipart `file`; an existing id is 409); `GET`/`PUT /denylist {terms}`; `GET /health`; `GET /config` (secrets show `set`) |

Errors: `{"detail": "...", "status": "<run status, if known>"}`. 404 unknown id; 409 stale hash,
wrong state, second approval, report before the run is done, delete of a running run; 422 invalid
input, bad brief or template, tier; 403 approval without the right. The interactive docs and
`/openapi.json` are off; `app.openapi()` still builds the schema (tested).

Phase-1 calls return at once; the model work runs in a thread pool of the API process, one job
per session (a second request meanwhile: 409). Poll `GET /sessions/{id}` until `busy` is false;
`error` holds a model error. Approval is the exception: it needs no model and returns the run id.

```bash
KEY=udr_...   # from `udr apikey create`
curl -s -H "Authorization: Bearer $KEY" -X POST localhost:8541/v1/runs \
  -H 'Content-Type: application/json' \
  -d '{"brief": "# Titel\n\n1. Frage\n", "tier": "light", "template_id": "auto"}'
curl -s -H "Authorization: Bearer $KEY" localhost:8541/v1/runs/$RUN/search-plan   # sha + text
curl -s -H "Authorization: Bearer $KEY" -X POST localhost:8541/v1/runs/$RUN/search-plan/approve \
  -H 'Content-Type: application/json' -d "{\"plan_sha256\": \"$SHA\"}"
curl -s -H "Authorization: Bearer $KEY" "localhost:8541/v1/runs/$RUN/report?format=pdf" -o r.pdf
```

## Events and cancel

Events go to `data/events.jsonl` and, while a run executes, also to `data/runs/<id>/events.jsonl`
with `run_id`; each step announces itself with `step_started` and `step_finished`. `…/events?after=N` returns the lines after the first N and the new cursor. The
stream sends one SSE event per line (`id:` is the line number, `Last-Event-ID` resumes) and ends at
a final status (`done`, `blocked`, `failed`, `cancelled`); the last event is written before the
status changes. Cancel acts at once on a run that does not execute (`queued`, waiting for an
approval) and otherwise sets a flag the worker reads before each step, before each search query
and before each draft section; the run becomes `cancelled`, never `failed`, and `resume` queues it
again. `DELETE` removes the run's rows, checkpoints, directory and the upload directory of its
session; the archived brief stays.

## MCP (`/mcp`)

Streamable HTTP, stateless, JSON responses, the same keys. Thirteen tools: `start_clarification`,
`answer_clarification`, `get_session`, `revise_brief`, `approve_brief`, `start_research`,
`get_run_status`, `get_search_plan`, `update_search_plan`, `approve_search_plan`, `get_report`
(Markdown), `list_templates`, `cancel_run`. A domain error is a tool error `"<status>: <message>"`.
The SDK checks the `Host` header (loopback only). For Claude Code:

```bash
claude mcp add --transport http udr http://127.0.0.1:8541/mcp --header "Authorization: Bearer $KEY"
```

## Code

`api/facade.py` (one method per endpoint, the only module routes and tools call), `api/rest.py`
and `routes_*.py` (FastAPI), `api/mcp.py`, `api/keys.py`, `api/jobs.py`, `api/errors.py` (error to
HTTP), `api/schemas.py`, `api/server.py` (composition, the only uvicorn import); `worker.py`. Only
`src/app/api/` imports fastapi, starlette, uvicorn and mcp (`tests/test_layer_rules.py`). A run's
`summarize_model` drives its `summarize` and `extract` roles (`LLMService.with_models`, warning
event `summarize_model_override`).

## Tests

`test_api_auth.py` (every route without, with a wrong and with a revoked key; the approval rule),
`test_api_rest.py`, `test_api_openapi.py`, `test_api_facade.py`, `test_api_mcp.py`,
`test_worker.py`, `test_worker_crash.py` (a real SIGKILL of a worker child; no finished step
starts again), `test_research_queue.py`, `test_brief_background.py`; the rig is `tests/api_rig.py`.
