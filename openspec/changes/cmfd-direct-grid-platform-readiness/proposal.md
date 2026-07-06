## Why

Before any CMFD basin is migrated to IFS/GFS direct-grid forcing, the platform must be proven ready on a single, frozen, auditable software/schema/solver baseline. The repository already implements the direct-grid contract parser, exact-cell lookup, standard SHUD packaging, and runtime `.sp.att FORC` validation, but OpenSpec task state and code state have drifted (source-of-truth §4/P0.2), the production SHUD solver's actual use of station `X/Y/Z` and ancillary `*.tsd.*` inputs has never been audited, and no capacity baseline exists against the live 13-basin / 6,290-station deployment. This change establishes Platform Readiness Gate P0 (version pinning, re-run evidence, and solver forcing-consumer audit) so that migration risk is judged on pinned-commit evidence rather than on checkbox state.

## What Changes

- Add a versioned, immutable **readiness release manifest** that pins every software, schema, CRS, and algorithm identity required for direct-grid migration (SHUD-NWM commit, forcing producer version and resource limits, canonical converter versions gfs `m1.4` / ifs `m4.1` / era5 `m2.0`, SHUD runtime commit and staging limits, SHUD-OpenMP outer repo commit AND the SHUD solver git-submodule exact commit recorded separately with build provenance, DB schema migration version as both repo head and node-27 deployment-applied version, PROJ/CRS database version derived on the deployment host, and mapping-builder algorithm version `nearest_cell_barycenter_geodesic_v1` per its declared source-of-truth §6.1 authority). The manifest is a committed evidence file bound by SHA-256 checksum with a committed completeness check.
- Add a **readiness evidence run** that re-executes the direct-grid contract/producer/exact-cell/standard-package/runtime-staging/out-of-range-`.sp.att`-negative/idempotency/DB-migration test suites on the pinned commit on node-27 (the deployment host), plus a real-object-store + real-DB smoke and a minimal-basin execution with the production SHUD binary on node-27/node-22, plus a Gate G9 capacity baseline report against deployment config and live row counts measured on node-27. Because none of the 13 live model instances carries a direct-grid contract, the smoke and minimal-basin execution use a hand-assembled **synthetic direct-grid evidence fixture** (contract + minimal multi-station package) registered under a dedicated evidence-only model instance with isolated, `active_flag=false`, cleaned-up DB writes — no production instance or basin package is touched.
- Add a **solver forcing-consumer audit** of the production SHUD solver pin covering all readers of `.sp.att FORC`, any independent river/lake forcing index, whether station `X/Y/Z` participate in numeric computation, any elevation correction, non-weather `*.tsd.*` inputs (`tsd.lai`, `tsd.mf`, `tsd.rl`), independence of `Prcp_Correction`/LAI/MF series, and which legacy forcing-directory files must be preserved — producing an explicit `z_policy` verdict.
- Establish that readiness is judged on pinned-commit evidence, NOT on OpenSpec checkbox state.
- This change produces evidence and pinning artifacts only, with minimal or no production-code changes; it performs no basin migration.

## Capabilities

### New Capabilities
- `platform-release-pinning`: Pin every software/schema/CRS/algorithm identity for direct-grid migration into one immutable, checksum-bound readiness manifest.
- `direct-grid-readiness-evidence`: Re-run the direct-grid test suites, real-object-store/real-DB smoke, minimal-basin production-binary execution, and G9 capacity baseline on the pinned release, and assemble a same-baseline evidence package in which every artifact references the identical manifest checksum and `baseline_commit`.
- `solver-forcing-consumer-audit`: Audit the pinned production SHUD solver's use of `.sp.att FORC`, station `X/Y/Z`, elevation, and ancillary `*.tsd.*` inputs, and issue an explicit `z_policy` verdict.

### Modified Capabilities
- None.

## Impact

- `openspec/changes/cmfd-direct-grid-platform-readiness/`: readiness manifest schema, evidence-package layout, and audit-report requirements captured as committed artifacts.
- Evidence generation binds to existing suites `tests/test_direct_grid_e2e.py`, `tests/test_forcing_producer.py`, `tests/test_shud_runtime.py`, and DB-migration tests `tests/test_migrations.py`; no test is weakened or deleted.
- Pinned identities are read from existing sources: `workers/canonical_converter/converter.py` (converter versions), `workers/shud_runtime/runtime.py` (SHUD executable `shud_omp`, staging byte/line limits at lines 52-58), `workers/forcing_producer/producer.py` (10k stations / 10k timesteps / 10M rows / 32 MiB manifest limits), `db/migrations/` (schema version), and package `gis/*.prj` (per-basin CRS).
- node-27 (real DB + object store + display oracle) hosts the pinned-commit pytest suite re-runs, the real-backend smoke, and the capacity-baseline evidence; node-22 (Slurm/SHUD runtime behavior) hosts the minimal-basin production-binary execution evidence, per CLAUDE.md verification-oracle routing.
- No change to scheduler routing, state manager, display, grid registry (`canonical-source-grid-registry`), or mapping builder (`forcing-mapping-asset-build`); no basin migration is performed.
