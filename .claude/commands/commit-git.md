---
description: Commit all uncommitted changes as small Conventional Commits (never pushes without asking)
disable-model-invocation: true
---

Commit the current uncommitted work.

1. Inspect: `git status --porcelain` and `git diff HEAD`, plus untracked files.
2. Group the changes by concern (one feature, fix, or doc change per commit). Keep each commit small.
3. For each group, stage explicit paths only (`git add <path>...`). Never `git add -A` or `git add .`.
   Never stage `.env*` (except `.env.example`), `data/`, or anything `.gitignore` excludes.
4. Commit with a Conventional Commit message in imperative mood:
   `<type>(<optional scope>): <subject>`. Types: feat, fix, docs, test, refactor, perf, build, ci, chore.
   Subject ≤ 72 chars; add a body only when the "why" isn't obvious.
5. The pre-commit gate runs on each commit. If it fails or rewrites files: fix the cause,
   re-stage, and commit again. Never use `--no-verify`. Never amend unless asked.
6. Report the commits created (`git log --oneline -n <count>`).
7. Do not push. Ask before running `git push`.
