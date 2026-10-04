# Phase 2: the research run

Requirements: [PRD.md](../PRD.md) M5, AD1, AD5 to AD10. Plan: [plans/m5-lite.md](plans/m5-lite.md).
Phase 2 turns an approved brief into a run. Part A (steps 0, 1, 2.1 and 2) exists; drafting,
polish, readability, the ship gate and export (Part B) are planned.

## Flow

```
bootstrap -> decompose -> plan -> approve_plan (interrupt) -> sweep -> finish -> END
   step 0      step 1      2.1        the owner approves        step 2
```

`graphs/research.py` holds the graph and `ResearchRunner` (thread id = run id, the checkpointer
file of the brief graph, `durability="sync"`). `research/steps.py` (`LightSteps`) has one method
per node; each wraps its work in `Manifest.start_step` and `finish_step`, so `run.json` lists the
steps in the order they ran. The interrupt node does no work before the interrupt; every other
node is idempotent ([brief.md](brief.md) explains why).

## Steps

- **0, workspace** (`workspace.py`). `query.md` holds the archived brief byte for byte (never
  scrubbed); `scaffold.md` is the run's notebook with one replaceable `## ` section per topic.
  `read_brief` refuses a missing archive or one whose bytes no longer match the approved hash.
- **1, decomposition** (`decompose.py`). `reason` with thinking splits the brief into items
  (`i01`... : sub-questions, then entities, then time periods), then up to
  `coverage_iterations` coverage checks, with a revision after each check that has gaps except the
  last. Gaps left go to `scaffold.md` and an event; a failed check ends the loop with a warning.
  Headings: a fixed template's exactly; for `auto` the derived ones, validated with the template
  rules (`templates.heading_problem`), one repair call, then one heading per sub-question.
  Section weights are clamped and become word budgets that add up to the middle of the format's
  word range. Every model answer is kept in `temp/step1.json`, so a resumed step makes no call
  twice. The lever shims (`shims/*.md`) are composed by code from the upstream texts in
  `prompts/shims.py`.
- **2.1, search plan** (`plan.py`). `reason` drafts queries through the lenses breadth, depth,
  adversarial and, with time periods, period. Code drops unknown items, empty and duplicate
  queries, trims to the maximum (adversarial first, then round-robin over items) and asks one
  repair call if the plan falls short. Rows are stored as `plan_queries` (wave 1) and sanitized one
  by one through the gateway; a denylist hit or a sanitizer failure makes the row `blocked` before
  any provider sees it. Depth queries use the scholarly channel only when an assigned domain is
  scholarly-first.
- **Approval.** `plan_sha256` covers the wave-1 rows that are not deleted, as sent (never the local
  original). The owner can edit, delete and add queries; each is sanitized again and rewrites
  `search-plan.json`. `ResearchService.approve_plan` needs the current hash (`StalePlan`), no
  blocked row and at least one planned row (`PlanBlocked`), and re-checks every sent text against a
  denylist loaded fresh; hits are blocked and refused. `LightSteps.approve` checks the hash again.
- **2, width sweep** (`sweep.py`). Planned rows are searched in id order; a row becomes `done`
  together with its hits in one statement, so a search is never repeated. `build_queue` takes the
  first N hits, deduplicates, drops known sources, orders each item's URLs by host tier and takes
  them round-robin up to the cap; the queue of each wave is kept in `temp/sweep.json`, so a resume
  fetches the same URLs. Coverage counts complete, original, non-Wikipedia sources found by an
  item's queries (`uncovered`, `thin`, `adequate`, `well`). Thin or uncovered items get one wave 2
  (capped queries, sanitized without a human, never redrafted once rows exist). The result is
  `temp/coverage-gaps.md`.

## Service and command

`ResearchService` (`research/service.py`, one lock per run id) is what `udr run` calls now and
REST and MCP will call in M6: `start`, `resume`, `get`, `plan`, `edit_query`, `delete_query`,
`add_query`, `approve_plan`, `prepare_external`, `create_external_run`, `list_runs`. A model error
or `ResearchError` marks the run `failed` with its reason; `start` continues it from the last
checkpoint. A crash (any `BaseException`) is never caught. `start` refuses a run without a hash, a
tampered archive and tier `full` ("Full-Tier ab M8").

`udr run RUN_ID [--no-input]` starts a queued or failed run and continues a waiting or interrupted
one; `udr run --brief FILE --tier light --template ID [--format F] [--language L] [--yes]` first
shows the brief as it will be archived (Method line `extern geliefert`, rendered Output section)
with its sha256 and asks before creating the run; `udr run --list`. The plan review
(`research/console.py`) is German: table, then `f` approve, `b` edit, `l` delete, `n` new, `q` quit.
Exit codes: 0 done or waiting, 1 failed or error, 2 bad input.

## Where files live

| What | Where |
|---|---|
| Runs, plan queries, run settings | `data/udr.sqlite` (migration 3) |
| Checkpoints | `data/checkpoints.sqlite` |
| Archived briefs | `data/briefs/` |
| Files of a run | `data/runs/<run_id>/`: `query.md`, `scaffold.md`, `run.json`, `events.jsonl`, `outbound.jsonl`, `prompt-decomposition.json`, `search-plan.json`, `shims/`, `temp/`, `notes/` |

## Tests

`test_research_{store,workspace,decompose,plan,sweep,service,console,resume}.py`, `test_cli_run.py`
on the rigs in `tests/research_rig.py` (a fake gateway with a denylist and call counts, scripted
models, the wired service) and `tests/research_crash_child.py`. Kill and resume: three in-process
crashes (sanitizing, searching, wave-2 drafting) and a real SIGKILL in the fourth search.
