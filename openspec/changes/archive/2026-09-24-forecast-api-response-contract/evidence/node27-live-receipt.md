# node-27 live receipt: forecast-api-response-contract (PR #2623)

- **Merge:** `0c75aba80` (PR #2623). The node-27 `/home/nwm/NWM` checkout was clean (`git status --porcelain` empty) and moved from `035d8a8e` to `0c75aba8` with `git pull --ff-only`.
- **Restart:** `scripts/ops/start-display-api.sh`: `OK systemd_main_pid=1403750 workers=2 basin_id=basins_sw_ylzb (smoke check passed)`. Captured on 2026-09-24 (UTC).
- **Pre-state:** captured 2026-09-24T12:59:35Z on the `035d8a8e` display, from the same 21 routes on `https://test.nwm.ac.cn/api/v1`.

## C1 (tasks 5.1)

```text
/health                 200 {"status":"ok","service":"nhms-api","version":"0.1.0"}
/api/v1/runtime/config  200 service_role=display_readonly control_mutations_enabled=false slurm_routes_enabled=false display_readonly=true
/api/v1/slurm/health    404 {"detail":"Not found"}
```

`/slurm/health` (no `/api/v1`) returns the SPA `index.html` fallback with a 200. It is not a Slurm route.

## Structural comparison (tasks 5.2)

Method: each route is re-fetched and both bodies are parsed. For each body I build the set of `(JSON path, JSON value type)` pairs, where array elements share the path `[]` and the types are `null`, `bool`, `int`, `float`, `str`, `list` and `object`. The pre and post sets are then compared for equality, so a key set or value type that changed anywhere in the tree, including int vs float and null vs absent, shows up as a diff.

| route | post status | result | paths (pre / post) |
|---|---|---|---|
| basins | 200 | IDENTICAL-SHAPE | 10 / 10 |
| basin_versions | 200 | IDENTICAL-SHAPE | 25 / 25 |
| models | 200 | IDENTICAL-SHAPE | 49 / 49 |
| model | 200 | IDENTICAL-SHAPE | 104 / 104 |
| segments | 200 | IDENTICAL-SHAPE | 35 / 35 |
| segment | 200 | IDENTICAL-SHAPE | 47 / 47 |
| runs | 200 | IDENTICAL-SHAPE | 35 / 35 |
| run | 200 | IDENTICAL-SHAPE | 29 / 29 |
| latest_product | 200 | IDENTICAL-SHAPE | 66 / 66 |
| forecast_series | 200 | IDENTICAL-SHAPE | 15 / 15 |
| data_sources | 200 | IDENTICAL-SHAPE | 63 / 63 |
| cycles | 200 | IDENTICAL-SHAPE | 21 / 21 |
| met_stations | 200 | IDENTICAL-SHAPE | 18 / 18 |
| best_available | 200 | IDENTICAL-SHAPE | 1 / 1 |
| pipeline_status | 200 | IDENTICAL-SHAPE | 15 / 15 |
| pipeline_stages | 200 | IDENTICAL-SHAPE | 36 / 36 |
| jobs | 200 | IDENTICAL-SHAPE | 28 / 28 |
| stage_duration | 200 | IDENTICAL-SHAPE | 9 / 9 |
| success_rate | 200 | IDENTICAL-SHAPE | 9 / 9 |
| queue_depth | 503 | IDENTICAL-SHAPE | 9 / 9 |
| state_snapshots | 200 | IDENTICAL-SHAPE | 5 / 5 |

All 21 routes have an identical shape; no path or type was added or removed.

These three give no structural evidence, as the fixture expected:
- `queue_depth` is the display_readonly 503 envelope (`CONTROL_PLANE_QUEUE_UNAVAILABLE`), the same before and after.
- `best_available` is `[]`.
- `state_snapshots` is an empty page.

`run` and `runs` keep the same 25-key `HydroRun` set as before. The live `hydro.hydro_run` has no non-public column today, so #2222 filters nothing on live data.

## #2177 source spellings

```text
source=gfs: 200 data_sha256=b4e035febe51a0b97dcbf14613f17b0614fdd4361f6d9eec240fe0ad96015786 total=4972
source=GFS: 200 data_sha256=b4e035febe51a0b97dcbf14613f17b0614fdd4361f6d9eec240fe0ad96015786 total=4972
source=Gfs: 200 data_sha256=b4e035febe51a0b97dcbf14613f17b0614fdd4361f6d9eec240fe0ad96015786 total=4972
spellings identical: True
```

## Timing gate (D2: p50 of 8 may regress by at most max(15 %, 30 ms))

| route | pre p50 (s) | post p50 (s) | delta | gate |
|---|---:|---:|---:|---|
| forecast-series (run_id) | 0.440 | 0.397 | -44 ms (-9.9 %) | PASS |
| latest-product | 0.248 | 0.270 | +22 ms (+9.1 %) | PASS |
| runs?limit=50 | 0.331 | 0.344 | +13 ms (+3.8 %) | PASS |

## Production log since the restart

`/tmp/display-api.log` from the last `Application startup complete` (711 lines):
- 0 responses with status 500.
- 0 `RESPONSE_VALIDATION_ERROR`.
- The only `ERROR` line is the expected `CONTROL_PLANE_QUEUE_UNAVAILABLE` 503 from `/api/v1/queue/depth` in display_readonly mode.

## Pre-merge verification recap

- **node-27 full pytest on `5f901ff8a`:** 20726 passed, 1 failed, 62 skipped. The one failure is the pre-existing #2615, the same set as the K3 baseline.
- **node-27 fix delta on `b0e670808`:** 1340 passed.
- **CI on `e02a79d96`:** Unit Tests, Frontend Build, OpenAPI Validate, SQL Migration Dry Run and Entropy Audit all passed.
- **fix_gate:** round 1 not clean (1 P1, 3 P2), round 2 not clean (1 P2), round 3 clean.
- **Deferred:** #2624 (pipeline oracle selector gap). #2048 owns the live `hydro.run_status` `frequency_done` drift.
