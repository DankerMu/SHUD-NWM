## Context

Direct-grid forcing (change `direct-grid-forcing`) already validates a basin asset's declared `grid_id` and `grid_signature` against the canonical product grid used for the current cycle. But nothing pins what a *registered* source grid is: grid definitions exist only as `canonical/{source}/grid/{grid_id}/grid.json` files, and `met.canonical_met_product` (which carries a `grid_definition_uri` column) has 0 rows in production. There is no immutable, checksummed, ordered-cell snapshot that a binding can point at, and no place to record the pinned download bbox that generates the flat `grid_cell_id` strings.

Three live constraints from the 2026-07-06 node-27 baseline (source doc §5, appendix A) shape the design:

1. **`grid_cell_id` is a bbox-derived flat index string** (e.g. `"36268"`). The download bbox comes from `NHMS_DOWNLOAD_BBOX_*` (defaults 63–145°E / 8–64°N in `workers/data_adapters/region.py:21-24`, read by `china_buffered_bbox_from_env`). If the bbox changes, every `grid_cell_id` shifts, silently invalidating every binding. So bbox must be part of grid snapshot identity, and a deployment env inconsistent with the registered bbox must fail closed.
2. **Sharing is keyed on signature, not `grid_id` string.** `ifs_0p25` and `gfs_0p25` have the identical signature `6c008901b8b7…` while `grid_id` strings differ; `grid_id` can never match across sources. Sharing must therefore key on a source-agnostic `canonical_grid_key` derived from the signature.
3. **`source_id` case is inconsistent** in live data (`IFS` vs `gfs`). Registry and `applicable_source_ids` validation must normalize with the same rule as the contract parser (`packages/common/source_identity.py` `normalize_source_id`, which returns `IFS` / `gfs`).

The signature algorithm already exists and is authoritative: `workers/forcing_producer/producer.py:2642-2650` computes SHA-256 over ordered `(grid_cell_id, lon rounded 12dp, lat rounded 12dp)` tuples via `_grid_signature` / `_grid_signature_hash`, using `sha256_bytes` from `packages/common/object_store.py`. The registry MUST reuse this, not reimplement it.

## Goals / Non-Goals

**Goals:**

- Register each usable direct-grid grid as an immutable, checksummed Grid Snapshot with full ordered-cell geometry and the pinned bbox.
- Compute `grid_signature` through the single shared implementation the producer already uses.
- Fail closed when a deployment's `NHMS_DOWNLOAD_BBOX_*` disagrees with the registered snapshot bbox.
- Decide GFS/IFS shared-binding eligibility by signature equality via a source-agnostic `canonical_grid_key`.
- Make grid drift a first-class new-version event that invalidates old bindings and retains old snapshots.

**Non-Goals:**

- Do not build mapping assets (`.sp.att` rewrite, station bindings) — that is `forcing-mapping-asset-build`.
- Do not change forcing producer runtime value generation, the scheduler, warm state, or display **runtime behavior / read paths / API surface**. The single narrow exception is supersession propagation into the display-side derived caches: on new-version registration, the registry MUST mark rows in `met.met_station` and `met.interp_weight` tied to the superseded snapshot's `grid_snapshot_id` as stale (via `active_flag=false` or `superseded_at` non-NULL); the registry writes the stale flag, the display API only reads active rows and is not modified by this change. See §7 (Ownership boundary for derived-cache supersession) for the ownership rationale.
- Do not implement the mapping algorithm, ownership tables, or distance QA.
- Do not modify the download/clip pipeline; the registry only reads and pins the bbox identity.

## Decisions

### 1. Storage: extend `met.canonical_met_product` is rejected; add a new immutable grid snapshot table

`met.canonical_met_product` is a **per-variable, per-cycle, per-valid-time product-instance** table (its primary key is `canonical_product_id`, and it carries `variable`, `cycle_time`, `valid_time`, `lead_time_hours`, `object_uri`, `checksum`). A grid snapshot is a *different grain*: one immutable row per `(normalized source_id, grid_id, bbox, converter version, validity window)` describing the grid geometry itself, independent of any single cycle/variable/product file. Overloading the product-instance table would either duplicate the grid definition across every product row (no single immutable authority) or bolt a second grain onto a table whose semantics are cycle-scoped.

**Decision:** add a new append-only table `met.canonical_grid_snapshot` (plus a child `met.canonical_grid_cell` for ordered cells) under a new `db/migrations/` file. The migration also adds a nullable column `met.canonical_met_product.grid_snapshot_id UUID REFERENCES met.canonical_grid_snapshot(grid_snapshot_id)` as the sole cross-reference form; `met.canonical_met_product.grid_definition_uri` remains as a display/cross-check field but the FK is the referential-integrity anchor. Insertion of a `met.canonical_met_product` row whose `grid_snapshot_id` does not reference a registered snapshot MUST be rejected. The snapshot's `grid_definition_uri` + SHA-256 checksum remain the immutable authority; the `grid.json` file stays the on-disk origin.

Snapshot columns: `grid_snapshot_id UUID` (PK), `canonical_grid_key`, `source_id` (normalized), `grid_id`, `grid_signature`, `grid_definition_uri`, `grid_definition_checksum`, `longitude_convention`, `latitude_order`, `flatten_order`, `native_resolution`, `bbox_south/north/west/east`, `converter_version`, `valid_from`, `valid_to`, `applicable_source_ids TEXT[]` (normalized source_ids scoped to this snapshot's `canonical_grid_key`), `superseded_at TIMESTAMPTZ NULL` (set when a newer version supersedes this snapshot; append-only lifecycle marker, not a mutation of identity fields), `created_at`. Cell rows: `grid_snapshot_id` (FK, `ON DELETE CASCADE`), `grid_cell_id`, `longitude`, `latitude`, `canonical_ordinal`, unique on `(grid_snapshot_id, grid_cell_id)` and `(grid_snapshot_id, canonical_ordinal)`; `canonical_ordinal` values are integers `1..N` contiguous and match the deterministic ordering the producer uses to compute `grid_signature`.

Alternative considered: reuse `met.canonical_met_product`. Rejected — wrong grain, and it would prevent one immutable authority per grid.

### 2. Shared signature helper extraction

Extract `_grid_signature` and `_grid_signature_hash` from `workers/forcing_producer/producer.py:2642-2650` into a new shared module, `packages/common/grid_signature.py`, exposing `grid_signature_tuples(grid_points)` and `grid_signature_hash(grid_points)` over a minimal `GridPoint`-like protocol (`grid_cell_id`, `longitude`, `latitude`). The producer imports from this module (its `_grid_signature` becomes a thin re-export/wrapper so existing producer tests keep passing), and the registry imports the same functions. This guarantees a single algorithm: SHA-256 over ordered `(grid_cell_id, round(lon,12), round(lat,12))` via `sha256_bytes`.

Alternative considered: have the registry call the producer directly. Rejected — creates a registry→producer dependency and couples the registry to producer internals; a shared `packages/common` module is the correct layer.

### 3. Registry entry point

Add a registry worker/CLI module (e.g. `workers/grid_registry/registry.py` with a thin CLI `python -m workers.grid_registry`). It reads a `canonical/{source}/grid/{grid_id}/grid.json` definition, computes the signature through the shared helper, runs stability verification inputs (representative cycles/variables/backends supplied as fixtures/paths), derives `canonical_grid_key`, checks the bbox against the deployment env, and writes one immutable snapshot + ordered cell rows. Registration is append-only: attempting to register a grid whose identity differs from an existing snapshot for the same `(source_id, grid_id)` under the same bbox creates a **new version**, never an in-place update.

### 4. Immutability enforcement (append-only, checksummed)

Snapshots are never updated or deleted. The migration enforces this at the DB layer (no `UPDATE`/`DELETE` path in the store interface; snapshot rows are insert-only, and the store raises on any attempt to mutate an existing `grid_snapshot_id`). Each snapshot binds `grid_definition_uri` to its SHA-256 `grid_definition_checksum`; a load recomputes/verifies the checksum. Cell rows are inserted atomically with their parent snapshot. `grid_signature` in a snapshot can never be edited — a changed signature is a new snapshot (see Decision 6).

### 5. Bbox pinning and fail-closed check placement

The snapshot stores `bbox_{south,north,west,east}` taken from the bbox that produced the grid. A `verify_download_bbox_matches_registry()` guard compares the deployment's `china_buffered_bbox_from_env()` (`NHMS_DOWNLOAD_BBOX_*`) against the registered snapshot bbox. The check runs at registration time (the snapshot records the bbox in force) and is exposed for the platform readiness/producer preflight to call before any direct-grid production; on mismatch it raises a structured error and blocks — it never silently proceeds with drifted `grid_cell_id`s. The registry itself does not run production, so the guard is a reusable function, not a producer edit in this change.

### 6. `canonical_grid_key` derivation and grid-drift versioning

`canonical_grid_key` is derived deterministically from the normalized grid identity so two source grids that produce the same `grid_signature` under the same pinned bbox and native resolution map to the same key. It is a function of exactly three inputs `(grid_signature, pinned bbox, native_resolution)` (the identity components that define "same normalized grid"), and is source-agnostic — `source_id` is NOT an input. Same-signature snapshots under different bbox or different `native_resolution` MUST derive different `canonical_grid_key` values (the three-input rule is positive; any implementation, stored or computed, MUST respect it). Both `ifs_0p25` and `gfs_0p25` snapshots (identical signature `6c008901b8b7…`, same bbox 63–145°E / 8–64°N, same 0.25° resolution) therefore share one `canonical_grid_key`; their `grid_id` strings and `source_id`s stay distinct on their own snapshot rows.

Grid drift = any change to cell count, coordinates, latitude order, longitude convention, `grid_cell_id`, flatten order, bbox, converter cell-identity semantics, or source product upgrade. Any such change yields a different `grid_signature` (product upgrade MUST change it) and therefore a new snapshot version; the prior snapshot is retained immutably for historical reproduction. Registering a new version does not touch the old one.

### 7. Ownership boundary: supersession propagation into display-side derived caches

Per source doc §13 and INV-3, `met.met_station` and `met.interp_weight` are *derived caches* of the direct-grid station bindings; a superseded snapshot's derived rows would otherwise be silently served as "active" by the display read path, violating "old bindings become invalid, forcing production fails closed" (source doc §13). Two placements were considered:

- **Placement A (chosen):** the registry owns the *write* of `active_flag=false` (or `superseded_at` non-NULL) into `met.met_station` and `met.interp_weight` on new-version registration, in the same transaction as setting the prior snapshot's `superseded_at`. Only the flag is written; no value rows are inserted, updated for content, or deleted, and the display API is not touched.
- **Placement B (rejected):** the registry only exposes `superseded_at` and `latest_snapshot_for(canonical_grid_key)`; a downstream display / met-store change enforces staleness on read.

Placement A is chosen because supersession is an atomic identity event owned by the registry (§6): the registry is the single writer of `superseded_at`, so the derived-cache flag flip belongs in the same transaction to avoid a window in which the snapshot is superseded but the display cache still reports it active. Placement B would split the invariant across two changes and leave a live inconsistency until the downstream change lands. The Non-Goal on "display" is therefore narrowed to display **runtime / read-path / API** behavior; the write of `active_flag` / `superseded_at` on the two derived-cache tables is an in-scope registry-owned exception. The display API's active-row read semantics are consumed by, but not modified in, this change.

## Risks / Trade-offs

| Risk | Mitigation |
| --- | --- |
| Signature helper extraction breaks existing producer tests | Keep producer's `_grid_signature`/`_grid_signature_hash` as thin wrappers re-exporting the shared functions; run the full producer test suite as regression evidence. |
| Two grids with genuinely different geometry collide on `canonical_grid_key` | Key is derived from `grid_signature` (content hash of ordered cells) + bbox + resolution; a hash collision is cryptographically negligible, and stability verification proves the signature actually differs on real drift. |
| Deployment env bbox drifts from a registered snapshot and cell ids shift silently | Bbox is part of snapshot identity; `verify_download_bbox_matches_registry()` fails closed on mismatch before any binding is used. |
| Sharing wrongly enabled when only one source's variables were verified | Shared eligibility requires all required variables verified on representative cycles for BOTH sources plus explicit `applicable_source_ids` listing both plus archived comparison evidence; a single-source verification does not grant sharing. |
| A grid with dynamic per-cycle cropping enters the registry | Stability verification refuses grids whose `grid_cell_id`/signature changes across cycles; such grids are rejected until the canonical grid contract is stabilized. |
| Manifest `grid_signature` edited without rebuilding `.sp.att`/bindings | Forbidden by the drift lifecycle spec; registry never updates a signature in place, and a signature change is a new snapshot version that invalidates old bindings and fails production closed. |
| Storing grid definition twice (`grid.json` file + DB) diverges | Snapshot binds `grid_definition_uri` to a SHA-256 checksum; `met.canonical_met_product.grid_snapshot_id` is an FK into the registered snapshot (referential-integrity anchor), and its `grid_definition_uri` becomes a display/cross-check field validated against the snapshot's checksum on insert. |

## Migration Plan

1. Add `packages/common/grid_signature.py` with `grid_signature_tuples` / `grid_signature_hash`; repoint `workers/forcing_producer/producer.py` `_grid_signature` / `_grid_signature_hash` to the shared functions (behavior-preserving).
2. Add a `db/migrations/` file creating `met.canonical_grid_snapshot` (with `applicable_source_ids TEXT[]` and `superseded_at TIMESTAMPTZ NULL`) and `met.canonical_grid_cell` (append-only, `ON DELETE CASCADE` from snapshot), and add a nullable `grid_snapshot_id UUID REFERENCES met.canonical_grid_snapshot(grid_snapshot_id)` cross-reference column on `met.canonical_met_product`.
3. Add the registry store interface (insert-only snapshot + cells, checksum-verified load) and the `canonical_grid_key` + bbox-pin helpers.
4. Add the registry CLI/worker entry point that registers a grid from `canonical/{source}/grid/{grid_id}/grid.json` after stability verification.
5. Add the `verify_download_bbox_matches_registry()` guard reading `NHMS_DOWNLOAD_BBOX_*`.
6. Backfill: register the live `ifs_0p25` and `gfs_0p25` 0.25° snapshots (shared `canonical_grid_key`) from the existing grid definitions as the first snapshots.

Rollback: the migration is additive (new `met.canonical_grid_snapshot` and `met.canonical_grid_cell` tables + nullable `met.canonical_met_product.grid_snapshot_id` FK column); dropping the new tables, dropping the FK column, and reverting the signature module wrapper restores prior behavior with no data loss, since production `met.canonical_met_product` has 0 rows and no bindings reference snapshots yet.

## Open Questions

- Whether `canonical_grid_key` should be a stored derived column or computed on read — leaning stored for immutability and query simplicity, resolved during store implementation.

## Resolved (previously open)

- **Representative-cycle/variable/backend fixture strategy for stability verification.** Fixtures for §4 stability verification are compact NetCDF files committed under `tests/fixtures/canonical/{source}/{cycle}/*.nc` sized to a small (~5×5) sub-grid of the pinned bbox, one per representative cycle (≥3 consecutive cycles for the multi-cycle case) and one per required variable. Backends are enumerated from `workers/data_adapters/{ifs_adapter,gfs_adapter,era5_adapter}.py`. Required variables are the five SHUD station variables: `Prcp`, `Temp`, `RH`, `Wind` (U/V pair counts as one), `RN`. Real object-store paths are used only in node-27 live verification of the backfill (§6 in Migration Plan), not in the CI-runnable test suite.
