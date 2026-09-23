# node-27 receipt — #2504 read-only populated-but-empty coverage audit (PR #2588 task 6.5)

- Capture: node-27, by the batch-G1 orchestrator on 2026-09-23 (`2026-09-23T10:51:46Z`), from an
  isolated oracle worktree at the PR head `4a72f7f13219f12c34562b602b7d5a3dddf1cd11`.
- **Read-only.** DSN role `nhms_display_ro` (no INSERT/UPDATE/DELETE). The audit is
  `scripts/node27_refresh_coverage.py --audit-populated-empty`: a readonly autocommit session,
  one `SELECT EXISTS` per populated eligible coverage row, bounded to that row's stored
  `[river_valid_time_start, river_valid_time_end]`, each its own statement with
  `statement_timeout = 30000` and `lock_timeout = 2000`, so no AccessShareLock is held across chunks.
  Zero writes: no coverage row, unit or env file touched.
- Window: `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS=21`, taken from the live retention env
  (`infra/env/node27-timeseries-retention.env`; raw header `window_days(from retention env)=21`) for
  this command only — `node27-ingest.env` does not carry it (design D6).
- Timing: run outside the retention (06:36 UTC) and compression (04:25 UTC) windows; the capture
  records both units `inactive` at start.
- Result: `in_window` 0 empty of 4089; `out_of_window` 2616 empty of 2666; `null_end` 0;
  `probe_failed` 0 in both buckets; `elapsed_s` 101 (6755 probes, serial). Watermark
  `2026-09-22T12:00:00Z`, cutoff `2026-09-01T12:00:00Z` (watermark − 21 days).
- Reading: no populated in-window row has lost its facts (the #1446 guard's protected band holds
  nothing anomalous); 2616 out-of-window rows advertise an empty curve and are what the first
  window-enabled convergence lowers to 0 (expected audit delta after deployment:
  `out_of_window.empty` 2616 → 0, `out_of_window.total` 2666 → 50 plus whatever crossed the cutoff
  since). The deployment command is in
  [`../../production-ops/service-bringup.md`](../../production-ops/service-bringup.md) (#2504 bullets).
  Production convergence itself is oracle-blocked on deployment authorization.

## Raw output (verbatim)

```
## 6.5 audit receipt 2026-09-23T10:51:46Z head=4a72f7f13219f12c34562b602b7d5a3dddf1cd11
retention=inactive compression=inactive
window_days(from retention env)=21 role=nhms_display_ro
{
  "cutoff": "2026-09-01T12:00:00Z",
  "elapsed_s": 101.161,
  "in_window": {
    "empty": 0,
    "probe_failed": 0,
    "sample_empty_run_ids": [],
    "total": 4089
  },
  "mode": "audit-populated-empty",
  "null_end": {
    "total": 0
  },
  "out_of_window": {
    "empty": 2616,
    "probe_failed": 0,
    "sample_empty_run_ids": [
      "fcst_gfs_2026053106_basins_heihe_shud",
      "fcst_gfs_2026053106_basins_qhh_shud",
      "fcst_gfs_2026053112_basins_heihe_shud",
      "fcst_gfs_2026053112_basins_qhh_shud",
      "fcst_gfs_2026053118_basins_heihe_shud",
      "fcst_gfs_2026053118_basins_qhh_shud",
      "fcst_gfs_2026060100_basins_heihe_shud",
      "fcst_gfs_2026060100_basins_qhh_shud",
      "fcst_gfs_2026060106_basins_heihe_shud",
      "fcst_gfs_2026060106_basins_qhh_shud"
    ],
    "total": 2666
  },
  "watermark": "2026-09-22T12:00:00Z",
  "window_days": 21
}
rc=0
```
