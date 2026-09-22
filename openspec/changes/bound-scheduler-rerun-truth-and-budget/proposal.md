## Why

Three pre-existing db-free scheduler defects (node-22 file journal) let one candidate burn Slurm capacity past its retry budget or wedge §8.7 self-healing. They share one root: **the scheduler trusts an older truth over a newer one, and the writer that mints retry attempts does not share the reader that charges them.**

- #2401 — `terminal_pipeline_success` / `terminal_completed_cycle` (`services/orchestrator/scheduler_state_decision.py:248-281`) have no "failure newer than success" guard, unlike `terminal_hydro_success` (`scheduler_state_identity_filter.py:1010-1019`). A failed §8.7 quarantine rerun is therefore re-read as a completed-type skip, rewritten into another `retry_journal_predecessor_identity_mismatch` (no retry-budget check on that leg, `scheduler_candidates.py:2542-2670`), and the failed stamped master never counts toward the breaker (`completed_pipeline_init_state_id_occurrences` counts completed masters only). The same shape feeds `terminal_run_manifest_missing` (`scheduler_candidates.py:620-628`), which has no budget gate either. Inferred result: an unbounded forced-resubmit loop.
- #2404 — retry job ids are minted per `run_id` prefix (`chain.py:859-879` `_next_retry_attempt_for_stage`, called from `chain_forecast_orchestrator_cycle.py:195-204`), while the strict warm-start budget reads the max `_retry_<n>` suffix across all candidate-authoritative rows whatever their prefix (`scheduler_state_rows.py:498-628`). When `run_id` switches from `..._full_<model>` to `..._forecast_<model>` (or a cohort digest changes), the new prefix restarts at bare/`_retry_1` and the budget read does not advance: about 2x the budget for one model, and possibly unbounded for cohorts.
- #2397 — a same-run_id §8.7 corrective rerun cannot rewrite the succeeded `hydro_run` row (`file_orchestration_journal.py:2581-2627`, `_write_hydro_run` retriable-only at `:9349-9377`), and `completed_pipeline_init_state_identity` (`:1413-1493`) ranks that row first. The recorded `init_state_id` stays frozen at the first run's stale token, so quarantine never converges and the breaker fail-stops a candidate that could have recovered.

## What Changes

- Terminal classification: `terminal_pipeline_success` and `terminal_completed_cycle` apply the same "newest truth wins" rule as the hydro leg. When the latest failure truth is strictly newer than the success truth, the candidate is not terminal and falls through to the budgeted `retry_failed_candidate` path. This removes the completed-skip input to every skip→forced-retry rewrite (quarantine, `terminal_run_manifest_missing`, strict warm-start) in one place.
- Identity authority: a terminal-success accepted-submit candidate master that is newer than the completed `hydro_run` row wins identity authority. When there is no such newer master, `hydro_run` keeps priority. Only the read side changes; `hydro_run` write semantics (retriable-only, submit-once) are untouched.
- Retry minting: the cycle-stage retry suffix is at least `stage-scoped budget attempt + 1`, derived from the same attempt read the budget uses, with a forward scan past occupied ids. It does not travel through `context.retry_attempt`. Cohort geometry is verified and fixed here if it leaks.

## Capabilities

### Modified Capabilities

- `production-scheduler-orchestration`: newest-truth terminal classification; newest-truth identity authority for §8.7.
- `job-retry-mechanism`: retry minting derives from the budgeted stage attempt across `run_id` prefixes.

## Impact

- Code: `services/orchestrator/scheduler_state_decision.py`, a new small time-guard helper module, `services/orchestrator/file_orchestration_journal.py` (identity accessor), `services/orchestrator/chain.py` / `chain_forecast_orchestrator_cycle.py` (minting), and possibly `scheduler_candidates.py` (passing the budget floor). Files over 1000 lines that are not already guard-exempt (`scheduler_state_identity_filter.py` 1059, `chain_forecast_orchestrator_cycle.py` 1036, `scheduler_discovery.py` 1033) must either stay untouched or be brought to ≤1000 lines by extracting a cohesive helper module. No new `.large-file-guard.json` excludes.
- Runtime: node-22 db-free scheduler only. The DB lane, breaker threshold, global retry limit, and #1555 operator re-entry semantics do not change.

## Triage

```text
Issue type: bugfix (x3, one PR at user request)
Fixture level: expanded
Upstream suggested level: absent (issues from issue-scribe)
Blast radius: production node-22 scheduler — wrong terminal/retry decisions either resubmit full SHUD forecasts past budget or wrongly skip/block candidates; a regression can stop a healthy cycle from being recognised as done.
Selected risk packs: concurrency/shared-state/ordering; legacy compatibility; error handling/partial outputs; resource limits
Evidence floor: red-then-green real-journal regressions for each issue; `uv run ruff check .`; node-27 `tests/test_production_scheduler.py tests/test_file_orchestration_journal.py tests/test_scheduler_generation.py tests/test_orchestration_chain.py tests/test_retry.py` + new test files all green
```
