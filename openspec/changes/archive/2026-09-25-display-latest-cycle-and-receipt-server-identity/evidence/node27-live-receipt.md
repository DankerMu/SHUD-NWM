# node-27 live receipt: display-latest-cycle-and-receipt-server-identity (PR #2629)

- **Merge:** `2024a5e4e` (PR #2629). The node-27 `/home/nwm/NWM` checkout was clean (`git status --porcelain` empty) and moved from `0c75aba8` to `2024a5e4` with `git pull --ff-only`.
- **Restart:** `scripts/ops/start-display-api.sh` at 2026-09-25T08:34:48Z: `OK systemd_main_pid=1736309 workers=2 basin_id=basins_sw_ylzb (smoke check passed)`.
- **Pre-state:** tasks 1.2, captured 2026-09-24 on the `0c75aba8` display.
- **Data drift between pre and post:** the IFS cycle `2026-09-24T12:00Z` was ingested after the pre capture, so the post responses resolve `issue_time` to 12Z where the pre ones resolved 00Z. Both are the latest cycle at capture time.

## C1 (tasks 5.1)

```text
/health                 200 {"status":"ok","service":"nhms-api","version":"0.1.0"}
/api/v1/runtime/config  200 service_role=display_readonly control_mutations_enabled=false slurm_routes_enabled=false
/api/v1/slurm/health    404 {"detail":"Not found"}
```

## Request timing (tasks 5.2)

node-27 local (`http://127.0.0.1:8080`), one warm-up request, then 8 timed requests (`curl -w %{time_total}`). All 16 post requests returned 200.

| request | pre p50 | pre max | post p50 | post max |
|---|---|---|---|---|
| issue URL: `basin-versions/basins_wj_vbasins/river-segments/basins_wj_shud_reach_000001/forecast-series?river_network_version_id=basins_wj_rivnet_vbasins&variables=q_down&scenarios=IFS&include_analysis=false&issue_time=latest` | 1.381 s | 1.392 s | **0.237 s** | 0.283 s |
| latest-product: `mvp/qhh/latest-product?source=gfs&basin_id=basins_huaiyss` | 0.049 s | 0.052 s | 0.049 s | 0.053 s |

```text
post issue URL:      0.251996 0.237570 0.236190 0.282927 0.232790 0.241472 0.182003 0.206463
post latest-product: 0.053189 0.051789 0.051317 0.049188 0.047017 0.049737 0.047685 0.043866
```

- **D11 (500 ms P95), indicative with 8 samples:** the issue URL max is 0.283 s, under 500 ms.
- **Attribution:** statement 2 (`latest_cycle_discovery`) is ~4 ms of that request. The latest-product request does not go through `_per_source_latest_cycles`, and its p50 is unchanged. The #2516 fence acts on the narrow station leg, which tasks 3.4 measured with the CTE fallback forced (statement 517524 → 466291 hit); the default request above does not take that fallback.
- **Statement 3 (#2417):** on the issue pin, `forecast_segment_rows` is 2882 hit / ~4.3 ms for `[IFS]`, but 247264 hit / ~151 ms for `[GFS, IFS]`. That is the remaining cost of a two-source request, tracked in #2417, and is not changed by this PR.

## Statement 2 EXPLAIN after deploy (tasks 5.2)

`EXPLAIN (ANALYZE, BUFFERS)`, read-only (`nhms_display_ro`), SQL rendered and driven by the deployed checkout (`head 2024a5e4e8b1a399002723306a99bfeee688d66e`), issue pin, cold then warm ×3.

| scenarios | result | cold | warm ×3 | pre (tasks 1.1) |
|---|---|---|---|---|
| `[IFS]` | `forecast_ifs_deterministic` → `2026-09-24T12:00Z` | 1381 hit / 7.0 ms | 1381 hit / 4.24, 3.63, 3.67 ms | 409069 hit |
| `[GFS, IFS]` | GFS and IFS → `2026-09-24T12:00Z` | 1454 hit / 6.9 ms | 1454 hit / 4.46, 3.75, 3.72 ms | — |

`shared_read` is 0 in every sample. Gate `shared hit <= 5000`: pass on both. Output is at `/home/nwm/tmp/l2/d1-postdeploy.out` on node-27.

## Production log (tasks 5.2)

`/tmp/display-api.log` from the first `Started server process` of this restart (line 1835417) onward: **0** lines with an HTTP 500 or a `Traceback`. The log also holds 500s from before the previous restart (the #2623 deploy), for `/api/v1/met/stations` and a `forecast-series` call without `variables`. None falls between that restart and this one, so none of them comes from this change.
