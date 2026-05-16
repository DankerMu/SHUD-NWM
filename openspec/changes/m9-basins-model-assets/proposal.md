## Why

`data/Basins` now provides 13 real, calibrated SHUD model asset directories through a development symlink to `/volume/data/nwm/Basins`. The project can move beyond placeholder model packages, but it lacks a repeatable way to discover these assets, validate their SHUD inputs, publish immutable object-store packages, and register basin/river-network/mesh/model metadata for runtime and frontend use.

## What Changes

- Add a Basins asset discovery and validation workflow for the known SHUD directory shape: `input/<shud_input_name>/`, `CALIB/`, `forcing/` or legacy typo `focing/`, and GIS sidecars.
- Add deterministic SHUD model package publication with manifest, checksum, object-store URI, and migration instructions that require copying real data in production instead of preserving the development symlink.
- Add registry import behavior that creates or updates `core.basin`, `core.basin_version`, `core.river_network_version`, `core.river_segment`, `core.mesh_version`, and `core.model_instance` from discovered Basins metadata.
- Add bounded runtime/API/frontend consumption checks so at least one Basins model can be staged by `SHUDRuntime`, listed by model APIs, loaded by river-segment map queries, and shown in model-asset UI work.
- Preserve current demo seed and fast tests; this stage introduces opt-in real-asset workflows and fixtures rather than making `/volume/data/nwm/Basins` mandatory for every test run.

## Capabilities

### New Capabilities

- `basins-asset-discovery`: Discovers Basins SHUD model directories, validates required files, normalizes known naming quirks, and emits a structured inventory.
- `shud-model-package-publication`: Packages validated SHUD input, calibration, GIS, and forcing metadata into immutable object-store artifacts with checksums and production migration evidence.
- `basins-registry-import`: Imports Basins-derived basin, river network, mesh, and model metadata into the existing model registry contracts.
- `basins-runtime-consumption`: Verifies Basins-backed models are consumable by SHUD runtime staging, API/model queries, river-segment map data, and frontend asset-management surfaces.

### Modified Capabilities

- None.

## Impact

- Affects `workers/model_registry/`, `packages/common/model_registry.py`, `apps/api/routes/models.py`, `workers/shud_runtime/`, object-store helpers, tests, and docs.
- Introduces explicit `nhms-model` subcommands or equivalent CLI entry points for `discover-basins`, `publish-basins`, `import-basins-registry`, and `basins-migration-report`.
- Introduces a new real-asset validation lane that depends on `data/Basins` only when explicitly enabled.
- Requires production migration documentation and scripts to copy the actual `/volume/data/nwm/Basins` data into the target environment; symlink-only migration is invalid.
- Does not change public forecast/flood APIs, current demo seed semantics, or require a live SHUD solver for baseline validation.
