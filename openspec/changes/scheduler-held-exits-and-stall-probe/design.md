# Design

Change surface: `scripts/node22_scheduler_stall_health.py`; `services/orchestrator/scheduler_candidates.py`,
`scheduler_state_failure.py`, `chain_repository_state.py` (read side only if needed);
`workers/shud_runtime/runtime.py` (`_object_checksum_limited`); `services/orchestrator/file_orchestration_journal.py`,
`cli.py`, `operator_action_listing_held.py`; runbooks `docs/runbooks/production-ops/stuck-detection.md`
and the held-reservation disposal runbook introduced by #2675.

Must preserve:
- Stall probe: read-only, stdlib-only with no `services.*` import (D4), exit-code contract, the twelve existing verdicts and their precedence, neutral
  pass rules, suppression of old-cycle (outside the frontier) `comment_accounting_unproven` entries with
  the entry still listed in `suppressed[]`; a healthy forecast in flight (`active_duplicate_pipeline`
  below the time gate) stays `ok`.
- Strict lane: non-forcing-input forecast failures and #2600 stage-added manual retries keep today's
  upgrade + witness fallback; `_upgrade_retry_for_strict_warm_start_manifest` keeps its 2-argument
  signature; `tests/test_manual_retry_failed_stage_restart.py` and the #2439 regressions unchanged.
  A genuinely over-limit `.tsd.forc` still reports `DIRECT_GRID_TSD_FORC_TOO_LARGE`.
- Held rows: `demote_operator_verified_reserved_job` (forecast), `bind_operator_verified_reserved_job`,
  `permit_forcing_submit_retry` and reconcile-computed `credible_absence` behave as before; zero sbatch
  on every operator exit. The forecast demote keeps returning `None` on any miss with the CLI's generic
  refusal line.
- Rollback: files genuinely older than the fence stay excluded (`os.utime(..., prepared_at - 60)` cases);
  existing rollback/quiescence/rollforward tests stay green; receipts written before this change (no
  fence field) still work.

Governing invariants:
1. #2662: the probe's "is the frontier advancing" predicates equal the scheduler's own: a skip reason
   outside `_RETENTION_TERMINAL_SKIP_REASONS` is in-flight, never "cleared". The probe's literal copy
   is pinned to the scheduler's set by a parity test (the probe stays stdlib-only).
2. #2670: a stage-less retry with forcing-input failure evidence regenerates forcing on every lane.
3. #2682: a held forcing row leaves `reserved` only through a CAS that re-checks the full held tuple
   under the journal lock and records who verified absence and on what evidence.
4. #2660: the fence and the compared mtimes come from the same clock domain.

## #2662 details

- D4 of the probe is preserved: stdlib-only, no `services.*` import (enforced by
  `tests/test_node22_scheduler_stall_health.py`). The terminal skip-reason set is therefore a literal
  in the probe plus a parity test asserting equality with
  `scheduler_runtime._RETENTION_TERMINAL_SKIP_REASONS`; `scripts/select_ci_tests.py` routes
  `services/orchestrator/scheduler_runtime.py` to that test so drift reds in the PR lane.
- Payload fields read: `skipped_candidates[].reason` and `.cycle_time` (+ source id), next to the two
  counts. Fail-safe like the scheduler: an unknown reason is in-flight; `skipped_candidate_count > 0`
  with the list missing or malformed is in-flight; no skipped candidates at all stays idle.
- New shape `in_flight_held`: `submitted_count == 0`, `blocked_candidate_count == 0`, at least one
  non-terminal skip. It neither counts as progress nor clears. A skipped row with `status == "excluded"`
  (permanent exclusion such as `lineage_scoped_out_pre_cutover`) is never in-flight: nothing is running
  for it, and counting it would alert on a healthy lane for the whole lookback window after a cutover.
- Time gate `NHMS_SCHEDULER_STALL_IN_FLIGHT_MINUTES`: let the run be the newest contiguous non-neutral
  passes that are all blocked or in-flight-held. Its start is the last progress pass's start when that
  pass is inside the scan window, otherwise the oldest scanned pass of the run (a lower bound of the
  real duration; the receipt says which). Run span >= gate -> non-`ok` (reuse `submission_stalled` or
  add one verdict; precedence stays coherent). The gate must be reachable inside the scan window:
  validate the configuration like the existing limit check, or document the upper bound in the runbook.
  Default calibrated from numbers recorded in the repo (#1736 forecast 65-79 min; inter-pass gap p99
  160.4 / max 193.9 min) and validated by a read-only replay over the real artifacts currently on
  node-22 (healthy period): no non-`ok` verdict caused by the new rules.
- Frontier = the newest NON-NEUTRAL pass (a lock-contended or resource-limit pass has no skipped list
  and must not make the bypass flicker). A tracker entry is on the frontier when the
  `<source>_<YYYYMMDDHH>` cycle in its `subject_id` equals a (source, cycle_time) of that pass's
  candidates or skipped candidates, source compared case-insensitively. Such an entry is not suppressed
  and is marked `suppression_bypassed_frontier` in the receipt.
- Receipt: in-flight-held count, run start + whether it is a lower bound, bypassed entries. Additive
  fields; the receipt schema version is bumped only if the existing version contract requires it.
- Fixtures: reconstruction of the freeze from #2662's measured values (96 candidates, 94
  `active_duplicate_pipeline` + 2 `terminal_hydro_success` at cycle 2026-09-25T00Z, last progress pass
  2026-09-25T16:45:42Z, 340 zero-submission passes of which the probe scans 64, tracker entries
  2200 / 2098 / 338 / 338). Tests: both fixes -> non-ok; only (a) -> non-ok; only (b) -> non-ok;
  pre-change code -> ok (red proof); progress pass outside the window -> non-ok with lower-bound flag;
  newest pass neutral -> bypass still applies.

## #2670 details

- `_manual_retry_state_evidence` writes an explicit marker (e.g. `manual_retry_forcing_input_failure`)
  when the failed stage is `forecast` and `_forcing_input_failure(state)` holds.
- At the upgrader call site, next to `_manual_retry_added_restart_strict_fallback`: pre-upgrade decision
  is `retry`, has no restart stage and carries the marker -> keep the pre-upgrade decision.
- `_object_checksum_limited`: a missing or unreadable object reports `FORCING_CHECKSUM_READ_FAILED`
  (the code `docs/spec/02_data_product_and_time_semantics.md` and
  `docs/modules/04_forcing_production_design.md` already pin); only a real over-limit read reports
  `limit.error_code`. Today both raise `ObjectStoreError`, so the implementation must introduce a real
  discriminator (for example a distinct exception subclass or attribute raised by
  `read_bytes_limited` for the over-limit case) rather than matching message text. Input -> code:
  member absent -> `FORCING_CHECKSUM_READ_FAILED`; member larger than the limit ->
  `DIRECT_GRID_TSD_FORC_TOO_LARGE`. `_forcing_input_failure` already matches the `FORCING_` prefix; its
  docstring is made true.
- Automatic-retry path: audit which restart stage it selects for a forecast forcing-input failure and
  report the finding; fix here only if it shares the defect, else record why not.

## #2682 design (published in the PR description before code)

- Problem: forcing master `reserved` / unbound / `submit_result_ambiguous`, reconcile keeps answering
  `query_unavailable`, and no master carries this attempt's comment
  (`nhms_forcing_attempt:<key>:a<attempt>`) in sacct/squeue. Bind has nothing to bind;
  `demote-reserved-job` rejects non-forecast rows; automatic `permit_forcing_submit_retry` needs a
  reconcile-computed `credible_absence` that never arrives. `query_unavailable` is a reconcile result,
  not a row field, so the CAS cannot and does not test it: the operator's verification replaces it.
- Entry: the existing `demote-reserved-job` command (click and argparse entries in
  `operator_reserved_demotion.py`), same options: `--job-id`, `--expected-attempt`,
  `--expected-attempt-started-at`, `--checked-by`, `--checked-at`, `--verification-note`, `--confirm`.
  No new command name.
- Dispatch: inside the cycle lock, on the freshly read row, `is_forcing_stage_name(stage, job_type)`
  selects the forcing branch BEFORE `accepted_submit_contract_is_current` (forcing rows carry no
  contract version; the same order `bind_operator_verified_reserved_job` uses). The CLI's pre-lock read
  is never the dispatch authority. The forecast branch is untouched: it still returns `None` on any
  miss and the CLI prints the generic refusal.
- CAS for the forcing branch (the held tuple `_bind_operator_verified_forcing_locked` uses), each miss
  a named refusal, zero bytes written:
  1. row exists - an unknown job id keeps the existing contract (method returns `None`, CLI prints
     `pipeline job not found`): without a row the lane is unknown;
  2. `forcing_submit_identity_is_complete(row)` (`identity_incomplete`);
  3. `status == "reserved"`, `slurm_job_id` and `matched_slurm_job_id` both empty,
     `submit_outcome == "submit_result_ambiguous"`, `reconciliation_decision in (None, "")` (`not_held`);
  4. `submission_attempt == --expected-attempt` (`stale_attempt`);
  5. `submission_attempt_started_at == --expected-attempt-started-at` (the anchor; `stale_attempt`);
  6. `checked_at` is RFC3339, not in the future, and `checked_at >= submission_attempt_started_at +`
     `reconcile.RESERVATION_ABSENCE_GRACE` - the grace the forcing `credible_absence` branch of
     reconcile uses, not `restart_reconcile_absence_seconds` (`verification_before_grace`).
  Result type `OperatorDemoteResult` mirrors `OperatorBindResult` (`refusal: str | None` + receipt) and
  is returned only for forcing rows by the same method (the CLI and its tests bind to that method
  name); the CLI exits non-zero and prints the refusal name.
- Attestation is mandatory: `--checked-by` and `--verification-note` non-blank after strip. No default,
  no "unknown". The note is the operator's sacct/squeue evidence (command and result). The CLI rejects
  a blank value or a malformed `--checked-at` in its option validation, before the journal is opened.
  At the journal-method level the lane is only known from the locked read, so an invalid attestation
  costs one locked READ and zero writes: a forcing row answers `attestation_missing` (or
  `verification_before_grace` for an unreadable `checked_at`), any other row re-raises the existing
  typed error (the forecast contract pinned by `tests/test_orchestrator_demote_core_cas.py`).
- Write, one locked append using the event-carrying writer the forecast demote uses:
  the row goes to `status = reservation_lost` with `reconciliation_decision = absence_retry_permitted`
  - exactly what `permit_forcing_submit_retry` writes, because forcing reclaim honours only that
  decision (the `operator_verified_absence` allowlist is forecast-only). The audit trail
  (`checked_by`, `checked_at`, `verification_note`, expected attempt and anchor) lives ONLY in a
  pipeline_event with `event_type = "operator_verified_absence"`, never on the row, so reclaim cannot
  carry a stale attestation into attempt+1.
- Why members unfreeze: `reservation_lost` is terminal, so `is_unresolved_forcing_attempt` is false and
  the scheduler's held-skip predicate stops matching: the members' decision becomes a retry with
  `restart_stage=forcing`, and the same-key reserve of that submit reclaims the master as attempt+1.
  Tests assert through the real reserve/reclaim path and the scheduler's own predicate, not by status
  alone. (Re-running `orchestrate_cycle` on the released run does not reclaim by itself - identical to
  the automatic `permit_forcing_submit_retry`, i.e. pre-existing lane behaviour.)
- Double-write risk (accepted, same class as the forecast demote): if the job did run, the retry is a
  second writer over forcing artifacts keyed by source/cycle/basin, not attempt. Code-side mitigations:
  the grace gate above, the full held tuple, zero sbatch / zero scancel. The rest is the operator's
  verification that every master with this attempt comment is terminal/cancelled or absent; the runbook
  states it as the precondition and the event records it.
- Listing: the action->command mapping and the pinned command set
  (`{bind-reserved-job, triage, escalate}`) are unchanged; "no job found" is not a listing dimension.
  The listing's forcing guidance text and the runbook route the "no job found" branch to
  `demote-reserved-job`. The disposal procedure lives in `docs/runbooks/held-forcing-absence-exit.md`
  (`failed-basin-retry.md` is at the large-file guard limit).

## #2660 details

- The receipt is validated by an exact key set and a hash over `signed`, and is rewritten on status
  transitions, so neither a new receipt key nor the receipt's own mtime is a stable fence. The fence is
  a sidecar file in the journal root named exactly `reconcile-inventory-rollback-fence-v1` (NOT
  dot-prefixed and not starting with any of the three reconcile-migration temp prefixes, because
  `_cleanup_reconcile_migration_temp_residues_unlocked` rejects unknown entries under those prefixes
  in ensure-migrated, prepare and rollforward - on old code too). Its `st_mtime_ns` is the fence, so
  both sides of the comparison come from the filesystem clock. `changed_since_prepare` compares
  `metadata.st_mtime_ns >= fence.st_mtime_ns`.
- Lifecycle: the FRESH prepare path (the only one that takes a new `prepared_at`) writes the sidecar
  immediately before `prepared_at` is taken and REPLACES any existing one (an orphan from a crash must
  not pin the fence at an old instant and widen the scan to all history). The two resume paths do not
  touch it. Rollforward removes it where it removes the receipt. An orphan sidecar without a receipt
  is harmless.
- No sidecar (receipts prepared by older code, or a crash before it was created) -> the old wall-clock
  comparison. Old code ignores the sidecar, so mixed-version reads are safe in both directions and the
  receipt schema is unchanged.
- Deterministic regression test, local: `os.utime` the sidecar to T and the continuation segment to
  T + 1 ms while wall-clock `prepared_at` is T + 5 ms (coarse-mtime shape); assert
  `query_rollback_unsettled_jobs()` returns the continuation job. Red on pre-change code. A file at
  T - 60 s stays excluded. A receipt without sidecar keeps the wall-clock behaviour. With the sidecar present, prepare,
  ensure-migrated and rollforward do not raise. A fresh prepare over an orphan sidecar takes a new fence.

Sibling surfaces:
- `services/production_closure/readiness_scheduler_evidence.py`: cardinality check only - none.
- `tests/test_gateway_reconcile_writer_quiescence.py`, `..._rollforward.py`: same mtime gate - must stay green.
- DB-lane retry service / `chain_repository.py`: none - file-journal lane only.
- Forecast demote / forcing bind / automatic permit: must preserve (above).
- `DIRECT_GRID_TSD_FORC_TOO_MANY_LINES` / `_LINE_TOO_LONG`: real limit violations, codes unchanged.

Seams under test: `grade()` of the probe on fixture directories; journal-level scheduler decision tests
(strict lane) as in `tests/test_manual_retry_failed_stage_restart.py`; runtime checksum helper; the
journal method + CLI for #2682; `query_rollback_unsettled_jobs()`.

Required evidence: per-issue tests listed in tasks.md, each red on pre-change source and green after.

Non-goals: see proposal.

Review focus:
1. Probe predicate parity with the scheduler and no false positive on a healthy in-flight forecast.
2. #2682 CAS completeness, zero-write rejections, mandatory attestation, no double-submit path.
3. Strict-lane recovery cannot leak to non-forcing-input retries.
4. Fence lock-in across receipt rewrites and backward compatibility of old receipts.
