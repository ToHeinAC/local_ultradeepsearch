# Architecture

## Code layers

| Layer | Where | Rule |
|---|---|---|
| Core logic | `config`, `llm/{types,roles,structured,service,errors}`, `events`, `artifacts`, `text`, `templates`, `documents`, `store/`, `pipeline/`, `brief/`, `research/`, `calibration`, `doctor` | Typed. I/O only through injected collaborators (a `Transport`, an `EventSink`, a `/api/ps` probe), so it is tested without a network. |
| Adapters | `adapters/ollama_transport`, `adapters/ollama_instance`, `adapters/system_probe`, `adapters/outbound/` | The only code that opens connections or starts processes. Ollama adapters accept loopback URLs only; everything bound for the internet goes through `adapters/outbound/` (enforced by `tests/test_egress_guard.py`). |
| Graphs | `graphs/` | The only code that imports LangGraph (`tests/test_layer_rules.py`). Nodes call core logic; the rest of the code talks to a graph through `BriefRunner` or `ResearchRunner`. |
| Composition root | `bootstrap` | The one place that picks real adapters and wires them into a `Runtime`. |
| Entry points | `cli` (`udr`) | Thin: build the runtime, call pure logic, print. |

Boundaries for later milestones are in [AGENTS.md](../AGENTS.md) §5.2: prompts in `app/prompts/`,
LangGraph only in `app/graphs/`, the GUI only through the API client. Module details:
[llm-layer.md](llm-layer.md), [ollama-runtime.md](ollama-runtime.md), [outbound.md](outbound.md),
[vault.md](vault.md), [brief.md](brief.md), [research.md](research.md), [api.md](api.md),
[gui.md](gui.md).

## Quality gate flow

```
Claude edits a .py file  -> PostToolUse hook: ruff format (that file only)
Claude stops             -> Stop hook: if .py files changed, run the gate; exit 2 = keep working
git commit               -> pre-commit: the gate on staged files
push / pull request      -> CI: uv sync --locked, then the gate on all files (Python 3.11 and 3.14)
```

The gate itself is defined once, in `.pre-commit-config.yaml`. The Stop hook and CI only call it.

## Design decisions

- **Everything that talks to a model goes through `LLMService`.** Callers state a role and a
  Pydantic schema; retries, repair, truncation handling and telemetry live in one place
  ([llm-layer.md](llm-layer.md)).
- **One door out, checked at the wire.** The `OutboundGateway` re-checks the exact payload of every
  send against the denylist and every URL (and redirect hop) against the private-URL guard, and
  logs every attempt. Callers can prepare queries however they like; nothing reaches a provider
  unchecked ([outbound.md](outbound.md)).
- **Resumable everywhere (PRD AD10).** Progress is stored item by item, and a stage change and the
  rows it depends on are one transaction. A stopped run therefore restarts from its last committed
  stage: nothing stored is fetched again, finished model work is not redone, credits are not
  spent twice. Code that is interrupted must never be swallowed (`BaseException` is not caught),
  and each pipeline step has a kill-and-resume test ([vault.md](vault.md)).
- **Interrupts are model-free, checkpoints are synchronous.** LangGraph runs an interrupted node
  again from its start, so nodes with an `interrupt` do no model work and write nothing before it;
  every other node is idempotent. Checkpoints use `durability="sync"`: the default writes them in
  the background, and a real SIGKILL test showed it loses the last steps ([brief.md](brief.md)).
- **Phase 1 never reaches the Internet.** Its modules do not import the outbound package (an AST
  test and a fresh-process test), so "zero outbound requests" is structural, not a promise.
- **The owner approves what is sent.** The plan hash covers the sanitized query texts, never the
  local originals; an approval names that hash, and the gateway checks every payload again at the
  wire ([research.md](research.md)).
- **One research run at a time, by a lock the OS releases.** The worker slot is a lock file held
  only while a graph executes, so a killed run never blocks the next one.
- **The API never runs a research graph.** `udr serve` writes intent (approved, cancelled) into the
  `runs` table; `udr worker` is the queue and executes. A run left `running` is resumed first
  ([api.md](api.md)).
- **The GUI knows only the API.** `app.client` is its single import from the application and the
  only file besides the outbound package that uses `httpx`; both are enforced by tests
  ([gui.md](gui.md)).
- **Confidentiality fails closed, availability fails open.** A sanitizer error blocks the query.
  An exhausted or invalid Tavily account switches the run to ddgs instead of failing it.
- **Fail open, say so.** If our own Ollama instance cannot start, both endpoints use the shared
  daemon and a warning event is written, instead of the run failing. The doctor still reports it as
  an error.
- **Fakes at every seam.** `ScriptedTransport`, a fake instance probe and spawner, and an injectable
  clock make the 30-second startup timeout and the retry backoff testable instantly. Mutation checks
  (deliberately breaking the code) confirm the tests fail when the behavior is wrong; the M3 ones
  found a real race, a thread-unsafe file write, an untested transaction boundary and, in M4, a
  durability bug that only a real kill exposed.
- **Python 3.11 is the floor.** CI runs 3.11 and 3.14, so no syntax newer than 3.11;
  `tests/test_py311_syntax.py` enforces it.
- **One gate definition.** The pre-commit config is the only list of checks. The Stop hook and CI
  run `pre-commit run --all-files`, so the three can't drift apart.
- **Tool versions come from `uv.lock`.** The local pre-commit hooks use `language: unsupported`
  (the new name for `system`) and call `uv run <tool>`, so ruff, pyright and pytest aren't
  pinned a second time in the pre-commit config.
- **The edit hook formats but never lint-fixes.** `ruff check --fix` would delete an import
  that Claude adds one edit before its first use.
- **The Stop hook is cheap and can't loop.** It only runs when `.py` files changed. The
  `stop_hook_active` flag allows at most one forced continuation per stop.
- **Coverage runs only in the gate** (`pytest --cov`), not in `addopts`. Running a single test
  file must not fail on the coverage threshold.
- **Function length is checked by an AST test.** ruff has no rule for function lines;
  `tests/test_code_rules.py` has one.

## Known limits

- `pre-commit run --all-files` checks only files git tracks. New untracked files are formatted by
  the edit hook, and pyright and pytest cover the whole project. ruff lint reaches new files at
  commit time.
- gitleaks scans staged changes, so it protects commits but CI doesn't rescan history.
  Contributors must run `uv run pre-commit install`.
- Claude Code permission rules are not a security boundary. `Read(.env)` stops the Read tool,
  not every shell command. Keep real secrets out of the repo directory where you can.
