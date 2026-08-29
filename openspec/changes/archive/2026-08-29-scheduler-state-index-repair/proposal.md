## Why

A manual edit to the shared state snapshot index invalidates its payload checksum and blocks every scheduler candidate before entries are parsed. Master has no supported single-entry removal or checksum-only repair path, so recovery still depends on private helpers or another unsafe manual edit.

The same PR also closes #1482's open design choice: projection-only repaired annotations remain deliberately outside the durable manual-repair marker contract rather than becoming a second persisted repair authority.

## What Changes

- Add a dry-run-by-default operator CLI for exact state-index entry removal across the private/reference and shared/destination indexes, plus explicit-lane checksum-only repair.
- Reuse the production state-index schema, checksum, canonical serialization, identity key, provider locks, compare-and-swap, and read-back validator through a narrow shared helper; never copy either full index over the other.
- Require private pre-image archives before mutation and emit a bounded per-lane receipt that distinguishes refusal, partial commit, and commit-uncertain outcomes.
- Document checksum-mismatch diagnosis and recovery in the current node-22 operations runbook; prohibit hand-editing the index.
- Finalize #1482 with option (c): accept and document the existing conservative over-pin limitation when a target is already repaired only in a projection copy at marker-write time. No durable pipeline-row schema or retry decision changes are introduced.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `file-state-snapshot-index`: Add a supported, archive-first, exact and receipted operator repair contract.
- `job-retry-mechanism`: Make the projection-only repaired-at-write limitation an explicit terminal contract rather than an open follow-up.

## Impact

- `packages/common/state_manager.py`
- New `scripts/scheduler_state_index_repair.py`
- `tests/test_state_manager.py` and new CLI tests
- `docs/runbooks/current-production-ops.md`
- Retry contract documentation in OpenSpec and existing source comments; no retry runtime code or persisted row schema change
- Node-22 is the deployment oracle only for the scheduler/state-index operator surface; no Slurm job submission or node-27 database/display behavior changes
