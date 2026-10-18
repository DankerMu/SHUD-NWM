# Precip Agent Instructions

This file is the scoped supplement of root `AGENTS.md` for `services/precip/`.
Shared vocabulary lives in `openspec/glossary.md`; use its terms exactly as
defined there.

## Required Reading

- `openspec/specs/precipitation-raster-overlay/spec.md`
- `docs/runbooks/canonical-precip-mirror-backfill.md`
- `apps/api/routes/precip.py`

`services/precip` is the display-side library behind `/api/v1/precip/**`. It
reads the mirrored canonical precipitation tree that the compute plane writes;
it never produces canonical slices itself.

## Ownership Boundaries

- Module owners: `mirror.py` owns the keyspace and the past-24h window resolver
  (`resolve_window`, `cycle_is_mirrored`, `slice_digest`); `field.py` owns grid
  loading and 24h accumulation; `render.py` owns resampling, palette
  classification, and PNG encoding; `cache.py` owns the PNG file cache and the
  ETag; `constants.py` owns env names, palette, bounds, and URL templates;
  `errors.py` owns the typed failures.
- `constants.py` is stdlib-only. `services/tiles/mvt.py` imports it on every
  layer-catalog request, so it must never pull in numpy or netCDF4.
- `__init__.py` re-exports `field` and `render` names lazily (PEP 562
  `__getattr__`) for the same reason. Keep new heavy names in `_LAZY_EXPORTS`,
  not as eager imports; `tests/test_precip_overlay.py` asserts the boundary in
  a fresh interpreter.
- The route layer imports leaf modules, not the package. HTTP shapes, status
  codes, and request validation belong in `apps/api/routes/precip.py`; this
  package raises domain errors and never imports `apps`.
- Source and cycle mapping goes through exactly two rules in `mirror.py`: the
  closed `{gfs, ifs}` enum, then
  `packages/common/source_identity.py` normalization, and the `%Y%m%d%H` cycle
  token. Do not import compute-plane modules such as
  `workers/canonical_converter/converter.py` to share that logic.
- The display API reads `NHMS_PRECIP_MIRROR_ROOT`, never the compute-side
  copyback root; `apps/api/runtime_mode.py` refuses the latter for the display
  role. The only local write is the PNG cache under `NHMS_MVT_FILE_CACHE_DIR`,
  through `write_cached_png` (tmp file plus `os.replace`, failures degrade to
  "not cached"). Retention of that tree is owned by
  `scripts/node27_raw_retention.py`.

## Error Model

- `errors.py` defines `PrecipError` (base, a `RuntimeError`) and three
  subclasses. Every payload carries relative object keys, never absolute mirror
  paths, because the route renders them into a 404 body.
- `PrecipWindowIncomplete(missing=..., reason=..., window_end=...)` is raised
  by `resolve_window` when a slice is absent (`reason="missing_slice"`) or no
  mirrored cycle precedes the window end. The route maps it to 404
  `PRECIP_WINDOW_INCOMPLETE` with `reason`, `missing`, and `window_end` details.
- `PrecipSliceInvalid(object_key, reason)` is raised by `field.py` when a grid
  definition or slice violates the canonical contract (stable `reason` strings
  such as `grid_definition_shape_mismatch` or `slice_unit_mismatch`). The route
  maps it to the same 404 `PRECIP_WINDOW_INCOMPLETE` code with `reason` and
  `object_key`.
- `PrecipCycleNotMirrored` is reserved for library callers; nothing raises it
  today. The route answers the mirrored-cycle gate itself with 404
  `PRECIP_CYCLE_NOT_MIRRORED`, including the unconfigured-root case.
- `scripts/node27_mvt_prewarm.py` classifies these reasons to decide which
  failures are expected gaps; a new `reason` string must be reviewed there.
- A failure is a typed error with a stable reason, never a generic 500, a
  partial PNG, or a cache entry. `read_cached_png` treats a truncated file as a
  miss because the ETag is derived from identity, not bytes.

## Focused Verification

Always run the issue-required governance checks after changing this file or
precip scoped context:

```bash
uv run pytest -q tests/test_entropy_audit_*.py
openspec validate --all --strict --no-interactive
```

For resolver, accumulation, rendering, cache, error, or route-contract changes,
run the focused set:

```bash
uv run pytest -q tests/test_precip_overlay.py tests/test_node27_mvt_prewarm.py tests/test_node27_raw_retention.py tests/test_api_contract.py tests/test_openapi_drift.py
```
