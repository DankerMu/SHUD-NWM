# Data Adapters Agent Instructions

This file is the scoped supplement of root `AGENTS.md` for
`workers/data_adapters/`. Shared vocabulary lives in `openspec/glossary.md`;
use its terms exactly as defined there.

## Required Reading

- `openspec/specs/gfs-data-acquisition/spec.md`
- `openspec/specs/ifs-data-adapter/spec.md`
- `openspec/specs/era5-data-acquisition/spec.md`
- `openspec/specs/adapter-cycle-hours/spec.md`

These adapters acquire raw forecast and reanalysis files for a source cycle.
Console entries are `cli.py:main()` (`nhms-gfs`), `cli.py:era5_main()`
(`nhms-era5`), and `cli.py:ifs_main()` (`nhms-ifs`).

## Ownership Boundaries

- `base.py` owns the adapter contract (`DataSourceAdapter` with
  `discover_cycles`, `build_manifest`, `download_plan`, `verify_manifest`), the
  manifest dataclasses, and the cycle-time helpers (`parse_cycle_time`,
  `format_cycle_time`, `cycle_id_for`, forecast-hour segmentation).
- `gfs_adapter.py`, `ifs_adapter.py`, and `era5_adapter.py` each own one source
  family: remote URLs, backend fallback order, idx or request selection, size
  limits, and their own `*AdapterError` hierarchy. Do not move source-specific
  rules into `base.py`.
- `cycle_hours.py` owns the configured UTC cycle hours per source;
  `region.py` owns the clipping bounding box (`GeoBBox`,
  `china_buffered_bbox_from_env`), which is folded into product identity so a
  region change cannot collide with cached cycles.
- `base.py` and `region.py` have importers outside this package:
  `packages/common/state_cli.py`, `packages/common/state_manager.py`, and
  `packages/common/grid_registry_bbox_guard.py` import them under entries in
  `tests/import_layering_allowlist.json`, and the forcing producer imports
  `GeoBBox`. A signature change here is a cross-package change; do not add new
  `packages` importers of this directory.
- Source identifiers go through
  `packages/common/source_identity.py` normalization. Workers may import
  `packages`, never `services` or `apps`.

## State And Mutation Fences

- Downloaded payloads and manifests are written only through
  `LocalObjectStore.write_bytes_atomic` from `packages/common/object_store.py`,
  rooted at `OBJECT_STORE_ROOT`. `verify_manifest` re-checks existence, minimum
  size, and checksum of every entry and reports typed failures
  (`MISSING_FILE`, `SIZE_TOO_SMALL`, `CHECKSUM_MISMATCH`).
- `from_env()` wires `packages/common/met_store.py:PsycopgMetStore` as the
  repository, so discovery and manifests upsert `met.data_source` and forecast
  cycle rows. Tests and DB-free callers inject a repository or pass none; keep
  the repository optional and keep writes behind the injected repository
  (for GFS, the `ForecastCycleRepository` protocol in `gfs_adapter.py`).
- The GFS backend circuit breaker persists its cooldown as JSON under
  `<object store root>/state/source_circuit/`, written with a temp file plus
  rename so concurrent sbatch processes never read a partial file.
- Local clipping of global GFS mirror and IFS payloads shells out to `cdo`; a
  missing or failing binary raises `CdoMissingError` or `CdoClipError` rather
  than degrading to the unclipped global payload.
- URLs, request bodies, and error text that may carry credentials pass through
  `packages/common/redaction.py` before they reach CLI output or evidence.

## Focused Verification

Always run the issue-required governance checks after changing this file or
data adapter scoped context:

```bash
uv run pytest -q tests/test_entropy_audit_*.py
openspec validate --all --strict --no-interactive
```

For adapter, manifest, cycle-hour, resolution, or object-store root changes,
run the focused set:

```bash
uv run pytest -q tests/test_gfs_adapter.py tests/test_ifs_adapter.py tests/test_era5_adapter.py tests/test_data_adapter_resolution.py tests/test_object_store_roots.py
```

A change to `base.py` or `region.py` also runs the layering guard:

```bash
uv run pytest -q tests/test_import_layering_guard.py
```
