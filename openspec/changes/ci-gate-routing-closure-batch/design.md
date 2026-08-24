## Context

Fixture level: expanded. Repair intensity: broad-expanded. Project profile: NHMS.

This batch changes the shared CI selector entrypoint and the workflow's only
unattended PostgreSQL/Timescale integration trigger. The defects share one
invariant: changing an oracle-owned source must schedule that oracle without
silently deleting existing baseline coverage. #1597 is a merged predecessor and
is treated only as an unchanged compatibility row.

## Goals / Non-Goals

**Goals:**

- Close #1711, #1672, #1656, #1688, and #1744 in one coherent selector/workflow
  change; retain and close the already-delivered #1597 trace.
- Derive variable source/importer sets from the tracked tree where practical.
- Prove every new test bites with a pre-change red run and keep lane cost bounded.

**Non-Goals:**

- No production logic or test-oracle assertion changes.
- No full `packages/common` 210-pair disposition campaign (the rejected #1744
  path A), no unbounded transitive importer closure, and no suite-to-suite graph.
- No DSN model change, new scheduled workflow, production deployment, or remote
  oracle requirement.

## Decisions

### D1: Shared-library narrow rules are additive to core smoke

For any backend Python path under `packages/common/**`, the selector keeps the
core-smoke baseline even when an explicit narrow rule or same-name rule matches.
This is #1744 path B: it fixes the surprising replacement semantics for every
shared module with one local rule rather than classifying 210 importer pairs.
Other backend prefixes keep today's known-rule suppression semantics. A
constructed rule-list test and real `state_manager.py`/`state_cli.py` examples
must prove that a narrow target cannot remove `test_production_scheduler.py`.

Alternative rejected: add `packages/common` to `DIRECTORY_RULE_AUDIT_PATHS` and
manually disposition 210 pairs. It is larger, reason-token heavy, and leaves the
underlying fallback-shadowing behavior intact.

### D2: Tree-wide invariant routing is supplemental and monotonic

Introduce a small tuple of `(source-root glob, invariant suite)` supplemental
rules applied independently of ordinary `PATH_TEST_RULES`. The four roots are
`workers/**`, `packages/common/**`, `scripts/**`, and `db/**`; each maps to
`tests/test_timescale_write_guard_wire_site_invariant.py`. Supplemental rules do
not set `matched`, do not participate in stop rules, and only add tests. This
covers future files without changing ordinary ownership or fallback behavior.
The selector meta-suite derives these roots from the invariant test's `_scan_roots`
AST rather than maintaining a second frozen root list.

### D3: Source closures are derived at the highest stable seam

- `apps/api/routes/hydro_display.py` joins `GUARDED_MODULE_CLOSURES`; its direct
  union one-hop non-gated importer set is the authority for its explicit rule.
- `workers/mapping_builder/**` gets one directory rule to all eight existing
  `tests/test_mapping_builder_*.py` suites, and joins
  `DIRECTORY_RULE_AUDIT_PATHS` so future module/importer growth is dispositioned.
  The three state-clone suites that import `rewrite.py` remain owned by their
  state-clone surfaces and are recorded as live `edge-consumer` pairs; they do
  not contaminate the mapping-builder rule, preserving #1711's explicit
  no-`tests/test_state_clone.py` boundary.
- `packages/common/state_clone_hook.py` maps to
  `tests/test_state_clone_cutover_hook.py`.
- `scripts/node22_clone_direct_grid_cutover_states.py` maps to the two
  recalibration suites.

The mapping-builder target set is mechanically derived from tracked
`tests/test_mapping_builder_*.py` files in the meta-suite. The two irregular
file mappings remain explicit because their suite names are intentionally not
same-name derivable.

### D4: Integration source triggers are a finite registry, then mechanically guarded

The finite registry below is the authority for this change. Every #1688 affected
surface is covered; none is silently deferred:

| Integration-owned production surface | `database` filter pattern |
|---|---|
| `packages/common/forecast_store.py` | exact path |
| `packages/common/display_coverage.py` | exact path |
| `services/tiles/mvt.py` | exact path |
| `apps/api/routes/hydro_display.py` | exact path |
| `apps/api/main.py` | exact path |
| `scripts/node27_autopipeline.py` | exact path |
| `workers/output_parser/parser.py` | exact path |
| `packages/common/timescale_write_guard.py` | exact path |
| `packages/common/object_store.py` | exact path |
| `packages/common/model_registry.py` | exact path |
| `packages/common/grid_registry_store.py` | exact path |
| `workers/grid_registry/**` | bounded package glob |
| `workers/model_registry/**` | bounded package glob |
| `workers/forcing_producer/**` | bounded package glob |
| `services/orchestrator/scheduler.py` | exact path |

`.github/workflows/ci.yml` itself also joins the `database` filter. A PR changing
the real-DB gate must execute that gate, rather than rely on a later unrelated
production-source diff. The selector meta-suite parses the `database:` block,
expands the three bounded globs over tracked files, and proves every registered
path matches at least one pattern. Removing the `forecast_store.py` pattern from
a constructed workflow copy must fail and name that source. The existing
workflow self-routing rule selects `tests/test_select_ci_tests.py`, so filter
edits execute this contract on their own PR.

The filter does not select individual integration tests; it opens the existing
`real-db-integration` job, which runs all `-m integration` tests with its service
container and DSN. Its pytest invocation gains `-vv -rs` only, so the run log
names each node and skip reason. This produces auditable proof that the seven
residual-debt tests passed rather than skipped without changing markers, DSN,
service topology, or test selection.

### D5: #1597 is a compatibility anchor, not implementation scope

The MVT rule and `GUARDED_MODULE_CLOSURES` entry delivered by PR #1670 remain
byte-for-byte unchanged unless another required change mechanically forces a
comment/count update. Existing MVT exact-set and closure tests are part of the
regression matrix. The PR body records #1597 as already delivered and closed by
traceability, not newly implemented.

### D6: Collection-smoke provenance is independent of final target shape

Round-1 review confirmed that the `scripts/**` supplemental invariant target
made a selector-source-only diff cease to be `meta_guard_only`, silently
removing the canonical full-tree collection smoke. Keep `meta_guard_only` as its
existing final-list shape property; do not overload or weaken it. Add a separate
`collection_smoke_required` GitHub output that is true when either:

- the final selection is exactly the selector meta-guard; or
- the changed-file set contains `scripts/select_ci_tests.py` or
  `tests/test_select_ci_tests.py`, even when supplemental targets make the final
  selection non-collapsed.

The workflow consumes this provenance field for the full-tree collect-only
branch. Zero-selection behavior stays separate and unchanged. This preserves
both #1656's Timescale invariant and the canonical selector-development oracle.

### D7: One positive helper owns each standing mutation contract

Round-1 verification confirmed four tests described a mutant's bad output while
remaining green instead of proving that the live contract rejected the mutant.
For supplemental roots, shared baseline, mapping-builder targets, and the
`database` filter registry, extract one positive violation helper per invariant:
live state returns no violations; the constructed mutant is passed through that
same helper and must return a violation naming the missing root/target/source.
Expected values remain independent from the monkeypatched production authority.

The real-DB job receives the same treatment. A structured job-scoped contract
helper must bind job-level `needs`, the exact dispatch/database/push/non-draft
condition, the Timescale service image, job-level opt-in, the dedicated
`NHMS_INTEGRATION_DATABASE_URL` consumed by `tests/conftest.py`, and the named
`pytest -vv -rs -m integration` step. Its finite load-bearing branch inventory
must carry standing same-helper mutants for needs/gate, service image, opt-in
scope and lexical truth, dedicated-DSN existence/scope/nonempty value, named-step
identity, and command identity. The collection-smoke consumer likewise binds its
condition, targeted-before-collection ordering, scoped collect command, truthful
label, and nonzero collection-failure exit, with a standing mutant for each
load-bearing predicate. The canonical inventory lives in
`.workplans/pr-1834/review/round-2-branch-completeness-inventory.md`; evidence
claims may not exceed the standing mutants in that inventory.

## Risk Packs Considered

Core:

- Public API / CLI / script entry: selected — selector is CI's public routing entry.
- Config / project setup: selected — `ci.yml` database filter changes.
- File IO / path safety / overwrite: not selected — tracked-tree reads only; no writes/publish.
- Schema / columns / units / field names: not selected — no data schema change.
- Auth / permissions / secrets: not selected — no credentials or role boundaries change.
- Concurrency / shared state / ordering: selected — additive rule order and stop-rule interaction are load-bearing.
- Resource limits / large input / discovery: selected — derived tree scans and lane wall time must remain bounded.
- Legacy compatibility / examples: selected — all existing selector mappings and fallbacks are userspace.
- Error handling / rollback / partial outputs: selected — unknown-path fallback and missing-target behavior must remain intact.
- Release / packaging / dependency compatibility: not selected — no dependencies or packaging.
- Documentation / migration notes: selected — PR traceability and stale #1597 closure must be explicit.

NHMS domain packs:

- PostGIS / TimescaleDB domain behavior: selected — the structural write guard and parity oracle protect Timescale/PostgreSQL behavior, though implementation is local CI routing.
- Published NHMS artifacts / display identity: selected — hydro-display/MVT importer oracles are routed, without changing runtime behavior.
- Geospatial/CRS, forcing windows, SHUD numerical runtime, Slurm lifecycle,
  provider snapshots, run-manifest/QC provenance: not selected — no runtime code in those domains changes.

## Invariant Matrix

- Governing invariant: every changed source must schedule all requirement-owned
  local or real-DB oracles, and adding a specific mapping must never remove the
  shared baseline that previously covered the source.
- Source of truth: tracked source/test import graph, invariant test `_scan_roots`,
  selector rule tables, and `ci.yml` `database:` patterns.
- Producers: `scripts/select_ci_tests.py::select_tests` and dorny paths-filter.
- Validators/preflight: selector meta-tests derive importer/root/filter closure.
- Storage/cache/query: Git tracked tree and workflow changed-file JSON only; no DB state mutation.
- Public entrypoints: selector CLI and GitHub Actions `changes`/`real-db-integration` jobs.
- Downstream consumers: targeted pytest command, PostgreSQL integration pytest job, existing MVT/hydro/mapping/state suites.
- Failure paths: unknown backend fallback, stop rules, missing test targets, integration-marked suites without DSN.
- Evidence/audit: red proofs, focused/full selector suite, selected consumer suites,
  ruff, strict OpenSpec validation, PR CI run and post-merge master receipt for #1688.
- Regression rows:
  - Each named source/root -> its required local suite(s), plus shared baseline for `packages/common/**`.
  - A future source under one of four invariant roots -> write-site invariant suite.
  - A hydro-display importer added without a rule -> guarded closure fails.
  - `state_manager.py` narrow rule -> scheduler baseline remains selected and injected lineage regression routes to a red suite.
  - `forecast_store.py`-only diff -> `database=true` and real-DB job executes the seven residual-debt integration tests.
  - Unchanged `services/tiles/mvt.py` -> existing exact selection and guarded closure stay green.

## Boundary-Surface Checklist

- Shared helper roots: `scripts/select_ci_tests.py`, `packages/common/**` matching policy.
- Public entrypoints: selector CLI, workflow filters/jobs.
- Read surfaces: tracked test/source tree and YAML filter text.
- Write/delete/overwrite and publish/rollback: none.
- Producer/consumer evidence: source path -> selected suite; database filter -> integration job.
- Stale-state/idempotency: repeated selection is set-union deterministic; no cache or durable state.
- Unchanged consumers: all pre-existing selector tests, MVT closure, stop rules,
  unknown-backend fallback, frontend/docs filters.

## Risks / Trade-offs

- [Every `packages/common/**` PR gains core smoke] -> measure representative
  explicit-rule lanes; baseline is five already-accepted suites and shared-library
  changes merit the cost.
- [Tree-scanning invariant suite added broadly] -> one ~2-second suite per affected
  backend PR, deduplicated by set union; bound roots explicitly.
- [Database lane triggers more often] -> use integration-owned source globs only;
  record CI wall time and retain the existing all-integration job rather than new infrastructure.
- [YAML pattern parser can drift] -> reuse/extend existing AST/YAML contract helpers
  and include red mutation arms; fail loud rather than silently return an empty set.

## Migration / Rollback

No data migration. Rollback is one revert of selector rules/tests/workflow filter.
Because behavior only strengthens pre-merge gates, rollback cannot corrupt runtime
state; it reopens known blind spots and therefore requires an explicit follow-up.

## Open Questions

None. #1744 path B is selected; the user requested autonomous delivery and the
repository evidence favors the smaller global invariant over a 210-entry campaign.
