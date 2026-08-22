## Why

Issue #1648 identified symptom-first diagnostics in tests that poll production state. The current scope needs a factual split: production `_LeaseHeartbeat._run` already catches `renew()` exceptions and maps them to `lost=True`, while the terminal-state test's two test-owned workers can still lose unexpected exceptions as non-fatal thread warnings.

## What Changes

- Make the shipping heartbeat test distinguish a real `renew() -> False` takeover from `renew()` raising, and report a captured exception before either `heartbeat_seq` or `lost` assertions.
- Add an explicit regression for the unchanged production `renew()` exception-to-`lost` fail-closed mapping.
- Make the terminal finalizer/reader test surface unexpected worker exceptions before result assertions while preserving expected `TerminalStateError` as a substantive reader result.
- Preserve existing polling/join bounds, real production calls, file-lock cleanup, takeover and terminal-publication oracles.
- Add direct failure-injection evidence against the exact shipping test functions; do not change global pytest warning/timeout policy.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `real-integration-test-matrix`: production-state polling tests gain cause-first worker diagnostics without being reclassified as #1633 dedicated-completion-sentinel harnesses.

## Impact

- Tests: `tests/test_production_scheduler.py` and `tests/test_node27_timeseries_compression_supervisor.py`.
- Contract/evidence: this OpenSpec delta and test-only failure-injection coverage.
- No production module, API, scheduler behavior, terminal receipt format, dependency, CI workflow, selector rule, global warning policy, DB, Slurm, display deployment, or live environment changes.
