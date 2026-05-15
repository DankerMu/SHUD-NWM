## Context

Issue #122 is part of Epic #120. The M3 DAG ends with `publish`, and `infra/sbatch/publish_tiles.sbatch` calls `nhms-pipeline publish-tiles --cycle-id "$NHMS_CYCLE_ID"`. The CLI currently returns `failed_publish` with `publish_tiles_not_implemented`, so production-like runs cannot complete with delivery evidence.

Fixture level: expanded
Project profile: other

Change surface:
- CLI entrypoint: `services/orchestrator/cli.py`
- Publish service: `services/tile-publisher` or importable equivalent module
- Orchestrator publish metadata/status mapping: `services/orchestrator/chain.py`
- Slurm template: `infra/sbatch/publish_tiles.sbatch`
- Delivery persistence: `map.tile_layer`, `map.tile_cache`, or documented object-store tile metadata
- Tests and docs for publish success/failure observability

Must preserve:
- `publish-tiles` requires an explicit `--cycle-id`; invalid or missing IDs must not publish.
- A publish failure must keep cycle/job observability through `failed_publish`, error code/message, pipeline events, and log URI where available.
- Partial basin behavior in the M3 orchestrator must preserve excluded basin metadata and only publish successful basin products.
- Existing flood alert/map consumers must keep using documented product-ready states and delivery fields, including `frequency_done` / `published` readiness, `/api/v1/tiles/flood-return-period` discovery, `layer_id`, `tile_format`, `tile_uri_template`, `published_flag`, and `publish_time`.
- Re-running `publish-tiles --cycle-id <id>` for the same publishable cycle must be idempotent: it may update deterministic metadata but must not create duplicate logical layers or conflicting cache rows.

Must add/change:
- Successful publish creates verifiable delivery metadata/artifacts instead of a placeholder response.
- CLI JSON output names the cycle, status, published layers/artifacts, and lineage enough for monitoring/API discovery.
- Slurm publish path uses the same implementation as local CLI.
- Product discovery must be scoped to the requested cycle/run lineage and documented object-store prefix; it must not scan arbitrary workspace roots or publish mismatched source products.

## Goals / Non-Goals

**Goals:**
- Implement the smallest production-credible publish behavior for forecast/flood products.
- Record enough lineage to connect a cycle/run to tile delivery state.
- Cover success and failure paths with tests.

**Non-Goals:**
- Rebuild the frontend map experience.
- Implement full national MVT tile generation if the existing release can publish GeoJSON/object metadata first.
- Change unrelated OpenAPI drift or Slurm Analysis/Hindcast behavior covered by later Epic #120 issues.

## Decisions

### 1. Publish Success Requires Existing Delivery Evidence

The implementation should prefer existing `map.tile_layer` / `map.tile_cache` tables and flood return-period outputs. If tile bytes are already created elsewhere, publish may register metadata rather than regenerate tiles. A successful result must be backed by a row or artifact that tests can assert.

### 2. CLI Owns Publish Result Shape

`nhms-pipeline publish-tiles` should emit stable JSON with `status`, `cycle_id`, and `layers` or `artifacts`. It should return zero only when delivery evidence exists and non-zero with a clear error payload otherwise.

### 3. Failure Remains a First-Class Terminal State

If inputs are missing, database access fails, or no publishable product exists, the command must fail and let the orchestrator map the stage to `failed_publish`. It must not return a skipped/no-op success.

### 4. Publish Completion Uses Current M3 Cycle Status

The existing M3 orchestrator uses `complete` as the final cycle status for full success and `parsed_partial` when upstream basin stages partially failed but publish still runs for successful basins. Issue #122 should preserve those final statuses while adding publish evidence; it should not introduce a new final status without a separate migration and API contract update.

## Risk Packs Considered

- Public API / CLI / script entry: selected - `nhms-pipeline publish-tiles` is a Slurm-facing CLI contract.
- Config / project setup: selected - environment variables provide workspace/object store/database context.
- File IO / path safety / overwrite: selected - publish writes or registers delivery artifacts and must avoid unsafe paths/overwrites.
- Schema / columns / units / field names: selected - `map.tile_layer` / `map.tile_cache` metadata must match migrations and consumers.
- Geospatial / CRS / shapefile sidecars: not selected - this issue registers existing tile/flood products, not CRS conversion.
- Time series / forcing / temporal boundaries: selected - publish lineage is cycle/run scoped.
- Numerical stability / conservation / NaN: not selected - no solver or fitting math changes are intended.
- Solver runtime / performance / threading: not selected - publish runs after forecast/frequency products.
- Resource limits / large input / discovery: selected - publish must discover products without unbounded scans.
- Legacy compatibility / examples: selected - existing mock orchestration and Slurm sbatch paths must still work.
- Error handling / rollback / partial outputs: selected - failed publish must not mark completion or leave misleading success metadata.
- Release / packaging / dependency compatibility: selected - service must be importable with repository-managed dependencies.
- Documentation / migration notes: selected - docs must name the selected release behavior.

Selected risk packs:
- Public API / CLI / script entry
- Config / project setup
- File IO / path safety / overwrite
- Schema / columns / units / field names
- Time series / forcing / temporal boundaries
- Resource limits / large input / discovery
- Legacy compatibility / examples
- Error handling / rollback / partial outputs
- Release / packaging / dependency compatibility
- Documentation / migration notes

## Risks / Trade-offs

- Minimal metadata publish may not generate full vector tiles -> Mitigation: document selected artifact format and keep API/frontend discovery aligned.
- Database schema assumptions can drift from migrations -> Mitigation: tests assert actual table/column names used by the implementation.
- Publish failures can reduce apparent pipeline success -> Mitigation: this is intentional; no-op publish is not successful delivery.
- Partial products can be over-reported -> Mitigation: include published/excluded basin counts and lineage in metadata.
- Duplicate publishes can create confusing map layers -> Mitigation: use deterministic layer IDs or upsert semantics keyed by cycle/product lineage and test repeat invocation.
- Product discovery can become too broad on large stores -> Mitigation: only inspect known cycle/run product prefixes or repository rows for the requested cycle.

## Migration Plan

1. Add/adjust tests that fail against the placeholder CLI and no-op publish behavior.
2. Implement publish service and CLI result/failure handling.
3. Wire Slurm template and orchestrator metadata/status assertions.
4. Update docs for the selected delivery artifact.
5. Run targeted tests plus baseline backend verification.

Rollback strategy: revert to explicit non-zero `failed_publish`; never use no-op success as a rollback.

## Review Focus

- CLI exit code and JSON result semantics.
- Delivery metadata is concrete and test-asserted.
- `failed_publish` remains visible on missing inputs/failures.
- No unsafe path writes or unbounded product discovery.
- Mock orchestration and Slurm template invoke the same command path.
