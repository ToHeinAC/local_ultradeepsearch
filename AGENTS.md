# AGENTS.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

## 5. Project specific instructions

§1–4 are adopted verbatim from [andrej-karpathy-skills](https://github.com/forrestchang/andrej-karpathy-skills)
(MIT, see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)); do not edit them. §5 is this project's
contract; §5.4 lists what enforces it.

### 5.1 Documentation

Read [IMPLEMENTATION.md](IMPLEMENTATION.md) first (Claude Code auto-loads it via `CLAUDE.md`). Open the
other files only when the task needs them, to keep context small.

| File | Holds | Max lines |
|---|---|---|
| [PRD.md](PRD.md) | What and why: problem, non-goals, milestones with acceptance criteria. Changed only with the user's approval. | 800 |
| [IMPLEMENTATION.md](IMPLEMENTATION.md) | Current state: phase table (one row per PRD milestone), module map, run/verify. Only place for phase status. | 500 |
| [docs/architecture.md](docs/architecture.md) | Module responsibilities, data flow, design decisions. | 800 |
| `docs/<component>.md` | Deep docs of one component, linked from IMPLEMENTATION.md. | 800 |
| [README.md](README.md) | For humans: purpose, quickstart, where rules are enforced. | 300 |
| AGENTS.md | Rules (this file). | 200 |

- State each fact once; link to it instead of repeating it.
- After a change that affects docs, run `/documentation-update`.

### 5.2 Conventions

- Python ≥ 3.11. Environment via `uv`: `uv sync` once, then `uv run <cmd>`. Add dependencies with
  `uv add <pkg>` (dev tools: `uv add --dev <pkg>`), never `pip install`. Commit `uv.lock`.
- Layout: code in `src/app/`, tests in `tests/`.
- Functions ≤ 50 lines, cyclomatic complexity ≤ 10.
- Type hints everywhere; `src/` passes pyright strict.
- I/O (network, files, databases) lives in dedicated adapter modules. Core logic stays pure and is
  tested without I/O.
- Configuration via environment variables. Secrets only in `.env`; `.env.example` lists the keys
  without values. Runtime data goes in `data/`. Both are gitignored.
- Plan and implement token-efficiently. The first implementation must be review-ready: a second
  tool (e.g. Codex) reviews every change against this file.

### 5.3 Workflow and definition of done

1. **Shape**: a new product or large feature needs `PRD.md` first (README: "Skills and commands").
   Add its milestones to the phase table in IMPLEMENTATION.md.
2. **Red**: write the test first, run it, and confirm it fails for the expected reason.
3. **Green**: write the minimum code that makes it pass.
4. **Gate**: `uv run pre-commit run --all-files` passes.
5. **Docs**: `/documentation-update`.
6. **Commit**: `/commit-git`. Push only when the user asks.

A change is done when steps 2–6 are complete. Report the red and green results in the summary.

### 5.4 Testing and quality gate

- pytest; all tests live in `tests/` and are written in Python.
- The suite is offline: `tests/conftest.py` blocks sockets. Use synthetic fixtures.
- Red-green rule: a test counts only if it was seen failing against missing or wrong code, and
  passing against correct code. A repo-wide guard test also needs a test that feeds its detector
  a violating input.
- The gate is `.pre-commit-config.yaml`; its numbers live in `pyproject.toml`:

| Rule | Enforced by |
|---|---|
| Format, lint, complexity ≤ 10 | ruff |
| Types (strict in `src/`) | pyright |
| Branch coverage ≥ 85 %, suite ≤ 60 s | pytest-cov `fail_under`, pytest-timeout `session_timeout` |
| Functions ≤ 50 lines | `tests/test_code_rules.py` |
| Doc size limits, no broken local links | `tests/test_docs.py` |
| No secrets, no `.env`, no private keys | gitleaks, `no-env-files`, `detect-private-key` |

- The gate runs on every commit (pre-commit), when Claude stops with changed `.py` files (Stop hook),
  and in CI. Never bypass it: `--no-verify` is denied.

### 5.5 Licensing

The project is Apache-2.0 ([LICENSE](LICENSE)). Dependencies and copied code must use a permissive
license compatible with Apache-2.0 (MIT, BSD, ISC, PSF, Apache-2.0). Record copied code in
THIRD_PARTY_NOTICES.md.
