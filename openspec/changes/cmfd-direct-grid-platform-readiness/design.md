## Context

The SHUD-NWM repository already implements the runtime-side direct-grid capabilities: the contract parser, exact `grid_cell_id` lookup, one-cell `weight=1.0` mapping, standard multi-station SHUD packaging, runtime `.sp.att FORC` range validation, and the fail-closed prohibition on IDW fallback (source-of-truth §1, §4/P0.2). The live deployment (appendix A, node-27 audit 2026-07-06) has 13 basins, 6,290 legacy IDW stations, and `met.forcing_station_timeseries` accumulating ~121M rows per two weeks (~8M rows/day). None of the 13 model instances currently carries a direct-grid contract; the mapping builder does not yet exist in code.

Platform Readiness Gate P0 (source-of-truth §4) is the mandatory precondition to migrating any real basin. It has three parts: P0.1 version pinning, P0.2 implementation evidence on the pinned release, and P0.3 solver forcing-consumer audit. This change delivers exactly those three, as immutable evidence and pinning artifacts, with minimal or no production-code change. It explicitly does not build the grid registry (`canonical-source-grid-registry`), does not build the mapping builder (`forcing-mapping-asset-build`), does not touch scheduler routing, state manager, or display, and performs no basin migration.

The central constraint from source-of-truth §4/P0.2 is that OpenSpec task state and code state have drifted, so readiness must be judged on a frozen commit plus test/smoke/audit evidence — not on checkbox completion.

## Goals / Non-Goals

Goals:
- Produce one immutable, checksum-bound readiness manifest pinning all P0.1 identities, including the SHUD solver submodule commit recorded separately from the outer SHUD-OpenMP repo.
- Re-run the direct-grid test suites plus a real-backend smoke and a production-binary minimal-basin execution on the pinned release, and produce a G9 capacity baseline against deployment config and live facts.
- Produce a solver forcing-consumer audit report ending in an explicit `z_policy` verdict.
- Bind every evidence artifact to the manifest checksum so readiness is provable against a single baseline.

Non-Goals:
- No grid registry, no mapping builder, no rewriting of any production basin's `.sp.att`, no scheduler/state/display change (deferred to changes 2–8). A hand-assembled synthetic direct-grid evidence fixture (contract + minimal package, see "Synthetic direct-grid evidence assets") is in scope as an evidence asset: it is not produced by a mapping builder and it touches no production basin, package, or model instance.
- No migration of any of the 13 basins.
- No new production feature; if a code change is unavoidable to pin an identity or to carry the evidence smoke (env-gated test/script), it is minimal and does not alter forcing behavior.

## Decisions

### Where the readiness manifest lives and its schema
The readiness manifest is a committed evidence file under the change's evidence package (`openspec/changes/cmfd-direct-grid-platform-readiness/evidence/readiness-manifest.<version>.json`), with a companion `.sha256`. It is a flat, versioned JSON document with these pinned fields (P0.1 table):

- `manifest_version` (must match the committed versioned filename), `created_utc`, `baseline_commit` (SHUD-NWM commit/tag)
- `forcing_producer_version`
- `canonical_converter_versions`: `{ gfs: "m1.4", ifs: "m4.1", era5: "m2.0" }` (read from `workers/canonical_converter/converter.py`)
- `shud_runtime_commit`, `shud_executable: "shud_omp"`
- `shud_openmp_outer_commit` AND `shud_solver_submodule_commit` (recorded as two distinct fields — the outer repo references the solver via git submodule, so the outer commit alone is insufficient; source-of-truth §4/P0.1). Both commits are resolved on the deployment/build host that owns the production solver checkout (`git rev-parse HEAD` on the outer repo, `git submodule status` for the solver pin), never assumed from a remote default branch.
- `db_schema_migration_repo_head` (highest migration file in `db/migrations/`; currently `000042`) AND `db_schema_migration_version` (the deployment-applied migration version resolved by live query on node-27's active primary PG). These are distinct facts — repo-available is not deployment-applied; a mismatch between them leaves the schema identity unresolved and blocks the baseline.
- `proj_crs_database_version` (derived on the deployment host from the installed PROJ: the PROJ release string plus the `proj.db` layout/build metadata, e.g. `uv run python -c "import pyproj; print(pyproj.proj_version_str)"` and a `proj.db` metadata query — never copied from documentation)
- `mapping_builder_algorithm_version: "nearest_cell_barycenter_geodesic_v1"` (declared authority: source-of-truth §6.1 — the algorithm identifier is spec-declared there because the mapping builder has no in-repo implementation yet; `source_locations` records that doc section as the authority until the `forcing-mapping-asset-build` change lands an in-repo source)
- `forcing_producer_limits` (10,000 stations / 10,000 timesteps / 10,000,000 timeseries rows / ~32 MiB manifest, resolved from `workers/forcing_producer/producer.py` including any deployment env overrides in effect)
- `shud_runtime_staging_limits` (the seven direct-grid `MAX_DIRECT_GRID_*` byte/line limits defined in `workers/shud_runtime/runtime.py`: `MAX_DIRECT_GRID_TSD_FORC_BYTES`, `MAX_DIRECT_GRID_FORCING_CSV_BYTES`, `MAX_DIRECT_GRID_SP_ATT_BYTES`, `MAX_DIRECT_GRID_TSD_FORC_LINES`, `MAX_DIRECT_GRID_FORCING_CSV_LINES`, `MAX_DIRECT_GRID_SP_ATT_LINES`, `MAX_DIRECT_GRID_STAGING_LINE_BYTES`; explicitly excludes `MAX_PACKAGE_MANIFEST_BYTES` which is a non-direct-grid best-effort PRCP-manifest cap)
- `source_locations`: per-identity pointer to the authoritative source (file, command, or declared doc section) so a reviewer can re-derive each value.

Rationale: a single committed JSON keeps the pin diffable, reviewable, and versioned in git alongside the spec. The `.sha256` companion binds evidence to an exact baseline. Alternative considered: storing the pin in the object store — rejected because the pin must travel with the spec and be reviewable in PR, and the object store is a runtime cache, not a spec source (INV-3 style: manifest is authority).

### How evidence is bound to commits (checksums)
Every evidence artifact (test run log, node-27 smoke record, minimal-basin execution record, G9 capacity report, solver audit report) records: (1) the `baseline_commit` it ran against, and (2) the SHA-256 checksum of the readiness manifest it was validated against. A reviewer verifies readiness by confirming all evidence references the same manifest checksum and the same `baseline_commit`. This is the mechanism that makes readiness provable on a frozen baseline rather than on mutable checkbox state.

### Synthetic direct-grid evidence assets (contract, package, smoke carrier)
None of the 13 live model instances carries a direct-grid contract, keliya's live package is legacy IDW, and the mapping builder is out of scope — so the node-27 smoke and the node-22 minimal-basin execution cannot use any live asset. Both use a **hand-assembled synthetic minimal direct-grid evidence fixture**, scoped as an allowed evidence asset (not a mapping-builder product, not a production migration):

- **Package**: a synthetic minimal direct-grid package whose structure mirrors the `tests/test_direct_grid_e2e.py` fixture package — rewritten-`FORC` `.sp.att`, a §7.2/§7.3-conformant binding manifest, a standard multi-station `shud/qhh.tsd.forc`-style `.tsd.forc`, and per-station CSVs. All FORC/binding values are hand-derived and documented; construction provenance and SHA-256 checksums are recorded in the evidence.
- **Contract registration (node-27)**: the synthetic contract is registered as a dedicated evidence-only `core.model_instance` row carrying `resource_profile.direct_grid_forcing` (INV-3/§7.1), under a dedicated non-production `basin_version_id`/`model_id`. The 13 production model instances are untouched.
- **Isolation and cleanup (node-27)**: smoke-derived rows stay confined to the dedicated non-production identity; any `met.met_station` mirror rows are written with `active_flag=false` so the station-MVT layer cannot mix old/new stations (source-of-truth §10 mixed-display hazard). After evidence capture the derived/mirror rows are removed, or verifiably remain confined to the inactive dedicated identity; a display spot-check confirms production display is unaffected.
- **Smoke carrier**: the smoke is carried by a new env-gated test or script committed in this change (marker style of `real_disk`/`integration`, cf. `tests/test_object_store_forcing_real_disk.py` / `tests/test_real_database_integration.py`, or a dedicated `scripts/` smoke) — the repo has no pre-existing real-backend direct-grid smoke, so "existing fixtures/assets only" is replaced by this explicit supply constraint.
- **Minimal-basin execution (node-22)**: stages and runs this same synthetic multi-station package with the production `shud_omp`, exercising standard multi-station **direct-grid** staging with the production binary. keliya reuse is rejected: its existing package carries no direct-grid contract and producing one would require the out-of-scope mapping builder / production FORC rewrite.

### node-27 vs node-22 execution split for evidence runs
Per CLAUDE.md verification-oracle routing (后端单测/集成 pytest → node-27; local is limited to ruff/openspec/frontend checks) and source-of-truth §4/P0.2 (re-run evidence must be produced on the actually deployed release):
- **node-27 (real DB / oracle / display)** runs: the pinned-commit re-runs of the direct-grid and DB-migration pytest suites (tasks 2.1/2.2), the real-object-store + real-DB smoke, the G9 capacity baseline (needs live `met.forcing_station_timeseries` row counts), and any real-DB pytest. node-27 is the only node with the active primary PG and object store, and is the deployment host the re-run evidence must come from.
- **node-22 (Slurm / SHUD runtime behavior)** runs: the minimal-basin execution with the production SHUD binary (`shud_omp`), because Slurm/SHUD runtime behavior is the node-22 oracle. Its evidence records the pinned solver submodule commit and the binary's build provenance.
- **Local (macOS)** runs: `openspec validate` and `ruff` only. No readiness pytest evidence is produced locally — a local pass is not deployed-release evidence.

The solver forcing-consumer audit is a static/source-level audit of the pinned solver commit; it can be prepared locally but its verdict must reference the exact pinned submodule commit that node-22 executes, so the two stay consistent.

### Why checkbox state is not evidence
Source-of-truth §4/P0.2 records residual drift between OpenSpec task state and code implementation state. A checked box means "a task was marked done," not "the pinned release passes." Certifying readiness on checkboxes would let stale or divergent task state silently certify an unready baseline. Therefore the `direct-grid-readiness-evidence` capability requires the pinned manifest, passing re-run evidence, node-27 smoke, minimal-basin execution, the G9 capacity baseline with no unresolved limit breach, and the audit verdict, and requires any observed drift to be recorded rather than assumed absent.

## Risks / Trade-offs

| Risk | Mitigation |
| --- | --- |
| Solver submodule commit omitted, pinning only the outer SHUD-OpenMP repo → the actual numeric solver is unidentified | Manifest schema makes `shud_solver_submodule_commit` a required, distinct field; missing it blocks readiness (spec: separate-recording scenario). |
| `z_policy` set to `sentinel` without proving Z unused, then a later solver path reads Z → silent elevation error | Audit must prove Z numerically unused before approving `sentinel`; otherwise an explicit elevation source is mandated (spec: sentinel-rejected scenario). `Z=-9999` in baselines is consistent with, but not proof of, sentinel. |
| Evidence produced on a moving baseline (commits landing during the run) | Every artifact records `baseline_commit` + manifest checksum; mismatched references invalidate the evidence set. |
| Capacity check reported as formula only, not against live config | G9 spec requires recording the deployment configuration values actually used and the live legacy comparison (13 basins / 6,290 stations / ~121M rows per 2 weeks). |
| Readiness certified on checkbox state despite code drift | Readiness capability forbids checkbox-only certification and requires drift to be recorded. |
| A pinning code change unintentionally alters forcing behavior | Scope constraint: pinning changes are minimal and must not change forcing behavior; evidence re-run would catch a behavior regression. |
| `shud_omp` on node-22 was not built from the pinned solver submodule commit → the z_policy audit verdict applies to a different binary than production runs | Evidence must record build provenance linking the deployed binary to `shud_solver_submodule_commit` (build-tree submodule HEAD, build log, or checksum comparison against a rebuild from the pin — at least one); the audit report cites this provenance. |
| Synthetic smoke rows leak into production display (station-MVT mixed display, source-of-truth §10) | Smoke writes are confined to a dedicated non-production identity with `active_flag=false` mirrors and cleaned up after evidence capture; a display spot-check is part of the smoke evidence. |

## Migration Plan

This change deploys no runtime behavior. Rollout:
1. Derive and commit the readiness manifest + `.sha256` from authoritative sources, and pass the manifest completeness check.
2. Provision the synthetic direct-grid evidence assets and smoke carrier; run `openspec validate`/`ruff` locally; run the pinned-commit pytest suite re-runs, smoke, and capacity evidence on node-27; run the node-22 minimal-basin production-binary execution.
3. Produce the solver audit report and `z_policy` verdict against the pinned submodule commit and its build provenance.
4. Assemble the evidence package with all artifacts bound to the same manifest checksum and `baseline_commit`; any mismatch invalidates the set.

Rollback: because no production code path changes, rollback is simply not certifying readiness; the pinned manifest and any partial evidence remain committed for audit. Basin migration (changes 2–8) does not begin until this evidence package certifies readiness.

## Risk Packs Considered

Core packs (`references/issue-risk-contract.md` §Risk Packs):

- Public API / CLI / script entry: **not selected** — this change adds no runtime routes/CLI/scheduler entrypoints; only diff-only evidence artifacts.
- Config / project setup: **not selected** — pinning captures existing env overrides (`FORCING_MAX_*`) but does not add config; §1.1 records values in effect, no new keys.
- File IO / path safety / overwrite: **selected** — evidence file writes under `openspec/changes/cmfd-.../evidence/` (append-only per version), synthetic-package construction on node-27 disk, `active_flag=false` DB mirrors, post-capture cleanup semantics (2.3/2.4).
- Schema / columns / units / field names: **selected** — `db_schema_migration_repo_head` vs `db_schema_migration_version` identity check (1.1/2.4), INV-3 `resource_profile.direct_grid_forcing` binding (2.3), the seven `MAX_DIRECT_GRID_*` staging constants pinned separately from `MAX_PACKAGE_MANIFEST_BYTES` (units/field boundary), producer 10k/10k/10M/32 MiB limit fields.
- Auth / permissions / secrets: **not selected** — no auth surface touched; node-27 PG queries are read-only against active primary; SSH access is documented in `CLAUDE.md`, no new credential handling.
- Concurrency / shared state / ordering: **not selected** — no runtime routing, no state cutover, no scheduler concurrency changes; 2.4/2.5 execute one synthetic evidence run at a time under a dedicated `basin_version_id`/`model_id`.
- Resource limits / large input / discovery: **selected** — G9 capacity baseline (2.6) evaluates producer limits 10,000 stations / 10,000 timesteps / 10,000,000 rows / ~32 MiB manifest and the seven `MAX_DIRECT_GRID_*` staging limits against live 13-basin / 6,290-station / ~121M-rows-per-2-weeks facts; any breach is flagged.
- Legacy compatibility / examples: **selected** — 13 live IDW basins and their `core.model_instance` rows are unchanged sibling consumers (must remain untouched); `MAX_PACKAGE_MANIFEST_BYTES` explicitly excluded from staging pin so a change to the PRCP best-effort cap does not silently shift the readiness contract; existing pytest suites executed as-is on the pinned baseline (no weakening).
- Error handling / rollback / partial outputs: **selected** — manifest immutability per version (no in-place edits), `.sha256` bind, completeness check FAIL blocks §2/§3, cross-artifact consistency check invalidates the evidence set on any mismatch, synthetic evidence cleanup/isolation, station-MVT display spot-check as the leaked-row detector.
- Release / packaging / dependency compatibility: **selected** — `shud_omp` build provenance must link the deployed binary to `shud_solver_submodule_commit`; `shud_openmp_outer_commit` and solver submodule commit are two distinct fields; PROJ release string + `proj.db` layout metadata pinned on deployment host.
- Documentation / migration notes: **selected** — source-of-truth §4/§6.1/§7.1-3/§10 citations required in tasks + specs; observed OpenSpec/code drift must be recorded (2.7); readiness certification is defined against pinned-commit evidence, not checkbox state.

Domain packs (`openspec/project-profile.md` §Domain risk packs):

- Geospatial / CRS / basin geometry: **selected** — `proj_crs_database_version` derived on the deployment host from installed PROJ (release string + `proj.db` layout/build metadata), never copied from docs.
- Hydro-met time series / forcing windows: **selected** — synthetic direct-grid contract + `.sp.att FORC` range validation + producer 10k/10k/10M limits + staged `.tsd.forc` `ID` set membership guard (2.5).
- SHUD numerical runtime / conservation / NaN: **selected** — solver forcing-consumer audit (3.1-3.4) covers all `.sp.att FORC` readers, station X/Y/Z numeric participation, elevation correction, and non-weather `*.tsd.*` inputs (`tsd.lai`, `tsd.mf`, `tsd.rl`); ends in explicit `z_policy` verdict (sentinel only if Z proven unused, else `canonical_orography`/`model_dem_at_cell_center`).
- PostGIS / TimescaleDB domain behavior: **selected** — node-27 active primary PG live migration version query (1.1), `met.forcing_station_timeseries` row-count measurement for G9 (2.6), `met.model_instance` synthetic evidence row under dedicated non-production identity with `active_flag=false` mirrors so the station-MVT layer cannot mix old/new stations.
- Slurm production lifecycle / mock-vs-real parity: **selected (narrow)** — node-22 minimal-basin execution stages the production `shud_omp` binary (2.5) to prove direct-grid multi-station staging works end-to-end with the pinned solver submodule commit; no sbatch/scheduler routing change, so parity scope is bounded to the execution binding.
- External hydro-met providers / snapshot reproducibility: **not selected** — no provider snapshot change; canonical converter versions (gfs `m1.4` / ifs `m4.1` / era5 `m2.0`) are pinned as identities only, no re-conversion.
- Run manifest / QC provenance: **selected** — the readiness manifest itself is the run-manifest-like authority; every evidence artifact must cite `baseline_commit`, manifest SHA-256 checksum, `shud_solver_submodule_commit` (when applicable), and executing host; drift recording required.
- Published NHMS artifacts / display identity: **selected** — station-MVT / production display spot-check on node-27 after the 2.4 smoke, confirming synthetic evidence rows never leak into production display; source-of-truth §10 mixed-display hazard is the guard.

## Invariant Matrix

Fixture level: broad-expanded (pinning identities span shud_omp outer + solver submodule + DB schema + PROJ + producer/runtime constants + multi-host evidence + synthetic-package boundary + display safety).

Governing invariant: Platform readiness certification is only provable on a single frozen baseline. Every evidence artifact (P0.1-P0.3) MUST reference the identical `baseline_commit`, the identical readiness-manifest SHA-256 checksum, and the identical `shud_solver_submodule_commit`; any mismatch invalidates the evidence set and blocks basin migration.

Source-of-truth identity/contract: `openspec/changes/cmfd-direct-grid-platform-readiness/evidence/readiness-manifest.<version>.json` + `.sha256` companion (versioned, immutable, checksum-bound).

Surfaces:
- Producers: draft/final manifest (1.1-1.3); pinned-commit pytest re-run runners on node-27 (2.1-2.2); synthetic-package construction + smoke carrier (2.3); real-object-store + real-DB smoke on node-27 (2.4); minimal-basin `shud_omp` runner on node-22 (2.5); G9 capacity baseline query on node-27 (2.6); solver forcing-consumer audit report (3.1-3.4).
- Validators/preflight: manifest completeness check (1.3); cross-artifact consistency check binding every artifact to identical baseline_commit + manifest checksum (4.1); `openspec validate` + `openspec status --change` (4.2); schema-identity mismatch guard between `db_schema_migration_repo_head` and `db_schema_migration_version` (1.1).
- Storage/cache/query: `db/migrations/` (repo head); node-27 active primary PG live migration version; node-27 `met.model_instance` synthetic evidence row (dedicated non-production `basin_version_id`/`model_id`, `resource_profile.direct_grid_forcing`); node-27 `met.met_station` mirror rows written with `active_flag=false`; node-27 `met.forcing_station_timeseries` (read-only for G9 baseline); object store (synthetic direct-grid fixture only).
- Public routes/entrypoints: none — this change adds no runtime routes/CLI/scheduler entrypoints.
- Frontend/downstream consumers: station-MVT / production display layer (source-of-truth §10) must remain unaffected; verified by the 2.4 smoke's display spot-check.
- Failure paths/rollback/stale state: manifest is not editable in place — a new version file is required; synthetic evidence rows are either deleted after capture or remain confined to the dedicated non-production identity with `active_flag=false` mirrors; a mismatched manifest checksum, mismatched `baseline_commit`, or `shud_omp` binary not built from `shud_solver_submodule_commit` invalidates the evidence set.
- Evidence/audit/readiness: `openspec/changes/cmfd-direct-grid-platform-readiness/evidence/` (manifest + `.sha256` + 1.3 completeness-check output + 2.1-2.6 run/smoke/execution/capacity records + 2.7 evidence index + §3 audit report + §4 assembled package).

Regression rows:
- Draft manifest carries every P0.1 identity with non-empty values and complete `source_locations` mapping → completeness check reports every enumerated P0.1 identity field resolved (per §Where the readiness manifest lives), §2/§3 tasks unblocked on this pinned set.
- Manifest omits `shud_solver_submodule_commit` (only outer `shud_openmp_outer_commit` present) → completeness check FAILs; §2/§3 evidence tasks blocked; readiness not certifiable.
- `db_schema_migration_repo_head` != `db_schema_migration_version` (node-27 live) → baseline schema identity unresolved; mismatch recorded as blocking; no §2 evidence runs on this pinned set.
- `.sp.att FORC` values fall outside the staged `.tsd.forc` `ID` set on the node-22 minimal-basin run → execution rejected with stable staging error; evidence set invalidated for this baseline.
- Synthetic evidence run writes rows tied to a production `basin_version_id`/`model_id`, or writes `met.met_station` mirrors with `active_flag=true` → station-MVT mixed-display hazard triggers; evidence run invalidated and rows cleaned before re-run.
- P0.3 audit issues `z_policy=sentinel` without evidence that `Z` is numerically unused → verdict rejected; `canonical_orography` or `model_dem_at_cell_center` chosen with recorded justification before certification.
- Unchanged sibling consumer: all 13 production `core.model_instance` rows and their live packages → untouched by P0.2/P0.3; diff against the 2026-07-06 appendix A snapshot shows zero production model_instance rewrites.
- Unchanged sibling consumer: `workers/forcing_producer`, `workers/shud_runtime`, scheduler/state/display → no runtime code changes in this change; downstream compatibility preserved.

Boundary-surface checklist:
- Shared helper roots: `packages/common/` — none touched; verified in Phase 2 by grep against changed files.
- Public entrypoints: none touched.
- Read surfaces: `workers/canonical_converter/converter.py`, `workers/forcing_producer/producer.py`, `workers/shud_runtime/runtime.py`, `db/migrations/`, node-27 live PG (read-only for schema version + G9 row counts), deployment-host PROJ install — read-only.
- Write/delete/overwrite surfaces: `openspec/changes/cmfd-.../evidence/` (append-only new files per version); node-27 dedicated non-production `met.model_instance`/`met.met_station` rows (evidence-only, `active_flag=false`, cleaned up).
- Staging/publish/rollback surfaces: manifest is immutable per version; a new manifest version file is the only supported "rollback" (append-only).
- Producer/consumer evidence boundaries: every evidence artifact cites (a) `baseline_commit`, (b) manifest SHA-256 checksum, (c) `shud_solver_submodule_commit` when applicable, (d) executing host (node-27 or node-22).
- Stale-state/idempotency boundaries: repeated 2.1/2.2 pytest suites on same pinned commit produce identical pass results; 2.4 smoke and 2.6 capacity queries idempotent modulo timestamped measurements; 2.5 node-22 execution re-runnable without altering the synthetic fixture.
- Unchanged downstream consumers: station-MVT/display frontend, production 13-basin scheduler flows, and non-CMFD forcing producers all remain unaffected.

## Open Questions

Both prior open questions are resolved in this revision:

- `proj_crs_database_version` format — **Resolved**: pin both the PROJ release string and the bundled `proj.db` layout/build metadata, derived on the deployment host (see manifest schema); never copied from documentation.
- keliya vs. synthetic minimal package for the minimal-basin execution — **Resolved: the synthetic minimal direct-grid package is mandatory** (see "Synthetic direct-grid evidence assets"). keliya's live package is legacy IDW with no direct-grid contract, and producing a direct-grid keliya package would require the out-of-scope mapping builder / production FORC rewrite. Acceptance criterion: the execution must exercise standard multi-station **direct-grid** staging with the production binary — a legacy (non-direct-grid) package does not satisfy it.
