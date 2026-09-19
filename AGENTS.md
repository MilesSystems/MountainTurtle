# Repository instructions

- If the user curses at you, the next message must be solution-oriented. Explain
  concrete actions you can take to fix the problem.

## Complete every Codex change

- For every completed Codex change to this repository, bump `VERSION`, commit
  the finished change, and push the tested result to `origin/main` before
  reporting the task complete. This applies to code, UI, documentation, and
  repository instruction changes. Use the next patch version unless the user
  requests another version. One completed task may contain several edits and
  commits; bump once for that task, including any fixes made during validation.
- Work in an isolated `codex/` branch/worktree and preserve unrelated changes.
  Fetch current `origin/main` before integrating and again before pushing. Keep
  concurrent work, resolve conflicts, and validate the final result. Never
  force-push or overwrite other people's commits.
- Run the relevant tests, the full test suite, the universal macOS build, and
  signature/version checks required by `.github/workflows/validate.yml`. For UI
  changes, inspect screenshots of the installed app and verify the affected
  interaction. Keep existing connections, cached data, and credentials safe.
- Verify that the pushed commit is on `origin/main` and inspect its GitHub
  validation result. If a check or push is blocked, report the concrete blocker
  and do not claim delivery is complete.
- Publishing a downloadable GitHub release remains the separate workflow in
  `docs/RELEASING.md`; pushing source to main does not publish updater assets.
