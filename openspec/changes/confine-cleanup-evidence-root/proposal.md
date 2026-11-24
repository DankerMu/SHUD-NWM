# Confine the cleanup CLI's evidence root

## Why

#3302. `nhms-pipeline cleanup --execute` decides whether it may delete from
the latest pass evidence receipt. `_cleanup_frontier`
(`services/orchestrator/cli.py`) derives the evidence directory from
`ProductionSchedulerConfig()` and reads the receipt without checking the
directory. Construction is always DB-free now and tolerates an evidence root
outside the workspace, leaving the verdict to the pass-side root preflight —
which the cleanup never runs. A directory outside the workspace that exists
and holds a fresh receipt therefore yields frontier `ok`, and the cleanup
really deletes. A scheduler pass on the same configuration is blocked with
`out_of_workspace`.

## What Changes

- `_cleanup_frontier` applies the pass's single-field evidence-root verdict
  (`_scheduler_root_check("evidence_root", …, require_under_workspace=True)`)
  to the derived root, inside the existing fail-closed guard. A blocker
  becomes `unavailable` / `evidence_dir_unresolved` with a null
  `evidence_dir`, which forces dry-run; the two exemptions are under
  Decisions.
- `production-scheduler-orchestration`: one scenario added to "Out-of-pass
  deletion surfaces respect the pipeline frontier or fail closed".

## Decisions

- **Single-field check, not the whole runtime-root preflight.** The full
  preflight also requires a lock root, a non-empty approved-roots list and a
  service role. None of them says whether the evidence can be trusted, and
  requiring them would lock cleanup into dry-run in an operator shell that
  works today.
- **Not the bare lexical `_require_under_workspace`.** DB-free configuration
  paths are deliberately not fully resolved; a workspace reached through a
  symlinked prefix must not be rejected, or production cleanup would be stuck
  in dry-run for good.
- **Which verdicts reject.** The check runs with `must_exist=True`,
  `allow_create=False`, `require_approved_root=False`,
  `require_under_workspace=True`. Every blocker rejects except two: "not
  found" is handed on to `read_latest_pass_frontier`, so a missing directory
  stays `evidence_dir_missing` with the probed path disclosed (#1503); "not
  writable" is ignored, because the cleanup only reads evidence and a
  read-only directory inside the workspace is no less trustworthy. Both
  exemptions additionally require the check's own `under_workspace` to be
  true. The helper usually judges outside-the-workspace before those two, but
  not always: when `lstat` fails with a permission error it returns "not
  writable" first, for a path that may lie outside the workspace. Without the
  extra condition such a path would reach the receipt read (which then fails
  with a read error and still forces dry-run, but under the wrong reason and
  with the path disclosed).

## Not in this change

The pass-side preflight itself; `db_free_runtime_preflight`'s path specs; the
admission of retention deletion roots (#1318 / #2596);
`scheduler_journal_retention.py` (it already validates its evidence root).

## Impact

`services/orchestrator/cli.py`, `tests/test_cli_cleanup_frontier.py`, spec
`production-scheduler-orchestration`.
