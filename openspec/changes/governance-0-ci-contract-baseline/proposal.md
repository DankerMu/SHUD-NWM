## Why

Current governance work must not start from a red trunk. The latest master run for `0135f4f` fails in `tests/test_api_contract.py::test_generated_frontend_types_match_openapi`, proving that the static OpenAPI contract and generated frontend types are already out of sync.

This change establishes the pre-governance baseline: restore CI green, pin the contract-generation workflow, and make future governance PRs start from a verifiable state rather than mixing cleanup with existing failures.

## What Changes

- Restore the master unit-test gate by reconciling `openapi/nhms.v1.yaml` with `apps/frontend/src/api/types.ts`.
- Document and automate the exact contract regeneration command used by backend and frontend contributors.
- Align local tooling entrypoints with repository rules by moving Makefile Python/lint commands to `uv run`.
- Record the P0 governance entry criteria: full backend fast tests and generated contract drift checks must pass before role-boundary, dead-code, docs, or entropy-automation PRs begin.

## Capabilities

### New Capabilities

- `ci-contract-baseline`: Establishes the required green-trunk and contract-generation baseline for all subsequent governance changes.

### Modified Capabilities

<!-- No existing OpenSpec capability is modified; this is a governance baseline gate. -->

## Impact

- Backend contract tests: `tests/test_api_contract.py`, `tests/test_openapi_drift.py`.
- Contract artifacts: `openapi/nhms.v1.yaml`, `apps/frontend/src/api/types.ts`.
- Tooling: `Makefile`, `.github/workflows/ci.yml`, `AGENTS.md`/README command references if needed.
- Governance sequencing: `governance-1` through `governance-4` depend on this baseline being green.
