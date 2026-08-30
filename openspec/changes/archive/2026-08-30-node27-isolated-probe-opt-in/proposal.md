## Why

The generic GitHub real-database job runs on a Docker-capable host but is not the
node-27 PostgreSQL 15.2 / TimescaleDB 2.10.2 oracle. Since #1892, that Docker
surface accidentally opts the isolated-cluster probe into every database-scoped
PR, so an absent live `nhms-db` makes the required check deterministically fail
before the disposable cluster is created.

## What Changes

- Exclude the node-27-only `timescaledb_210` marker from the generic
  `SQL Migration Dry Run` marker expression while retaining ordinary integration
  coverage and existing targeted-test selection.
- Keep node-27 as the explicit `-m timescaledb_210` oracle and document the exact
  command; Docker socket/container presence is never authorization by itself.
- Make the probe integration test report the probe status/error before asserting
  successful cleanup, without weakening ownership-bound cleanup or PASS proof.
- Add workflow, selector, routing, and failure-evidence regressions, including the
  still-open PR #1907 sibling shape.

## Capabilities

### New Capabilities

- `node27-isolated-probe-routing`: explicit routing and truthful failure evidence
  for disposable TimescaleDB 2.10.2 cluster probes.

### Modified Capabilities

- `ci-contract-baseline`: the generic real-database job keeps ordinary
  integration coverage but excludes the node-27-only marker by contract.

## Impact

Affected surfaces are `.github/workflows/ci.yml`, the workflow contract tests,
the isolated probe integration test, selector marker invariants, and the CI
routing runbook. No production database, probe engine, cleanup mutation,
TimescaleDB migration, public API, or PR #1901 Slurm behavior changes.
