# Design — reconcile-module-split

## Partition

These are original-line references on `origin/master`. Every top-level symbol is assigned to exactly one module.

| module | contents | ≈ lines |
|---|---|---|
| `reconcile_records` (leaf root) | `_as_utc_datetime`, `_strict_utc_datetime`; the 4 query exception classes; `SacctRecord`, `SacctQuerier`; `_parse_master_sacct_row`, `_sacct_record_from_fields`, `_aggregate_array_member_rows`; `CommentAccountingResult`, `CommentSacctQuerier`; `_parse_fallback_submit_instant`, `_fallback_master_id`, `_FALLBACK_SUBMIT_FORMATS`; `_callable_accepts_keyword`; shared constants `MAX_FALLBACK_MASTERS`, `FALLBACK_BASIS_*`, `RECONCILE_UNVERIFIED_STATUS`, `RESERVATION_LOST_STATUS`, `RESERVATION_ABSENCE_GRACE`, `ABSENCE_UNCONFIRMED_ACTION`, `IDENTITY_MISMATCH_RELEASED_ACTION`, `JOURNAL_QUARANTINED_ACTION`, `MAX_EXACT_COMMENT_MATCHES` | 390 |
| `reconcile_identity` | `_expected_job_name_token`, `_GENERIC_ARRAY_JOB_NAMES`, the stage alias sets, `_identity_matches` and its `_record_*` / `_identity_*` / `_stages_match` / `_stage_family` helpers; `_is_forecast_cohort_job`, `_forcing_submit_reconcile_job`, `_forcing_accounting_identity_matches`, `_file_cohort_runtime_identity_matches`, `_fallback_comment_allowed`, `_accepted_submit_versioned_job` | 270 |
| `reconcile_sacct_probe` | visibility constants; `_terminate_and_reap`, `_bounded_visibility_stdout`, `_private_data_allows_global_jobs`, `default_global_accounting_visibility_probe`, `_accounting_store_flags_classify`, `default_comment_storage_probe`; the controller-identity cluster (`_controller_*`, `_CONTROLLER_IDENTITY_FIELDS`, `_query_forcing_controller_identity`) | 400 |
| `reconcile_comment_sacct` | `COMMENT_SACCT_*`, `FALLBACK_JOB_NAME`, `FALLBACK_SACCT_FORMAT`, `_SUBMITLINE_COMMENT_FLAG`, `MAX_COMMENT_SACCT_BYTES/ROWS`; `_fallback_candidate_in_window`, `_submitline_comment_key`, `_FallbackMaster`, `_parse_fallback_sacct_rows`, `_fallback_eligibility`, `_forcing_controller_identity_request`, `default_comment_sacct_querier`, `_SacctScanBudget`, `_bounded_sacct_page`, `_bounded_sacct_stdout`, `_index_comment_sacct_matches`, `default_sacct_querier` | 700 |
| `reconcile_comment_proof` | `_CommentAccountingProof`, `_comment_query_records`, `_bounded_comment_records`, `_classify_global_comment_records`, `_effective_versioned_absence_coverage`, `_query_comment_accounting_proof`, the 4 `_comment_query_accepts_*`, `_comment_query_capability` | 380 |
| `reconcile_inflight` | `ReconcileOutcome`, `_sacct_query_job_id`, `reconcile_inflight_jobs`, `_bound_forcing_submit_reconcile_job`, `_terminal_file_cohort_identity_matches`, `_file_cohort_task_projections`, `_transition_file_runtime_status` | 430 |
| `reconcile_reserved_support` | `_targeted_reserved_unbound_jobs`, `ReservationReconcileOutcome`, `_job_submission_attempt`, `_job_attempt_anchor`, `_record_file_reconciliation`, `_job_identity_blocked_*`, `_identity_blocked_*`, `_release_identity_blocked_reservation`, `_transition_forcing_submit_ambiguity`, `_transition_file_reconciliation`, `_accepted_submit_reconcile_job`, `_accepted_submit_runtime_lane_eligible` | 400 |
| `reconcile_reserved` (**guard-excluded**) | `reconcile_reserved_unbound_jobs`, `_reserved_record_identity_matches` | 1125 |
| `reconcile` (facade) | docstring, `import subprocess`, the `SLURM_JOB_ID_RE` re-import from `real_backend`, `LOGGER`, re-exports of the import surface | 150 |

The implementer may move a symbol between leaves when a dependency check requires it. The constraints still apply:
no cycle, no leaf importing the facade, patched names co-located with their callers, and every non-excluded module at
or under 1000 lines. Each such move is recorded in the PR body.

## Import DAG

```
records <- identity <- probe <- comment_sacct
records <- proof
records, identity <- inflight
records, identity <- support
records, identity, support, proof <- reserved
all leaves <- reconcile (facade); no leaf imports the facade
```

`file_orchestration_journal.py:5287-5289` already lazy-imports `reconcile` because of the existing load-time edge,
so the split adds no cycle. The `FileOrchestrationJournalError` import moves with `reserved`. The `real_backend` import
is split across the leaves that use `SLURM_JOB_ID_RE`, `SLURM_STATE_MAP`, `_normalize_slurm_state` and
`map_slurm_error_code`.

## Patch-target table (explorer audit at 5988ef56b; re-derived for E3)

| name | patched at | target after the split |
|---|---|---|
| `_bounded_sacct_stdout` | ~39 sites in 10 files (comment_accounting, comment_sacct_bounds, comment_capability, forcing_submit_ambiguity, bind_reserved_*, production_scheduler:59260,59416) | `reconcile_comment_sacct`; `_bounded_sacct_page` reads `inspect.signature(_bounded_sacct_stdout)`, so they must share a module |
| `_bounded_visibility_stdout` | 13 sites (bind_reserved_forcing_lane, orchestration_chain:11920, comment_capability, forcing_submit_ambiguity) | `reconcile_sacct_probe` |
| `MAX_COMMENT_SACCT_BYTES`, `MAX_COMMENT_SACCT_ROWS`, `COMMENT_SACCT_TIMEOUT_SECONDS` | comment_sacct_bounds, comment_accounting, production_scheduler:59323 | `reconcile_comment_sacct` (callers `_SacctScanBudget`, `_bounded_sacct_stdout`, `_index_comment_sacct_matches`, `_parse_fallback_sacct_rows`) |
| `COMMENT_SACCT_VISIBILITY_TIMEOUT_SECONDS`, `MAX_VISIBILITY_PROBE_BYTES/ROWS` | comment_capability:554-648 | `reconcile_sacct_probe` |
| `_parse_fallback_sacct_rows` | comment_accounting:839 | `reconcile_comment_sacct` |
| `_reserved_record_identity_matches` | comment_accounting:1560 | `reconcile_reserved` |
| `forecast_cohort_identity_is_valid` (imported name; callers in inflight, reserved and support) | file_cohort_identity:944 | `reconcile_inflight`, the leaf whose caller the test drives (narrowing recorded in the proposal) |
| `default_comment_sacct_querier`, `default_sacct_querier` | production_scheduler:48989,48994 | **facade**: read at call time by `scheduler_runtime.py:1907,1915` and `chain_stage_execution.py:259` |
| `RESERVATION_ABSENCE_GRACE` | orchestrator_demote_reserved_job_forcing:539 | **facade**: read as `reconcile.RESERVATION_ABSENCE_GRACE` by `file_orchestration_journal.py:5300` and `scheduler_runtime.py:1585`; it reaches `reconcile_reserved_unbound_jobs` only as a def-time default (`:2201`), which a facade patch never governed |
| `subprocess.Popen` | comment_sacct_bounds:324,434, comment_accounting:644 | global object; the facade keeps `import subprocess` |

## Risks
- **A missed patch target passes vacuously.** Mitigated by the E3 re-derivation and the mutation check. CI and
  node-27 have no `sacct`, so a missed bound patch could fall through to a real subprocess call. That call fails
  loudly (`ReconcileQueryUnavailable`) rather than passing silently.
- **Selector drift.** New leaves widen the `services/orchestrator/**` derived importer set.
  `test_directory_rule_importer_gaps_are_dispositioned` and the `runtime-budget` table must be updated, or the
  selector meta-tests fail. That failure is loud.

## Commit order

The large-file guard counts staged line totals. While `reconcile.py` is over 1000 lines, every commit touching it is
blocked, and the new ~1100-line `reconcile_reserved.py` is blocked unless its exclusion is staged with it. The retire
cannot land first on the monolith. The stages below order the work and its evidence. They may land as one commit,
because the end state satisfies the guard.

- **Commit A:** the partition.
  - Leaves are cut by script from `origin/master` line ranges, including leading comments.
  - The same commit carries the facade, the patch retargets, the logger names, the guard entry, the path-keyed
    consumers and the line-reference rewrites.
  - E2 allows only import and `LOGGER` differences. The two parsers still exist in `reconcile_comment_sacct.py` at
    this point.
- **Commit B:** the retire. The two parsers are removed from `reconcile_comment_sacct.py`, the LOW-2 cases are
  retargeted, and E7 runs.
- **Commit C:** selector changes and the selector assertion.

## Notes for the implementer
- **Live parser behaviour:** `_index_comment_sacct_matches` folds `.batch` / `.extern` step rows into their master by
  `split(".", 1)[0]` and dedupes them; it does not skip them. Retargeted LOW-2 docstrings and assertions must not
  claim "skipped" for step rows. Where a step row is involved, assert that `job_name` and `state` come from the master
  row.
- **Held-reservations scan:** `tests/test_operator_action_listing_held_reservations.py:103-147` now scans every
  `reconcile*.py`. It must merge the constant table across files: the constants are in `records`, and the
  `ReservationReconcileOutcome(...)` calls are in `support` and `reserved`. Otherwise it fails with "unresolvable
  action variable".
- **Allowlist order:** `tests/status_literal_allowlist.json` stays sorted by (file, members)
  (`tests/test_status_literal_scan.py:157`).
