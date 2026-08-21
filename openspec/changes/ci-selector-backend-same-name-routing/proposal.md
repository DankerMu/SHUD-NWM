## Why

Issue #1587 exposes a standing targeted-CI blind spot: a changed backend Python source outside `scripts/` can have a tracked same-name suite and still select only unrelated core-smoke tests. On the current master tree, the scripts-only derivation leaves 15 source/suite pairs without their same-name suite, including 13 paths that fall back to core smoke alone.

## What Changes

- Generalize the existing basename-derived source-to-suite mapping from `scripts/**/*.py` to every backend Python prefix already recognized by the selector: `apps/api/`, `packages/`, `services/`, `workers/`, and `scripts/`.
- Keep existence checking, explicit-rule union, unknown-backend fallback, changed-test routing, and node-id handling unchanged.
- Replace the scripts-only tree guard with a tracked-tree guard over all five prefixes, and explicitly rewrite the former scripts-only scope assertion.
- Pin the current cross-prefix same-stem collision so a shared same-name suite must import every colliding source module instead of silently becoming an unrelated basename match.
- Record collected-test and wall-clock cost for the newly reached suites; do not change CI timeout or workflow topology.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `ci-contract-baseline`: expand the existing same-name targeted-test routing contract from `scripts/**/*.py` to all recognized backend Python prefixes.

## Impact

- Code: `scripts/select_ci_tests.py`.
- Tests/meta-guards: `tests/test_select_ci_tests.py`.
- Contract: `openspec/specs/ci-contract-baseline/spec.md` after archive.
- No production module, test assertion body, database, frontend, Slurm, or CI workflow file changes.
