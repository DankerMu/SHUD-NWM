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

## Issue #543 Fixture Addendum

Fixture level: expanded
Repair intensity: high
Project profile: NHMS

Change surface:
- `met.interp_weight` DDL/migrations and migration tests for direct-grid-compatible row shape.
- `workers/forcing_producer/store.py` `load_interp_weights` and `upsert_interp_weights` replacement semantics.
- Store/unit integration tests for exact one-cell mappings; no producer row generation in #543.

Must preserve:
- Existing IDW rows, uniqueness, load ordering, grid-signature stale-weight behavior, forecast/display/hindcast consumers, and current IDW producer tests remain compatible.
- `upsert_interp_weights` continues to replace exactly one `(source_id, grid_id, model_id)` scope at a time.
- #543 does not load direct-grid bindings, call producer materialization, write SHUD packages, or change lineage/idempotency behavior.

Must add/change:
- The persistence contract explicitly permits `method='direct_grid'` rows with `weight=1.0`, exactly one `grid_cell_id` per `(station_id, variable)`, and the current canonical `grid_signature`.
- Schema/check/index behavior is reviewed and updated only if existing DDL cannot represent that contract.
- Replacement semantics are documented and tested: direct-grid and IDW weights for the same `(source_id, grid_id, model_id)` scope are mutually replacing snapshots, not merged row sets.

Selected risk packs:
- Public API / CLI / script entry: not selected - no public route or CLI behavior changes in #543.
- Config / project setup: not selected - no deployment setting or environment switch is added.
- File IO / path safety / overwrite: not selected - #543 performs no file/object-store reads or writes.
- Schema / columns / units / field names: selected - `met.interp_weight.method`, `weight`, `grid_signature`, uniqueness, and migration compatibility are the issue boundary.
- Auth / permissions / secrets: not selected - no credential or permission boundary changes.
- Concurrency / shared state / ordering: selected - replace-one-scope semantics prevent stale IDW/direct-grid row mixing across reruns.
- Resource limits / large input / discovery: not selected - no discovery or bulk grid ingest is added in #543.
- Legacy compatibility / examples: selected - existing IDW weight persistence and downstream membership queries must continue unchanged.
- Error handling / rollback / partial outputs: selected - invalid mixed-scope upserts must keep the existing stable store error and not partially replace rows.
- Release / packaging / dependency compatibility: not selected - no new runtime dependency is expected.
- Documentation / migration notes: selected - OpenSpec records direct-grid persistence and non-goals for later producer issues.
- Geospatial / CRS / basin geometry: not selected - #543 stores already-authorized `grid_cell_id`s but does not validate geometry.
- Hydro-met time series / forcing windows: not selected - no valid-time or forcing-window logic changes.
- SHUD numerical runtime / conservation / NaN: not selected - no value generation.
- PostGIS / TimescaleDB domain behavior: selected - DB migration/DDL must represent direct-grid rows without breaking existing Timescale/PostGIS setup.
- Slurm production lifecycle / mock-vs-real parity: not selected - no Slurm surface.
- External hydro-met providers / snapshot reproducibility: selected - source/grid/model scope and grid signature preserve provider-grid identity for GFS/IFS.
- Run manifest / QC provenance: selected - persisted method/signature rows become later lineage inputs, but #543 does not write forcing lineage.
- Published NHMS artifacts / display identity: not selected - no published display artifact changes.

Boundary-surface checklist:
- Shared helper roots: `InterpolationWeight` and store replacement helpers only; no producer materialization changes.
- Public entrypoints: none in #543.
- Read surfaces: `load_interp_weights` must round-trip IDW and direct-grid rows with method, weight, grid cell, and grid signature intact.
- Write/delete/overwrite surfaces: `upsert_interp_weights` deletes and inserts only the supplied `(source_id, grid_id, model_id)` scope.
- Producer/consumer evidence boundaries: downstream consumers that join `met.interp_weight` for station membership remain method-agnostic unless a later issue explicitly changes them.
- Stale-state/idempotency boundaries: direct-grid rows carry `grid_signature`; same-scope replacement prevents stale IDW/direct-grid mixtures.
- Unchanged downstream consumers: forecast API/display coverage/hindcast station membership queries remain compatible with existing columns and indexes.

Invariant Matrix
Governing invariant: Persisted interpolation weights for one `(source_id, grid_id, model_id)` scope represent a single coherent spatial mapping snapshot, either IDW or direct-grid, and direct-grid rows are exact one-cell weights.
Source-of-truth identity/contract: `met.interp_weight` columns `source_id`, `grid_id`, `model_id`, `station_id`, `variable`, `grid_cell_id`, `weight`, `method`, and `grid_signature`, plus the unique key on `(source_id, grid_id, model_id, station_id, variable, grid_cell_id)`.
Surfaces:
- Producers: none changed in #543 - later issues will materialize direct-grid rows.
- Validators/preflight: existing store mixed-scope guard and migration checks.
- Storage/cache/query: `db/migrations/000005_met.sql`, follow-up migrations if needed, and `workers/forcing_producer/store.py`.
- Public routes/entrypoints: none changed in #543.
- Frontend/downstream consumers: forecast/display/hindcast membership joins must tolerate `method='direct_grid'` rows.
- Failure paths/rollback/stale state: mixed-scope upsert raises `MetStoreError` before replacement; same-scope replacement removes stale rows from prior mapping method.
- Evidence/audit/readiness: migration and store tests prove direct-grid row shape and replacement behavior.
Regression rows:
- upsert direct-grid rows with one `grid_cell_id` and `weight=1.0` per station/variable -> load returns `method='direct_grid'`, weight 1.0, and current `grid_signature`.
- replace IDW rows with direct-grid rows for the same `(source_id, grid_id, model_id)` -> old IDW rows are removed and no mixed-method snapshot remains.
- upsert rows spanning two `(source_id, grid_id, model_id)` scopes -> stable `MetStoreError` and no partial replacement.
- existing IDW producer/store tests -> unchanged behavior and load ordering.

## Issue #544 Fixture Addendum

Fixture level: expanded
Repair intensity: high
Project profile: NHMS

Change surface:
- `workers/forcing_producer/producer.py` direct-grid branch after #542 validation, before value generation.
- Materialization of validated `DirectGridForcingContract.stations` into one-cell `InterpolationWeight` rows.
- Producer tests proving direct-grid does not load legacy stations or call IDW neighbor search, and that legacy IDW remains unchanged.

Must preserve:
- Legacy assets with absent mapping metadata and explicit `forcing_mapping_mode="idw"` continue through existing station loading, IDW weight reuse/compute, package output, and readiness behavior.
- IDW weight reuse remains limited to homogeneous `method='idw'` snapshots; same-scope `direct_grid` rows or mixed-method rows are treated as stale and replaced by recomputed IDW weights in absent/explicit `idw` mode.
- #542 fail-closed validation remains before direct-grid materialization and before existing-ready reuse.
- #543 store replacement semantics remain the only persistence path for materialized direct-grid weights.
- #544 does not generate direct-grid station values, write SHUD packages, change lineage/idempotency, or stage runtime assets.

Must add/change:
- After direct-grid validation succeeds, the producer derives station-like binding metadata from the authoritative contract instead of `met.met_station`.
- Before direct-grid materialization, every canonical product in the run must have the same actual grid definition/order as the validated representative grid for its `(source_id, grid_id)` group.
- Direct-grid mode enforces the configured station-count limit against `DirectGridForcingContract.stations` before writing interpolation weights.
- For every bound station and output variable, the producer creates exactly one `InterpolationWeight` row with `method='direct_grid'`, `weight=1.0`, contract `grid_id`, station `grid_cell_id`, and the validated canonical `grid_signature`.
- Direct-grid materialization persists the same `(source_id, grid_id, model_id)` scope through `upsert_interp_weights` and then stops at the #544 boundary until exact value rows are implemented by #545.
- Direct-grid materialization must not call `compute_idw_weights()` or IDW station loading.
- Direct-grid `met.met_station` mirror rows are FK-compatible derived cache only: they use a non-legacy role and direct-grid cache properties so legacy IDW station loading excludes them.
- Direct-grid station mirror upserts are collision-safe: an existing `station_id` may be refreshed only when it is already the same basin and same direct-grid derived binding identity; non-derived, different-basin, or different-binding conflicts fail closed before interpolation weights are written.

Selected risk packs:
- Public API / CLI / script entry: selected - `ForcingProducer.produce` changes the explicit direct-grid success boundary observed by orchestration callers.
- Config / project setup: not selected - no environment or deployment switch is added.
- File IO / path safety / overwrite: not selected - #544 reuses #542 validated assets and does not add file/object-store reads or writes.
- Schema / columns / units / field names: selected - materialized rows must match `met.interp_weight` direct-grid method/weight/grid field semantics.
- Auth / permissions / secrets: not selected - no credential or permission boundary changes.
- Concurrency / shared state / ordering: selected - materialization replaces the mapping snapshot for one source/grid/model scope and must not mix with stale IDW rows.
- Resource limits / large input / discovery: selected - row creation is bounded by validated basin station bindings and output variables, not global grid discovery.
- Legacy compatibility / examples: selected - existing IDW tests and explicit `idw` mode remain unchanged.
- Error handling / rollback / partial outputs: selected - validation or persistence failure must not create ready forcing outputs or fall back to IDW.
- Release / packaging / dependency compatibility: not selected - no dependency/package change.
- Documentation / migration notes: selected - OpenSpec records that direct-grid materialization is the #544 boundary and exact values remain #545.
- Geospatial / CRS / basin geometry: selected - #544 consumes #542-validated `grid_cell_id`, `grid_id`, and `grid_signature`; station coordinate validation and SHUD coordinate preservation are #542/#546 concerns, not revalidated here.
- Hydro-met time series / forcing windows: not selected - no station value rows or valid-time data are generated in #544.
- SHUD numerical runtime / conservation / NaN: not selected - no numerical forcing values or runtime execution are produced.
- PostGIS / TimescaleDB domain behavior: selected - persisted direct-grid mapping rows must satisfy #543 DDL/store constraints.
- Slurm production lifecycle / mock-vs-real parity: not selected - no Slurm surface.
- External hydro-met providers / snapshot reproducibility: selected - source/grid scope and grid signature bind GFS/IFS canonical grid identity.
- Run manifest / QC provenance: selected - materialized mappings carry binding/grid identities needed for later lineage, but #544 does not write lineage.
- Published NHMS artifacts / display identity: not selected - no published display artifact is created.

Boundary-surface checklist:
- Shared helper roots: direct-grid contract helpers, producer direct-grid branch, and interpolation-weight store helper only.
- Public entrypoints: `ForcingProducer.produce` returns a stable not-yet-implemented/direct-value-boundary failure after materialization until #545.
- Read surfaces: repository contract loader and validation assets already covered by #542; #544 also reads canonical product grid definitions/coordinates for every product before direct-grid materialization.
- Write/delete/overwrite surfaces: `upsert_interp_weights` replaces only the validated direct-grid `(source_id, grid_id, model_id)` scope.
- Producer/consumer evidence boundaries: persisted direct-grid weights become derived cache from the manifest, not a new authority.
- Stale-state/idempotency boundaries: materialization must run after validation and before any ready-output reuse that could mask changed direct-grid bindings.
- Unchanged downstream consumers: IDW package generation, forecast API/display membership joins, and current store tests remain compatible.

Invariant Matrix
Governing invariant: Once a direct-grid contract has passed validation, the producer's spatial mapping snapshot for the current source/grid/model must be the exact manifest binding: one station-variable row to one grid cell, weight 1.0, with no IDW fallback.
Source-of-truth identity/contract: `DirectGridForcingContract` fields `stations[*].station_id`, `stations[*].grid_cell_id`, `grid_id`, `grid_signature`, selected `source_id`, selected `model_id`, and configured output variables.
Surfaces:
- Producers: `ForcingProducer.produce` direct-grid branch and materialization helper.
- Validators/preflight: #542 validation remains mandatory before materialization.
- Storage/cache/query: `InterpolationWeight` rows persisted through repository `upsert_interp_weights`.
- Public routes/entrypoints: orchestration sees a stable direct-grid boundary failure until #545, with no ready outputs.
- Frontend/downstream consumers: none in #544 - no API/frontend payload changes.
- Failure paths/rollback/stale state: invalid contracts fail before materialization; persistence errors propagate as failed forcing with no ready package.
- Evidence/audit/readiness: tests prove row shape, no IDW calls, no legacy station load, and IDW compatibility.
Regression rows:
- valid direct-grid contract with two stations and matching canonical grid -> `upsert_interp_weights` receives `len(stations) * len(output_variables)` direct-grid rows, no ready output is written, and production stops at the stable #545 value-generation boundary.
- direct-grid station bound to `cell-001` for variable `Precip` -> persisted row uses `grid_cell_id='cell-001'`, `method='direct_grid'`, `weight=1.0`, and validated `grid_signature`.
- explicit direct-grid contract -> `load_met_stations` and `compute_idw_weights()` are not called.
- direct-grid contract with a non-representative canonical product whose actual ordered grid points differ under the same source/grid metadata -> validation fails before `upsert_interp_weights`, with no ready outputs and no IDW fallback.
- direct-grid contract with valid two-station bindings and `max_station_count=1` -> station-count validation fails before materialization, with no legacy station load, no IDW fallback, and no ready outputs.
- direct-grid validation mismatch -> no interpolation weights are written and no ready output is produced.
- direct-grid mirror `station_id` collides with an existing non-derived or different-binding station -> materialization fails before `met.interp_weight` writes and does not overwrite the existing station row.
- direct-grid mirror for the same basin and binding identity is ensured repeatedly -> mirror refresh is idempotent.
- direct-grid mirror rows exist in `met.met_station` -> absent/explicit `idw` mode still loads only legacy forcing-grid stations and recomputes IDW weights from those stations.
- direct-grid mapping persistence failure -> no package, forcing version, station timeseries, or ready cycle state is written, and no IDW fallback occurs.
- legacy absent/explicit `idw` asset -> existing IDW path still computes/reuses homogeneous IDW weights and writes outputs; same-scope `direct_grid` cached rows are recomputed and replaced as IDW.

## Issue #545 Fixture Addendum

Fixture level: expanded
Repair intensity: high
Project profile: NHMS

Change surface:
- `workers/forcing_producer/producer.py` direct-grid branch after #544 mapping materialization.
- Direct-grid station row generation from canonical products using exact bound `grid_cell_id` values.
- Tests for exact value equality, missing bound cells, canonical conversion preservation, and legacy IDW compatibility.

Must preserve:
- Legacy absent/explicit `idw` behavior continues to load legacy stations, compute/reuse IDW weights, write current outputs, and ignore direct-grid derived station mirrors.
- #542 validation, #544 station mirror isolation, direct-grid weight shape, all-product grid checks, and no-IDW fallback invariants remain intact.
- Canonical physical conversion semantics remain mandatory: direct-grid consumes canonical products, preserves precipitation/unit/radiation/wind/RH/pressure handling semantics, and does not read raw IFS/GFS products.
- #545 does not write SHUD packages, ready forcing versions, forcing lineage/idempotency freshness, or runtime staging outputs.

Must add/change:
- After direct-grid mappings are materialized, the producer builds direct-grid station objects from the validated contract and generates `ForcingTimeseriesRow` values from canonical products.
- For every station/variable/valid time, each value equals the canonical value from the station's bound `grid_cell_id` after the same canonical-to-forcing conversion path used by IDW.
- Canonical product reads use/retain required direct-grid `grid_cell_id`s through the existing required-cell retention path where supported; deeper lazy/indexed NetCDF reads are not claimed unless task 2.5 is implemented.
- Missing bound `grid_cell_id` in any required canonical product fails closed before package/version/timeseries/ready outputs and does not fall back to IDW.
- Non-finite canonical values at a bound cell fail through the existing non-finite interpolated-value guard before package/version/timeseries/ready outputs.
- Direct-grid generation stops at the #546 package/lineage boundary after rows/components are generated, until package persistence is implemented.

Concrete #545 value fixture:
- Station `qhh_forc_001` binds `grid_cell_id='0'`; station `qhh_forc_002` binds `grid_cell_id='1'`.
- At one generated valid time, canonical cell values are: PRCP `1.0/2.0` mm/day, TEMP `10.0/20.0` degC, RH `0.50/0.75`, net radiation `100.0/200.0`, wind U `3.0/6.0`, wind V `4.0/8.0`.
- Expected direct-grid rows are: `qhh_forc_001` PRCP `1.0`, TEMP `10.0`, RH `0.50`, Rn `100.0`, wind `5.0`; `qhh_forc_002` PRCP `2.0`, TEMP `20.0`, RH `0.75`, Rn `200.0`, wind `10.0`.

Selected risk packs:
- Public API / CLI / script entry: selected - `ForcingProducer.produce` explicit direct-grid behavior advances from mapping-only to row-generation boundary.
- Config / project setup: not selected - no deployment setting or environment switch is added.
- File IO / path safety / overwrite: not selected - #545 only reads already-selected canonical products and writes no packages.
- Schema / columns / units / field names: selected - generated rows must use existing `ForcingTimeseriesRow` variable/unit/source fields and not add SHUD CSV `Press`.
- Auth / permissions / secrets: not selected - no credential or permission boundary changes.
- Concurrency / shared state / ordering: selected - direct-grid rows must be generated only after validated mapping snapshot and before any ready-output side effects.
- Resource limits / large input / discovery: selected - canonical reads must use/retain required bound cells where supported by the current reader; no global grid search or IDW neighbor discovery.
- Legacy compatibility / examples: selected - IDW row generation and output behavior remain unchanged.
- Error handling / rollback / partial outputs: selected - missing cells or non-finite values fail with no package/version/timeseries/ready state.
- Release / packaging / dependency compatibility: not selected - no new dependency/package change.
- Documentation / migration notes: selected - OpenSpec records #545 package/lineage non-goals.
- Geospatial / CRS / basin geometry: selected - value lookup uses validated `grid_cell_id` rather than coordinate proximity.
- Hydro-met time series / forcing windows: selected - valid-time planning, fallback time plan, and canonical variable mapping must match existing forcing semantics.
- SHUD numerical runtime / conservation / NaN: selected - generated values must be finite and use existing unit/conversion semantics; runtime staging remains later.
- PostGIS / TimescaleDB domain behavior: not selected - #545 does not persist `met.forcing_station_timeseries`.
- Slurm production lifecycle / mock-vs-real parity: not selected - no Slurm surface.
- External hydro-met providers / snapshot reproducibility: selected - values remain canonical GFS/IFS products bound by grid signature and source scope.
- Run manifest / QC provenance: selected - components/row evidence are prepared for later lineage, but #545 does not write lineage.
- Published NHMS artifacts / display identity: not selected - no published display artifact is created.

Boundary-surface checklist:
- Shared helper roots: direct-grid branch, `_generate_timeseries_streaming`, `_read_canonical_field`, and direct-grid station conversion helper.
- Public entrypoints: `ForcingProducer.produce` returns a stable #546 package/lineage boundary failure after row generation.
- Read surfaces: canonical products selected by source/cycle, with required direct-grid cell retention where supported by the current field-read path.
- Write/delete/overwrite surfaces: no direct-grid package/version/timeseries writes in #545; `met.interp_weight` materialization remains #544 behavior.
- Producer/consumer evidence boundaries: generated rows/components are in-memory evidence for #546, not ready artifacts.
- Stale-state/idempotency boundaries: existing-ready reuse still cannot mask invalid or changed direct-grid bindings.
- Unchanged downstream consumers: legacy IDW package generation and station mirror exclusion remain compatible.

Invariant Matrix
Governing invariant: A direct-grid station value is exactly the canonical forcing value for its validated bound `grid_cell_id`, after the same canonical physical conversions as IDW, and no IDW spatial interpolation or fallback can affect explicit direct-grid rows.
Source-of-truth identity/contract: validated `DirectGridForcingContract.stations[*].grid_cell_id`, direct-grid `InterpolationWeight` rows, selected canonical products, product valid-time plan, and `OUTPUT_UNITS`.
Surfaces:
- Producers: `ForcingProducer.produce` direct-grid branch and row-generation boundary.
- Validators/preflight: #542/#544 validation and direct-grid weight materialization remain prerequisites.
- Storage/cache/query: direct-grid weights are read/generated as derived cache; no station timeseries persistence in #545.
- Public routes/entrypoints: orchestration sees a stable #546 boundary failure with no ready outputs after rows are generated.
- Frontend/downstream consumers: none in #545 - no API/frontend payload changes.
- Failure paths/rollback/stale state: missing bound cells, mismatched grids, non-finite values, or package-boundary failures leave no ready package/version/timeseries.
- Evidence/audit/readiness: tests prove exact values, required-cell retention, missing-cell failure, no IDW calls, and IDW compatibility.
Regression rows:
- valid direct-grid contract with station `grid_cell_id='0'` and canonical PRCP/TEMP/RH/Rn/Wind values -> generated station rows equal bound cell values after existing conversions and stop at #546 boundary.
- two-station fixture with canonical cells `0` and `1` as listed above -> rows contain PRCP/TEMP/RH/Rn/wind values `1/10/0.50/100/5` and `2/20/0.75/200/10` at the planned valid time.
- direct-grid valid-time plan with the same GFS cycle/products as IDW -> generated row valid times match `_expected_forcing_valid_times` and component set matches selected canonical products.
- direct-grid product lacks a bound `grid_cell_id` -> production fails before package/version/timeseries writes and no IDW fallback occurs.
- direct-grid bound cell has non-finite canonical value -> production fails before package/version/timeseries writes and no IDW fallback occurs.
- direct-grid required-cell set contains only bound cells -> `_read_canonical_field` receives the direct-grid required cells and retains those values where supported; deeper lazy/indexed NetCDF reads remain task 2.5.
- direct-grid wind rows -> wind speed is derived from bound U/V canonical cells using existing `wind_speed` logic.
- legacy absent/explicit `idw` asset -> existing IDW generation and package output remain unchanged.

## Issue #546 Fixture Addendum

Fixture level: expanded
Repair intensity: high
Project profile: NHMS

Change surface:
- `workers/forcing_producer/producer.py` direct-grid branch after #545 row/component generation.
- SHUD package writing, package manifest and forcing version lineage metadata, `met.forcing_station_timeseries` persistence, and existing-ready freshness checks.
- Producer tests for direct-grid package shape, lineage identity, idempotency, stale binding invalidation, pressure handling, no-IDW fallback, and legacy IDW compatibility.

Must preserve:
- Legacy absent/explicit `idw` behavior continues to use current station loading, IDW weights, SHUD package output, lineage, idempotency, and finite-value validation.
- #542/#544/#545 fail-closed validation, direct-grid mirror isolation, exact one-cell mapping, exact canonical cell values, required-cell retention, and no-IDW fallback invariants remain intact.
- Direct-grid per-station SHUD CSV files keep the SHUD five-variable contract `Precip`, `Temp`, `RH`, `Wind`, and `RN`; pressure may be persisted in station timeseries and lineage/metadata but must not appear in SHUD station CSV columns.
- #546 does not change SHUD runtime staging, does not validate staged `.sp.att` `FORC` against `.tsd.forc` IDs, and does not alter the runtime fallback single-station rewrite path; those are #547.

Must add/change:
- After direct-grid rows/components are generated, direct-grid production writes the standard SHUD forcing package instead of stopping at the #546 boundary.
- Direct-grid `.tsd.forc` IDs, coordinates, filenames, and station count come from the validated direct-grid station contract and rewritten `.sp.att` `FORC` ownership.
- Direct-grid package manifest and `met.forcing_version.lineage_json` record `forcing_mapping_mode='direct_grid'`, `spatial_mapping_method='direct_grid'`, binding URI/checksum, model input package id, `.sp.att` path/checksum, applicable source ids, grid id, grid signature, direct-grid station signature, and canonical input signature.
- Direct-grid `met.forcing_station_timeseries` rows are persisted for every generated variable, station, and valid time; `Press` remains persisted there when generated but is excluded from SHUD station CSV files.
- Existing-ready freshness checks compare mapping mode and direct-grid contract identity. Recompute-able drift, such as binding URI, `.sp.att` checksum/path, applicable source ids, direct-grid station signature, canonical input signature, or mapping mode, invalidates prior ready outputs for the same source/model/cycle and replaces the same forcing version. Identity drift that would require overwriting an existing derived direct-grid station mirror with a different binding checksum, model input package id, or canonical grid identity/signature remains fail-closed before weight writes or ready publication, preserving the #544 mirror-collision invariant.
- Parent pending-version creation, package file writes, package manifest write/checksum, component child rows, station timeseries child rows, lineage persistence, and finalize/readiness failures leave no finalized ready output and remain retryable through existing incomplete-version replacement semantics.

Selected risk packs:
- Public API / CLI / script entry: selected - `ForcingProducer.produce` explicit direct-grid behavior changes from a boundary failure to a successful ready output observed by orchestration callers.
- Config / project setup: not selected - no deployment setting or environment switch is added.
- File IO / path safety / overwrite: selected - #546 writes package files, SHUD station CSVs, and package manifests; filenames come from validated direct-grid contract fields and must remain safe.
- Schema / columns / units / field names: selected - package manifest/lineage fields, SHUD CSV columns, `met.forcing_station_timeseries`, and `met.forcing_version` lineage are the behavior contract.
- Auth / permissions / secrets: not selected - no credential or permission boundary changes.
- Concurrency / shared state / ordering: selected - direct-grid ready publication must preserve parent/child/finalize ordering and retry semantics.
- Resource limits / large input / discovery: selected - station/time/row/manifest limits must apply before package writes.
- Legacy compatibility / examples: selected - IDW package generation and existing-ready reuse remain unchanged.
- Error handling / rollback / partial outputs: selected - failed validation/package/persistence cannot produce a finalized ready direct-grid version or fallback to IDW.
- Release / packaging / dependency compatibility: not selected - no new dependency/package change.
- Documentation / migration notes: selected - OpenSpec records package/lineage boundary and defers runtime staging to #547.
- Geospatial / CRS / basin geometry: selected - `.tsd.forc` station IDs/coordinates must match validated direct-grid contract and `.sp.att` ownership.
- Hydro-met time series / forcing windows: selected - persisted station timeseries and package time ranges must match generated direct-grid rows.
- SHUD numerical runtime / conservation / NaN: selected - package output must use finite canonical-converted values and keep pressure out of SHUD station CSVs.
- PostGIS / TimescaleDB domain behavior: selected - `met.forcing_version`, component rows, and station timeseries persistence/readiness are changed for direct-grid success.
- Slurm production lifecycle / mock-vs-real parity: not selected - no Slurm surface.
- External hydro-met providers / snapshot reproducibility: selected - lineage must bind output to provider/source scope and canonical grid signature.
- Run manifest / QC provenance: selected - package manifest and DB lineage must carry direct-grid binding/canonical identity.
- Published NHMS artifacts / display identity: selected - a direct-grid forcing package becomes a published worker artifact that downstream stages will consume.

Boundary-surface checklist:
- Shared helper roots: direct-grid branch, `_write_outputs_and_records`, package/manifest lineage helpers, `_existing_forcing_version_is_current`, SHUD package formatting, and direct-grid station conversion helper.
- Public entrypoints: `ForcingProducer.produce` returns `forcing_ready` or `already_done` for valid direct-grid assets.
- Read surfaces: validated direct-grid contract, canonical products, existing forcing version/manifest, and child-row completeness checks.
- Write/delete/overwrite surfaces: object-store forcing package files/manifest, `met.forcing_version`, `met.forcing_version_component`, and `met.forcing_station_timeseries`.
- Staging/publish/rollback surfaces: parent forcing version is inserted with pending checksum, files/children are replaced, then finalized with manifest checksum; failures must be retryable and not finalized.
- Producer/consumer evidence boundaries: package manifest and DB lineage must bind the same direct-grid contract identity, grid signature, canonical inputs, and output files.
- Stale-state/idempotency boundaries: existing-ready reuse must be invalidated by mapping mode or direct-grid contract identity drift; direct-grid mirror identity collisions are invalidation failures, not automatic mirror overwrites.
- Unchanged downstream consumers: legacy IDW package consumers, runtime staging behavior, and frontend/API payloads remain compatible.

Invariant Matrix
Governing invariant: A ready direct-grid forcing version is publishable only when its package files, station timeseries, lineage, manifest, and freshness key all bind to the same validated direct-grid contract and canonical product grid identity.
Source-of-truth identity/contract: `DirectGridForcingContract` fields `forcing_mapping_mode`, `binding_uri`, `binding_checksum`, `model_input_package_id`, `sp_att_path`, `sp_att_checksum`, `applicable_source_ids`, `grid_id`, `grid_signature`, `stations[*].station_id`, `stations[*].shud_forcing_index`, `stations[*].forcing_filename`, `stations[*].grid_cell_id`, selected canonical product ids/signature, and output file checksums.
Surfaces:
- Producers: `ForcingProducer.produce` direct-grid branch and package writing path.
- Validators/preflight: #542/#544 validation plus #545 value generation remain prerequisites before ready publication.
- Storage/cache/query: direct-grid station mirror, `met.interp_weight`, `met.forcing_version`, `met.forcing_version_component`, and `met.forcing_station_timeseries`.
- Public routes/entrypoints: orchestration observes `forcing_ready`/`already_done` only for a current direct-grid identity.
- Frontend/downstream consumers: package manifest and object-store URIs remain the stable forcing package contract; runtime staging validation is #547.
- Failure paths/rollback/stale state: invalid or stale direct-grid identity, package write failure, child-row failure, or manifest mismatch leaves no finalized ready output and no IDW fallback.
- Evidence/audit/readiness: tests inspect object-store package files, DB lineage, manifest lineage, persisted timeseries, idempotency reuse, stale invalidation, and failure side effects.
Regression rows:
- valid direct-grid contract after #545 row generation -> production writes SHUD `.tsd.forc`, per-station SHUD CSVs, package manifest, forcing version, components, and station timeseries, then marks cycle `forcing_ready`.
- station `qhh_forc_001` with `shud_forcing_index=1` and contract filename -> `.tsd.forc` contains ID `1`, matching coordinates, and per-station CSV path from the contract.
- direct-grid SHUD station CSV -> columns are time plus `Precip`, `Temp`, `RH`, `Wind`, `RN`; `Press` is absent from the CSV and present in persisted station timeseries.
- direct-grid lineage/manifest -> contains mapping mode, spatial mapping method, binding URI/checksum, model input package id, `.sp.att` path/checksum, applicable source ids, grid id/signature, station signature, canonical input signature, and output file checksums.
- rerun unchanged direct-grid source/model/cycle -> returns existing-ready/already-done behavior and does not create duplicate ready forcing versions.
- direct-grid binding URI, `.sp.att` path/checksum, applicable source ids, station signature, canonical input signature, or mapping mode changes -> existing ready output is considered stale and the same forcing version is recomputed/replaced when no mirror identity collision is introduced.
- direct-grid binding checksum, model input id, `grid_id`, or grid signature changes that collide with existing derived direct-grid mirror station ids -> production fails closed before weight writes or ready publication; no stale `already_done`, IDW fallback, or finalized wrong package is allowed.
- parent pending-version creation failure -> no package files or child rows are published and retry can complete normally.
- package file or package manifest write/checksum failure -> no finalized ready output; retry replaces package files/manifest without duplicate ready versions.
- component child-row or station-timeseries child-row failure -> parent remains pending/unfinalized; retry replaces child rows without duplicates or stale orphaned rows.
- lineage persistence or finalize/readiness failure -> no finalized ready output; retry completes and binds lineage/package manifest/checksum consistently.
- explicit direct-grid validation/value/package failure -> no legacy station loading, no IDW neighbor search, no fallback ready output.
- legacy absent/explicit `idw` asset -> existing package, lineage, idempotency, and finite-value validation behavior remains unchanged.

## Issue #547 Fixture Addendum

Fixture level: expanded
Repair intensity: high
Project profile: NHMS

Change surface:
- `workers/shud_runtime/runtime.py` SHUD project staging after model and forcing package files are staged.
- Runtime tests for direct-grid package lineage, standard multi-station forcing staging, `.sp.att FORC` validation, and legacy fallback compatibility.

Must preserve:
- Legacy packages without direct-grid lineage may still use the existing fallback `forcing_debug.csv` / `forcing.tsd.forc` single-station staging and `_remap_sp_att_forcing(..., forcing_index=1)`.
- Existing standard SHUD package staging for IDW or lineage-absent packages remains compatible.
- Existing checksum/path/symlink validation remains unchanged.
- #547 does not change forcing production, direct-grid binding validation, or package publication semantics.

Must add/change:
- Runtime detects direct-grid forcing from forcing/package lineage metadata (`forcing_mapping_mode='direct_grid'` or `spatial_mapping_method='direct_grid'`) rather than by guessing from filenames alone.
- Direct-grid runtime staging requires the standard `shud/qhh.tsd.forc` multi-station package path; missing standard SHUD files fail closed instead of falling back to `forcing_debug.csv` / `forcing.tsd.forc`.
- Runtime validates staged project `.sp.att` `FORC` values against the staged `.tsd.forc` `ID` set for direct-grid packages.
- Runtime validation failure occurs before SHUD execution and before a run can be marked `staged` / `running`.

Selected risk packs:
- Public API / CLI / script entry: selected - `SHUDRuntime.prepare_workspace` / `execute` is the runtime boundary used by orchestration.
- Config / project setup: not selected - no deployment setting or environment switch is added.
- File IO / path safety / overwrite: selected - model package and forcing package files share a workspace, and direct-grid must not let fallback staging overwrite `.sp.att` ownership.
- Schema / columns / units / field names: selected - `.tsd.forc` `ID` and `.sp.att` `FORC` are the runtime file contract.
- Auth / permissions / secrets: not selected - no credential or permission boundary changes.
- Concurrency / shared state / ordering: selected - staging must fail before status transitions to `staged` / `running`.
- Resource limits / large input / discovery: selected - validation parses staged files without unbounded object-store reads.
- Legacy compatibility / examples: selected - IDW and lineage-absent runtime staging remain unchanged.
- Error handling / rollback / partial outputs: selected - direct-grid validation failures must not leave a successful staged run state.
- Release / packaging / dependency compatibility: not selected - no new dependency/package change.
- Documentation / migration notes: selected - #547 records runtime migration rules in OpenSpec.
- Geospatial / CRS / basin geometry: selected - `.sp.att FORC` ownership binds triangles to direct-grid forcing stations.
- Hydro-met time series / forcing windows: selected - standard SHUD package time shifting remains unchanged.
- SHUD numerical runtime / conservation / NaN: selected - wrong `FORC` ownership would feed triangles from the wrong forcing station.
- PostGIS / TimescaleDB domain behavior: not selected - no DB schema/query change.
- Slurm production lifecycle / mock-vs-real parity: selected - tests use mock runtime but validate the same staged file contract before solver execution.
- External hydro-met providers / snapshot reproducibility: not selected - provider products are producer-side.
- Run manifest / QC provenance: selected - runtime consumes forcing lineage/manifest metadata to select the safe staging path.
- Published NHMS artifacts / display identity: not selected - no display artifact change.

Boundary-surface checklist:
- Shared helper roots: `_prepare_shud_project_forcing`, `_stage_standard_shud_forcing`, `_write_shud_forcing_files`, `_remap_sp_att_forcing`, `.tsd.forc` parsing, `.sp.att` parsing/validation, and forcing package manifest metadata helpers.
- Public entrypoints: `SHUDRuntime.prepare_workspace` and `SHUDRuntime.execute`.
- Read surfaces: runtime manifest forcing block, package manifest, staged `shud/qhh.tsd.forc`, per-station CSV files, and staged `<project>.sp.att`.
- Write/delete/overwrite surfaces: generated `<project>.tsd.forc`, copied per-station CSVs, shifted project time inputs, and legacy fallback `.sp.att` rewrite.
- Staging/publish/rollback surfaces: workspace preparation before `hydro_run` status transitions.
- Producer/consumer evidence boundaries: producer lineage/manifest mapping mode, `.tsd.forc` station IDs, and `.sp.att FORC` values.
- Stale-state/idempotency boundaries: stale staged `.sp.att` ownership must not be hidden by fallback rewrite.
- Unchanged downstream consumers: non-project command style, legacy IDW fallback, and frontend/API payloads.

Invariant Matrix - #547 runtime staging
Governing invariant: A runtime manifest whose forcing package declares direct-grid may reach SHUD execution only when staged `.sp.att FORC` ownership references the multi-station `.tsd.forc` IDs from the package, and runtime never rewrites that ownership through the legacy single-station fallback path.
Source-of-truth identity/contract: forcing/package lineage `forcing_mapping_mode` / `spatial_mapping_method`, staged `shud/qhh.tsd.forc` `ID` column, per-station CSV filenames, and staged `<project>.sp.att` `FORC` column.
Surfaces:
- Producers: #546 emits standard `shud/qhh.tsd.forc` packages and direct-grid lineage consumed by runtime.
- Validators/preflight: runtime validates direct-grid lineage, standard SHUD package presence, CSV presence/time axis, and `.sp.att FORC` range before SHUD execution.
- Storage/cache/query: object-store staging and checksum verification are unchanged; validation uses staged files.
- Public routes/entrypoints: `prepare_workspace` fails closed before `execute` marks the run `staged` / `running`.
- Frontend/downstream consumers: no API/frontend changes.
- Failure paths/rollback/stale state: direct-grid packages missing standard SHUD files or containing out-of-range `.sp.att FORC` fail closed without fallback rewrite.
- Evidence/audit/readiness: tests inspect staged files and error codes for direct-grid and legacy paths.
Regression rows:
- direct-grid package with standard `shud/qhh.tsd.forc` and `.sp.att FORC` values in the ID set -> stages multi-station `<project>.tsd.forc` and copied CSVs without fallback remap.
- direct-grid package missing standard `shud/qhh.tsd.forc` but containing legacy fallback `forcing_debug.csv` or `forcing.tsd.forc` -> fails closed; no `_remap_sp_att_forcing(..., forcing_index=1)` rewrite.
- direct-grid package whose staged `.sp.att FORC` references an ID absent from `.tsd.forc` -> fails closed before SHUD execution.
- legacy package without direct-grid lineage and without standard SHUD package -> existing fallback single-station rewrite behavior remains unchanged.
- package with absent/explicit `idw` lineage and standard SHUD package -> existing standard staging remains unchanged.

## Issue #548 Fixture Addendum

Fixture level: expanded
Repair intensity: medium
Project profile: NHMS

Change surface:
- Compact end-to-end pytest fixture spanning direct-grid mode resolution, validation, exact value generation, package publication, lineage/idempotency, and SHUD runtime staging.
- Documentation under the forcing/model-asset source-of-truth docs for migration, rollback, source scope, grid signature, `Press`, and canonical conversion requirements.
- CI targeted-test selection only if needed to keep the compact fixture in the PR merge gate without selecting slow whole-suite runs.

Must preserve:
- No new direct-grid runtime or producer semantics beyond the behavior already implemented by #540-#547.
- Legacy absent/explicit `idw` forcing behavior, package shape, and runtime fallback compatibility remain unchanged.
- The compact fixture must not rely on live GFS/IFS downloads, Slurm, external object stores, a full national basin, or wall-clock-heavy integration setup.
- Documentation must not imply that direct-grid can skip canonical conversion or raw IFS/GFS physical normalization.

Must add/change:
- A compact fixture that uses a minimal two-station direct-grid asset and canonical product values to prove the full implemented path: mode resolution, binding validation, direct-grid weight/value mapping, SHUD package files, lineage/manifest metadata, runtime staging validation, and idempotency.
- The fixture verifies exact values for the two-station canonical-cell example used in #545: station `qhh_forc_001` bound to cell `0` and station `qhh_forc_002` bound to cell `1`, including wind speed from U/V and SHUD CSV exclusion of `Press`.
- The fixture verifies the runtime consumes the standard multi-station package and validates `.sp.att FORC` references against `.tsd.forc` IDs without legacy fallback rewrite.
- Documentation clarifies dual-mode operation, migration workflow, rollback by model/input asset version, direct-grid source scope and grid signature applicability, `Press` handling, and why canonical conversion remains mandatory.
- Targeted test commands and evidence are recorded in PR evidence.

Selected risk packs:
- Public API / CLI / script entry: selected - the fixture spans producer and runtime entrypoints used by orchestration.
- Config / project setup: not selected - no deployment config or global mode toggle is introduced.
- File IO / path safety / overwrite: selected - compact fixture writes package files and stages runtime workspace files; docs must preserve safe package/source-of-truth boundaries.
- Schema / columns / units / field names: selected - SHUD CSV columns, manifest/lineage fields, `Press` exclusion, and `.sp.att FORC` / `.tsd.forc ID` contracts are verified.
- Auth / permissions / secrets: not selected - no credential or permission surface changes.
- Concurrency / shared state / ordering: selected - fixture must prove idempotency/rerun behavior without duplicate ready outputs.
- Resource limits / large input / discovery: selected - the fixture must remain compact and must not reintroduce broad CI runtime.
- Legacy compatibility / examples: selected - docs and fixture must keep IDW/direct-grid separation explicit.
- Error handling / rollback / partial outputs: selected - docs describe rollback by asset version; fixture covers fail-closed runtime ownership validation through the already-implemented runtime gate.
- Release / packaging / dependency compatibility: not selected - no new dependency should be required.
- Documentation / migration notes: selected - #548 is the documentation closure issue.
- Geospatial / CRS / basin geometry: selected - direct-grid station ownership and `grid_cell_id`/`grid_signature` applicability are documented and fixture-bound.
- Hydro-met time series / forcing windows: selected - fixture verifies valid-time/package timeseries shape at compact scale.
- SHUD numerical runtime / conservation / NaN: selected - fixture verifies exact canonical-converted values and avoids wrong station ownership.
- PostGIS / TimescaleDB domain behavior: selected only through existing fake/repository test surfaces; no new schema migration.
- Slurm production lifecycle / mock-vs-real parity: selected - runtime staging is tested before solver execution without submitting real Slurm jobs.
- External hydro-met providers / snapshot reproducibility: selected - docs must explain GFS/IFS source scope, grid signature, and canonical conversion.
- Run manifest / QC provenance: selected - fixture verifies lineage/manifest identity.
- Published NHMS artifacts / display identity: selected - the direct-grid forcing package is the artifact consumed by runtime.

Boundary-surface checklist:
- Shared helper roots: direct-grid producer fixture helpers, compact object-store/repository fakes, runtime staging fixture helpers, and CI test selector if changed.
- Public entrypoints: `ForcingProducer.produce` and `SHUDRuntime.prepare_workspace` / `execute` staging path.
- Read surfaces: direct-grid binding contract, compact canonical products, model input package files, forcing package manifest, and runtime manifest.
- Write/delete/overwrite surfaces: compact object-store package outputs, forcing version records in fakes, station timeseries records, runtime workspace staging files.
- Staging/publish/rollback surfaces: producer package publication/idempotency and runtime pre-execution staging.
- Producer/consumer evidence boundaries: producer manifest/lineage identity must be the same identity consumed by runtime direct-grid detection.
- Stale-state/idempotency boundaries: unchanged direct-grid rerun must not duplicate ready forcing versions or mutate station/package identity unexpectedly.
- Unchanged downstream consumers: legacy IDW docs and tests remain valid; #548 does not change frontend/API payloads.

Invariant Matrix - #548 E2E/docs closure
Governing invariant: The documented migrated direct-grid flow and the compact E2E fixture must describe and prove the same implemented contract: explicit per-asset direct-grid selection, exact canonical grid-cell values, standard SHUD package output, lineage-bound runtime staging, and asset-version rollback without IDW fallback.
Source-of-truth identity/contract: selected model/input asset manifest, direct-grid binding URI/checksum, model input package id, `.sp.att` checksum/path, `applicable_source_ids`, `grid_id`, `grid_signature`, station `shud_forcing_index`/`grid_cell_id`, package manifest lineage, and runtime-staged `.tsd.forc` IDs.
Surfaces:
- Producers: compact direct-grid production fixture from mode resolution through ready package/idempotency.
- Validators/preflight: direct-grid contract, grid/source scope, `.sp.att FORC`, and runtime package staging checks.
- Storage/cache/query: fake repository/object-store records for interpolation weights, forcing version, components, and station timeseries.
- Public routes/entrypoints: no API changes; worker/runtime entrypoints are exercised directly.
- Frontend/downstream consumers: no frontend behavior change; SHUD runtime package contract remains the downstream consumer boundary.
- Failure paths/rollback/stale state: docs define rollback by asset version; fixture covers no fallback rewrite and unchanged rerun idempotency.
- Evidence/audit/readiness: PR evidence records targeted pytest commands, docs touched, and CI status.
Regression rows:
- compact direct-grid asset with two stations and canonical cells `0`/`1` -> producer resolves `direct_grid`, validates binding/source/grid/model identity, writes exact SHUD values, lineage, package manifest, and station timeseries.
- compact rerun with unchanged direct-grid identity -> returns existing-ready/already-done behavior without duplicate ready forcing versions or duplicate station timeseries.
- runtime staging of the produced package with `.sp.att FORC` values `1` and `2` -> stages multi-station `.tsd.forc` and station CSVs, validates ownership, and does not call the legacy single-station fallback rewrite.
- documentation describes migration/rollback by asset version, source scope and grid signature checks, `Press` persistence-but-not-SHUD-CSV behavior, and canonical conversion as mandatory before direct-grid lookup.
