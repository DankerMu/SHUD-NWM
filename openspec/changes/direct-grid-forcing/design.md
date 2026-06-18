## Context

The current forcing producer loads fixed SHUD forcing stations from `met.met_station`, computes or reuses IDW weights from canonical IFS/GFS grid points, and writes SHUD `.tsd.forc` plus per-station CSV files. This is correct for legacy CMFD/LDAS-derived station layouts, but national-scale operation multiplies the cost of runtime interpolation across many basins and cycles.

The new basin model assets will precompute the spatial ownership between SHUD triangles and IFS/GFS 0.25 degree grid cells. In those migrated assets, the station list is no longer a legacy CMFD/LDAS station set; it is a source-grid forcing station set whose `shud_forcing_index` matches the already-rewritten `.sp.att` `FORC` values. Runtime should therefore perform exact canonical grid-cell lookup instead of IDW.

Canonical conversion remains required. IFS/GFS raw products still need precipitation/radiation de-accumulation, humidity derivation, wind speed composition, unit normalization, lineage, and QC before SHUD forcing is written.

## Goals / Non-Goals

**Goals:**

- Select `idw` or `direct_grid` per basin/model asset, not by global deployment toggle.
- Make `direct_grid` opt-in and fail-closed: if declared direct-grid metadata is incomplete or stale, forcing production fails instead of falling back to IDW.
- Keep legacy IDW behavior unchanged for old basin assets.
- Reuse the existing SHUD forcing output contract: contiguous forcing indexes, `.tsd.forc`, per-station CSV files with `Precip/Temp/RH/Wind/RN`, `met.forcing_version`, and `met.forcing_station_timeseries`. Pressure can remain persisted metadata/timeseries but is not added to SHUD station CSV files.
- Avoid loading or processing unnecessary global grid cells once a direct-grid binding provides the required `grid_cell_id` set.

**Non-Goals:**

- Do not change canonical physical conversion rules for GFS/IFS.
- Do not define the offline GIS algorithm that rewrites `.sp.att` `FORC`; that is an upstream model asset build step.
- Do not support implicit direct-grid activation merely because some station rows contain `grid_cell_id`.
- Do not remove IDW support.

## Decisions

### 1. Explicit per-asset mode resolution

`forcing_mapping_mode` is resolved from the model/input asset manifest associated with the selected `model_id` and `basin_version_id`. Supported values are `idw` and `direct_grid`.

- If absent, mode defaults to `idw` for compatibility.
- If present and unsupported, production fails with a contract error.
- If `direct_grid`, all direct-grid validation must pass before any forcing output is marked ready.

Alternative considered: infer mode from station metadata. Rejected because partial or stale station bindings could silently change results.

### 2. Direct-grid bindings are part of the model asset contract

The direct-grid binding is a JSON artifact referenced by the model/input asset manifest. The manifest is authoritative; database rows may mirror it for query performance but cannot override it. The manifest MUST provide `forcing_mapping_mode`, `binding_uri`, `binding_checksum`, `model_input_package_id` or equivalent immutable package identity, `.sp.att` path/checksum, `applicable_source_ids`, `grid_id`, and `grid_signature`.

Each direct-grid forcing station binding MUST provide:

- `station_id`
- `shud_forcing_index`
- `forcing_filename`
- longitude/latitude in WGS84-compatible coordinates
- `x`, `y`, `z` or equivalent SHUD output coordinates
- `grid_id`
- `grid_cell_id`

Source applicability is manifest-level through `applicable_source_ids`, such as `["GFS", "IFS"]` when the same normalized 0.25 degree binding is valid for both sources, or a single-source list when it is source-specific. Per-station source variation is out of scope for this change.

The model asset manifest MUST identify the `.sp.att` file version whose `FORC` values were rewritten to these station indexes.

Alternative considered: keep bindings only in database tables. Rejected as the only source because direct-grid correctness depends on the file package and `.sp.att`; database rows may be refreshed independently. Database persistence can mirror the contract, but the asset manifest remains the traceable source.

### 3. Reuse interpolation infrastructure with direct weights first

The initial implementation should be able to persist direct bindings into the existing weight path as `method='direct_grid'`, `weight=1.0`, and one `grid_cell_id` per station/variable. This minimizes downstream changes to aggregation and persistence. A later cleanup can introduce a separate binding table if needed.

Alternative considered: bypass `met.interp_weight` entirely. Rejected for the first slice because it duplicates row generation and lineage paths that already understand station/variable/grid-cell mappings.

### 4. Grid signature validation protects against stale bindings

Direct-grid mode MUST compare the binding's declared `grid_id` and `grid_signature` with the canonical product grid used for the current cycle/source. The `grid_signature` is the SHA-256 content signature already derived from the canonical grid definition: schema version plus ordered longitude/latitude coordinate arrays or ordered cell list after applying the producer's longitude normalization rules. A mismatch fails production before values are read. GFS `0..360` source longitude and IFS `-180..180` source longitude MUST normalize to the same WGS84 geometry convention for station coordinates, while `grid_cell_id` remains the authoritative lookup key.

### 5. Runtime exact lookup still writes SHUD station outputs

For direct-grid mode, the producer reads canonical values for required `grid_cell_id`s, maps each station to its bound cell value, derives combined output variables such as wind speed through the existing canonical-to-forcing path, and writes the same SHUD package shape as IDW mode. The SHUD station CSV remains the five-variable contract `Precip/Temp/RH/Wind/RN`; pressure may still be persisted in `met.forcing_station_timeseries` and lineage when available. The value differs only by spatial mapping method: exact cell lookup rather than weighted neighboring cells.

## Risks / Trade-offs

- **Risk: direct-grid assets and `.sp.att` get out of sync** → Validate contiguous forcing indexes, station count, referenced `FORC` range, grid signature, binding checksum, model input package identity, and `.sp.att` checksum before publishing a ready forcing version.
- **Risk: source grid definitions drift across canonical products** → Treat grid signature mismatch as a hard blocker and include expected/actual signatures in the failure details.
- **Risk: exact nearest-cell mapping creates blockier forcing than IDW** → Accept this as the explicit scientific trade-off for migrated assets; retain IDW assets where smoothing is desired.
- **Risk: direct-grid path accidentally bypasses physical conversions** → Keep canonical products as the only allowed input; direct-grid applies only after canonical conversion.
- **Risk: large station counts increase SHUD file IO** → Require basin-scoped grid subsets rather than global shapefile ingestion, and record station counts in validation evidence.

## Migration Plan

1. Add metadata parsing and validation for `forcing_mapping_mode`, direct-grid JSON bindings, `applicable_source_ids`, model input identity, `.sp.att` checksum, `grid_id`, and `grid_signature`.
2. Check or update persistence compatibility for `method='direct_grid'`, `weight=1.0`, indexes, and idempotent replacement semantics.
3. Implement direct binding load/persist behavior as one-cell weights with `method='direct_grid'`.
4. Add exact grid-cell value extraction and reuse existing SHUD package formatting.
5. Add model asset validation that checks station indexes and `.sp.att` `FORC` references.
6. Add runtime staging validation so direct-grid packages use the standard multi-station SHUD forcing path and never trigger single-station `.sp.att` rewrites.
7. Migrate basins gradually by publishing new model/input versions that declare `direct_grid`; old versions continue to run with IDW.
8. Rollback by deactivating the direct-grid model/input version and reactivating the prior IDW asset version.

## Issue #540 Fixture Addendum

Fixture level: expanded
Repair intensity: high
Project profile: NHMS

Change surface:
- Contract parsing/types for direct-grid model asset manifests and station bindings.
- Repository/store interface boundary that exposes the authoritative manifest-backed contract.
- Tests for parse errors, required fields, source scope, and grid signature field handling.

Must preserve:
- Existing forcing producer behavior remains IDW-only until later issues add mode selection and direct-grid production.
- Existing model asset/public projections do not start treating database mirrors as authoritative direct-grid bindings.
- Existing SHUD forcing station validation and package formatting remain unchanged.

Must add/change:
- A single authoritative in-process contract model for direct-grid binding manifests.
- A repository/store read interface for `load_forcing_mapping_contract(model_id, basin_version_id)` or an equivalent name.
- Structured contract errors for missing required manifest or station fields.

Selected risk packs:
- Public API / CLI / script entry: selected - repository interfaces are consumed by worker entrypoints even though #540 does not wire producer switching.
- Config / project setup: not selected - no deployment/runtime config is changed.
- File IO / path safety / overwrite: selected - binding and `.sp.att` paths/checksums are accepted as manifest identities but #540 must not read arbitrary files or publish outputs.
- Schema / columns / units / field names: selected - JSON field names, source scope, grid identity, and station binding fields are the main contract.
- Auth / permissions / secrets: not selected - no credentials or permission boundary changes.
- Concurrency / shared state / ordering: not selected - no mutable shared state or scheduling behavior changes.
- Resource limits / large input / discovery: selected - binding parsers must reject malformed/unbounded station structures at unit-test scale and avoid global discovery.
- Legacy compatibility / examples: selected - absent direct-grid metadata must leave existing IDW assets unaffected.
- Error handling / rollback / partial outputs: selected - invalid contracts must raise structured errors and must not create ready forcing outputs in later producer issues.
- Release / packaging / dependency compatibility: not selected - no new runtime dependency is required for #540.
- Documentation / migration notes: selected - docs and OpenSpec must identify manifest authority and DB mirror non-authority.
- Geospatial / CRS / basin geometry: selected - station coordinates, longitude convention, `grid_id`, and `grid_signature` fields are part of the contract.
- Hydro-met time series / forcing windows: not selected - #540 defines spatial contract only, no time-window logic.
- SHUD numerical runtime / conservation / NaN: not selected - no value generation or runtime execution in #540.
- PostGIS / TimescaleDB domain behavior: not selected - persistence changes are #543.
- Slurm production lifecycle / mock-vs-real parity: not selected - no Slurm surface.
- External hydro-met providers / snapshot reproducibility: selected - `applicable_source_ids`, `grid_id`, and `grid_signature` bind GFS/IFS compatibility.
- Run manifest / QC provenance: selected - binding checksum, model input package identity, and `.sp.att` checksum are future lineage inputs.
- Published NHMS artifacts / display identity: not selected - no published products are created in #540.

Boundary-surface checklist:
- Shared helper roots: new direct-grid contract helpers only; do not modify canonical conversion or SHUD package helpers.
- Public entrypoints: no CLI/API behavior change in #540.
- Read surfaces: manifest mapping input supplied as in-memory metadata or repository-returned JSON; no direct file/object-store reads in this issue.
- Write/delete/overwrite surfaces: none.
- Producer/consumer evidence boundaries: contract fields must preserve binding checksum, model input identity, `.sp.att` checksum, source scope, grid identity, and grid signature without deriving authority from DB mirrors.
- Stale-state/idempotency boundaries: parsed contract must carry identities needed by later freshness checks, but #540 does not implement freshness.
- Unchanged downstream consumers: legacy IDW station loading and existing model asset APIs remain compatible.

Invariant Matrix
Governing invariant: A direct-grid forcing contract is valid only when the selected model asset manifest is the single authority for binding identity, source scope, grid identity, and station-to-grid-cell fields.
Source-of-truth identity/contract: model asset manifest fields `forcing_mapping_mode`, `binding_uri`, `binding_checksum`, `model_input_package_id`, `sp_att_path`, `sp_att_checksum`, `applicable_source_ids`, `grid_id`, `grid_signature`, and station binding rows.
Surfaces:
- Producers: `workers/forcing_producer` repository protocol and later producer consumers; #540 adds interface/types only.
- Validators/preflight: direct-grid contract parser and structured `DirectGridContractError`.
- Storage/cache/query: repository/store load interface; DB mirror values are derived cache only.
- Public routes/entrypoints: none in #540 - producer/CLI wiring is #541+.
- Frontend/downstream consumers: none in #540 - no API payload changes.
- Failure paths/rollback/stale state: missing or malformed contract fields raise errors before any ready output can be produced by later issues.
- Evidence/audit/readiness: parsed contract retains checksums, package identity, source scope, and grid signature for later lineage/freshness.
Regression rows:
- valid direct-grid manifest with two contiguous station bindings and source `GFS` -> parsed contract preserves every identity and station `grid_cell_id`.
- manifest missing `binding_checksum`, `grid_signature`, station `grid_cell_id`, or current source scope -> structured contract error with field/source detail.
- legacy asset without direct-grid contract -> repository interface returns no direct-grid contract or IDW-compatible absence without changing existing IDW behavior.

## Issue #541 Fixture Addendum

Fixture level: expanded
Repair intensity: high
Project profile: NHMS

Change surface:
- `workers/forcing_producer/producer.py` production entrypoint mode resolution before spatial mapping work.
- Repository contract consumption via `load_forcing_mapping_contract(model_id, basin_version_id, source_id)`.
- Unit tests proving legacy IDW compatibility, explicit IDW compatibility, direct-grid selection, and unsupported/malformed mode failure behavior.

Must preserve:
- Existing model assets without direct-grid metadata continue through the current IDW station loading, canonical product validation, weight load/create, output, and readiness path.
- Explicit `forcing_mapping_mode="idw"` behaves the same as absent metadata.
- No direct-grid value generation, exact mapping materialization, `met.interp_weight` direct-grid persistence, lineage/idempotency change, or SHUD runtime staging behavior is introduced in #541.

Must add/change:
- Producer resolves mapping mode from the authoritative repository contract entrypoint after model/basin identity is known and before IDW station/weight work that direct-grid must not fallback into.
- Explicit `direct_grid` selects a direct-grid validation gate placeholder and fails closed until later issues implement successful validation/value generation.
- Unsupported mapping mode or malformed direct-grid contract errors surface as forcing production failures, update the forecast cycle failure state, and do not mark outputs ready.

Risk packs considered:
- Public API / CLI / script entry: selected - `ForcingProducer.produce` is the worker entrypoint used by CLI/orchestration.
- Config / project setup: not selected - no deployment setting or environment switch is added.
- File IO / path safety / overwrite: not selected - #541 does not read binding files or write new path classes beyond existing IDW path.
- Schema / columns / units / field names: selected - `forcing_mapping_mode` values and contract error fields drive control flow.
- Auth / permissions / secrets: not selected - no credential or permission boundary changes.
- Concurrency / shared state / ordering: selected - mode must be resolved before IDW weights/output readiness side effects.
- Resource limits / large input / discovery: not selected - station binding parsing limits were #540; #541 does not add discovery.
- Legacy compatibility / examples: selected - absent/explicit IDW assets must keep existing behavior.
- Error handling / rollback / partial outputs: selected - direct-grid/unsupported failures must not create ready outputs and must update failure state.
- Release / packaging / dependency compatibility: not selected - no dependency/package change.
- Documentation / migration notes: selected - OpenSpec fixture documents this PR boundary and later tasks.
- Geospatial / CRS / basin geometry: not selected - no grid geometry validation in #541.
- Hydro-met time series / forcing windows: not selected - no time-window behavior changes.
- SHUD numerical runtime / conservation / NaN: not selected - no numerical value generation.
- PostGIS / TimescaleDB domain behavior: not selected - no persistence/schema changes.
- Slurm production lifecycle / mock-vs-real parity: not selected - no Slurm surface.
- External hydro-met providers / snapshot reproducibility: selected - mode resolution is source-aware through `source_id` passed to the contract loader.
- Run manifest / QC provenance: selected - failure/readiness state must truthfully represent selected mapping mode.
- Published NHMS artifacts / display identity: not selected - no published display artifact changes.

Boundary-surface checklist:
- Shared helper roots: direct-grid contract parser from #540; producer mode resolver helper if introduced.
- Public entrypoints: `ForcingProducer.produce`; CLI remains a thin caller.
- Read surfaces: repository `load_forcing_mapping_contract` using selected `model_id`, `basin_version_id`, and `source_id`.
- Write/delete/overwrite surfaces: existing IDW output path only for legacy/IDW; explicit direct-grid failure must not write package/records.
- Producer/consumer evidence boundaries: forecast-cycle failure state and exceptions must distinguish unsupported/direct-grid-not-implemented from legacy IDW success.
- Stale-state/idempotency boundaries: existing already-done path must remain legacy-only unless direct-grid readiness is implemented later.
- Unchanged downstream consumers: existing forcing package shape and IDW tests.

Invariant Matrix
Governing invariant: Once a model asset explicitly selects `direct_grid`, the producer must not silently continue into IDW station/weight/output readiness work.
Source-of-truth identity/contract: repository-returned forcing mapping contract for the selected `model_id`, `basin_version_id`, and normalized `source_id`; absent contract means legacy IDW.
Surfaces:
- Producers: `ForcingProducer.produce` mode resolution and branch ordering.
- Validators/preflight: direct-grid contract loader errors and direct-grid-not-yet-implemented gate in #541.
- Storage/cache/query: repository contract load only; no `met.interp_weight` direct-grid writes in #541.
- Public routes/entrypoints: forcing CLI/orchestrator callers see stable `ForcingProductionError` and failure state.
- Frontend/downstream consumers: none in #541 - no API/frontend payload changes.
- Failure paths/rollback/stale state: unsupported/direct-grid failures update forecast cycle failure state and do not create ready forcing outputs.
- Evidence/audit/readiness: tests must prove no IDW fallback calls occur for explicit direct-grid or unsupported mode.
Regression rows:
- legacy asset with no contract -> existing IDW path succeeds and existing tests remain valid.
- explicit `idw` contract/absence -> existing IDW station/weight path is used.
- explicit `direct_grid` contract -> producer stops at validation gate, records failure, and does not call IDW station/weight/output operations.
- malformed/unsupported mapping mode from contract load -> producer fails closed and does not mark `forcing_ready`.

## Issue #542 Fixture Addendum

Fixture level: expanded
Repair intensity: high
Project profile: NHMS

Change surface:
- Direct-grid validation gate reached after #541 mode resolution and before any direct-grid value generation.
- Contract/parser validation for station identity, source scope, WGS84 longitude convention, binding/model input identity, and duplicate-safe SHUD output filenames.
- Producer validation against canonical product grid identity/signature and model input `.sp.att` ownership.
- Failure-state tests proving direct-grid validation failures do not create ready outputs or fall back to IDW.

Must preserve:
- Legacy assets with absent mapping metadata and explicit `forcing_mapping_mode="idw"` continue through the existing IDW station loading, weight load/create, package output, and readiness path.
- The #541 no-IDW-fallback invariant remains intact for explicit `direct_grid`.
- #542 does not implement successful direct-grid row generation, direct-grid `met.interp_weight` persistence, SHUD direct-grid package writes, lineage/idempotency freshness, or SHUD runtime staging.

Must add/change:
- Direct-grid contracts are validated as complete, source-applicable, safe, basin/model-owned assets before production can proceed.
- Validation checks include station required fields, contiguous unique `shud_forcing_index`, safe unique `forcing_filename`, finite WGS84-compatible coordinates with longitude normalized to `[-180, 180)`, `binding_checksum`, `model_input_package_id`, `.sp.att` checksum, canonical `grid_id`, canonical `grid_signature`, and `.sp.att FORC` references.
- Failure details include the relevant field/source and expected/actual identity where a mismatch is detected.
- Direct-grid validation errors update forecast cycle failure state and stop before IDW station loading, IDW weight computation, output package writes, forcing version readiness, components, or station timeseries.

Selected risk packs:
- Public API / CLI / script entry: selected - `ForcingProducer.produce` is still the worker entrypoint and validation failures must surface as stable production failures.
- Config / project setup: not selected - no deployment toggle or environment-level switch is introduced.
- File IO / path safety / overwrite: selected - `.sp.att` identity and checksum are validated; SHUD forcing filenames must remain safe; #542 must not dereference arbitrary binding paths beyond explicit test fixtures.
- Schema / columns / units / field names: selected - contract fields, canonical `grid_id`, `grid_signature`, `.sp.att FORC`, and expected/actual failure details form the behavior contract.
- Auth / permissions / secrets: not selected - no credential or permission boundary changes.
- Concurrency / shared state / ordering: selected - validation must run before heavy IDW/output side effects and must not reuse stale ready outputs for direct-grid failures.
- Resource limits / large input / discovery: selected - validation must remain bounded to basin/model asset data and direct-grid station bindings, not discover global grids.
- Legacy compatibility / examples: selected - IDW behavior and legacy fake repositories remain supported.
- Error handling / rollback / partial outputs: selected - every validation failure must be fail-closed with `failed_forcing` and no ready records/packages.
- Release / packaging / dependency compatibility: not selected - no new runtime dependency is expected.
- Documentation / migration notes: selected - tasks/design identify validation gates and non-goals for later producer/runtime issues.
- Geospatial / CRS / basin geometry: selected - WGS84 longitude normalization, grid identity, grid signature, and `.sp.att FORC` ownership are core requirements.
- Hydro-met time series / forcing windows: not selected - #542 does not generate direct-grid time series values.
- SHUD numerical runtime / conservation / NaN: selected only for validation ownership - `.sp.att FORC` references must be valid, but runtime staging and numerical values are later tasks.
- PostGIS / TimescaleDB domain behavior: not selected - direct-grid persistence changes are #543.
- Slurm production lifecycle / mock-vs-real parity: not selected - no Slurm behavior change.
- External hydro-met providers / snapshot reproducibility: selected - canonical grid identity/signature must match current GFS/IFS source products.
- Run manifest / QC provenance: selected - binding checksum, model input identity, `.sp.att` checksum, source scope, and grid signature are future lineage inputs and current validation evidence.
- Published NHMS artifacts / display identity: not selected - no published display artifact changes.

Boundary-surface checklist:
- Shared helper roots: direct-grid contract helpers; any new validation helper must remain reusable by producer and tests without changing canonical conversion semantics.
- Public entrypoints: `ForcingProducer.produce`; CLI remains a thin caller.
- Read surfaces: repository contract loader, canonical product metadata/grid definitions, and model input `.sp.att` validation fixture/source.
- Write/delete/overwrite surfaces: none for direct-grid success in #542; validation failures must not write packages, forcing versions, components, timeseries, or ready states.
- Producer/consumer evidence boundaries: `failed_forcing` updates and exception details must identify field/source and expected/actual mismatch where applicable.
- Stale-state/idempotency boundaries: direct-grid validation occurs before existing-ready reuse so stale IDW-ready versions cannot mask validation failures.
- Unchanged downstream consumers: existing IDW package shape, IDW tests, IFS integration tests, and e2e legacy fake repositories remain compatible.

Invariant Matrix
Governing invariant: A model asset that explicitly selects `direct_grid` can proceed past the producer validation gate only when its binding contract, source scope, canonical grid identity/signature, model input identity, and `.sp.att FORC` ownership are mutually consistent; otherwise production fails closed without IDW fallback or ready outputs.
Source-of-truth identity/contract: repository-returned direct-grid contract for the selected `model_id`, `basin_version_id`, and normalized `source_id`; canonical product grid metadata for the current source/cycle; model input package identity and `.sp.att` checksum/FORC values declared by the asset.
Surfaces:
- Producers: `ForcingProducer.produce` direct-grid branch ordering and validation before IDW station/weight/output work.
- Validators/preflight: direct-grid contract parser, grid identity/signature checker, source scope checker, and `.sp.att FORC` validator.
- Storage/cache/query: repository contract load only; #542 does not persist `direct_grid` weights or lineage.
- Public routes/entrypoints: forcing CLI/orchestrator callers receive `ForcingProductionError` and a `failed_forcing` cycle status.
- Frontend/downstream consumers: none in #542 - no API/frontend payload changes.
- Failure paths/rollback/stale state: validation failures update failure state, include expected/actual details, and do not mark or reuse `forcing_ready` outputs.
- Evidence/audit/readiness: tests cover each required validation failure, no-IDW fallback side effects, and legacy IDW compatibility.
Regression rows:
- valid direct-grid contract with matching source, grid identity/signature, model input identity, `.sp.att` checksum, and FORC range -> reaches the #542 validation-success boundary, then fails only because successful direct-grid row generation is a later issue.
- missing station required field, duplicate filename, invalid longitude, or non-contiguous `shud_forcing_index` -> structured `DirectGridContractError` before ready output.
- current source not in `applicable_source_ids` -> structured source-scope failure before ready output.
- binding checksum, model input package identity, `.sp.att` checksum, canonical `grid_id`, or canonical `grid_signature` mismatch -> validation failure with expected/actual details and no ready output.
- `.sp.att FORC` references zero, negative, missing, or out-of-range station indexes -> validation failure with expected/actual details and no ready output.
- legacy absent/explicit `idw` asset -> existing IDW path still succeeds.
