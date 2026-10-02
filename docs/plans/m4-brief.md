# M4 implementation plan — Phase 1: clarification, uploads, brief

**Executor:** Sonnet 5.5, effort high, in `/home/he/ai/dev/langgraph/local_ultradeepsearch`.
- Follow [AGENTS.md](../../AGENTS.md) for every step: red → green → gate → commit.
- No push unless asked. End commit messages with the attribution line from the session's system
  reminder.
- Mutation-check every step before its commit (break the code on purpose, confirm a test fails).
  In M3 this found a race, a thread-unsafe write and an untested transaction boundary.

## Context

M1–M3 are done and pushed. M4 builds Phase 1: the owner (later also REST/MCP callers) states a
question, optionally uploads files, answers a few rounds of clarifying questions, and approves the
exact brief text. Phase 2 (M5) starts only from an approved brief.
Requirement source: `PRD.md` §1, §3.1, §3.5 (AD1, AD2, AD3, AD6, AD8, AD10), §3.6 (tier choice),
§3.9, §4 M4. Upstream original: `../ultradeep-researcher/.claude/skills/research-brief/` (8-gate
interview; the PRD replaces it with a lean adaptive checklist) and the tier rules of
`hyperresearch-1-decompose` (step 7).

**Owner decisions (2026-10-02), binding for this plan:**
1. **Open items after "genug" or round 5:** content items (context, goal/decision, audience, scope)
   are listed under assumptions as "not clarified"; nothing is invented. Output items take visible
   defaults: report language = interview language, response format per the §3.6/step-1 rules,
   template `auto`; depth = the recommendation (shown, chosen explicitly at approval).
2. **Upload context in the brief:** a digest of at most 500 words with file/page provenance.
3. **Direct text edit** of the brief is allowed besides `revise(feedback)`. The edited text gets a
   new hash without an LLM call; it must still parse (title and numbered research questions).
4. **Pasted finished prompt, strengthening declined:** the pasted text stays byte-for-byte; code
   adds the `Method` line and the Output section around it.
5. **Thinking:** off in the question rounds (`assess`); on for draft, revise, strengthen and the
   tier recommendation.
6. **Template is chosen in Phase 1** and rendered into the brief. Report language, response format
   and template are session settings; changing one re-renders the Output section (new hash).
   `approve` takes the hash and the tier; template and format come from the brief. The template
   loader and the four built-in templates move from M5 into M4.
7. **"save"** parks the session at its decision point (status `saved`, resumable) and writes the
   draft to `data/briefs/drafts/<session_id>.md`.
8. **Interview language:** `langdetect` (seeded) on the first message when it has ≥ 20 characters
   and probability ≥ 0.9; otherwise German.

Adopted defaults (challenge if wrong): brief labels exist in German and English; any other
interview language uses English labels. Phase-1 events go to
`data/uploads/<session_id>/events.jsonl`. Uploads are accepted only while the interview runs (not
after the draft).

## Step 0 — Contract updates (one docs commit)

1. Keep this plan at `docs/plans/m4-brief.md`; set the M4 row of `IMPLEMENTATION.md` to
   `in progress` with a link to it.
2. **PRD edits** (owner-approved through this plan). `PRD.md` is at 800 lines, its limit; the edits
   below are net zero. Check with `uv run pytest tests/test_docs.py -q`.
   - Replace the whole M4 section (from `### M4` to its `**Dependencies:**` line) with:
     ```
     ### M4 — Phase 1: clarification, uploads, brief
     - **Deliverable:**
       - **The `brief` graph:** `ingest_uploads → assess → ask (interrupt) ↺ → draft_brief →
         decide (interrupt: approve | revise | edit | settings | save) → finalize`.
       - **Checklist.** Seven items, each `clear | assumed | missing`: question, context,
         goal/decision, audience, scope, output (language, format/length, template), depth (tier).
       - **Rounds.**
         - Up to 5 questions per round, each with a drafted candidate answer the owner accepts, edits
           or replaces. The system never answers on the owner's behalf.
         - At most 5 rounds; "genug" ends early. Content items still open are listed as "not
           clarified"; output items take shown defaults (interview language, §3.6 rules, `auto`).
         - Unknowns become numbered research questions. Tier recommendation per §3.6.
       - **Uploads.**
         - PDF via pypdfium2 (pages with fewer than 50 text chars are OCRed with `deepseek-ocr` at
           200 dpi); DOCX via python-docx; MD/TXT as-is.
         - `summarize` distils each file into facts with file and page provenance. The brief carries
           a digest of at most 500 words.
       - **Brief template.** Labels are in the interview language. Sections, in order:
         1. `# question`, then a `Method:` line (rounds, skipped items), then audience/decision.
         2. Background and context; context from uploads (only if any); goal.
         3. Numbered research questions; scope (in / out / non-negotiables); assumptions; what a good
            answer looks like.
         4. Output: report language; sources in any language; quotation policy; register;
            `response_format` and target words; inline citations; template name and headings.
       - **Settings and edits.** Report language, format and template are session settings rendered
         into Output by code (new hash); a direct edit must keep the title and questions parseable.
       - **Approval and storage.** Approval is by sha256. The approved brief is archived immutable at
         `data/briefs/<UTC>.md`, with no front matter; "save" parks the session and writes
         `data/briefs/drafts/<session_id>.md`. `udr brief` is the interactive CLI.
     - **Acceptance criteria:**
       1. No research run can be created unless `approve(session_id, sha256)` matches the current
          brief; a stale hash is rejected.
       2. The archived bytes equal the approved bytes.
       3. A gateway fake records **zero** outbound calls over a full Phase-1 session with uploads.
       4. `revise(feedback)` yields a new draft and hash; the old hash no longer approves.
       5. "genug" after round 1 lists every `missing` item under assumptions.
       6. A session survives a service restart and resumes at its pending interrupt.
       7. Question prompts carry the interview language taken from the owner's first message.
       8. Limits: at most 10 files, 50 MB each, 500 pages in total. Violations get a clear error, and
          nothing is partially stored.
     - **Edge cases:**
       - Empty question: 422. Encrypted PDF: rejected. Oversized context: map-reduce with a notice.
       - A pasted finished prompt: one strengthening pass is offered; it may be installed verbatim
         (code adds the `Method` line and the Output section).
       - OCR model missing: scanned pages are skipped with a warning.
       - "weiß nicht" becomes a research question.
     - **Dependencies:** M1. M2's gateway fake is needed for criterion 3.
     ```
   - **M6 REST table, `sessions` row:** replace
     `` `POST …/approve {brief_sha256, tier, template_id, response_format?, summarize_model?}` ``
     with
     `` `PUT …/settings`; `PUT …/brief` (direct edit); `POST …/approve {brief_sha256, tier, summarize_model?}` ``
     (one table line, so no line is added).
3. `IMPLEMENTATION.md` §4: note that the template loader and built-ins moved from M5 to M4.

Commit: `docs: M4 plan and Phase-1 decisions`.

## Layout (new code)

```
config/profiles.toml            + [phase1] limits, + [response_formats] word/citation ranges (§3.8)
templates/                      auto.md, technische-stellungnahme.md, regulatorische-analyse.md,
                                literaturuebersicht.md, markt-unternehmensanalyse.md
src/app/templates.py            ReportTemplate, load_templates(dirs), get_template(), validation
src/app/documents.py            pdf_pages(), pdf_is_encrypted(), render_page_png(), docx_text(),
                                text_pages(); stdlib PNG encoder (no Pillow)
src/app/brief/__init__.py
src/app/brief/models.py         Phase1Limits, SessionSettings, Answer, Decision, SessionView, errors
src/app/brief/schemas.py        LLM I/O: Assessment, ChecklistEntry, Question, BriefDraft,
                                TierRecommendation, UploadFacts, UploadDigest
src/app/brief/language.py       detect_interview_language()
src/app/brief/labels.py         LABELS["de"], LABELS["en"]; labels_for(lang)
src/app/brief/render.py         render_brief(), render_verbatim(), canonical_text(), brief_sha256()
src/app/brief/parse.py          parse_brief(text) -> ParsedBrief(title, research_questions)
src/app/brief/uploads.py        validate_uploads() (AC8), page extraction + OCR, distil per part,
                                digest (map-reduce with notice)
src/app/brief/interview.py      Interviewer: assess, draft, revise, strengthen, recommend_tier
src/app/brief/archive.py        archive_brief() (immutable), write_draft()
src/app/brief/service.py        BriefService: the API that CLI now, REST/MCP in M6 call
src/app/graphs/__init__.py
src/app/graphs/brief.py         build_brief_graph(nodes, checkpointer) — LangGraph only here
src/app/prompts/brief.py        ASSESS_*, DRAFT_*, REVISE_*, STRENGTHEN_*, TIER_*, UPLOAD_FACTS_*,
                                UPLOAD_DIGEST_*, OCR_PAGE
src/app/store/db.py             + migration 2
src/app/store/sessions.py       SessionStore (sessions, uploads, upload_parts)
src/app/store/runs.py           RunStore: create_approved_run(), get_run()
src/app/bootstrap.py            + open_checkpointer(), build_brief_service(); disables LangSmith
src/app/cli.py                  + `udr brief`
tests/brief_fakes.py            scripted Interviewer replies by schema, upload fixtures
tests/brief_crash_child.py      child for the SIGKILL test
```

`src/app/adapters/outbound/extract.py` `pdf_to_text` delegates to `documents.pdf_pages` (the one
small refactor; Phase-1 code must not import from the outbound package).

## Fixed numbers (`config/profiles.toml`, AD3)

```toml
[phase1]
max_rounds = 5
max_questions_per_round = 5
max_files = 10
max_file_mb = 50
max_total_pages = 500
ocr_min_chars = 50          # a PDF page with less text is OCRed
ocr_dpi = 200
upload_digest_words = 500
pseudo_page_chars = 3000    # DOCX/MD/TXT are counted and cited in parts of this size
language_min_chars = 20
language_min_probability = 0.9

[response_formats.short]
words = [500, 2000]
citations = [15, 30]
[response_formats.structured]
words = [2000, 5000]
citations = [40, 80]
[response_formats.argumentative]
words = [5000, 10000]
citations = [80, 150]
```

Map-reduce chunk sizes reuse `pipeline/analysis.py` (`MAP_CHUNK_CHARS`, `REDUCE_BATCH_CHARS`).

## Data model

**Migration 2** (`store/db.py`, appended to `MIGRATIONS`):

```sql
CREATE TABLE sessions (
  session_id TEXT PRIMARY KEY,                 -- s + 12 hex
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('interviewing','awaiting_decision','saved','approved')),
  interview_language TEXT NOT NULL,
  report_language TEXT, response_format TEXT, template_id TEXT,   -- NULL = default not yet chosen
  upload_digest TEXT NOT NULL DEFAULT '', digest_notice TEXT NOT NULL DEFAULT '',
  approved_sha256 TEXT, archive_path TEXT, run_id TEXT);
CREATE TABLE uploads (
  session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
  file_id TEXT NOT NULL,                       -- f01 … per session
  name TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('pdf','docx','md','txt')),
  size INTEGER NOT NULL, pages INTEGER NOT NULL, sha256 TEXT NOT NULL,
  stage TEXT NOT NULL CHECK (stage IN ('stored','extracted','distilled')),
  pages_json TEXT NOT NULL DEFAULT '[]',       -- text per page/part after extraction and OCR
  warnings_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
  PRIMARY KEY (session_id, file_id));
CREATE TABLE upload_parts (                    -- one map chunk of distillation, persisted per item
  session_id TEXT NOT NULL, file_id TEXT NOT NULL, part INTEGER NOT NULL,
  facts_json TEXT NOT NULL,                    -- [{fact, page}]
  PRIMARY KEY (session_id, file_id, part),
  FOREIGN KEY (session_id, file_id) REFERENCES uploads(session_id, file_id) ON DELETE CASCADE);
ALTER TABLE runs ADD COLUMN session_id TEXT;
ALTER TABLE runs ADD COLUMN brief_sha256 TEXT;
ALTER TABLE runs ADD COLUMN brief_path TEXT;
ALTER TABLE runs ADD COLUMN tier TEXT;
ALTER TABLE runs ADD COLUMN summarize_model TEXT;
ALTER TABLE runs ADD COLUMN status TEXT NOT NULL DEFAULT 'created';
CREATE UNIQUE INDEX runs_session ON runs(session_id) WHERE session_id IS NOT NULL;
```

- `RunStore.create_approved_run(...)` is the only writer of `brief_sha256`; it inserts status
  `queued` (the M6 vocabulary; M5/M6 own the rest of the lifecycle). The unique index makes a
  re-executed `finalize` return the existing run instead of a second one.
- `RunStore.get_run(run_id)` returns the approval fields; M5 refuses a run without
  `brief_sha256` (AC1 holds end to end then).
- Graph state lives in LangGraph's `SqliteSaver` on `data/checkpoints.sqlite`
  (`SqliteSaver(sqlite3.connect(path, check_same_thread=False))`, thread id = session id). The
  `sessions` row is the queryable summary; the checkpoint is the conversation.

**Files:**
- `data/uploads/<session_id>/<file_id>-<safe name>`: raw upload bytes (needed again when OCR is
  resumed); `events.jsonl` next to them.
- `data/briefs/<UTC>.md`: approved brief, `%Y-%m-%dT%H-%M-%SZ`, created with mode `xb` and then
  `chmod 0o444`; a name clash gets `-2`, `-3`. Bytes = the approved text encoded as UTF-8, nothing
  else (no front matter, no newline translation, no scrubbing).
- `data/briefs/drafts/<session_id>.md`: the current draft on "save" (overwritten on each save).

## Brief text

**Canonical form.** Every draft, rendered or edited, is canonicalised before hashing: `\r\n` and
`\r` → `\n`, trailing whitespace stripped per line, exactly one final `\n`. The sha256 is over the
UTF-8 bytes of that form. The GUI/CLI shows and hashes exactly these bytes.

**Rendering** (`render.py`, deterministic, labels from `labels.py`): the LLM fills a `BriefDraft`;
code renders the PRD M4 template:

```
# <question>

Method: <n> Klärungsrunden[; nicht geklärt: <items>][; Prompt wörtlich übernommen]
Zielgruppe: <audience> · Entscheidung: <decision>

## Hintergrund und Kontext
## Kontext aus Unterlagen          (only with uploads: the ≤ 500-word digest, verbatim)
## Ziel
## Forschungsfragen                (numbered 1. 2. …)
## Umfang                          (Im Umfang / Nicht im Umfang / Nicht verhandelbar)
## Annahmen                        (assumed items; "Nicht geklärt: <item>" for each missing item)
## Was eine gute Antwort ausmacht
## Ausgabe                         (from settings, never from the LLM)
```

The Ausgabe section is rendered from `SessionSettings` and `config`: report language (name), "sources
in any language", quotation policy (§3.9: quotation marks only around verbatim text, translation in
parentheses), register (from the draft), response format with its word range, inline `[N]`
citations with a Sources section, template name and its H2 list (for `auto`: "headings are derived
in step 1"). A template whose `language` differs from the report language adds a notice (PRD M5
edge case, raised in Phase 1).

**Deterministic guarantees** (code, not prompt): every checklist item still `missing` at draft time
appears as "Nicht geklärt: <label>" under Annahmen (AC5); every question answered "weiß nicht" is
appended to Forschungsfragen if the draft lacks it (normalised match); the question list is never
empty (the title question is used).

**Verbatim install** (`render_verbatim`): Method line + blank line + the pasted text byte-for-byte
(canonicalised line endings only) + the Ausgabe section. The pasted text must still parse; if it
has no `# ` title or numbered questions, verbatim install is refused with a message and the owner
strengthens or edits instead.

**Parsing** (`parse.py`): title = the first `# ` line; research questions = the numbered list under
the Forschungsfragen/Research questions label (either label set). Used for edits, verbatim install,
and later by M5 (AD6: `extract` gets title and questions) and M6 (external briefs).

## Interview logic (`interview.py`, all calls through `LLMService`)

| Call | Role | think | Schema | Notes |
|---|---|---|---|---|
| assess | reason | no | `Assessment` | checklist (7 entries), ≤ `max_questions_per_round` questions with candidate answers, `finished_prompt` (round 1 only); prompt names the interview language (AC7) and carries question, answers so far, upload digest |
| draft | reason | yes | `BriefDraft` | from transcript, checklist and digest; never invents missing items |
| revise | reason | yes | `BriefDraft` | current brief text + owner feedback; keep everything the feedback does not touch |
| strengthen | reason | yes | `BriefDraft` | one pass over a pasted prompt |
| recommend_tier | reason | yes | `TierRecommendation` | step-1 rules table in the prompt; "when uncertain: full"; also proposes `response_format`; rationale 2–3 sentences |
| facts | summarize | no | `UploadFacts` | per part (≤ `MAP_CHUNK_CHARS`), fenced as untrusted; `[{fact, page}]`, pages outside the part are dropped |
| digest | summarize | no | `UploadDigest` | all facts → ≤ 500 words with "(<file>, S. <p>)"; if facts exceed one prompt: reduce in batches and set the digest notice |
| ocr | ocr | no | text | one page PNG at 200 dpi |

- Code caps questions at the limit and drops questions about `clear` items.
- The loop ends when no item is `missing` or `assumed`, after `max_rounds`, or on "genug".
- Answers: `accept` (candidate), `text` (edited or replaced), `unknown` ("weiß nicht"); free text
  and "genug" are separate messages.
- An `LLMError` in assess or draft surfaces as a typed error to the caller (the state is unchanged
  and the call can be retried); in facts or OCR it skips that part/page with a warning.

## Graph (`graphs/brief.py`)

```
START → ingest_uploads → assess ─┬─(open items, rounds left, no genug)─→ ask ⟲ (→ ingest_uploads)
                                 ├─(finished prompt)───────────────────→ offer ─→ draft | verbatim
                                 └─(done)─→ recommend → draft_brief ─→ decide ⟲ ─→ finalize → END
decide actions: approve → finalize | revise → revise_brief → decide | edit → decide
                settings → rerender → decide | save → park → decide
```

- **Interrupts only in `ask`, `offer` and `decide`,** and those nodes do no model work: LangGraph
  re-runs an interrupted node from its start on resume, so any LLM call there would repeat.
- Every node is idempotent: `ingest_uploads` processes only files and parts not yet stored;
  `finalize` uses the approval timestamp chosen by the service (passed in the resume value), so a
  re-run writes the same archive path, accepts an existing file with identical bytes, and the unique
  index returns the existing run.
- State is a `TypedDict` of JSON-serialisable values (Pydantic models validate at the edges).
- Routing functions stay ≤ 10 complexity; one node per function, each ≤ 50 lines.

## Service (`brief/service.py`)

`BriefService(store, runs, graph, interviewer, uploads, settings, events, now)`:

| Method | Does | Errors |
|---|---|---|
| `start(question, files=())` | validate (empty → `InvalidInput`), detect language, store files (AC8), run to the first interrupt | `InvalidInput`, `UploadRejected` |
| `add_files(session_id, files)` | only while interviewing | `WrongState`, `UploadRejected` |
| `answer(session_id, answers, text=None, genug=False)` | resume `ask` | `WrongState`, `NotFound` |
| `choose_offer(session_id, strengthen: bool)` | resume `offer` | `WrongState` |
| `revise(session_id, feedback)` / `edit(session_id, text)` / `set_settings(...)` / `save(...)` | resume `decide` | `InvalidInput` (unparseable edit, unknown template), `WrongState` |
| `approve(session_id, sha256, tier)` | hash check under the session lock, then resume `decide` | `StaleBrief`, `WrongState` |
| `get(session_id)` → `SessionView` | status, round, checklist, pending questions, brief + sha, recommendation, notices | `NotFound` |
| `recover()` | finish sessions whose graph stopped between interrupts (crash) | – |

- One `threading.Lock` per session id: two calls on one session never interleave (M6 maps the second
  approval to 409).
- Errors map to HTTP in M6: `InvalidInput` 422, `NotFound` 404, `StaleBrief`/`WrongState` 409.
- The service and the graph take no gateway. Bootstrap sets `LANGSMITH_TRACING=false` and
  `LANGCHAIN_TRACING_V2=false` before LangGraph is imported (no telemetry egress, PRD §3.2).

## Uploads (`brief/uploads.py`, `documents.py`)

1. **Validate everything first** (AC8): count ≤ 10, each ≤ 50 MB, type by extension and magic bytes
   (PDF `%PDF`, DOCX zip with `word/document.xml`, MD/TXT decodable as UTF-8 or via
   `charset-normalizer`), encrypted PDFs rejected, total pages ≤ 500 (PDF page count;
   DOCX/MD/TXT: ceil(chars / `pseudo_page_chars`)). Any violation → `UploadRejected` naming the
   file and rule; nothing is written.
2. **Store**: bytes to `data/uploads/<sid>/`, then the `uploads` rows in one transaction. On
   `recover()`, files without a row are deleted.
3. **Extract** per file → `pages_json`, stage `extracted`. PDF pages with fewer than 50 characters
   are rendered (`render_page_png`, 200 dpi, grey 8-bit PNG via `zlib`) and OCRed; a missing OCR
   model → page skipped with a warning (`ocr_unavailable`, once per session).
4. **Distil**: parts of ≤ `MAP_CHUNK_CHARS`; each part's facts committed to `upload_parts` before the
   next part starts; stage `distilled` after the last part.
5. **Digest**: after new files are distilled, rebuild the session digest from all facts (≤ 500
   words); store it with its notice.

## Work steps (each: tests first, see them fail, implement, mutation-check, gate, commit)

1. **Dependencies and telemetry.**
   - `uv add langgraph langgraph-checkpoint-sqlite python-docx langdetect`.
   - Audit every new transitive licence (`uv tree`, package metadata). Stop and ask if anything is
     outside AGENTS.md §5.5 plus the accepted `tld`/`certifi`.
   - Bootstrap disables LangSmith tracing; test that importing `app.bootstrap` sets both variables.
   - Extend `tests/test_egress_guard.py`: `app.brief`, `app.graphs`, `app.store.sessions`,
     `app.templates`, `app.documents` import nothing from `app.adapters.outbound` (with a violating
     input for the detector).
   - Commit: `build: LangGraph, python-docx, langdetect; LangSmith off`.
2. **Profiles and templates.**
   - `[phase1]` and `[response_formats]` in `config/profiles.toml`; loader `load_phase1()`,
     `load_response_formats()`.
   - `templates/*.md` with the PRD M5 headings; `auto.md` has front matter only.
   - `templates.py`: front matter is `key: value` lines (no YAML dependency); body H2s each followed
     by `<!-- section instructions -->`; 2–15 unique H2s, none reserved (Quellen, Sources, Anhang,
     Appendix); `data/templates/` overrides nothing built in (id clash → error).
   - Tests: the five built-ins load; every validation rule rejects a bad fixture.
   - Commit: `feat(templates): report templates and Phase-1 limits`.
3. **Brief text core.** `labels.py`, `render.py`, `parse.py`, `language.py`.
   - Tests: canonical form and hash; render → parse round trip (de, en, other → en labels); missing
     items listed; "weiß nicht" questions appended; verbatim install keeps bytes; unparseable edit
     rejected; language detection table (German, English, short, mixed → German); seed makes it
     deterministic.
   - Commit: `feat(brief): render, parse and hash the brief`.
4. **Store.** Migration 2, `SessionStore`, `RunStore`, `archive.py`.
   - Tests: migration 1 → 2 on an M3 database keeps its rows; session status transitions; upload
     rows and parts; `create_approved_run` twice for one session returns the same run; archive is
     exclusive, read-only, byte-exact (umlauts, a literal `<think>` stays); name clash suffix.
   - Commit: `feat(store): sessions, uploads and approved runs`.
5. **Documents and uploads.** `documents.py` (+ `pdf_to_text` delegation), `uploads.py`,
   upload prompts.
   - Tests (synthetic files from `tests/support.py`; add `make_docx()`): every AC8 rule with nothing
     stored; encrypted PDF; scanned page → OCR request with a PNG at 200 dpi (decode the PNG header
     in the test); OCR missing → warning, page skipped; facts with a foreign page dropped; per-part
     persistence; digest map-reduce sets the notice; digest ≤ 500 words is in the prompt (AD3).
   - Commit: `feat(brief): upload ingestion with OCR and distillation`.
6. **Interview logic.** `schemas.py`, `prompts/brief.py`, `interview.py`.
   - Tests (fake transport by schema, like `tests/fixtures_corpus.FakeModels`): `think` per call as
     in the table; language in the assess prompt (AC7); question cap; questions on `clear` items
     dropped; tier rules prompt contains the light/full table and "uncertain → full"; LLM errors
     typed.
   - Commit: `feat(brief): clarification rounds, drafting and tier recommendation`.
7. **Graph.** `graphs/brief.py`.
   - Tests with `SqliteSaver` on `tmp_path`: round loop ends on all-clear, on `max_rounds`, on
     genug; finished-prompt branch both ways; decide loop for every action; interrupt nodes make no
     model calls on resume (count fake calls across a resume); `finalize` re-run gives one archive
     file and one run.
   - Commit: `feat(graphs): brief graph with interrupts and checkpoints`.
8. **Service and composition.** `service.py`, `bootstrap.open_checkpointer/build_brief_service`.
   - Tests: AC1–AC8 and the edge cases at service level.
   - **AC3:** a recording gateway fake is constructed and never called over a full session with a
     PDF and a DOCX upload; plus the import guard of step 1.
   - **AC6 restart:** a new service, graph and stores on the same files show the same pending
     questions; answering continues the session.
   - **AD10 kill and resume:** in-process `SimulatedCrash` (a) in the second part of a distillation
     (resume distils only the missing part), (b) inside `draft` (resume drafts once, no duplicate
     questions round); SIGKILL test with `tests/brief_crash_child.py` that hangs in `draft`, then
     `recover()` in the parent finishes the draft (≤ 15 s).
   - Concurrency: two threads `approve` the same session → exactly one run, the other `WrongState`.
   - Commit: `feat(brief): Phase-1 service`.
9. **CLI `udr brief`.** German prompts. `udr brief [--file F]...` starts; `--session ID` resumes;
   `--list` lists sessions. Per question: Enter accepts the candidate, typed text replaces it,
   `?` = "weiß nicht", `genug` ends the rounds. Decision menu: Freigeben (asks for Lite/Full, default
   = recommendation), Überarbeiten (feedback), Bearbeiten (`$EDITOR` via `typer.edit`),
   Einstellungen, Speichern. Prints the sha256 and the archive path on approval.
   - Tests with `CliRunner` and a fake service.
   - Commit: `feat(cli): udr brief`.
10. **Mutation checks, then docs.**
    - Mutate and confirm a test fails: approve without the hash check; archive via the scrubbing
      writer; missing items not listed; "weiß nicht" not appended; an LLM call moved into `decide`;
      per-part commit removed; limits checked after storing; language threshold 0.5; tracing not
      disabled; `finalize` without the unique-run guard.
    - Run `/documentation-update` against the commit before step 0. Add `docs/brief.md` (flow,
      checklist, rendering, guarantees, resume contract, CLI); update `docs/architecture.md`
      (graphs layer, interrupt idempotency rule), `IMPLEMENTATION.md` (M4 row, module map,
      `udr brief`, open issues) and the README status.
    - Commit: `docs: Phase-1 docs and M4 status`.

## Verification

- `uv run pre-commit run --all-files` green after every step: coverage ≥ 85 %, suite ≤ 60 s
  (the SIGKILL child imports LangGraph; keep it to one child start).
- `uv run pytest tests/test_docs.py -q`: PRD ≤ 800 lines, links resolve.
- **Live (optional, ask before running; it uses our Ollama and the shared daemon, no network):**
  `tests/live/test_live_brief.py` runs one German session with a 3-page PDF (one scanned page)
  through the real `reason`, `summarize` and `ocr` roles, and records round latency and draft time
  in IMPLEMENTATION.md. Afterwards stop our daemon by PID after checking `/proc/<pid>/environ` shows
  `OLLAMA_HOST=127.0.0.1:11436`.
