## Why

Direct-grid forcing bindings reference a canonical `grid_id` and `grid_signature`, but there is no immutable, auditable record of what a registered source grid actually is. Grid definitions currently live only as `canonical/{source}/grid/{grid_id}/grid.json` files, `met.canonical_met_product` has the `grid_definition_uri` column but 0 rows in production, and `grid_cell_id` is a flat index string (e.g. `"36268"`) derived from the pinned download bbox (`NHMS_DOWNLOAD_BBOX_*`), so any bbox change silently shifts every cell id. Before any basin's `.sp.att` `FORC` is rewritten against a grid, the platform needs a source-grid registry that pins each grid as an immutable snapshot, decides GFS/IFS sharing by `grid_signature` equality rather than `grid_id` string equality (live fact: `ifs_0p25` and `gfs_0p25` share signature `6c008901b8b7…` while their `grid_id` strings never match), and treats any grid drift as a new grid version that invalidates old bindings and fails production closed.

## What Changes

- Add an immutable Canonical Source-Grid Registry: each usable direct-grid grid is registered as a Grid Snapshot with normalized `source_id`, `grid_id`, `grid_signature`, `grid_definition_uri` + SHA-256 checksum, per-cell `grid_cell_id` + normalized cell-center lon/lat + canonical ordinal, longitude convention, latitude order, flatten order, native resolution, validity window, converter version, and the pinned download bbox.
- Reuse the producer's own grid-signature algorithm (SHA-256 over ordered `(grid_cell_id, lon@12dp, lat@12dp)` tuples, `workers/forcing_producer/producer.py:2642-2650`) by extracting it into a shared module that both the producer and the registry import. The registry MUST NOT reimplement an "approximately equal" signature.
- Pin the download bbox into grid snapshot identity and **fail closed** when a deployment's `NHMS_DOWNLOAD_BBOX_*` env values are inconsistent with the registered snapshot's bbox.
- Add pre-registration grid-signature stability verification (multi-cycle, multi-variable, multi-backend, bbox-clip, latitude-order, longitude-normalization, product-upgrade), refusing any grid with dynamic per-cycle cropping.
- Introduce a source-agnostic `canonical_grid_key` so both source `grid_id`s map to one key when their signatures match, and key GFS/IFS shared-binding eligibility on signature equality plus per-source variable verification and explicit `applicable_source_ids`. Normalize `source_id` case (live data has `IFS` vs `gfs`) with the same rule as the contract parser.
- Add a grid-drift lifecycle: any change to cell count / coordinates / lat order / lon convention / `grid_cell_id` / flatten order / bbox / converter cell-identity semantics / product upgrade produces a NEW grid version; old snapshots are retained immutably, old bindings become invalid, and forcing production fails closed. Updating a manifest `grid_signature` without rebuilding `.sp.att` and bindings is forbidden.
- **Storage decision (design.md)**: choose `met.canonical_met_product` reuse vs a new grid snapshot table, justify the choice, and specify the migration so grid definitions are not stored twice without cross-validation.

## Capabilities

### New Capabilities
- `grid-snapshot-registration`: Register a source grid as an immutable, checksummed Grid Snapshot carrying full ordered-cell geometry, the producer's grid signature, and the pinned download bbox.
- `grid-signature-stability-verification`: Verify a grid's signature is invariant across cycles, variables, download backends, and latitude/longitude normalization before it enters the registry, refusing dynamically cropped grids.
- `source-shared-binding-eligibility`: Decide GFS/IFS shared-binding eligibility by `grid_signature` equality via a source-agnostic `canonical_grid_key`, with per-source variable verification and normalized source ids.
- `grid-drift-lifecycle`: Treat any grid identity change as a new immutable grid version that invalidates old bindings, fails production closed, and retains old snapshots for historical reproduction.

### Modified Capabilities
- None.

## Impact

- New shared grid-signature module extracted from `workers/forcing_producer/producer.py:2642-2650` (`_grid_signature` / `_grid_signature_hash`), imported by both the producer and the registry so the signature has a single implementation.
- Registry storage: either `met.canonical_met_product` (exists with `grid_definition_uri`, 0 rows in production) extended for grid-snapshot rows, or a new immutable grid snapshot table plus a DB migration under `db/migrations/`; the storage decision and migration are specified in design.md.
- Bbox pinning reads `NHMS_DOWNLOAD_BBOX_*` via `workers/data_adapters/region.py` (`china_buffered_bbox_from_env`, defaults 63–145°E / 8–64°N); registry adds a fail-closed check comparing deployment env bbox to the registered snapshot bbox.
- Source-id normalization reuses `packages/common/source_identity.py` `normalize_source_id` (returns `IFS` / `gfs`), the same rule the contract parser and met store use.
- New registry CLI/worker entry point and its tests under `tests/`; grid definitions continue to originate from `canonical/{source}/grid/{grid_id}/grid.json` files.
- Does NOT change forcing producer runtime behavior, the scheduler, state, or display runtime / read paths / API surface; does NOT build mapping assets (`.sp.att` rewrite / bindings — that is `forcing-mapping-asset-build`). One narrow in-scope exception: on new-version registration the registry writes the stale-marker flag (`active_flag=false` or `superseded_at` non-NULL) into the display-side derived caches `met.met_station` and `met.interp_weight` for rows tied to the superseded snapshot, so already-persisted derived rows are not served as active; the display API only reads active rows and is not modified. See design.md §7 (Ownership boundary) and the `grid-drift-lifecycle` requirement "Derived caches on superseded snapshots are marked stale". Depends conceptually on `cmfd-direct-grid-platform-readiness` (release pinning) but is code-independent.
