# Phase 2: the research run

Requirements: [PRD.md](../PRD.md) M5, AD1, AD4 to AD10, §3.2, §3.9, §3.10. Plan and the owner
decisions D1 to D12: [plans/m5-lite.md](plans/m5-lite.md). Phase 2 turns the approved brief of a
`queued` run into a cited report, or into an honestly `blocked` one. Only the Lite tier exists.

## Flow

```
bootstrap -> decompose -> plan -> approve_plan (interrupt) -> sweep -> draft -> polish
          -> readability -> gate -> export -> END
  step 0       1          2.1       the owner approves          2       10      15
                                                                              16       G       X
```

`graphs/research.py` is the graph and `ResearchRunner` (thread id = run id, the checkpointer file
of the brief graph, `durability="sync"`). The edges are the step list, so no model decides what
runs next. `research/steps.py` has one method per node. Every step is idempotent: it records its
transitions in `run.json`, reads what earlier steps stored (never graph state), and does nothing
when it has finished. The interrupt node does no model work and writes nothing before the
interrupt, because LangGraph runs an interrupted node again from its start ([brief.md](brief.md)).

## Steps

- **0, bootstrap.** `query.md` is the archived brief, never scrubbed. The report settings come
  from `runs.settings_json`, frozen when the brief was approved (`settings.py`); a run approved
  before that falls back to its session.
- **1, decomposition** (`decompose.py`). The brief's numbered questions are parsed by code; the
  model adds entities, time periods, domains, the voice and, for the template `auto`, the headings.
  A coverage check against the brief's own phrases, shims and `scaffold.md`. The owner's tier is
  binding; the model's recommendation is only recorded.
- **2.1, search plan** (`plan.py`). Lenses A breadth, B scholarly, C adversarial, D period-pinned;
  code enforces the profile's bounds and sends every query through the gateway's denylist and
  sanitizer. `search-plan.json` holds `plan_sha256`. The owner approves by that hash, so exactly
  the sanitized plan is searched. The service can replace the plan from edited text
  (`plan_edit.py`: one query per line; only changed or new queries are checked again).
- **2, sweep** (`sweep.py`, `candidates.py`). Every search is stored before its results are used,
  so a resumed run never sends a query twice and derives the same candidates and coverage again.
  Lens B uses OpenAlex and Crossref (arXiv for the STEM domains), the rest the web search. Thin
  items get one second wave. A provider outage stores an empty search and documents the gap.
- **10, draft** (`draft.py`, `evidence.py`). 8 to 15 must-read sources, chosen once
  (`temp/must-read.json`); one `reason` call per section over its own evidence pack, saved as soon
  as it exists. Evidence that does not fit is condensed by `summarize`, never cut silently. A
  section without evidence states the gap.
- **15 and 16, polish and readability** (`polish.py`, `readability.py`, `hunks.py`). A report
  changes only through hunks: `old` occurs once, is short, touches no heading. Polish cuts only;
  readability applies the allowed categories in the original order. Every decision is logged.
- **G, ship gate** (`shipgate.py`, `gate.py`, `fixes_code.py`, `fixes_model.py`). Checks G1 to G12,
  up to `fix_rounds` rounds; one-time fixes are remembered in `temp/gate-state.json`. A check that
  stays failing leaves the run `blocked`; `report.md` and the exports stay downloadable and
  `gate.json` lists every round.
- **X, export** (`export.py`, `adapters/pandoc.py`). `report.docx` and `report.pdf` through the
  pandoc binary, checked after the fact (every H2 in the DOCX, `%PDF` in the PDF). A missing or
  failing pandoc fails the exports and nothing else.

Citations are owned by code ([PRD.md](../PRD.md) §3.9): sections hold keys like `[S3]`;
`report.py` and `sections.py` render `report.md` from the section files with numbers by first
appearance, the Sources list and the appendix.

## Service and command

`ResearchService` (`research/service.py`) is what `udr run` calls and REST and MCP will call in
M6: `create_external_run`, `run`, `view`, `plan_text`, `update_plan`, `approve_plan`. It owns the
run status: `running` while a graph executes, `awaiting_plan_approval` while the owner reviews,
`done` or `blocked` at the end, `failed` if a step raised (with the step and the reason in
`run.json`). `run` continues a failed or interrupted run from its last checkpoint; a crash signal
(`BaseException`) is never caught. A run whose row says `running` while its graph already waits
for the plan is set back to `awaiting_plan_approval`.

The worker slot is the lock file `data/worker.lock` (`worker.py`), held only while a graph
executes and released at the plan interrupt; the operating system frees it when its holder dies.
A second `udr run` while it is held exits with "Ein anderer Lauf ist aktiv.".

`udr run RUN_ID` starts or continues an approved run and opens the German plan review
(`research/console.py`): table, then `f` approve, `b` edit in `$EDITOR`, `l` delete, `n` new,
`q` quit. `--approve-plan SHA` approves a hash without a dialog, `--no-input` prints the plan and
the command that approves it. `udr run --brief FILE --tier light --template ID [--language L]
[--format F]` creates the run of an external brief (Method line `extern übergeben`, rendered Output
section; running it from the owner's shell is the approval) and continues as above. Exit codes: 0
done or waiting; 1 failed, blocked, unknown run or another run active; 2 bad input, a stale or
blocked plan, tier `full` ("Full-Tier ab M8").

`bootstrap.build_research_service(rt)` wires it: one gateway per run and process (preparer,
searcher and fetcher, with the tier's credit cap), the fetch pipeline, the vault, the model
registry's context sizes, `SubprocessPandoc` and the lock.

## Where files live

| What | Where |
|---|---|
| Runs, settings, searches | `data/udr.sqlite` |
| Checkpoints, worker slot | `data/checkpoints.sqlite`, `data/worker.lock` |
| Archived briefs | `data/briefs/` |
| Files of a run | `data/runs/<run_id>/`: `query.md`, `scaffold.md`, `run.json`, `prompt-decomposition.json`, `search-plan.json`, `shims/`, `notes/`, `temp/` (sections, must-read set, evidence keys, gate state), `polish-log.json`, `readability-*.json`, `report.md`, `gate.json`, `report.docx`, `report.pdf` |

## Tests

`test_research_*.py` per module, `test_research_service.py` for a whole Lite run on fakes,
`test_bootstrap_research.py` for the real composition with fakes at the edges only,
`test_cli_run.py` and `test_research_console.py` for the command. Kill and resume:
`test_research_crash.py` SIGKILLs a child process (`research_crash_child.py`) in its third search
and in its third section; a fresh process finishes the run and repeats nothing it had stored. The
rigs are `tests/research_rig.py` and `tests/research_run_rig.py`.
