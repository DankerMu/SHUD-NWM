## Context

`timescaledb_210` is registered as a real-oracle marker whose only valid runtime
is node-27's disposable PostgreSQL 15.2 / TimescaleDB 2.10.2 cluster. It is
intentionally not a global pytest auto-skip marker: targeted PR tests must still
run unmarked/unit assertions in the owning suites, and node-27 selects the
marker explicitly. Before this change, the generic `real-db-integration` job
ran the whole `integration` marker set against `pg15-latest`; its closed command
now selects `integration and not timescaledb_210`. A Docker surface formerly let
the #1892 probe test run, then `inspect_live_image("nhms-db")` failed before
`docker run`; the probe now reports that primary status/error before successful
cleanup assertions.

Fixture level: expanded. Repair intensity: high. Project profile: NHMS.

## Goals / Non-Goals

**Goals:**

- Keep ordinary real-DB integration coverage in GitHub CI while excluding only
  the node-27-only `timescaledb_210` subset.
- Preserve the existing node-27 explicit oracle and cleanup ownership/PASS
  evidence.
- Make pre-create probe failure surface as status/error evidence, not a misleading
  cleanup assertion.
- Mechanically prevent marker-expression and selector-contract drift.

**Non-Goals:**

- No global auto-skip for `timescaledb_210`, no generic Docker-to-node27
  inference, no live `nhms-db` mutation, no probe-engine or cleanup-owner change,
  and no patch to PR #1901 or #1907 source branches.

## Decisions

### D1 — Route by marker expression, not Docker presence

The generic SQL job runs `-m "integration and not timescaledb_210"`. Node-27
continues to run `-m timescaledb_210` with the existing explicit integration
environment and frozen disposable-cluster isolation. This uses the marker's
existing meaning and automatically covers future or sibling probe suites such as
PR #1907. A new overlapping marker/env flag was rejected as duplicate state that
could drift from `timescaledb_210`.

### D2 — Preserve selector visibility

`timescaledb_210` remains in `NEVER_AUTO_SKIPPED_MARKERS` and out of
`GATING_MARKER_NAMES`. The PR targeted unit lane does not use the SQL job marker
expression and must keep selecting owning suites. Workflow contract tests record
the precise SQL expression; AST-derived selector guards continue to enforce the
existing conftest auto-skip set.

### D3 — Do not falsify cleanup evidence

`cleanup_owned` keeps `container_absent=false` when this run never created the
container and performs no inspect/removal. Name conflicts and pre-create failures
therefore cannot be laundered into successful cleanup. The integration test first
asserts/diagnoses the probe status and original error; cleanup proof is required
only after status is `passed`, matching `parse_probe_report` and `_all_passed`.

### D4 — CI is the generic-lane oracle; node-27 is the engine oracle

Local tests parse the workflow and prove marker truth tables. GitHub CI proves the
generic job executes ordinary integration rows without collecting
`timescaledb_210`. Node-27 executes the existing marker and records
`created_container=true`, `container_removed=true`, `container_absent=true`,
`work_root_absent=true`, and `identity_bound=true`. No connection to production
business data is required.

## Risk Packs

Core packs considered:

- Public API / CLI / script entry: selected — pytest marker expressions and the
  probe test are executable control surfaces.
- Config / project setup: selected — CI workflow and marker routing change.
- File IO / path safety / overwrite: selected — cleanup proof/path ownership must
  remain unchanged and no unowned resource may be touched.
- Schema / columns / units / field names: selected — cleanup/status/error evidence
  semantics are assertions consumed by operators/tests.
- Auth / permissions / secrets: not selected — no credential boundary changes;
  fixture commands use placeholders and existing env redaction.
- Concurrency / shared state / ordering: selected — generic CI and node-27 lanes
  must be mutually unambiguous; no duplicate probe may run accidentally.
- Resource limits / large input / discovery: not selected — no discovery or input
  bound changes.
- Legacy compatibility / examples: selected — existing ordinary integration,
  targeted selection, node-27 command, and PR #1907 sibling remain compatible.
- Error handling / rollback / partial outputs: selected — setup failure must retain
  its primary error and cleanup must remain ownership-bound.
- Release / packaging / dependency compatibility: not selected — no dependency or
  packaging change.
- Documentation / migration notes: selected — runbook must state the two lanes.

Domain packs considered:

- PostGIS / TimescaleDB domain behavior: selected — only node-27 2.10.2 is a valid
  engine oracle.
- Other NHMS domain packs: not selected — no geospatial, forcing, SHUD, Slurm,
  provider, manifest/QC, or published-artifact behavior changes.

## Invariant Matrix

- Governing invariant: generic CI never executes node-27-only disposable-cluster
  tests, while node-27 explicit execution and cleanup PASS evidence remain intact.
- Source of truth: pytest `integration` / `timescaledb_210` markers, the SQL job
  marker expression, and `OwnedResources.created_*` cleanup ownership.
- Producers: `.github/workflows/ci.yml`; node-27 explicit pytest command; probe
  report writer.
- Validators/preflight: pytest marker selection, workflow contract parser,
  selector AST guard, live-identity refusal.
- Storage/cache/query: disposable container/work root only; no business DB.
- Public routes/entrypoints: GitHub `SQL Migration Dry Run`, pytest marker CLI,
  probe integration test.
- Frontend/downstream consumers: PR merge gates and operator receipts; no frontend.
- Failure paths/rollback/stale state: absent `nhms-db`, inspect failure, name
  conflict, pre-create failure, created-resource cleanup, stale marker expression.
- Evidence/audit/readiness: workflow contract tests, focused pytest, GitHub CI,
  node-27 receipt.

Regression rows:

- Generic CI + ordinary integration item -> item executes.
- Generic CI + `integration` + `timescaledb_210` item -> item is deselected.
- Targeted PR lane + owning suite -> unmarked/unit assertions remain selected;
  `timescaledb_210` is still not a conftest auto-skip marker.
- Node-27 explicit `-m timescaledb_210` -> disposable probe executes and cleans up.
- Pre-create inspect failure -> status/error identifies `ProbeError`; cleanup stays
  `created_container=false` / `container_absent=false` with zero remove calls.
- Successful run -> PASS still requires all cleanup proof fields true.

Boundary checklist: workflow command; marker registration/expression; selector
AST anchor; probe status/error assertion; cleanup owner and PASS consumers; node-27
receipt command; PR #1907 sibling marker shape; unchanged ordinary integration.

## Risks / Trade-offs

- Generic CI no longer executes engine-specific marker rows → this is already the
  marker contract; node-27 remains mandatory and explicit.
- A broad `-m timescaledb_210` command can collect future unrelated rows → marker
  is intentionally the capability boundary; every such row declares node-27-only.
- Workflow string drift could silently reintroduce the bug → structured owning
  tests assert exact semantic marker inclusion/exclusion, not a loose substring.

## Migration Plan

1. Add red workflow/routing/failure-evidence tests, then implement the narrow
   expression/documentation/assertion-order change.
2. Run focused tests, selector meta-guards, strict OpenSpec, ruff and full local
   regression.
3. Merge #1914 after required CI is green; rerun PR #1907 and PR #1901.
4. On node-27, run the explicit marker once from frozen SHA and record cleanup
   PASS without changing live `nhms-db`.

## Open Questions

None.
