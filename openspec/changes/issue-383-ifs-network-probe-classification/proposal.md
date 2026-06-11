## Why

Issue #383 tracks a node-22 production failure where IFS DNS/name-resolution errors from a compute node were reported as `status=unavailable`. That payload conflates source data latency with a retryable compute-network boundary failure, which misleads operators and can prematurely block downstream automation.

## What Changes

- Classify IFS availability-probe DNS/network/timeout failures separately from genuine 404/not-yet-published source-cycle unavailability.
- Preserve attempted mirror evidence plus redacted concrete error type/message in adapter discovery, CLI output, and scheduler evidence.
- Keep genuine forbidden and unpublished source states distinct from retryable network probe failures.
- Add regression coverage for all configured mirrors failing with DNS/name-resolution errors.
- Document operator recovery for shared source-cycle download failures caused by compute-node network issues.

## Capabilities

### New Capabilities

- `ifs-network-probe-classification`: IFS availability probing and source-cycle download evidence distinguish provider unavailability from compute-node network/probe failures.

### Modified Capabilities

None.

## Impact

- Affects `workers/data_adapters/ifs_adapter.py`, `workers/data_adapters/cli.py`, scheduler evidence consumers in `services/orchestrator`, focused IFS/scheduler tests, and node-22 runbook notes.
- No database migration, frontend change, public API route change, or Slurm gateway routing change.
