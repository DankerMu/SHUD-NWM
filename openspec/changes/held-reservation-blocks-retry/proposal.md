## Why

node-22 production resubmitted a whole forecast cohort (issue #2666). On 2026-09-27 the IFS 0925 12Z forecast array submit timed out at the gateway. Slurm had accepted it as 57553, but the journal row stayed `reserved` / `submit_result_ambiguous` / `slurm_job_id=null`. The next pass's restart reconcile bound only the gfs sibling; #2655 accepts one bind per pass, so the IFS row reported transient `query_unavailable`.

In the same pass the candidate-state decision judged all 47 IFS members `retry_after_completed_stage`: forcing had succeeded, so `completed_stage_retry_supersedes_hydro_placeholder` treated their `created` hydro placeholders as stale. It sbatched 57637, a second forecast array for the same runs. The two arrays overlapped in the same `run_id` directories and basin 18 of 57553 failed with `OUTPUT_ROW_COUNT_MISMATCH`.

The decision never looks at the candidate's own held reservation:

- `_state_active_jobs` requires a real Slurm binding.
- `ACTIVE_PIPELINE_STATUSES` lacks `reserved`.
- On the DB-free path the journal's `has_active_pipeline` check (which does treat `reserved` as active) is skipped because a `state_provider` is configured.

The 0925 00Z cohort (#2655) was blocked only incidentally: a failed state_save_qc row in that cycle suppressed the completed-stage resume.

## What Changes

- The candidate-state decision fails closed on a held reservation. It returns `skip` / `active_duplicate_pipeline` (decision `skip_active`, `active_status: reserved`, with the held rows as evidence) when the candidate's state carries a pipeline row that meets both conditions:
  - its status is `reserved` (`persistence.RESERVED_STATUS`);
  - it has no real Slurm binding.

  The held row must be attributed to the candidate by the existing candidate-state membership rules (own/model row, `member` cohort row, or `unwitnessed` cycle-scope row; `non_member` rows are already removed).
- The check runs before every resume, retry, and failure branch: the completed-stage resume, the terminal and hydro-placeholder supersession, and the failure policy. The reservation's reconcile reason class is irrelevant: it may be transient `query_unavailable`, `comment_accounting_unproven`, or `ambiguous_fallback_match`.
- Once restart reconcile binds the row (`matched_bound`) or releases it (`reservation_lost`), the check no longer applies and the existing decisions resume unchanged.

## Triage

```text
Issue type: bugfix
Fixture level: expanded
Upstream suggested level: absent (P0 production double submission; scheduler decision contract on the live DB-free path)
Blast radius: a missed block re-sbatches a cohort that Slurm already accepted (duplicate compute, same-run-dir collision, corrupted member outcome); an over-broad block freezes candidates behind stale reserved rows. Production journal holds exactly one reserved row today (the #2666 row), so the fail-closed side has a measured footprint of one cohort until reconcile binds it.
Selected risk packs: Concurrency/shared state; Legacy compatibility; Error handling; Documentation; Slurm production lifecycle
Evidence floor: red-before/green-after decision tests, focused scheduler-state + chain + gateway-reconcile suites, full `uv run pytest -q` on node-27, `uv run ruff check .`, strict OpenSpec, node-22 scratch-journal rehearsal + first live pass receipt
```

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `job-retry-mechanism`: add "A held reservation SHALL block the candidate-state retry decision".

## Impact

- Code: `services/orchestrator/scheduler_state_decision.py` (and a helper in `scheduler_state_rows.py`); no journal, reconcile, or evidence-schema change.
- Tests: scheduler state-decision tests plus a chain-level reproduction on the file journal.
- Operations: `docs/runbooks/scheduler-dbfree-typed-reasons.md` notes the held-reservation shape of `active_duplicate_pipeline`. node-22 deploy is `git pull --ff-only` with the timer stopped (no dependency change, no `uv sync`).
- No DB schema, display, gateway, or sbatch change.
