# Invariant Matrix — I4 / issue #2007 (hydro-national {source}/{cycle})

Per-issue fixture addendum (repair intensity `high`). Kept as its own file, not inlined into the
shared `tasks.md`, because 14 issues write that file concurrently.

Governing invariant: a `hydro-national` tile's bytes, its cache entry, and its 200/424 verdict are
determined entirely by the requested identity `(source, cycle, variable, valid_time, z/x/y)`; when the
requested `(source, cycle)` has no display-ready run the route MUST answer 424, never an empty 200; and
the legacy source-less route keeps its pre-change run selection, response bytes, and 200/424 verdict.
The legacy route's *cache key* deliberately rotates once, because task 3.1 bumps
`NATIONAL_DISCHARGE_QUERY_VERSION` to `-v5` and that value feeds `national_discharge_source_version`,
which the legacy route embeds in its `source_version`. One post-deploy cold miss per legacy tile is
expected and must not be read as a regression in the node-27 cold/hot numbers.

Source-of-truth identity/contract: the pair `(lower(hydro.hydro_run.source_id), hydro.hydro_run.cycle_time)`
bound as `:source` / `:cycle`, spelled through `canonical_mvt_time` (`YYYY-MM-DDTHH:MM:SSZ`); cache identity is
`cache_key(TileInput)` over `source_version` + `variant_id` + `valid_time`.

## Decided here (the fixture left these open)

1. **Legacy pass-through form.** The two predicate sites are NULL-guarded and the legacy route binds
   `:source`/`:cycle` as `None`:

   ```sql
   AND (CAST(:source AS text) IS NULL OR lower(h.source_id) = :source)
   AND (CAST(:cycle AS timestamptz) IS NULL OR h.cycle_time = :cycle)
   ```

   `CAST(...)`, never `:source::text`: SQLAlchemy's `text()` bind regex backtracks across a following
   `::` and emits a bogus extra bind (measured on this repo's SQLAlchemy 2.0.49:
   `:source::text` yields binds `['sourc', 'source']`; the `CAST` form yields `['source']`). The bogus
   bind passes every fake-session test and only fails against a real driver.
   Consequence for the shape assertion: the literal contiguous string
   `lower(h.source_id) = :source AND h.cycle_time = :cycle` appears ZERO times. This decision
   **supersedes** issue #2007's acceptance-criterion wording ("形状测试断言 ... `lower(h.source_id) =
   :source AND h.cycle_time = :cycle` 出现在 ... 两处"): that AC was written before the legacy
   pass-through form was chosen, and no guard form can satisfy both it and an unchanged legacy route.
   The AC bullet in the issue body has been amended to match. What is asserted instead: each
   sub-predicate located separately at each of the two sites, plus the NULL guard itself, so the legacy
   pass-through is locked too.
2. **Sub-second instants.** `canonical_mvt_time` does not truncate: a datetime with non-zero
   microseconds round-trips as `...:00.500000Z`. The new route therefore accepts the zero-microsecond
   spellings the contract is written for (`...T12:00:00.000Z`, `...T12:00:00+00:00`, `...T12:00:00Z`)
   and rejects a non-zero-microsecond `cycle`/`valid_time` with 422. Silent truncation is not chosen:
   it would serve the `12:00:00` tile under a `12:00:00.500Z` request.
3. **ETag is not part of the identity contract.** `stable_etag(data)` hashes tile bytes only
   (`services/tiles/mvt.py:271-272`); `source_version` reaches `cache_key`, not the ETag. The
   identity assertion is therefore on `cache_key` / the `X-Tile-Cache-Key` response header. Two
   different `(source, cycle)` tiles having different ETags is a content observation, not an
   invariant. `stable_etag` and `build_raw_tile_response` are shared by all five tile layers and are
   OUT of this PR's boundary — do not touch them.

## Surfaces

- Producers: `services/tiles/mvt.py::postgis_tile_sql("hydro-national")` — the `latest_runs` CTE and the
  `source_identity_stats_sql` probe's inline `SELECT DISTINCT ON (mi.river_network_version_id)` sub-select;
  `national_discharge_source_version`.
- Validators/preflight: `apps/api/routes/hydro_display.py` new route's `source` enum + `cycle` RFC3339 parse,
  the non-zero-microsecond rejection, `_validate_supported_hydro_variable`, `validate_xyz`.
- Storage/cache/query: `cache_key(TileInput)`, `_file_cache_path`, DB tile-cache read/upsert (`source_version`).
- Public routes/entrypoints: new 7-segment national route; legacy 5-segment national route (alias, unchanged
  behavior — its fetch helper does change, because `text()` raises on a missing named bind).
- Frontend/downstream consumers: none in this PR — the `/api/v1/layers` discharge catalog entry stays on the
  legacy template (I5/#2008); `apps/frontend/src/api/types.ts` is refreshed only by `pnpm generate:api`.
- Failure paths/rollback/stale state: the `source_identity_count <= 0` 424 branch in `_fetch_postgis_tile_bytes`
  vs. the same-code 424 from `_require_live_postgis_mvt` (told apart by `details`).
- Evidence/audit/readiness: `apps/api/openapi_patching.py::_patch_mvt_tile_openapi` `mvt_paths`,
  `openapi/nhms.v1.yaml`, `tests/test_openapi_drift.py`, `tests/test_display_publish_status_only.py`
  (counts the display-ready status predicate exactly twice inside `postgis_tile_sql`),
  `tests/test_mvt_national_identity_probe_integration.py` (throwaway-DB oracle; opt-in via
  `NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=...`, skipped in the PR lane, so it must be run
  explicitly on node-27), node-27 live receipt.

## Regression rows

- New route, `(gfs, cycle)` with a display-ready run -> 200, non-empty PBF.
  Evidence: integration test + node-27 receipt.
- New route, `(ifs, same cycle)` with no run while gfs has one -> 424 `MVT_LIVE_POSTGIS_UNAVAILABLE`, `details`
  carries z/x/y and NOT `required_env`. Evidence: integration test (deterministic seed; live data currently has
  both sources complete at every cycle, so the receipt must use a cycle with no runs at all and say so).
- New route, path `ifs` against a run stored as `source_id = 'IFS'` -> 200 (case-insensitive `lower()` match).
  Production stores exactly `gfs` and `IFS`, verified on node-27. Evidence: integration test.
- New route, `cycle=...T12:00:00.000Z` / `...T12:00:00+00:00` / `...T12:00:00Z` -> same bound `:cycle` value and
  the same `cache_key`; two different `(source, cycle)` pairs -> different `cache_key`. Evidence: unit test.
- New route, non-zero-microsecond `cycle` -> 422. Evidence: unit test.
- New route, `source=ERA5` / `source=best` / non-RFC3339 `cycle` -> 422 with no tile SQL executed (session
  override whose `execute` raises). Evidence: unit test.
- Legacy 5-segment route -> unchanged run selection, bytes and 200/424 verdict with `:source`/`:cycle` bound
  NULL. Evidence: the three existing cases in `tests/test_mvt_national_identity_probe_integration.py`, which
  drive the legacy route, must stay green unchanged.
- Unchanged sibling layers: `postgis_tile_sql("hydro")` and `postgis_tile_sql("river-network-national")` contain
  no `:source`/`:cycle` bind. Evidence: unit test.
- `national_discharge_source_version(session)` called with no arguments emits SQL that still satisfies the
  existing assertions at `tests/test_hydro_display_mvt_scaling.py:62-68`. Evidence: that test, unchanged.
- Runtime `app.openapi()` for the new path -> has the 424 response, the `q_down` variable enum, and z/x/y
  `maximum` 14/16383, and equals the hand-written `openapi/nhms.v1.yaml` entry. Evidence: new unit test +
  `tests/test_openapi_drift.py`.
