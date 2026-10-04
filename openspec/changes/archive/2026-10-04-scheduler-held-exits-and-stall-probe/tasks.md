# Tasks

## Risk packs

- Public API / CLI / script entry: selected - probe verdict/receipt, `demote-reserved-job` forcing branch -> 3.x, 4.x.
- Concurrency / shared state / ordering: selected - journal CAS under lock, double-write precondition -> 4.2, 4.3.
- Error handling / rollback / partial outputs: selected - zero-write rejections, rollback fence, error-code split -> 1.x, 2.3, 4.3.
- Auth / permissions / secrets: selected - mandatory operator attestation -> 4.3.
- Legacy compatibility / examples: selected - receipts without the fence field, old-cycle suppression -> 1.3, 3.4.
- Documentation / migration notes: selected - stuck-detection.md, held-reservation runbook -> 3.6, 4.5.
- Config / project setup: selected - `NHMS_SCHEDULER_STALL_IN_FLIGHT_MINUTES` default and unit env example -> 3.2.
- Resource limits / large input / discovery: selected - the probe now parses skipped lists and the time gate
  is bounded by the scan window -> 3.2, 3.4.
- Schema / columns / units / field names: selected - rollback receipt key set stays exact (sidecar fence),
  probe receipt fields, journal row vs event fields -> 1.3, 3.3, 4.2.
- File IO / path safety / overwrite: not selected - no new write path; journal append uses the existing locked writer.

## 1. #2660 rollback fence clock domain

- [x] 1.1 Sidecar fence file `reconcile-inventory-rollback-fence-v1` written by the fresh prepare path; compare `st_mtime_ns`; receipt schema unchanged.
- [x] 1.2 Deterministic coarse-mtime regression test (red before, green after).
- [x] 1.3 Receipt without sidecar keeps the wall-clock comparison and still validates; genuinely-older files
      stay excluded; resume paths do not move the fence; a fresh prepare over an orphan sidecar takes a new
      fence; prepare, ensure-migrated and rollforward tolerate the sidecar; rollforward removes it.
- [x] 1.4 `uv run pytest -q tests/test_file_orchestration_journal.py -k rollback tests/test_gateway_reconcile_writer_quiescence.py tests/test_gateway_reconcile_writer_rollforward.py`

## 2. #2670 strict-lane forcing-input retry

- [x] 2.1 Evidence marker + call-site recovery; strict-lane journal tests for `FORCING_PACKAGE_CHECKSUM_MISMATCH`,
      `FORCING_FILE_NOT_STAGED`, `SHUD_FORCING_CSV_MISSING`: full chain, no restart stage, reason is not
      `strict_warm_start_retry_run_manifest_mismatch`, next pass submits convert, forcing, forecast.
- [x] 2.2 Strict lane unchanged for non-forcing-input failures and #2600 stage-added retries.
- [x] 2.3 `_object_checksum_limited` missing/unreadable vs over-limit; matcher recognises it; one None-lane
      and one strict-lane test for a missing `.tsd.forc` member; real over-limit keeps `*_TOO_LARGE`.
- [x] 2.4 Automatic-retry path audit recorded in the PR.

## 3. #2662 stall probe

- [x] 3.1 In-flight-held shape; streak not cleared; terminal reason literal + parity test against
      `scheduler_runtime._RETENTION_TERMINAL_SKIP_REASONS`; selector routes `scheduler_runtime.py` to it;
      missing/malformed skipped list and unknown reasons are in-flight.
- [x] 3.2 Time gate env + default with the calibration recorded.
- [x] 3.3 Frontier suppression bypass; receipt fields.
- [x] 3.4 Tests on the reconstructed freeze fixture: both / only (a) / only (b) -> non-ok; pre-change -> ok;
      old-cycle-only tracker stays suppressed; healthy in-flight forecast below the gate -> ok; progress pass
      outside the scan window -> non-ok with the lower-bound flag; newest pass neutral -> bypass still applies.
- [x] 3.5 Orchestrator: read-only replay of the fixed probe over the real artifacts currently on node-22.
- [x] 3.6 Runbook `stuck-detection.md` section 6.2.1 table and 6.2.4 suppression/residual-risk clauses.

## 4. #2682 forcing operator-verified absence exit

- [x] 4.1 Design, CAS and audit fields published in the PR description BEFORE the code commit.
- [x] 4.2 Journal method + CLI branch; reproduction test: held forcing row -> retryable, members no longer
      held-skipped, zero sbatch.
- [x] 4.3 Negative tests, each a named refusal with the journal byte-identical: attempt mismatch, anchor
      mismatch, not held (each of: status, bound `slurm_job_id`, `matched_slurm_job_id`, submit outcome,
      existing reconciliation decision), identity incomplete, blank `checked_by`, blank `verification_note`,
      future / malformed `checked_at`, `checked_at` before anchor + grace. Forecast rows keep the `None`
      contract. Both CLI entries (click, argparse) covered; `--confirm` preserved.
- [x] 4.4 Listing guidance text routes the forcing "no job found" branch to `demote-reserved-job`; the
      action->command mapping and the pinned command set are unchanged.
- [x] 4.6 Audit fields live only in the `operator_verified_absence` pipeline_event; after reclaim the
      attempt+1 row carries none of them.
- [x] 4.5 Runbook: precondition (sacct/squeue proof), double-write risk, command.

## 5. Verification

- [x] 5.1 `uv run ruff check .`
- [x] 5.2 `uv run pytest -q tests/test_node22_scheduler_stall_health.py tests/test_manual_retry_failed_stage_restart.py
      tests/test_operator_action_listing_held_reservations.py tests/test_orchestrator_demote_cli_security.py
      tests/test_orchestrator_demote_core_cas.py tests/test_orchestrator_demote_reclaim_lifecycle.py
      tests/test_orchestrator_demote_projection_faults.py tests/test_scheduler_held_reservation_block.py
      tests/test_forcing_submit_ambiguity.py tests/test_gateway_reconcile_writer_quiescence.py
      tests/test_gateway_reconcile_writer_rollforward.py tests/test_select_ci_tests.py`,
      `uv run pytest -q tests/test_file_orchestration_journal.py -k rollback`, `uv run pytest -q tests/test_shud_runtime.py -k "checksum or tsd_forc or forcing"`,
      plus every test file added by 1-4.
- [x] 5.3 `openspec validate scheduler-held-exits-and-stall-probe --strict --no-interactive`
- [x] 5.4 CI green.

## Evidence Floor deviation

- node-27 真实 DB receipt 待链路恢复后补 (node-27 link fault, high IO forbidden; nothing below was run there):
  - `uv run pytest -q tests/test_file_orchestration_journal.py::test_file_journal_rollback_scope_iteration_tolerates_continuation_segments --count=50`
    (or a 50x shell loop) on ext4 with `TMPDIR=/home/nwm/tmp` -> 0 failures (#2660 acceptance 1; replaced
    in this PR by the local coarse-mtime injection test)
  - the targeted pytest files of 5.2
- node-22, operator-gated, not done in this batch:
  - #2682 scratch-journal drill of the new exit, zero sbatch
  - #2662 live read-only probe run (`--json`) after deployment
- #2662 real-evidence replay of the freeze is impossible: the artifacts were deleted by retention. Done
  instead (task 3.5): the 379 real artifacts on node-22 (2026-10-02..10-04, copied read-only) replayed
  locally at 47 instants with the pre-change and the fixed probe: identical verdicts (46 `ok`, 1
  `pass_limit_blocked`), no verdict caused by the new rules.
- `DEFAULT_SCAN_LIMIT` 64 -> 96 with config validation (`IN_FLIGHT_MINUTES <= (SCAN_LIMIT - 13) * 5`): a
  node-22 drop-in pinning `SCAN_LIMIT` below 85 would make the probe exit 2 after deployment. Checked
  2026-10-04: node-22 has no drop-in directory for the probe unit.
- #2670 automatic-retry audit: the main automatic lane never retries a forcing-input forecast failure
  (pinned by test); the model-package-refresh lane can, filed as #2719 (not fixed here).
