## Why

`tests/test_gateway_reconcile.py` contains 14,184 lines and cannot be touched under the repository's
1,000-line guard without an exemption. It must be physically partitioned without changing the 538
collected gateway-reconcile cases, their assertions, or the targeted-CI coverage that currently
selects the monolith.

## What Changes

- Replace the monolithic suite with flat, responsibility-focused `tests/test_gateway_reconcile_*.py` modules, each at or below 1,000 lines.
- Move shared test-only fixtures into non-collected support modules, also at or below 1,000 lines, and route support-module changes to their consumers.
- Preserve every collected test function and parameter ID through an explicit old-to-new node-id mapping; do not weaken or rewrite any assertion or oracle.
- Replace the deleted suite path in the existing `services/slurm_gateway/**` selector target inventory without changing selector ownership or broadening the `services/orchestrator/**` runtime-budget boundary.
- Update checked-in test imports, active OpenSpec commands, canonical owner references, inventory commands, runbook node IDs, and the stale accepted-decision comment to the new paths.
- Do not change production source or add a `.large-file-guard.json` exemption.

## Capabilities

### New Capabilities

- `gateway-reconcile-test-partitioning`: Governs size, collection identity, oracle preservation, and targeted-CI routing for the split gateway-reconcile suites.

### Modified Capabilities

- `real-integration-test-matrix`: Update the two gateway Barrier harness owner paths after the mechanical split; the bounded-concurrency requirement is unchanged.

## Impact

- Test modules under `tests/`, `scripts/select_ci_tests.py`, selector governance tests, compatibility inventories, and gateway-reconcile runbook references change.
- Production runtime behavior, APIs, schemas, dependencies, database behavior, and Slurm scheduling behavior do not change.
- Full old and new node-id strings differ only in their module-path prefix; the `::test_name[param-id]` suffix is the compatibility identity and must remain byte-identical.