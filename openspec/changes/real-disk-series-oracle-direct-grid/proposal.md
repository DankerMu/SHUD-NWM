# The real-store station-series oracle follows the Direct Grid artifacts

Issue: #2699 part (A), and the series half of part (B); completes #2595. Fixture level: **compact**.
Risk packs: **Test evidence fidelity**. Tests and spec text only: no production code changes.

## Why

`tests/test_object_store_forcing_real_disk.py` is the only oracle of
`GET /api/v1/met/stations/{id}/series` against the real object store (node-27 only). #2595 made it pick a live
cycle, but its four combos still name the legacy stations and models (`heihe_forc_001` / `basins_heihe_shud`,
`qhh_forc_001` / `basins_qhh_shud`). The store holds no such directory in any retained cycle: every cycle has
only Direct Grid directories, `forcing/<src>/<cycle>/<basin_version>/dg_<hash>/shud/station_000NN.csv`. So
the suite fails with its own "no cycle where all 4 combos are present" diagnostic and the read path has no
real-store evidence.

Measured on node-27, 2026-10-07 (read-only): `basins_heihe_vbasins` has four `dg_*` models (two generations
per source), each with 287 stations whose ids are `dg-<src>-<hash>::cell:<n>`; the station prefix differs per
model, so a station id cannot be derived from a model id or the other way round. The link is
`met.interp_weight(model_id, station_id)`. A live response of a Direct Grid station is recorded in this
change as `dg-series-sample.node27-20261007.json` (heihe, IFS, cycle 2026-10-06T12Z): five variables, 53
points each, station `properties_json` with the Direct Grid keys (`direct_grid`, `forcing_filename`
`station_000NN.csv`, `grid_cell_id`, …) and none of the legacy ones.

Owner decision (2026-10-07): a legacy `model_id` on the series path answering 404 once its artifacts have
left the store is the expected behaviour and is recorded in the spec; the server does not map it to a Direct
Grid variant.

## What changes

1. The suite derives its combos instead of naming them. A combo is `(basin_version_id, source)`, for the two
   basin versions and two sources the suite covers today (`basins_heihe_vbasins`, `basins_qhh_vbasins` ×
   `IFS`, `gfs`). For a candidate cycle, each combo is resolved to `(model_id, station_id)`:
   - `model_id`: the one directory named `dg_*` under
     `<root>/forcing/<source lower>/<cycle>/<basin_version_id>/`. Zero or more than one such directory makes
     the combo absent for that cycle (more than one is reported in the diagnostic: the suite does not guess).
   - `station_id`: `SELECT min(station_id) FROM met.interp_weight WHERE model_id = %s` — by `model_id`
     only, as the production list path does (a `dg_*` model is bound to one source; no `source_id` and no
     `active_flag` predicate), the minimum taken by the database. No row makes the combo absent with the
     reason "no interp_weight row for <model_id>". Then the station lookup the suite already uses gives
     `basin_version_id` and `forcing_filename`, and the file must exist and be settled (the existing 600 s
     rule).
   There is no shared connection today (`PsycopgStationLookup.from_env()` opens its own per lookup): the
   real-disk test module opens one read-only connection from `DATABASE_URL` for the resolver and passes the
   resolver to the support module as a callable, so the support module stays free of any database import.
   The cycle chosen is the newest one in which all four combos resolve. The support module
   (`tests/object_store_forcing_real_disk_support.py`) owns this; its selection logic stays testable without
   a database (the model/station resolver is passed in), and its local unit suite
   (`tests/test_object_store_forcing_real_disk_support.py`) covers: all four resolve; a combo with no `dg_*`
   directory; a combo with two; a combo with no weight row; a station file missing or not settled; no cycle complete. The diagnostic keeps
   what `node27-test-oracle-lanes` requires today (the root and the per-combination counts) and adds, per
   combo, the reason it was absent in the newest cycle examined.
2. Every test of the suite uses a resolved combo, not a literal station or model: the four-combo 200 test;
   the error/filter test (first resolved combo; `MISSING_CYCLE` and `bogus_forc_999` stay literal
   negatives); the side-effect-free test (first resolved combo); the shape test (the
   `(basins_heihe_vbasins, IFS)` combo only, the one the baseline was recorded from: the point count of
   another source or basin is not known).
   The 404 detail depends on the station: for a station with `active_flag` false (every Direct Grid station
   today) the reader returns `details == {"station_id": <id>}` and no path; only for `True` / unknown does it
   return `expected_path`. The `MISSING_CYCLE` assertion branches on the resolved station's `active_flag`:
   false → details are exactly the station id and the response text holds no part of the store root; else
   → `expected_path` starts with `forcing/<source lower>/2020010100/`.
3. One more negative, for the owner decision: the legacy model id of a covered basin
   (`basins_heihe_shud`) with a resolved Direct Grid station and the chosen cycle answers 404
   `STATION_FORCING_FILE_NOT_FOUND`. Only the status and the code are asserted (the details carry no model id for
   an inactive station), together with the 200 of the same station, cycle and source under its real model id
   in the same test, so the 404 is attributable to the model id alone.
4. The shape baseline becomes a Direct Grid one: `tests/fixtures/station_series_baseline_direct_grid.json`,
   made from the recorded sample: the served envelope as recorded (the shape test reads through the API, so
   the key order is the served one), written compact on one line so it stays far below the 1000-line limit
   with no guard exemption. The legacy fixture `station_series_baseline_heihe_ifs_2026060100.json` **stays
   and is not touched**: `tests/test_object_store_forcing.py` (not gated), the CI selector and its guard, the
   large-file guard and `ci-contract-baseline` all read it, and it has the reader's own key order, which
   differs from the served one. Only the real-disk suite moves to the new baseline. The new fixture has no
   ungated reader, so it needs no selector rule (same reasoning as the selector's note on the old one). The shape comparison keeps what
   it checks today (key order and nesting of the station block including `properties_json`, per-variable
   point count equal to the baseline's, variable order a subsequence of the baseline's); the point count is
   53 in the sample, as in the old baseline.
5. Each test prints the chosen `(cycle, basin_version, source, model, station)` so a node-27 run with `-rA`
   or `-s` records what was measured.
6. Skipping stays as it is (the three environment gates); a real store with no resolvable cycle fails with
   the diagnostic and never skips.

## Must preserve

- No production code changes. `packages/common/object_store_forcing.py` is read, not edited.
- The suite only reads: the database through `DATABASE_URL` as given (node-27 runs it as
  `nhms_display_ro`), files through `OBJECT_STORE_ROOT`.
- CI behaviour: the suite still skips without its gates; the support unit suite runs in CI.
- No file over 1000 lines; Python 3.11 and 3.12.

## Out of scope

The station list endpoint (part (B) of #2699, a separate change); the station-set flip; retention.

## Evidence

- Local: the support unit suite (cases in item 1) and a local run of the real-disk suite against a temporary
  store and a stub resolver where the suite already has such a harness; `uv run ruff check .`;
  `tests/test_select_ci_tests.py` (after staging, last); `openspec validate` strict.
- node-27 after merge (read-only, `nhms_display_ro`): the suite passes and its output names the four chosen
  combos. This is the receipt #2595 was left open for.
