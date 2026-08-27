## Why

The node-22 state-clone CLI can leave live state-index rows without a persistent receipt, and an `O_EXCL` receipt-write failure can replace the real clone-abort reason. Issues #1709, #1713, and #1715 are three control-flow gaps in the same evidence invariant and should close together so the two transfer modes do not diverge.

## What Changes

- Require `--receipt` for every `recalibration` invocation, including dry-run, while preserving the existing optional receipt contract for `baseline_cutover`.
- Make `baseline_cutover` preserve and declare already-persisted clone decisions when a later basin/source fails, then propagate the original failure.
- Give both modes one receipt-write discipline: on an already-failing invocation, a receipt-write error is attached to the original failure and cannot replace it; on a clean invocation, receipt-write failure still fails normally.
- Add focused CLI tests for partial baseline application, both receipt-masking branches, the required flag, successful recalibration artifact creation, and unchanged baseline invocation compatibility.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `fingerprint-gated-state-clone`: make persistent clone evidence and abort-error precedence explicit for the file-index CLI routes.
- `ci-contract-baseline`: after splitting the oversized recalibration CLI suite at its `§6.8 --pairs resolution` marker, route node-22 clone-script changes to all four owned suites, base shared-fixture changes to all four direct consumers, and CLI-helper (`tests/state_clone_recalibration_cli_fixtures.py`) changes to exactly the two recalibration CLI modules (excluding the baseline suite).

## Impact

- Code: `scripts/node22_clone_direct_grid_cutover_states.py`.
- Tests/CI selection: `tests/test_state_clone_recalibration_cli.py`, split validation module `tests/test_state_clone_recalibration_cli_validation.py`, focused `tests/test_state_clone_baseline_cutover_cli.py`, `scripts/select_ci_tests.py`, and its selector contract test.
- Operations: node-22 CLI only; no DB, Slurm scheduling, state-clone gate, state-index schema, receipt schema version, or successful receipt field changes.
- Documentation: `docs/runbooks/current-production-ops.md` is checked for consistency; its recalibration commands already pass unique receipt paths.
