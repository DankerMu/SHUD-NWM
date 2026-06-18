## Why

Forecast M3 can reach the final `publish` stage, but `nhms-pipeline publish-tiles` still returns `publish_tiles_not_implemented`, so the cycle cannot produce verifiable tile delivery artifacts. This blocks the production forecast loop, frontend flood map discovery, and reliable monitoring of publish failures.

## What Changes

- Implement `nhms-pipeline publish-tiles --cycle-id <id>` so it no longer fails by default when frequency products are available.
- Add a minimal tile publication service that records delivery metadata in the existing `map.tile_layer` / `map.tile_cache` schema or documented object-store artifacts.
- Keep publish failures observable through non-zero CLI exit, `failed_publish` cycle status, `pipeline_job` errors, events, and logs.
- Update the Slurm publish template and tests so real Slurm and mock orchestration both exercise the same CLI path.
- Document the implemented release behavior and evidence expected for downstream API/frontend consumption.

## Capabilities

### New Capabilities

- `publish-delivery-implementation`: Forecast publish stage records verifiable tile delivery artifacts and exposes clear failure status.

### Modified Capabilities

- `publish-delivery-contract`: Publish success behavior changes from a fail-fast placeholder contract to a concrete delivery contract for issue #122.

## Impact

- Affects `services/orchestrator/cli.py`, `services/orchestrator/chain.py`, `services/tile-publisher`, `infra/sbatch/publish_tiles.sbatch`, map/flood delivery docs, and backend tests.
- Uses existing database/object-store contracts where possible; no new production table is expected unless implementation discovers a necessary backward-compatible migration.
- Verification includes OpenSpec strict validation, targeted publish CLI/orchestration tests, and baseline backend checks.
