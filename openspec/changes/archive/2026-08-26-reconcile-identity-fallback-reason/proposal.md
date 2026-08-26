## Why

Restart reconcile currently mixes per-submission snapshots into cross-submission cohort identity, cannot safely recover a reserved-unbound cohort on a cluster that explicitly does not store Slurm comments, and collapses terminal cohort identity failures into one opaque bit. Together these defects leave reclaim attempts permanently blocked, force manual accounting archaeology, and make a safe comment-less recovery path unavailable even when accounting yields one uniquely owned candidate.

## What Changes

- Stop comparing `hydro_run.submission_attempt` as cross-submission runtime identity; it remains immutable lineage evidence on the row that wrote it.
- On a cluster whose `AccountingStoreFlags` explicitly proves comments are not stored, query one bounded attempt window by forecast job name and exact owner/account, and bind only one uniquely identified candidate that passes every remaining identity gate.
- Keep zero, ambiguous, incomplete-ownership, and identity-mismatched fallback results fail-closed in `reserved`; preserve the durable `accounting_unavailable` / `comment_accounting_unproven` held tuple used by the guarded operator demotion path.
- Record successful fallback binding with a source distinct from `slurm_exact_comment`; do not change comment-storing clusters or unknown-capability failures.
- Add stable reason classes to every terminal file-cohort identity failure and expose the additive field through restart-reconcile inflight evidence while preserving `action="identity_mismatch_blocked"` and zero durable writes.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `pipeline-job-persistence`: amend accepted-submit cohort identity, comment-less reserved recovery, and restart-reconcile diagnostic evidence contracts.

## Impact

- Code: `services/orchestrator/reconcile.py`, `services/orchestrator/accepted_submit_identity.py`, `services/orchestrator/file_orchestration_journal.py`, `services/orchestrator/scheduler_runtime.py`.
- Tests: split gateway-reconcile suites and restart-reconcile scheduler evidence tests.
- Operations: `docs/runbooks/failed-basin-retry.md`; node-22 live read-only accounting plus scratch-journal receipt, with no `sbatch`, `scancel`, or production-journal mutation.
- No database schema, display API, frontend, or node-27 live-DB semantics change.
