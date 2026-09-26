# Proposal — retry-blocked-reads-failed-stage-restart (#2566, #2567, #2600)

## Why

### #2566 and #2567: the runtime-root walk of the file-journal manual retry

Production reaches this walk only through `POST /runs/{run_id}/retry` → `attempt_manual_retry`. The walk resolves runtime roots from recorded provenance, and the current process environment is its last fallback. PR #2565 made two provenance readers degrade on a read-blocked journal row. Two defects remain:

- **#2566.** The walk's own same-run companion read, `query_pipeline_jobs_by_run`, has no blocked-row check. The blocked row lacks `job_type`, so the download-job filter silently drops it. The persisted evidence then reads exactly as "this run has no companion download job".
- **#2567.**
  - When the provenance reads degrade, only the environment candidate is left. The retry is then submitted with the current `OBJECT_STORE_ROOT`/`WORKSPACE_ROOT` and returns HTTP 200. If an operator changed those roots, the retry points at roots that differ from the original submission.
  - Before PR #2565, this case did not submit at all.
  - `candidate_counts` is byte-identical to "no submission event", so the fallback is not auditable after the fact.

### #2600: manual-retry restart point

A manual-retry marker (`record_manual_repair`) on a cohort whose only failure was `state_save_qc` reruns the whole chain from convert. On 2026-09-23 (node-22) that took 19 minutes where 9 seconds was needed; for a 47-member cohort it would recompute 47 forecasts.

Root cause: `_manual_retry_state_evidence` sets no `restart_stage`. The only exception is the cold-start quarantine case, which is forced to `forecast`. The candidate therefore joins the `(0, "full")` restart cohort.

## What changes

- **#2566.** The same-run companion read detects the blocked marker, logs the journal `reason`/`field`, contributes no companion candidate, and counts one blocked read.
- **#2567.**
  - `_RuntimeRootCandidateBatch` gains a `blocked_reads` counter, default 0. It is emitted as `candidate_counts.blocked_reads` only when it is non-zero, so evidence without a blocked read (the DB lane, the file-lane golden) is byte-identical.
  - When `blocked_reads > 0`, runtime roots are required for any job type, and the environment candidate is neither used nor read. Roots resolve only from recorded provenance. If no recorded candidate resolves, the retry ends with the classified `RETRY_RUNTIME_ROOTS_UNRESOLVED` outcome (the existing governed `submission_failed` + 503 path) carrying `blocked_reads`.
  - This deliberately overturns the "degrade may fall back to the environment" trade-off recorded by PR #2565.
- **#2600.**
  - The manual-retry decision resolves the failed stage on the scope-blind axis, so a failure on a cohort master counts. It carries that stage as `restart_stage` when the stage is after forecast (`parse`/`state_save_qc`/`publish`) and the candidate's own forecast output is attributably durable (its hydro success, or an attributable native-SHUD success row; never an `incomplete` one). Permanence is ignored.
  - A failed `forecast` restarts at `forecast` only with the forcing witness.
  - The existing restart-stage guards run on the emitted stage. A guard failure, or any other case, keeps the full chain (fail-closed, never blocked).
  - The criterion is written into the PR description before any code.

## Out of scope

- The DB-lane retry service.
- `record_manual_repair` and the node-22 script CLI: the marker is unchanged.
- Restarting at `convert` or `forcing` from a marker.
- The retry-id allocator unreachability claim. It is re-verified, not changed.
- #2655.
