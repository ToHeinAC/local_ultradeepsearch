# Architecture

## Code layers

| Layer | Where | Rule |
|---|---|---|
| Core logic | `config`, `llm/{types,roles,structured,service,errors}`, `events`, `artifacts`, `calibration`, `doctor` | Typed. I/O only through injected collaborators (a `Transport`, an `EventSink`, a `/api/ps` probe), so it is tested without a network. |
| Adapters | `adapters/ollama_transport`, `adapters/ollama_instance`, `adapters/system_probe` | The only code that opens connections or starts processes. Ollama adapters accept loopback URLs only. |
| Composition root | `bootstrap` | The one place that picks real adapters and wires them into a `Runtime`. |
| Entry points | `cli` (`udr`) | Thin: build the runtime, call pure logic, print. |

Boundaries for later milestones are in [AGENTS.md](../AGENTS.md) §5.2: prompts in `app/prompts/`,
LangGraph only in `app/graphs/`, the GUI only through the API client. Module details:
[llm-layer.md](llm-layer.md), [ollama-runtime.md](ollama-runtime.md).

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
- **Fail open, say so.** If our own Ollama instance cannot start, both endpoints use the shared
  daemon and a warning event is written, instead of the run failing. The doctor still reports it as
  an error.
- **Fakes at every seam.** `ScriptedTransport`, a fake instance probe and spawner, and an injectable
  clock make the 30-second startup timeout and the retry backoff testable instantly. Mutation checks
  (deliberately breaking the code) confirmed the tests fail when the behavior is wrong.
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
