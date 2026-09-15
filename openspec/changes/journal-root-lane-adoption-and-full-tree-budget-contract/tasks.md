# Tasks

## 1. #1955 — journal-root seam on the remaining lanes

- [x] 1.1 `operator_released_reservation_recovery.py:124`: verify the root
      (`setting="--journal-root"`) before constructing the repository.
- [x] 1.2 The three rollback lanes verify at their **first statement**, not at
      the repository constructor: `prepare_file_journal_rollback` (`:67`),
      `launch_file_journal_rollback_writer` (`:210`) and
      `complete_file_journal_rollforward` (`:986`). The lock is taken at `:87`,
      `:231`, `:997` — ahead of the repositories at `:96`, `:202`, `:1006` — so
      the verified root must reach the lock, the repository, and (launch only)
      the `ProductionSchedulerConfig(scheduler_journal_root=...)` at `:224`.
- [x] 1.3 `file_orchestration_migration.py:1255` (`import_historical_scheduler_state`):
      verify; on a refusal whose `error_type` is `FileNotFoundError`, create the
      root with `ensure_directory_no_follow` and verify again (design D2). Any
      other refusal stays a refusal.
- [x] 1.4 `_rollback_execution_lock` (`:942-949`): accept the verified root,
      derive `lock_path` from it, drop `.expanduser().resolve()` (design D3).
- [x] 1.5 `scripts/node22_manual_retry_failed_runs.py:81` and
      `scripts/ops/node22_repair_placeholder_hydro_uris.py:55,57`: verify before
      the repository and before the dry-run glob; typed non-zero return from
      `main()` with one stderr line, no traceback. **Every** later use of that
      root inside each script uses the verified (expanded) value — including
      `relative_to(...)` at `node22_repair_placeholder_hydro_uris.py:66`, which
      would otherwise raise `ValueError` on any legitimate `~` root.
- [x] 1.6 Widen `except` arms to include `OrchestratorError`:
      `operator_released_reservation_recovery.py:250,284`; `cli.py:600,637,666`
      (click) and `:894,911,926` (argparse). Leave `migrate-scheduler-state`
      (`cli.py:559,875`) unchanged.
- [x] 1.7 `journal_scope_census.py:544`: wrap `expanduser` and raise the new
      `CENSUS_OUTPUT_UNEXPANDABLE`; update the CLI help so the code list and its
      timing wording stay true (design D5).
- [x] 1.8 Tests: for the recovery lane and the three rollback commands, blank /
      relative / tilde-unexpandable roots on **both** entrypoints — typed
      `FILE_JOURNAL_INVALID_ROOT` line, non-zero exit, no traceback, no path
      leak (mirror `tests/test_orchestrator_demote_cli_security.py:808,896`).
      For the two scripts, `main()`-level return code and zero writes.
- [x] 1.8a Tests: a **legitimate** `~/journal` root still works on every lane —
      the lane operates on the expanded path and the lock path, glob,
      `relative_to` and receipt path are all derived from the same expanded root
      (mirror `tests/test_orchestrator_demote_cli_security.py:850`, the tilde
      success case #1955's acceptance names).
- [x] 1.9 Test, for **all three** rollback commands: a blank `--journal-root`
      leaves no `.reconcile-inventory-rollback-execution.lock` in the working
      directory, and a symlink-aliased root is refused rather than locked on the
      realpath.
- [x] 1.10 Test: the import lane still creates a fresh root and imports, and
      still refuses a blank/relative/symlinked one.
- [x] 1.11 Test: `census-job-id-scope --output '~nosuchuser/receipt.json'` exits
      1 with `CENSUS_OUTPUT_UNEXPANDABLE` on both entrypoints, no traceback,
      zero bytes written.
- [x] 1.12 Re-census the `verify_directory_no_follow` / `ensure_directory_no_follow`
      call surface inside `services/orchestrator/` and record the updated
      detection means in this change's design.md: grepping
      `verify_directory_no_follow` + `FILE_JOURNAL_INVALID_ROOT` alone cannot
      see the second authority (`scheduler_journal_retention.py:120-136`,
      `scheduler_journal_restore.py:93`, `RetentionFailure("journal_root_*")`).
      No archived change is edited; convergence of the two authorities is filed
      as a follow-up issue, not done here (#1955 acceptance item 6).

## 2. #1953 — full-tree budget contract

- [x] 2.1 `_RecordBudget` (`:1024`) carries the read lane (defaulted, so the
      third construction site `:2074` `rollback_scope_records` is untouched) and
      puts it in the raised error's evidence; the whole-tree construction
      (`:6662`, inside `_replay_all_pipeline_job_records` `:6660`) passes
      `full_tree_replay` and the cycle-scoped one (`:6906`, inside
      `_replay_pipeline_job_records_for_cycle` `:6867`) passes its own lane.
      Reason token and field unchanged.
- [x] 2.2 `_blocked_query_job` (`:13046`): status names the blocked read instead
      of `"running"`; `job_id` defaults, `file_journal` marker, `error_code` and
      every other field unchanged (design D7). The three sibling sentinels
      (`:1497-1500`, `:12799`, `:12844`) are **not** changed (design D7a).
- [x] 2.3 Update exactly the tests that pin `"status": "running"` on rows
      produced by `_blocked_query_job`, enumerating them in the PR body; leave
      the sibling-sentinel assertions alone — in particular the whole-dict
      assertion at `tests/test_file_orchestration_journal.py:5225-5232`
      (`active_slurm_jobs`) must still expect `"running"`. No other assertion in
      a touched test is weakened.
- [x] 2.4 Test: the blocked row stays non-`None` / one-element, stays outside
      `TERMINAL_JOB_STATUSES` (assert against the imported set, not a literal
      list), is rejected by `_file_auto_retry_job_can_be_reused`, and still
      makes `_pipeline_job_conflicts_unlocked` report a conflict.
- [x] 2.5 Test: a whole-tree budget refusal carries `lane=full_tree_replay` in
      evidence and a cycle-scoped refusal carries its own lane, with identical
      reason token and field.
- [x] 2.5a Test: the third reach point — the `unscoped` branch of
      `query_released_identity_blocked_jobs` (`:1971-1976`) — **raises** rather
      than synthesising a row, and the recovery command's widened arms (task
      1.6) render it as one typed stderr line with a non-zero exit (design D7b,
      #1953 acceptance item 2).
- [x] 2.6 Static test: `query_pipeline_job_by_slurm_id` has no production caller
      (grep-style pin, in the spirit of the existing `#1734 D1a / I8` pin).
- [x] 2.7 Receipt: add the node-22 measurement JSON plus a short transcript
      under `docs/runbooks/receipts/journal-scope-census/` naming the command,
      node-22 head `7b38bcb8`, and the byte-identity check against master.
- [x] 2.8 Runbook: record that on a production-sized tree the census default
      budget refuses with a lane-tagged `file_journal_record_limit_exceeded`,
      that `--max-records` is the sanctioned escape, and that the default budget
      is deliberately not raised (#1810's "raising it only moves the cliff").

## Evidence Floor

- [x] EF-1 `uv run ruff check .` → clean.
- [x] EF-2 `uv run pytest -q` over at least
      `tests/test_file_orchestration_migration.py`,
      `tests/test_file_orchestration_journal.py`, `tests/test_retry.py`,
      `tests/test_gateway_reconcile_file_cohort_identity.py`, the orchestrator
      CLI module(s) carrying the rollback/recovery entrypoint tests, the census
      test module, and every module the implementer adds or touches → green.
      Red-before evidence is owed by every new **behavioral** case. The
      regression pins required by tasks 1.8a, 1.10 and 2.6 are green-before by
      construction (`safe_fs` already expanded `~` before this change, and a
      "no production caller" pin asserts today's truth), so they are declared as
      pins rather than counted as red evidence. The exact module list and the
      pin list are reported in the PR body.
- [x] EF-3 Every new/changed CLI case asserts exact stderr shape: one typed
      line, no traceback, no filesystem path.
- [x] EF-4 `openspec validate journal-root-lane-adoption-and-full-tree-budget-contract
      --strict --no-interactive` → valid.
- [x] EF-5 node-22 measurement receipt committed, with the interpreter
      discipline stated (`/scratch/frd_muziyao/NWM/.venv/bin/python`, no
      `uv run`, no `uv sync`).
- [x] EF-6 No node-27 live receipt required: no display, API boundary or DB
      write surface is touched (stated as a deviation-free scope claim).
