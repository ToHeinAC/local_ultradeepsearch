# IMPLEMENTATION

Current state of the code, and the only place for phase status. What and why: [PRD.md](PRD.md).
Rules: [AGENTS.md](AGENTS.md). Design: [docs/architecture.md](docs/architecture.md).

## 1. Run and verify

| Task | Command |
|---|---|
| Install (once per clone) | `uv sync && uv run pre-commit install` |
| Tests (fast loop) | `uv run pytest` or `uv run pytest tests/test_core.py` |
| Full gate | `uv run pre-commit run --all-files` |

## 2. Phase status

One row per PRD milestone. Status: `planned`, `in progress`, `done`.

| Phase | Milestone | Status | Verified by |
|---|---|---|---|
| 0 | Blueprint skeleton (no PRD milestone) | done | full gate green |
| 1 | M1: Foundation and model infrastructure | planned | ollama adapter, role registry, doctor tests |
| 2 | M2: Outbound gateway and retrieval adapters | planned | denylist/guard/ledger tests, egress AST test |
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
| `src/app/core.py` | Example of pure logic (`slugify`). Replace it with your own. |
| `tests/conftest.py` | Shared fixtures; blocks network access in all tests. |
| `tests/test_code_rules.py` | Enforces functions ≤ 50 lines in `src/`, `tests/`, `.claude/hooks/`. |
| `tests/test_docs.py` | Enforces doc size limits and resolvable local links. |
| `.claude/hooks/format_on_edit.py` | PostToolUse hook: ruff-formats each `.py` file Claude edits. |
| `.claude/hooks/stop_gate.py` | Stop hook: runs the gate if `.py` files changed; blocks the stop on failure. |

## 4. Open issues

- README.md still describes the claude-dev-schema template; replace it in M1.
- `src/app/core.py` and `tests/test_core.py` are template examples; remove them once M1 adds code.
