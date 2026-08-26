## Why

The PR-targeted selector and the real-database paths filter still omit five known
source-to-oracle edges (#1711, #1672, #1656, #1688, #1744). A source-only PR can
therefore merge with unrelated or zero relevant assertions and discover the
regression only after merge—or, for integration-only parity, never run the
oracle at all. Issue #1597 is already implemented by merged PR #1670; this batch
keeps its guarded MVT closure as a must-preserve baseline and closes that stale
issue without duplicating its implementation.

## What Changes

- Make explicit `packages/common/**` rules additive with the existing core-smoke
  baseline so a narrow shared-library rule cannot silently remove scheduler/API
  coverage (#1744, path B).
- Route all four source roots scanned by the Timescale write-site invariant to
  its structural guard suite, including future files under those roots (#1656).
- Add derived closure routing for `apps/api/routes/hydro_display.py`, all
  `workers/mapping_builder/**` modules, the state-clone hook, and the node-22
  clone script; install meta-guards so future importer/module growth fails
  visibly (#1672, #1711).
- Add the production modules guarded by real-database integration suites to the
  `database` paths filter, including `packages/common/forecast_store.py`; add a
  mechanical workflow contract guard so future integration consumers cannot be
  added without a trigger decision (#1688).
- Preserve the already-merged `services/tiles/mvt.py` importer closure from
  #1597 and close #1597 through this PR's traceability, with no new MVT routing
  behavior.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `ci-contract-baseline`: targeted selection preserves the shared-library
  fallback baseline, tree-scanning invariants and derived source closures route
  to their assertion suites, and real-database integration consumers trigger
  the database lane.

## Impact

- `scripts/select_ci_tests.py`
- `tests/test_select_ci_tests.py`
- `.github/workflows/ci.yml`
- No production runtime, schema, database migration, frontend, Slurm, or node-22/
  node-27 deployment behavior changes.
