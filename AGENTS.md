# Repository instructions

- If the user curses at you, the next message must be solution-oriented. Explain
  concrete actions you can take to fix the problem.

## Complete every Codex change

- For every completed Codex change to this repository, bump `VERSION`, commit
  the finished change, push the tested result to `origin/main`, and publish its
  signed GitHub release before reporting the task complete. This applies to
  code, UI, documentation, and repository instruction changes. Use the next
  patch version unless the user requests another version. One completed task
  may contain several edits and commits; bump once for that task, including any
  fixes made during validation.
- Work in an isolated `codex/` branch/worktree and preserve unrelated changes.
  Fetch current `origin/main` before integrating and again before pushing. Keep
  concurrent work, resolve conflicts, and validate the final result. Never
  force-push or overwrite other people's commits.
- Run the relevant tests, the full test suite, the universal macOS build, and
  signature/version checks required by `.github/workflows/validate.yml`. For UI
  changes, inspect screenshots of the installed app and verify the affected
  interaction. Keep existing connections, cached data, and credentials safe.
- Verify that the pushed commit is on `origin/main` and that its GitHub
  validation passes before publishing.
- Reaching `origin/main` is standing authorization to automatically complete the
  release workflow in `docs/RELEASING.md`: prepare and review the signed assets,
  create and push the matching version tag at the prepared commit, and publish
  the complete GitHub release as latest. Do not ask for another publication
  confirmation unless the user explicitly pauses or limits release scope.
  Source pushes alone do not publish updater assets; run the release scripts
  from the release Mac using the existing signing credentials.
- Until Developer ID signing and notarization are configured, publish the
  explicitly labelled, Apple-signed Preview using `--preview`, following the
  existing release channel. Never publish unsigned assets, replace published
  assets, move an existing tag, or replace the trusted Sparkle signing key.
- Verify the public `/latest` appcast and versioned downloads against the
  prepared assets, and test the update/relaunch from an older installed version
  while preserving connections, cached data, and credentials. Include changes
  from any previously unpublished versions in the release notes.
- If validation, signing, pushing, publication, public delivery, or the actual
  update/relaunch check is blocked, report the concrete blocker and do not claim
  delivery is complete. A source push or prepared release alone is not completion.
