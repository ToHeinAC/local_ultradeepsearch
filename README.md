# local-ultradeepsearch

A local, two-phase deep research agent. Phase 1 clarifies the question with you and ends with a
brief you approve word for word. Phase 2 researches and writes a cited report, in a Lite or Full
depth, using only local Ollama models plus web search. It is a local port of
[ultradeep-researcher](https://github.com/ToHeinAC/ultradeep-researcher), built from the
[claude-dev-schema](https://github.com/ToHeinAC/claude-dev-schema) template. What and why:
[PRD.md](PRD.md).

## Status

Milestones M1 to M4 are done, and M5 is in progress:
- **M1:** configuration, the model-role registry, the LLM service, our own pinned Ollama instance,
  context calibration and `udr doctor`.
- **M2:** the outbound gateway. Denylist, private-URL guard, query sanitizer, outbound log, Tavily
  with automatic DuckDuckGo fallback, OpenAlex/Crossref/arXiv, and HTML/PDF fetching.
- **M3:** the per-run source vault. Fetched sources are filtered, deduplicated, summarised and
  stored with word-for-word verified claims. A stopped run continues where it stopped
  ([docs/vault.md](docs/vault.md)).
- **M4:** Phase 1. `udr brief` clarifies your question in a short dialog (with optional PDF, DOCX,
  MD or TXT context, OCR for scans) and ends with a brief you approve by its hash. No Internet is
  used, and a stopped session continues where it stopped ([docs/brief.md](docs/brief.md)).

- **M5 (in progress):** `udr run` plans the searches of a run, lets you approve exactly the queries
  that will be sent, then searches, writes, checks and exports the report
  ([docs/research.md](docs/research.md)). It has only run on fakes so far.

The GUI and the service API are not built yet. Phase status:
[IMPLEMENTATION.md](IMPLEMENTATION.md).

## Quickstart

Requirements: [uv](https://docs.astral.sh/uv/), git, Ollama with an NVIDIA GPU, and these models
pulled: `qwen3.8-27b:latest`, `gemma4:e4b`,
`deepseek-ocr:3b`. uv installs Python itself.

```bash
uv sync && uv run pre-commit install   # once per clone
cp .env.example .env                   # optional; every setting has a default
uv run udr doctor --calibrate          # checks the machine, measures the reason context
uv run udr denylist add "Client GmbH"  # terms that must never leave this machine
uv run udr brief "Meine Frage" -f a.pdf # Phase 1: clarify the question, approve the brief
uv run udr run <run_id>                # Phase 2: plan, approve the queries, report
uv run udr apikey create --name me --self-approve  # key for REST and MCP
uv run udr serve                       # API on 127.0.0.1:8541 (REST /v1, MCP /mcp)
uv run udr worker                      # executes the queued runs
uv run udr gui                         # German GUI on 127.0.0.1:8540 (needs UDR_GUI_API_KEY)
uv run pytest                          # offline tests
uv run pytest -m live                  # tests against the real models
```

`udr doctor` starts a second Ollama on `127.0.0.1:11436`, pinned to one GPU, if none is running
there. How that works, the settings and how to stop it:
[docs/ollama-runtime.md](docs/ollama-runtime.md).

The API, the keys and the worker: [docs/api.md](docs/api.md). The GUI: [docs/gui.md](docs/gui.md).

Web search uses your own SearXNG if `UDR_SEARXNG_URL` is set ([deploy/searxng/](deploy/searxng/README.md)), then Tavily if `TAVILY_API_KEY` is in `.env`, then DuckDuckGo. Everything that goes
out is checked against the denylist and logged; see [docs/outbound.md](docs/outbound.md).

## Layout

```
PRD.md               what and why (the contract)
IMPLEMENTATION.md    current state: phase table, module map, run/verify
AGENTS.md            rules for every AI coding tool (Claude Code via CLAUDE.md, Codex natively)
CLAUDE.md            imports AGENTS.md and IMPLEMENTATION.md
docs/                architecture, component docs, per-milestone plans in docs/plans/
src/app/             code; module map in IMPLEMENTATION.md
tests/               pytest suite (offline), live/ model checks, and the repo rule checks
.claude/settings.json      shared permissions and hooks
.claude/hooks/             hook scripts (tested in tests/)
.claude/commands/          /commit-git, /documentation-update
.pre-commit-config.yaml    the quality gate
.github/workflows/ci.yml   runs the gate on Python 3.11 and 3.14
```

## Where each rule is enforced

| Rule | Edit hook | Stop hook | Commit | CI |
|---|---|---|---|---|
| Formatting (ruff) | yes | yes | yes | yes |
| Lint, complexity ≤ 10 (ruff); types (pyright) | | yes | yes | yes |
| Tests, branch coverage ≥ 85 %, suite ≤ 60 s | | yes | yes | yes |
| Functions ≤ 50 lines; doc size limits and links | | yes | yes | yes |
| No `.env` files, no private keys | | yes | yes | yes |
| Secret scan of staged changes (gitleaks) | | | yes | |

The Stop hook runs only when `.py` files changed. The process rules (red-green, small commits,
docs updates) live in [AGENTS.md](AGENTS.md) §5.3, and the review checks them.
Details: [docs/architecture.md](docs/architecture.md).

## Skills and commands

In this repository (every clone gets them):

- `/commit-git`: small Conventional Commits through the gate. Only you can invoke it, and it never pushes without asking.
- `/documentation-update`: brings the docs in line with the code since the last commit.

Enabled as plugins in `.claude/settings.json` (Claude Code asks to install them when you trust the
folder; nothing is vendored):

- `frontend-design` from [anthropics/claude-code](https://github.com/anthropics/claude-code/tree/main/plugins/frontend-design):
  distinctive, production-grade frontend UIs.
- `ui-ux-pro-max` from [nextlevelbuilder/ui-ux-pro-max-skill](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill)
  (MIT): UI/UX design data and search, plus the `ui-styling`, `design`, `design-system`, `brand`,
  `banner-design` and `slides` skills.

Not in this repository:

- `first-principles-mindmap` (writes `MINDMAP.md`) and `prd-from-mindmap` (writes `PRD.md`) are
  personal skills in the author's claude.ai account (Customize > Skills). Other users can upload
  their own, or fill in `PRD.md` by hand; its headings are the contract.
- Claude Code built-ins such as `/code-review`, `/security-review` and `/simplify` need no setup.

## Credits and license

AGENTS.md §1–4 come from
[andrej-karpathy-skills](https://github.com/forrestchang/andrej-karpathy-skills) (MIT), see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Everything else: Apache-2.0, see [LICENSE](LICENSE).
