# The basin station list returns the stations of the latest displayable run

Issue: #2699 (part 2 of 3; part A merged as PR #2766). Fixture level: **compact**.
Risk packs: **Read-path query shape (node-27 role timeout 30 s)**, **Public API behaviour change**.

## Why

`GET /api/v1/met/stations?basin_version_id=…` (no `model_id`) returns `met.met_station` rows with
`active_flag = true`. In production no Direct Grid station is active (the activation flip never ran), so the
list returns the legacy `forcing_grid` stations where any exist and nothing elsewhere. Read-only on node-27,
2026-10-07, as `nhms_display_ro`:

- heihe lists 1709 legacy stations, qhh 386, kashigeer 941, hetianhe 581, weiganhe 401, tailanhe 37 — none of
  them can serve a series: their model artifacts have left the store (#2699 owner decision: 404, never mapped).
- about fifty producing basin versions list **0** stations although their latest run has 29–1314 Direct Grid
  stations with readable series.
- retired basin versions with no displayable run still list their old stations (keliya 32, qinyijiang 93,
  zhaochen_bst 626, …).

Owner decision (2026-10-07): the basin list shows only the stations of the current, displayable generation;
old generations and legacy stations are not listed. IFS and GFS stations of one generation sit on the same
cells (heihe 287/287, qhh 71/71 identical coordinates), and the popup draws both sources from the one clicked
station id, so the list returns **one** model's stations, not one per source.
Expected consequence: at equal `cycle_time` the higher `run_id` wins (production ids `fcst_(gfs|ifs)_<cycle>_…`,
so IFS once both have landed); the listed station ids switch between the two sources' id sets as runs land,
on identical positions. COUNT and page statements each resolve the latest run; a run landing between them can
mix two id sets for one page load — accepted, a reload restores it.

## What changes

1. `PsycopgForecastStore.list_met_stations` (`packages/common/forecast_store.py`), branch `model_id is None`:
   the predicate `ms.active_flag = true` is replaced by membership in the stations of the basin version's
   latest displayable forecast run:

   ```sql
   ms.basin_version_id = %s
   AND ms.station_id = ANY((
     SELECT array_agg(DISTINCT station_id) FROM met.interp_weight
     WHERE model_id = (
       SELECT h.model_id FROM hydro.hydro_run h
       WHERE h.basin_version_id = %s AND h.run_type = 'forecast'
         AND h.status IN ('succeeded', 'parsed', 'published') AND h.cycle_time IS NOT NULL
       ORDER BY h.cycle_time DESC, h.run_id DESC LIMIT 1)
   )::text[])
   ```

   Same uncorrelated scalar-array shape as the `model_id` branch (#2694): both subqueries are InitPlans, no
   JOIN, no DISTINCT on the outer query. COUNT and page statements share the predicate; params are
   `[basin_version_id, basin_version_id, …]`. The status set and the ordering are the ones
   `_fetch_latest_qhh_identity_candidates` already uses, without a source predicate. `active_flag` is not
   consulted in either branch any more. No displayable run → NULL → empty page, `total_count` 0, HTTP 200.
2. Nothing else changes in that method: the `model_id` branch, search, pagination, order, the response
   `filters` block (the `variables` filter stays "unavailable" without `model_id`).
3. No frontend behaviour change (comment only, task 1.4): `stores/stationLayerData.ts` already calls this branch when the source is unresolved
   and the `model_id` branch otherwise.

node-27 plan of the COUNT statement (EXPLAIN without ANALYZE, heihe, 2026-10-07): total cost 172; InitPlan 1
`Index Scan using hydro_run_forecast_basin_cycle_idx` + Incremental Sort + Limit; InitPlan 2 `Index Only Scan
using interp_weight_qhh_latest_membership_idx`; outer `Index Scan using met_station_pkey`.

## Must preserve

- The `model_id` branch statements byte for byte (pinned by `tests/test_list_search_contract.py` and
  `tests/test_met_station_model_filter_integration.py`).
- 422 `MISSING_REQUIRED_FILTER` without both filters; response envelope and item fields.
- The station MVT tile path (`services/tiles/mvt.py`, `apps/api/routes/hydro_display_identity.py`) keeps its
  `active_flag` rule: the frontend does not use it for this layer, and its source-version/caching design is
  tied to the flag. Out of scope, reported.

## Out of scope

Deleting old station rows (part 3); the activation flip hook not being registered; the station MVT path; the
example count "Heihe 1709" in `openspec/specs/met-station-cluster-layer/spec.md`; a `source` parameter.

## Evidence

- `tests/test_list_search_contract.py`: the basin-only statements contain the new predicate, no
  `active_flag`, no `JOIN met.interp_weight`; params order; the existing "no `met.interp_weight` in the
  basin-only SQL" pin (degrade test, ~:257-272) is replaced, not deleted without replacement, and its two
  `filters.available.variables is False` assertions stay. The basin id is now bound twice, so the wildcard
  escape test (~:317-323, #1669) moves its two positional pins from `count_params[1]` / `[2]` to `[2]` /
  `[3]` — positional per arm, never relaxed to `any(...)`.
- `tests/test_met_station_model_filter_integration.py` (real database, CI lane): basin-only cases with runs
  seeded for two models of one basin version — newest run's model wins; equal `cycle_time` → higher `run_id`;
  a newer `failed`/`superseded` run and a newer non-forecast run are ignored; an inactive station of the
  winning model is listed; an active station of no model or of the older model is not; another basin
  version's station of the same model is not; no displayable run → empty with `total_count` 0; search and
  pagination compose. The seed (`seed_issue_126_data`) already inserts one `parsed` forecast run of
  `MODEL_ID` / `BASIN_VERSION_ID` at `CYCLE_TIME`; newer/older runs are placed relative to it, and the
  tie-break pair of run ids differs only in a trailing lowercase letter (collation-proof).
  Plan shape: `test_interp_weight_is_only_scanned_inside_init_plans` gains a basin-only parameter set —
  `met.interp_weight` is scanned once, under an InitPlan, `Actual Loops == 1`, and `hydro.hydro_run` is
  scanned only under an InitPlan (the scan helper is generalised to take the relation name).
- node-27 after merge (read-only curls against the redeployed display API, or the store called as
  `nhms_display_ro`): heihe 287, qhh 71, one basin that lists 0 today, one retired basin → 0.
