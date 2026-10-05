# Phase 1: the brief

Requirements: [PRD.md](../PRD.md) M4, AD6, AD10. Plan: [plans/m4-brief.md](plans/m4-brief.md).
Phase 1 turns the owner's first message, and optionally some files, into one approved brief.
Nothing in it uses the Internet, and no run can exist without an approved brief.

## Flow

```
ingest_uploads -> assess -> ask (interrupt) -> ingest_uploads ...      the question rounds
ingest_uploads -> draft_brief          after the last round (files added in it are read too)
                      \-> offer (interrupt) -> strengthen_brief | install_verbatim
draft_brief -> recommend -> decide (interrupt) -> revise | edit | settings | save -> decide
                                              \-> finalize -> END (a queued run exists)
```

- **Checklist.** Seven items, each `clear`, `assumed` or `missing`: question, context, goal,
  audience, scope, output, depth. Only the four content items (context, goal, audience, scope) can
  stay open. Depth is never `missing`: the owner picks Lite or Full at approval, with a
  recommendation. Output has defaults.
- **Rounds.** At most 5, with at most 5 questions each, every question with a drafted proposal. The
  owner accepts it (Enter), types an answer, or says "don't know" (`?`). `genug` ends the rounds;
  the questions left over count as unknown. The model never answers for the owner.
- **A pasted finished prompt** is offered first: strengthen it once, or install it byte for byte
  (with a `Method` line and the Output section around it). A prompt without a `# ` title or
  numbered questions cannot be installed as it is; it is explained and strengthened.

## What code guarantees (not the model)

- Open content items are listed on the `Method` line and under Assumptions as "not clarified",
  never filled in; every "don't know" question becomes a numbered research question; headings in
  model text are escaped so they cannot change the structure.
- The Output section comes from the session settings and configuration only: language, response
  format and its word range, template headings, `Quellen`/`Sources`, a language-mismatch notice.
  A settings change re-renders only this section, so a hand edit survives.
- A question with a clear item, a repeated question, or a depth question is dropped; at most the
  configured number is asked, missing items first.
- The brief text is canonical (LF, one final newline) and its approval hash is the sha256 of those
  UTF-8 bytes. The archive holds exactly those bytes, with no front matter.

## Approval

`BriefService.approve(session_id, sha256, tier)` creates the run only if the hash is that of the
current brief and the session waits at its decision (`StaleBrief`, `WrongState` otherwise). The
check happens in the database: the session row holds the current text and hash, and
`RunStore.approve` verifies them, creates the `queued` run and marks the session approved in one
transaction. The brief is archived read-only at `data/briefs/<UTC>.md` before that, complete or not
at all; the same bytes archived again return the same file. `tier` may be `auto` (the
recommendation). One lock per session means the first of two approvals wins.

## Uploads

- **Limits** (`config/profiles.toml` `[phase1]`): 10 files, 50 MB each, 500 pages in total; PDF,
  DOCX, MD, TXT by extension and magic bytes. Encrypted, corrupt, empty and binary files are
  rejected with the file name and the rule. A rejected batch leaves no rows and no files.
- **Reading.** PDF text per page; a page with fewer than 50 characters goes to the `ocr` role as a
  greyscale PNG at 200 dpi, `clean_ocr` strips the OCR model's markup tokens, and the longer text wins. A missing OCR model skips the remaining
  scanned pages with one warning. DOCX, MD and TXT are cut into pseudo pages of 3000 characters.
- **Distilling.** The `summarize` role lists facts per part of a file (page markers inside an
  untrusted-source fence); facts naming a page outside their part are dropped.
- **Digest.** At most 500 words of facts, each rendered by code as `- fact (file, S. n)` after
  checking the file and page; too many facts are condensed in stages; a failing model gives a plain
  digest. The digest is the brief's "context from uploads" section.

## Resuming (PRD AD10)

- Graph checkpoints use `durability="sync"`: each step is on disk before the next one starts.
  LangGraph's default writes them in the background, and a real SIGKILL test showed it loses the
  last steps.
- LangGraph runs an interrupted node again from its start, so `ask`, `offer` and `decide` do no
  model work and write nothing before the interrupt. Every other node is idempotent, and `finalize`
  returns the same archive file and the same run when it runs twice.
- Uploads save per file (text and OCR once) and per part of distillation. A file becomes
  `distilled` in the same transaction as the digest that covers it, so a crash while digesting
  rebuilds the digest from the saved parts.
- `recover()` after a restart removes sessions that never got going, continues those that stopped
  between steps and cleans orphan upload files. A model error does not lose a session: the view
  says `waiting_for == "work"` with the error, and `retry` continues.
- Inputs are validated before a graph resumes (`app.brief.protocol`), so an invalid value never
  becomes a pending write.

## Using it

```bash
uv run udr brief "Wie lange dauert der Rückbau eines Forschungsreaktors?" --file bericht.pdf
uv run udr brief --list                # open and parked sessions
uv run udr brief --session sabc123456789   # continue one
```

Prompts are German. At the decision: Freigeben, Überarbeiten, Bearbeiten (`$EDITOR`),
Einstellungen, Speichern, Beenden. "Speichern" parks the session and writes
`data/briefs/drafts/<session>.md`. Library use: `bootstrap.build_brief_service(rt)`; REST and MCP
(M6) and the GUI (M7) call the same `BriefService`.

## Where things are

| What | Where |
|---|---|
| Sessions, uploads, facts, approved runs | `data/udr.sqlite` (migration 2) |
| Graph checkpoints | `data/checkpoints.sqlite` |
| Uploaded files (by content hash) | `data/uploads/<session>/` |
| Approved briefs; parked drafts | `data/briefs/<UTC>.md`; `data/briefs/drafts/` |
| Report templates | `templates/` (built in), `data/templates/` (the owner's) |
| Events | `data/events.jsonl` (`upload_*`, `llm_call`) |

## Tests

`tests/brief_rig.py` holds the scripted model and the wired session. Offline: rendering, store,
documents, uploads, digest, interview, graph, service and console tests, plus a real SIGKILL of a
child process (`tests/brief_crash_child.py`), which `recover()` then finishes. M4 AC3 holds because
a full session with uploads makes no outbound attempt and the Phase-1 modules never load the
outbound package (`tests/test_egress_guard.py`, a fresh-process test).
