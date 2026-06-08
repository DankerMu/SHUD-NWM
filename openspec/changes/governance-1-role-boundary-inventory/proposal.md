## Why

The code already has runtime role guards for `compute_control`, `display_readonly`, and `slurm_gateway`, but the repository lacks a single role-boundary inventory that tells maintainers which paths belong to each role and which paths are shared contracts. This leaves future work vulnerable to accidental cross-plane leakage.

## What Changes

- Add a current role-boundary source of truth covering four categories: `compute_control`, `display_readonly`, `slurm_gateway`, and `shared_contract`.
- Add static tests that enforce the most important boundaries: no Slurm routes on display, no compute-only env in display config, no business routes in the standalone Slurm gateway, and no production orchestrator reference to QHH diagnostic scripts.
- Identify and plan the layer-inversion cleanup where shared packages or workers import `apps.api.auth`.
- Add an implementation issue for shared auth/policy extraction so the layer inversion is not left as a permanent known finding.

## Capabilities

### New Capabilities

- `role-boundary-inventory`: Documents and enforces the repository's four-role ownership model.

### Modified Capabilities

<!-- No existing capability is modified; this change adds governance guardrails. -->

## Impact

- Dependency: starts after `governance-0-ci-contract-baseline` is merged, or with an explicit maintainer waiver that lists current red checks.
- Documentation: `docs/governance/ROLE_BOUNDARY.md`.
- Static tests: `tests/test_role_boundary_static.py`, plus references to existing runtime/two-node tests as evidence. #360 does not edit existing runtime/two-node test files unless a pre-existing test name/path has to be corrected for this new static test to import.
- Runtime/config reference points: `apps/api/runtime_mode.py`, `apps/api/main.py`, `apps/api/routes/pipeline.py`, `services/slurm_gateway/app.py`, `infra/env/*.example`, `infra/compose.*.yml`.
- Refactor planning: `packages/common/model_registry.py`, `services/orchestrator/retry.py`, `workers/flood_frequency/*`, `workers/model_registry/*`, `workers/shud_runtime/runtime.py`.
