# Tasks — mvt-budget-truncation-signal-2030

Fixture level: expanded · repair intensity: medium · seats round 1: 3 (`correctness`,
`test-evidence+spec-compliance`, `invariant-state`).

## 1. SQL projection

- [ ] 1.1 `services/tiles/mvt.py::postgis_tile_sql` final SELECT: add
  `(SELECT intersecting_feature_count FROM prefilter_stats) AS intersecting_feature_count,` and
  `(SELECT intersecting_coordinate_count FROM prefilter_stats) AS intersecting_coordinate_count,`
  immediately before the existing `(SELECT feature_coordinate_overflow_count FROM prefilter_stats) …` line.
  No other line of the statement changes (input: `git diff` of the function → expected: +2 lines, −0).

## 2. Route signal

- [ ] 2.1 `apps/api/routes/hydro_display.py`: `import logging`; module-level
  `logger = logging.getLogger(__name__)` next to the other module constants.
- [ ] 2.2 `_fetch_postgis_tile_bytes`: read `intersecting_feature_count`, `intersecting_coordinate_count`,
  `feature_coordinate_overflow_count`, `coordinate_dimension_overflow_count` with `int(row.get(k) or 0) if row else 0`.
  After the 424 raise and before `return bytes(...)`: if
  `(intersecting_coordinate_count > coordinate_count or intersecting_feature_count > feature_count)
  and feature_coordinate_overflow_count == 0 and coordinate_dimension_overflow_count == 0` →
  `logger.warning(...)` once, message per design D1, `extra={"layer_id": detail_layer_id, "z": z, "x": x,
  "y": y, "feature_count": …, "intersecting_feature_count": …, "max_features": MVT_MAX_FEATURES,
  "coordinate_count": …, "intersecting_coordinate_count": …, "max_coordinates": max_coordinates}`.
  The 500/413/424 branches are byte-for-byte unchanged.

## 3. Unit tests (`tests/test_hydro_display_mvt_scaling.py`)

- [ ] 3.1 caplog matrix on `_fetch_postgis_tile_bytes(session, "river-network-national", {}, z=3, x=6, y=3)`
  with `NHMS_ENABLE_LIVE_POSTGIS_MVT=true`, `caplog.at_level(logging.WARNING, logger="apps.api.routes.hydro_display")`:
  - (a) `feature_count=23, intersecting_feature_count=23, coordinate_count=38531, intersecting_coordinate_count=38531`,
    overflows 0 → returns `b"pbf-bytes"`, **zero** records containing `MVT_TILE_BUDGET_TRUNCATED`.
  - (b) coordinates `38531 / 86160`, features `23 / 56`, overflows 0 → bytes returned AND exactly one WARNING
    whose message contains `MVT_TILE_BUDGET_TRUNCATED`, `layer_id=river-network-national`, `z=3 x=6 y=3`,
    `coordinate_count=38531/86160`, `feature_count=23/56`, `max_coordinates=120000`, `max_features=10000`;
    `record.layer_id == "river-network-national"`, `record.intersecting_coordinate_count == 86160`.
  - (c) features `23 / 24`, coordinates equal, overflows 0 → one WARNING (feature arm alone fires).
  - (d) coordinates `38531 / 86160`, `feature_coordinate_overflow_count=1`, dimension overflow 0 → **zero** records.
  - (e) coordinates `38531 / 86160`, feature overflow 0, `coordinate_dimension_overflow_count=1` → **zero** records.
  - (f) `hydro-national` with `{"variable": "q_down"}` and coordinates `40000 / 60000`, overflows 0 → one
    WARNING with `layer_id=discharge` (the public id `public_hydro_layer_id("q_down")` the 413 details use)
    and `max_coordinates=50000`.
- [ ] 3.2 `test_every_tile_layer_projects_the_prefilter_intersecting_counts`: for each of the five layers,
  the slice of `postgis_tile_sql(layer)` after the literal `SELECT ST_AsMVT(` (unique; the `ST_AsMVTGeom` in `clipped`
  is not preceded by `SELECT`) contains `(SELECT intersecting_feature_count FROM prefilter_stats) AS intersecting_feature_count`
  and `(SELECT intersecting_coordinate_count FROM prefilter_stats) AS intersecting_coordinate_count` exactly once each.
  (The bare `AS intersecting_*` aliases already occur once inside the `prefilter_stats` CTE on the pre-change tree, so a
  whole-statement count of 1 would be red against the correct implementation.)
- [ ] 3.3 `test_every_column_the_tile_route_reads_is_projected_by_every_layer` (route↔SQL coverage lock):
  parse `inspect.getsource(hydro_display._fetch_postgis_tile_bytes)` with
  `re.findall(r'row\.get\("([a-z_]+)"\)', …)` (also match `row\["([a-z_]+)"\]`), assert the set is non-empty and
  contains the four new/now-read keys, then for each of the five layers assert every key appears as
  `AS <key>` in the text after the final `SELECT ST_AsMVT` / outer `SELECT` of `postgis_tile_sql(layer)`.
  Expected: passes on the new head; **red on pre-change `mvt.py`** (the two intersecting columns missing).
- [ ] 3.4 `_budget_row(coordinate_count)` gains `"intersecting_feature_count": 12,
  "intersecting_coordinate_count": coordinate_count, "feature_coordinate_overflow_count": 0,
  "coordinate_dimension_overflow_count": 0` so the existing three 413 tests and the bind-site test stay
  green **and** silent (add one assertion to
  `test_national_river_tile_over_the_shared_limit_but_within_its_own_is_rendered`: no WARNING record).
- [ ] 3.5 Red proof (batched, implementer): with pre-change `services/tiles/mvt.py` + `hydro_display.py`,
  3.1(b)(c)(f), 3.2, 3.3 fail; 3.1(a)(d)(e) and every pre-existing test pass. Paste the red run output.

## 4. Local verification (orchestrator, Phase 2)

- [ ] 4.1 `uv run ruff check .` → clean.
- [ ] 4.2 `uv run pytest -q tests/test_hydro_display_mvt_scaling.py tests/test_hhe_mvt_binding.py
  tests/test_display_publish_status_only.py tests/test_river_ts_template_golden.py
  tests/test_river_ts_read_path_surrogate_keys.py tests/test_sql_shape_helpers.py tests/test_tile_publisher.py
  tests/test_node27_mvt_prewarm.py` → all pass;
  the golden test passes **without** touching `GOLDEN_SHA256` or `tests/fixtures/river_ts_templates_*.json`.
- [ ] 4.3 `openspec validate mvt-budget-truncation-signal-2030 --strict --no-interactive` → valid.
- [ ] 4.4 Five-layer SQL regression (local, orchestrator): for each layer, `difflib.unified_diff` of
  `postgis_tile_sql(layer)` at `origin/master` (4b54e8d6) vs the PR head → exactly two `+` lines (the two
  projections), zero `-` lines; new sha256[:16] digests recorded in the receipt (baseline at 4b54e8d6:
  `river-network-national d35ec3b9a9dd539b`, `river-network c2b710cf13b5df6e`, `hydro 83de9958f39c395b`,
  `hydro-national da706a863a85fb99`, `met-stations 2691870627724076`).

## 5. node-27 live receipt (orchestrator, isolated worktree, RO role, `TMPDIR=/home/nwm/tmp`)

- [ ] 5.0 Setup: isolated worktree at the PR head (never the production checkout), `NHMS_ENABLE_LIVE_POSTGIS_MVT=true`
  (without it `_require_live_postgis_mvt` answers 424 for every in-process call), RO role URL extracted on the node
  and never written to disk in the repo, `TMPDIR=/home/nwm/tmp`. Old SQL text for 5.3 comes from
  `git show 4b54e8d6:services/tiles/mvt.py` loaded as a throwaway module (or the master checkout's import).
- [ ] 5.1 Signal fires: in-process, `hydro_display.collection_coordinate_limit` monkeypatched to 40000 for tile 3/6/3 and to 15000
  for 9/405/209 (`river-network-national`), `_fetch_postgis_tile_bytes` called against the live RO DB →
  returns non-empty bytes AND one `MVT_TILE_BUDGET_TRUNCATED` WARNING each, with `coordinate_count`
  < `intersecting_coordinate_count` (expected order of 38 5xx/86 160 and 14 9xx/22 592 per the #2025 receipt).
- [ ] 5.2 Production bind silent (`river-network-national`): one pass over `xyz_tiles(CHINA_BOUNDS, [3,4,5,6,7])`
  (516 tiles) with the new SQL and production binds → on every tile `intersecting_feature_count == feature_count`,
  `intersecting_coordinate_count == coordinate_count`, both overflow counts 0; zero WARNING records.
  `hydro-national q_down` (latest valid_time) on 3/6/3 + 5/25/12: measure and report the four counts and whether the
  WARNING fired — **no expected-silent assertion** (its production truncation state has never been measured; a
  WARNING there is the signal working, not a defect, and is reported as a finding for the operator).
- [ ] 5.3 Bytes unchanged: md5(tile) old-SQL vs new-SQL equal on 0/0/0, 1/1/0, 2/3/1, 3/6/3, 4/12/6, 5/25/12,
  6/52/24, 7/107/46, 9/405/209, 9/404/208 (`river-network-national`) and on 3/6/3 + 5/25/12 for
  `hydro-national q_down` at the current latest valid_time.
- [ ] 5.4 Receipt `docs/runbooks/receipts/2026-09-08-issue-2030-budget-truncation-signal-node27.md`: method,
  worktree SHA, 5.1–5.3 tables, the 4.4 digest table (supersedes the #2025 five-digest table), and the
  no-version-bump statement.

## Evidence Floor (issue #2030 acceptance criteria → evidence)

| Criterion | Evidence |
|---|---|
| `intersecting_feature_count` / `intersecting_coordinate_count` in the shared final SELECT of all five layers | 1.1, 3.2, 4.4 |
| `_fetch_postgis_tile_bytes` emits an observable signal with `layer_id/z/x/y` and both sides' numbers when selected < intersecting and both overflows are 0 | 2.2, 3.1(b)(c)(f), 5.1 |
| No false positive on the two non-budget drop paths | 3.1(d)(e) |
| Stub-row boundary: equal → silent; intersecting > selected → fires | 3.1(a) vs (b)(c) |
| Five-layer SQL regression evidence; bytes unchanged; no `*_QUERY_VERSION` bump | 4.4, 5.3, design D6, receipt 5.4 |
| node-27 live receipt: forced low limit fires; production bind silent on all 516 tiles | 5.1, 5.2, 5.4 |

## Risk pack → evidence

- Schema/columns: 1.1, 3.2, 3.3.
- Legacy compatibility: 3.4 (413 tests unchanged), 4.2 (golden untouched), 4.4, 5.3.
- Error handling / partial outputs: 3.1(d)(e), D2/D4.
- Documentation: 5.4.
- Display identity (domain): D6, 5.3.
- PostGIS domain (domain): 5.1, 5.2 on real PostGIS.
