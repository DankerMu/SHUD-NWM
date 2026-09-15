# Journal-root seam adoption on the remaining write lanes, and a truthful full-tree budget contract

## Why

Two residual findings from PR #1951's round-3 review, both pre-existing and both
about a contract the code no longer honours.

**#1955 — the one journal-root seam covers three lanes out of ten.**
`verify_journal_root_authority` (`services/orchestrator/journal_root_authority.py:57`)
was introduced with a deliberately narrow scope (demotion + scheduler + census).
Seven further lanes still build `FileOrchestrationJournalRepository` straight
from an operator-supplied `--journal-root`: released-reservation recovery
(`operator_released_reservation_recovery.py:124`), four migration lanes
(`file_orchestration_migration.py:96,202,1006,1255`) and two operator scripts
(`scripts/node22_manual_retry_failed_runs.py:81`,
`scripts/ops/node22_repair_placeholder_hydro_uris.py:55`). None of them is
read-only. `safe_fs` anchors a relative path on `Path.cwd()` and `Path("")` is
`Path(".")`, so a blank or relative root silently retargets a write lane at the
process working directory; a symlinked ancestor degrades every read to a blocked
row whose diagnostic never mentions a symlink. The sharpest instance is
`_rollback_execution_lock` (`file_orchestration_migration.py:945`), which does
`Path(journal_root).expanduser().resolve()` — `resolve()` follows symlinks, so
the subsequent `ensure_directory_no_follow` can only ever pass, the lock is
taken on the realpath while the repository is built on the alias, and a blank
root creates `.reconcile-inventory-rollback-execution.lock` in the working
directory. Separately, `journal_scope_census.py:544` calls `Path(output).expanduser()`
outside any handler, and `expanduser` raises a bare `RuntimeError`, which the
census entrypoints (which catch `OrchestratorError`/`FileOrchestrationJournalError`)
do not catch — a traceback from a CLI that documents "1 on a typed failure".

**#1953 — the file journal's fall-open is fall-closed on the production tree.**
#1734 D4 accepted the whole-tree replay as the safe fallback on the argument
that it is "merely as slow as the prior behaviour". Measured on node-22's live
db-free journal (2026-09-14, read-only; the receipt lands under
`docs/runbooks/receipts/journal-scope-census/` via task 2.7): 54,258 latest
rows + 94,123 segment records + 5,328 direct records = **153,709** raw budget
consumes against a default budget of **100,000**, for **17,025** unique jobs.
Every full-tree replay now refuses after ~91 s of IO. The five query lanes turn
that refusal into a synthetic row whose `status` is `"running"` — a row that
correctly keeps the duplicate-submission guards closed, but that reports an
unread journal as a running job, and whose error evidence cannot be told apart
from a cycle-scoped budget refusal (same reason token, same field).

## What Changes

- **#1955 A** — the seven lanes verify the root through the one seam before
  constructing a repository, each naming its own knob in `setting`. The
  create-capable lane (historical import) keeps its ability to create a fresh
  root, but through the no-follow creator, and verifies afterwards.
- **#1955 A2** — `_rollback_execution_lock` derives its lock path from the
  verified root and no longer calls `.resolve()`.
- **#1955 A3** — the recovery and rollback entrypoints (click and argparse)
  widen their `except` arms to include `OrchestratorError`, so the refusal is a
  typed single line instead of a traceback; the two scripts get the same typed
  exit at `main()`.
- **#1955 B** — `_require_output_outside_root` converts the bare `RuntimeError`
  from `expanduser` into the new typed `CENSUS_OUTPUT_UNEXPANDABLE`, distinct
  from the post-emit `CENSUS_OUTPUT_UNWRITABLE`.
- **#1953 A** — `_RecordBudget` carries the read lane, and the whole-tree replay
  raises with `evidence={"lane": "full_tree_replay"}` while the cycle-scoped
  replay names its own lane. Reason token and field are unchanged.
- **#1953 B** — the blocked synthetic row keeps every guard-visible property
  (present, non-terminal, not auto-retry-reusable, same identifiers, same
  `file_journal.status == "blocked"` marker) and stops calling itself
  `"running"`.
- **#1953 C** — the node-22 measurement is published as a receipt, and the
  census runbook records that on a production-sized tree the default budget
  refuses with a lane-tagged reason and `--max-records` is the sanctioned
  escape.

Not changed: `MAX_FILE_JOURNAL_RECORDS`, `safe_fs` anchoring/expansion
semantics, the db-free preflight lane, the retention/restore second authority,
`query_pipeline_job_by_slurm_id`'s existence (#1734 recorded "leave"; it gains a
static no-production-caller pin instead).
