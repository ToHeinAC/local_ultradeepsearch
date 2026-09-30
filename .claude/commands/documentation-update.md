---
description: Update project docs to match the code changes since the last commit (or since a given git ref)
argument-hint: "[git-ref, default HEAD]"
---

Update the documentation to match the code. Base ref: `$ARGUMENTS` (use `HEAD` if empty).

1. Find what changed: `git diff <ref>` and `git status --porcelain` (include untracked files).
2. Update, in this order, only what the changes affect:
   - `IMPLEMENTATION.md`: phase table, module map, run/verify commands. Current state only, no history.
   - `docs/<component>.md`: deep details of each changed component (create one if a new component
     appeared, and link it from `IMPLEMENTATION.md`). `docs/architecture.md` for data flow and design decisions.
   - `README.md`: only user-facing changes (install, usage, features).
   - `AGENTS.md`: only if a convention or workflow rule changed. Never restate phase status there.
   - `PRD.md`: never edit silently. If the code contradicts the PRD, report it to the user instead.
3. State each fact once and link to it elsewhere. Remove statements the change made false.
4. Verify: `uv run pytest tests/test_docs.py -q`. It checks size limits (AGENTS.md §5.1)
   and that every local link resolves.
