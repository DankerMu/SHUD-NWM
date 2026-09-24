# node-27 live receipt: #1729 evidence basin delete + #1480 seed provenance backfill

- Code: PR #2620, merge `035d8a8ed6649f2adf01f6931eb68c538498ac0c`; node-27 `/home/nwm/NWM` fast-forwarded to it.
- Executor: `scripts/ops/node27_oneshot_sql.py`, run as the owner role `nhms` over TCP `127.0.0.1:55432/nhms` (`DATABASE_URL` from `infra/env/node27-timeseries-compression-replay.env`). Each run is one transaction with `lock_timeout=10s` and `statement_timeout=300s`.
- Sequence: the user confirmed twice (once for the dry-run, once for `--apply`), per D5.
- Window: 2026-09-24 12:30–12:42Z (20:30–20:42 CST). The compression and retention systemd timers run 11:35–14:36 CST, so they were outside the window. TimescaleDB had no compression policy job scheduled.

## 1. Display deploy (read-side filter, D5 step 2)

- Migrations: the ledger was already at `000061` before the pull, and there were no dependency changes.
- Restart: `scripts/ops/start-display-api.sh`, new main PID 1238868, 2 workers, smoke check OK.
- `GET /api/v1/runtime/config` returned `display_readonly=true` and `control_mutations_enabled=false`. `/api/v1/slurm/health` returned 404.

Public `https://test.nwm.ac.cn`, before and after the deploy (rows still present in the DB):

| Request | Before (12:0xZ) | After deploy (12:27Z) |
|---|---|---|
| `/api/v1/basins?limit=500` | 200, 65 rows, includes `basin__evidence_cmfd_p02_synth` | 200, 64 rows, evidence basin absent (only change) |
| `/api/v1/basins?has_display_product=true` | 48 | 48 (unchanged) |
| `/api/v1/basins/basin__evidence_cmfd_p02_synth/versions` | 200 with synthetic MultiPolygon | 404 `MODEL_REGISTRY_NOT_FOUND` |
| `/api/v1/models/model__evidence_cmfd_p02_synth__v1` | — | 404 |
| `/api/v1/models?active=all` | — | 258 items, none evidence |

Default `/basins` compared with the node-22 `scheduler/registry/manifest-last.json` (96 models / 48 basins):
- The public default list contains 16 real basins that are not in the manifest. #2621 owns that difference.
- `has_display_product=true` equals the manifest set.

## 2. Live dry-run (runner default: ROLLBACK), 12:30:39Z

- #1729:
  - Every assertion passed: basin_group, the 20-FK set, no triggers, the ID sets, and 0 non-hypertable dependents.
  - Locked rows: basin=1, basin_version=1, river_network_version=1, mesh_version=1, model_instance=2, met_station=6.
  - Retained and not deleted: `ops.audit_log` (entity_id equality) 13 rows. `ops.pipeline_job` 0, `hydro.state_snapshot.cloned_from_model_id` 0, `flood.return_period_result` 0.
  - Timing: deleting `met.met_station` (6 rows) took 282.2s. That time is the NO ACTION RI lookups over every hypertable chunk. The session sat in `IO/DataFileRead` and never waited on a `Lock`, and the cluster had 0 lock waiters during the run. All other steps took under 0.02s.
- #1480:
  - Before: `heihe|qhh.tsd.forc|qhh.tsd.forc` = 1709, `qhh|qhh.tsd.forc|qhh.tsd.forc` = 386.
  - Backfilled 1709 rows, then rolled back. Took 5.3s.

## 3. Apply, 12:41:03Z

| Step | Result | Time |
|---|---|---|
| #1480 `--apply` | 1709 rows backfilled; after: `heihe\|heihe.tsd.forc\|heihe.tsd.forc` = 1709, `qhh\|qhh.tsd.forc\|qhh.tsd.forc` = 386; COMMITTED | 0.63s |
| #1480 rerun (dry-run, `expected_rows=0`) | backfilled rows: 0 (idempotent) | 0.47s |
| #1729 `--apply` | Same locks and retained counts as the dry-run; deleted met_station 6 (73.9s), model_instance 2, mesh_version 1, river_network_version 1, basin_version 1, basin 1; COMMITTED | 74.5s |

Read-only counts afterwards:
- `core.basin` is 64. The evidence basin, bv, rnv, mesh, models and stations are all 0.
- Seed-station groups are exactly `heihe|heihe.tsd.forc` 1709 and `qhh|qhh.tsd.forc` 386.

Public API after the apply: `/api/v1/basins` returns 64 rows and no evidence basin, and the versions endpoint returns 404.

## 4. Backups and rollback

The authoritative backups are the files written by the `--apply` runs. They were fsynced before COMMIT and are on node-27:
- `/home/nwm/tmp/1729-delete-apply-20260924T124103Z/`: `core.basin.copy`, `core.basin_version.copy`, `core.river_network_version.copy`, `core.mesh_version.copy`, `core.model_instance.copy`, `met.met_station.copy`, with 1/1/1/1/2/6 rows. They are byte-identical to the dry-run files (`diff -r`).
- `/home/nwm/tmp/1480-backfill-apply-20260924T124103Z/met_station_properties_json.copy`: 1709 rows with the full original `properties_json`.

A local copy is kept off node-27 by the operator.

SHA-256:

```
8e9562804239ad845e0eb9039aec76bc3e3e44fc08a621bb10cbde6f88849c99  core.basin.copy
140e76f4de56a642be6c54e411f4644457af16fee113c44821c7de13b557754e  core.basin_version.copy
322088fda4a980115954c71e3a4a26dc0162f89cb22e83c4a9c14d44dc844a3d  core.river_network_version.copy
321418bd780c52311b2f529eb46f533667e6d3839f9912ec16db48f114e9cab3  core.mesh_version.copy
2aedbb18286cba313198a541e9c11eb05eda1f96f344ccb4fc9a8f1a60faa658  core.model_instance.copy
b17398aad676684de38bfbf7cf4f59c4a7c5cf06d4a523e6775af65115983443  met.met_station.copy
559dea99bdd8a4d8d369cf890b4b28c0eefed1755c90258c00946525906b2d8a  met_station_properties_json.copy
```

Rollback commands, if ever needed (run from `/home/nwm/NWM` with the same env):
- `node27_oneshot_sql.py scripts/ops/node27_1729_delete_evidence_basin_rollback.sql --dsn-env DATABASE_URL --copy-dir /home/nwm/tmp/1729-delete-apply-20260924T124103Z --apply`
- `node27_oneshot_sql.py scripts/ops/node27_1480_backfill_seed_station_provenance_rollback.sql --dsn-env DATABASE_URL --copy-dir /home/nwm/tmp/1480-backfill-apply-20260924T124103Z --apply`

## 5. Arguments recorded instead of scans (D3)

- `hydro.river_timeseries.basin_version_key` / `river_network_version_key` have no FK. Its `run_key` is an FK to `hydro.hydro_run` (`000059:13`), and the evidence basin had 0 `hydro_run` rows (asserted), so no row there could reference the basin.
- `met.forcing_station_timeseries_legacy.basin_version_id` has no FK. It relies on the NOT NULL `forcing_version_id` FK to `met.forcing_version`, and the evidence models had 0 `forcing_version` rows (asserted).
- `met.forcing_station_timeseries.station_key` and `_legacy.station_id` have NO ACTION FKs, which the delete relied on. The commit succeeded, so no referencing row existed.
- Listed but not scanned (no FK to the deleted rows): `met.canonical_grid_*`, `ops.pipeline_event`, `ops.qc_result`.
- Provenance of the 20-FK pin: `.workplans/k3/k3-catalog.out` is the live `pg_constraint` inventory with `conrelid` restricted to schemas other than `_timescaledb_internal`. It matches `c_expected_fks` in the delete script entry for entry.
