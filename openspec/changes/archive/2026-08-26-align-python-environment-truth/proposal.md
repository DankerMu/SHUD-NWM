## Why

The repository has no tracked Python pin, so local `uv` can select Python 3.14
while merge-gate CI runs 3.11. Pinning alone is unsafe on node-22: live services
share a Python 3.12.7 `.venv`, and environment-updating `uv` commands can replace
it before an approved maintenance window. Historical QHH guidance and stale
e2e/grib routing further blur which environment is authoritative.

## What Changes

- Track `.python-version` as 3.11 while retaining `requires-python >=3.11`, CI pip resolution, and explicit Python 3.14 checks.
- Before the maintenance cutover, require exact active interpreters or checked-in wrappers for every tracked node-22 automatic/operator entrypoint.
- Keep QHH diagnostics in detached worktrees and make backend-smoke direct Python use that checkout's exact interpreter.
- Route e2e/grib validation to node-27's existing Python 3.11 environment using fail-fast `uv run --no-sync`; preserve pytest status through `tee`.
- Classify the QHH bring-up document as a governed historical baseline and make production-topology audit consume complete markers without allowing current authorities to self-exempt.
- Lock node-22/QHH/Docker/runbook command inventories and shell status behavior with static mutation tests.

## Capabilities

### New Capabilities

- `python-environment-truth`: Repository and operational Python decisions use explicit, environment-safe authorities.

### Modified Capabilities

None.

## Impact

Affected surfaces are Python defaults/instructions, node-22 units and current
runbooks, node-27 test routing, QHH diagnostic entrypoints, the primary Docker
runbook, production-topology marker classification, and their tests. No CI
pip-to-uv migration, database/display behavior, Slurm scheduling, SHUD science,
or active node-22 cutover is performed.
