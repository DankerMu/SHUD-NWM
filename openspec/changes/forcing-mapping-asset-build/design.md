## Context

SHUD-NWM already consumes direct-grid forcing assets: `workers/forcing_producer/direct_grid_contract.py` parses the binding contract, the producer materializes exact one-cell mappings, and runtime staging validates `.sp.att FORC` against `.tsd.forc` IDs (all from change `direct-grid-forcing`). What is missing is the offline tool that *produces* those assets. Live audit (docs/ForcingReplace §附录 A, 2026-07-06, node-27) confirms all 13 basins run legacy IDW, no direct-grid contract exists in any model, and no mapping builder code exists anywhere in the repo.

The builder's job is narrow and offline: take one baseline basin model package (hydrologic core + `.sp.mesh` + `.sp.att` + `gis/*.prj` + ancillary `*.tsd.*`) and one registered immutable grid snapshot, and emit a new source-specific model input package variant containing a rewritten `.sp.att`, a direct-grid binding, and an immutable evidence package. It must never mutate the baseline (INV-1) and must never generate cycle forcing, weather CSVs, or database rows (§8.1) — those belong to the runtime producer.

Baseline package input authority (production reality, node-27 2026-07-07 audit): the builder MUST read the baseline model package from the object-store release-frozen path `/home/ghdc/nwm/object-store/models/basins_<basin>_shud/<release>/package/` (release-frozen, immutable, per basin fully self-contained, 13/13 basins present with 33 total releases across the 13 buckets). It MUST NOT read from `/home/ghdc/nwm/Basins/<basin>/input/<basin>/` (node-27 basin-owner dev workspace) or `/volume/nwm/Basins/<basin>/input/<basin>/` (node-22 dev-workspace mirror) — both are best-effort dev workspaces owned by individual basin authors, are incomplete for 7 of the 13 declared basins (hetianhe/kashigeer/weiganhe/xinanjiang_upstream/tailanhe/qinyijiang/zhaochen_hhy have only skeleton dirs on node-27/node-22 as of 2026-07-07, with full SHUD inputs living exclusively in their object-store releases), and are not release-frozen. This input-source discipline MUST be enforced by the builder's package-path resolver: any non-object-store input path is rejected unless an explicit `--allow-dev-workspace` operator flag is set for one-off dev experiments (recorded in evidence with rationale).

The source of truth is docs/ForcingReplace/CMFD 建模资产向 IFSGFS Direct-Grid 的安全迁移.md, primarily §6 (Mapping Algorithm Spec), §7 (Direct-Grid Binding Spec), §8 (package/forcing boundary), Gates G0–G5, §14 (Evidence Package), and 附录 A (live audit facts).

## Goals / Non-Goals

**Goals:**

- Turn one baseline package + one grid snapshot into one immutable direct-grid mapping variant, deterministically (same inputs → byte-identical outputs).
- Enforce Gates G0–G5 as builder validation stages that fail closed before any output is written.
- Produce a binding + manifest that satisfy the *existing* parser contract in `direct_grid_contract.py` exactly, so the runtime consumer needs no change.
- Prove `.sp.att` non-`FORC` content is byte/semantically unchanged (G4) and keep the baseline immutable (INV-1).
- Produce an immutable evidence package whose checksum is bound to the mapping-asset checksum (§14).

**Non-Goals:**

- Do not modify the forcing producer, runtime staging, or contract parser (already implemented in `direct-grid-forcing`).
- Do not register scheduler routes (Change 4), touch state manager (Change 5), or touch display (Change 8).
- Do not perform scientific validation / A-B comparison (Change 6, Gate G11).
- Do not register or activate/migrate any basin; this change only *builds* assets.
- Do not define the grid registry itself (dependency `canonical-source-grid-registry`) or the platform release pin / `z_policy` verdict (dependency `cmfd-direct-grid-platform-readiness`).
- Do not generate cycle-dated `.tsd.forc`, station weather CSVs, or any `met.*` database rows (§8.1).

## Decisions

### 1. Builder module location: `workers/mapping_builder/`

The builder is an offline, package-processing worker in the same family as `workers/model_registry/` (which processes basin packages with focused modules plus a `cli.py`). It is not a producer/consumer of live cycles, so it does not belong in `workers/forcing_producer/`; it is not a throwaway script, so a `tools/` directory (which does not exist in this repo) is inappropriate. Layout mirrors the model_registry convention:

```text
workers/mapping_builder/
  __init__.py
  integrity.py     # G0 + G1 baseline/geometry integrity, station/CRS/startdate classification
  algorithm.py     # nearest_cell_barycenter_geodesic_v1: barycenter, geodesic nearest, tie-break, used-cell, index
  rewrite.py       # .sp.att FORC rewrite + G4 non-FORC-unchanged proof
  binding.py       # direct-grid binding + manifest emission (parser-contract-shaped)
  evidence.py      # immutable evidence package assembly + checksum binding
  builder.py       # orchestrates G0->G5 stages, writes the variant package
  cli.py           # entrypoint
```

Alternative considered: put it under `workers/forcing_producer/`. Rejected because the builder shares no runtime code path with the producer and mixing offline asset build with runtime production blurs the §8 boundary the whole change is defending.

### 2. Geodesic / CRS dependency: `pyproj` (already pinned)

`pyproj>=3.7.2` is already a project dependency. It provides both `Transformer` (package PROJCS → WGS84 CRS transform, driven by the package `gis/*.prj` WKT) and `Geod.inv` (geodesic distance on the WGS84 ellipsoid) — exactly the two operations §6.3–§6.4 require. Live audit shows all 13 basins are `PROJCS["unknown"]` custom Albers (×12) or Transverse Mercator (qhh), all without EPSG codes, so the transform MUST be constructed from the package `.prj` WKT per basin, never from a global assumption. The PROJ database version is pinned by the readiness change and recorded in evidence (P0.1). For regular lat/lon grids, §6.4 clause 7 allows independent lon/lat rounding as an equivalent fast path, but the result and tie rules MUST match the geodesic definition.

Alternative considered: reimplement haversine/planar distance. Rejected: §6.4 clause 5 forbids undeclared planar-degree distance, and reusing the pinned PROJ ellipsoid keeps the builder consistent with the registry and producer.

### 3. Input authority model: mesh + att + prj only; `domain.shp` excluded

Geometry authority is `.sp.mesh` (element three-vertex + node projected X/Y). Element-ID authority is the `.sp.att` `INDEX` column. CRS authority is the package `gis/*.prj` (checksum-bound). `domain.shp` is NOT an algorithm input — it is used only to render the old/new ownership comparison images for evidence, and its row order is never treated as element ID (§6.2). All element-to-cell association is by explicit element ID, never by row order, so row reordering in any file MUST NOT change the result (G1).

### 4. Output package layout: new model input package variant with parent lineage

The builder emits a *new* immutable `model_input_package` variant, never editing the baseline (INV-1, INV-2). The variant contains the hydrologic core files (copied by reference/checksum, unchanged), the rewritten `.sp.att`, the direct-grid binding artifact (or its immutable URI), the manifest with `resource_profile.direct_grid_forcing` nested section (§7.1), ancillary non-weather `*.tsd.*`, a model-core fingerprint, and the parent CMFD package identity. Old CMFD weather station CSVs MUST NOT appear as active forcing; if a compatibility tool needs them they go to a non-runtime directory marked inactive (§8.2). Each variant maps to its own `model_input_package_id`; GFS/IFS may share a single binding only when their `grid_signature` is proven equal (§2.2) — that sharing decision is the registry/routing concern, but the builder MUST honor `applicable_source_ids` scoping in the manifest it emits.

### 5. G0–G5 mapped onto builder validation stages

The builder runs the gates in order and fails closed before writing any output:

- **G0 Baseline Integrity** → `integrity.py`: baseline package checksum; `.sp.mesh`/`.sp.att` parseable; element IDs complete/unique; old `FORC` all positive integers; old `.tsd.forc` references legal if present; ancillary `*.tsd.*` inventory complete; duplicate-coordinate station registration (live: zhaochen_mc 4 stations same coords, Z=-9999); non-grid baseline classification (live: zhaochen_wem 5 irregular X1..X5 points, 0.02° spacing); startdate heterogeneity (1951–2024); baseline `.tsd.forc` line-2 absolute paths archived as known-harmless, baseline never modified.
- **G1 Geometry Identity** → `integrity.py`: mesh/att element-ID sets equal; non-degenerate triangles; CRS from package `gis/*.prj` only (checksum-bound, no EPSG); element count equal; row reorder does not affect mapping; builder uses mesh ID not row index.
- **G2 Grid Identity** → `algorithm.py` precondition: source/grid registered; grid signature recomputed via the shared registry/producer helper; basin fully inside grid coverage; no silent dynamic crop.
- **G3 Ownership** → `algorithm.py`: each element exactly one cell; every cell in registry; used-cell ↔ station binding one-to-one; `FORC` in `1..N` contiguous; zero duplicate/unused bindings; used-cell ≥ 4 (or approved override); reproducible tie decisions; distance QA (min/P50/P95/max, normalized, tie count, coverage-edge count) and the half-cell-diagonal sanity bound.
- **G4 Asset Delta** → `rewrite.py`: core fingerprint == baseline; mesh/river/lake/calibration checksums equal; `.sp.att` non-`FORC` columns equal; only mapping metadata added; no legacy weather path in active package.
- **G5 Contract** → `binding.py`: manifest complete; binding checksum correct; manifest station bindings match binding artifact; model input package identity correct; `.sp.att` checksum correct; source scope correct; grid id/signature correct; station IDs/filenames unique; station coordinates match bound cell (§7.3 tolerance); x/y recomputable; z policy has evidence; zero reserved-filename collisions.

Gates G6–G11 belong to downstream changes (producer/runtime/state/scientific) and are explicitly out of scope.

### 6. Small-basin override mechanics

`element-grid-ownership-mapping` refuses by default when the used-cell count is < 4 (§6.5 hard gate; live: zhaochen_wem = 1 cell, zhaochen_mc = 4 cells). The refusal is a hard blocker, not a warning. The only bypass is an explicit approval flag (e.g. `--allow-small-basin`) that MUST be recorded verbatim in the evidence package as an approval, with the approver identity, so the override is auditable and never silent. Without the flag the builder writes no output.

### 7. Determinism / reproducibility

Same baseline package + same grid snapshot + same algorithm version ⇒ byte-identical binding, `.sp.att`, and evidence (modulo an explicitly-excluded timestamp field, if any, which MUST NOT enter any checksum). Canonical ordinal ordering makes `shud_forcing_index` assignment deterministic; tie-break by smallest canonical ordinal removes floating-point ordering ambiguity; coordinate equality uses the same 12-decimal rounding as the grid signature (never float-literal equality, because live coords carry ~1e-7° noise). The algorithm identifier `nearest_cell_barycenter_geodesic_v1` is versioned: distance definition, tie-break, index order, and coordinate precision MUST NOT change without a new version suffix (§6.1).

### 8. Evidence checksum binding

`mapping-evidence-package` computes a single evidence checksum over the ordered evidence contents and binds it to the mapping-asset checksum (the variant package / binding checksum), so neither can be altered without invalidating the other (§14). Ownership map images (old vs new) are rendered from `domain.shp` for visualization only and included as evidence, not as algorithm inputs.

## Risks / Trade-offs

- **Risk: builder reimplements a near-but-not-identical grid signature** → Reuse the shared helper from `canonical-source-grid-registry` / the producer's `_grid_signature` logic; never hand-roll signature rules (§5.1).
- **Risk: wrong CRS transform silently produces plausible-but-wrong ownership** → CRS comes only from the checksum-bound package `.prj`; the half-cell-diagonal distance sanity bound (G3) turns CRS/clip/grid errors into hard blockers.
- **Risk: coordinate equality asserted with float literals fails on ~1e-7° noise** → Compare after the same 12-decimal rounding as the grid signature (§7.3); never assert raw float equality.
- **Risk: single/few-cell basin ships a degenerate uniform-forcing asset** → Small-basin hard gate (< 4 cells) refuses by default; override only via recorded explicit approval (§6.5).
- **Risk: builder accidentally emits runtime-producer artifacts** → §8.1 forbidden-output check is an explicit G5-adjacent assertion: no cycle `.tsd.forc`, no weather CSVs, no `met.*` rows, no cycle lineage.
- **Risk: baseline mutated during build** → INV-1 enforced by writing only into the new variant tree; baseline files are opened read-only and their pre/post checksums recorded in evidence.
- **Risk: station_id reuse across mapping versions collides with the DB mirror fail-closed policy** → `station_id` embeds immutable mapping-asset identity so it is never reused across versions (§7.4).

## Migration Plan

1. Land the builder modules under `workers/mapping_builder/` with unit coverage per capability, no basin activation.
2. Validate against the keliya integration fixture (484 elements / 32 stations / ~8 cells) end to end: G0→G5, binding parser-contract compatibility, `.sp.att` G4 proof, evidence checksum binding.
3. Because this change only *builds* assets, there is no runtime rollback surface here; produced variants are inert until a later change (Change 4 routing) references them. Superseding a variant means building a new variant with a new mapping-asset identity, never editing an existing one.
