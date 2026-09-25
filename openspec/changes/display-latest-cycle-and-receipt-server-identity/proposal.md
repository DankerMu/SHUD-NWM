## Why

Batch L2 of the 10-batch serial run (master `64f47adee`, after L1 #2623 / #2625).

- **#2424:** `PsycopgForecastStore._per_source_latest_cycles` (`packages/common/forecast_store.py:1151-1186`) answers a run-metadata question — which cycle has rows for this segment — by scanning the fact table.
  - `issue_time=latest` is the default request shape of the public forecast-series route (`apps/api/routes/forecast.py`, `Query(default="latest")`).
  - **Live measurement (node-27, 2026-09-24, `0c75aba8`, `nhms_display_ro`, pin `basins_wj_vbasins` / `basins_wj_shud_reach_000001` / IFS):**
    - The statement costs **409069 shared hits / ~1.1 s warm and returns 1 row**, which is 82× the D11 `shared hit <= 5000` gate.
    - The legacy branch the issue measured (647275 / 9.6 s) is already gone, because `STORES == ("narrow",)`.
    - Every compressed chunk costs about 25900 blocks to find the segment's ~30 batches. Compression `segmentby` is `(run_key, river_segment_key)`, so the per-chunk compressed index leads with `run_key`, and a probe on the segment key alone cannot seek.
- **#2516:** the narrow forcing leg of the QHH latest-product membership EXISTS compares `iw.variable = fst.variable_e::text` (`packages/common/forecast_store.py:392`).
  - Because of that comparison, `interp_weight_qhh_latest_membership_idx (model_id, station_id, variable, LOWER(source_id))` loses its `variable` column.
  - The result is 1→4 rows per probe and 2.4× the buffers at that node (issue body, node-27 EXPLAIN).
  - The byte-identical sibling in `packages/common/display_coverage.py:278` has the same problem.
- **#2418:** `nhms-pgdata-workload` receipts (`packages/common/node27_pgdata_workload.py:134-177`) record the query identity but not the server that answered it. So a `live: true` receipt from a disposable PostgreSQL is indistinguishable from one taken against node-27 production.

## What Changes

- **#2424:** latest-cycle discovery is driven from `hydro.hydro_run`.
  - For each scenario, candidate forecast runs of the requested basin are ordered by `cycle_time DESC`. The first one with a matching fact row (`EXISTS` keyed on `run_key` + `river_segment_key` + basin/network keys + `q_down`) gives that scenario's cycle.
  - **User decisions (2026-09-24):**
    - (a) **No `status` predicate is introduced**; the current semantics are kept.
    - (b) Candidates are narrowed by `h.basin_version_id = <requested basin>`, backed by a node-27 equivalence regression.
  - The per-scenario `dict[scenario_id -> cycle_time]` contract is unchanged.
- **#2516:** zero DDL. The membership EXISTS of the narrow latest-product and display-coverage station legs gets an `OFFSET 0` fence, so it runs as a correlated SubPlan and the probe's index condition includes `variable`. Candidate D was implemented first and dropped after the live 3.4 run; see design D3.
- **#2418:** the workload receipt gains a non-secret `server` block, captured on the same connection as the samples: `current_database()`, `pg_control_system().system_identifier`, `server_version`, and `inet_server_addr()` / `inet_server_port()`.
  - Addr and port are nullable (unix socket / container-internal).
  - `SCHEMA_VERSION` goes from `1.0` to `1.1`.
  - A `live` receipt refuses to be produced without a `system_identifier`.
  - Archived `1.0` receipts stay valid historical artifacts. No code reader re-validates them.

## Impact

- **Affected specs:**
  - `forecast-api`: latest-cycle discovery.
  - `qhh-latest-display-product`: narrow membership probe.
  - `node27-pgdata-relocation`: receipt server identity.
  - A pointer in the active `timeseries-narrow-store-expand-contract` design (D13), as #2424 requires.
- **Affected code:**
  - `packages/common/forecast_store.py`: `_per_source_latest_cycles` and the latest-product narrow leg.
  - `packages/common/display_coverage.py`: narrow leg.
  - `packages/common/node27_pgdata_workload{,_io,_measure}.py` and `scripts/node27_pgdata_workload.py` as needed.
  - Tests and the runbook §4.10 receipt field list.
- **Out of scope** (reported, not fixed):
  - Statement 3 (`_fetch_forecast_segment_rows`) costs 246964 blocks for a two-scenario `latest` request (node-27 measurement, same pin, `GFS,IFS`); that is #2417's area.
  - `_latest_run_type_valid_time` (hindcast path, same fact-scan shape) and `best_available.py:67` (a different table and index).
  - `_latest_issue_time` has no production caller (tests only).
  - The `query_indexes` report (issue #2516 comment 3).
  - `ALTER COLUMN met.interp_weight.variable TYPE met.forcing_variable` (candidate C).

## Triage

```text
Issue type: performance (public default read path) + evidence-integrity hardening
Fixture level: expanded
Upstream suggested level: needs-triage (#2424, #2516), none (#2418); expanded because #2424 rewrites a public API default-path query with a semantic narrowing, #2516 changes a production read template, and #2418 changes an acceptance-authority receipt schema
Blast radius: wrong latest cycle on the default forecast-series request (user sees a stale or empty curve); latest-product station membership changes; a live receipt that misattributes its cluster
Selected risk packs: Public API entry; Schema / field names (receipt); Legacy compatibility (archived 1.0 receipts); Resource limits (buffer gates); Release / operational (node-27 display deploy); Auth / secrets (pg_control_system privilege, no credential in the block); Error handling (SERVER_IDENTITY_MISSING, non-aborting isolated probe); Documentation (runbook §4.10, narrow-store D13); PostGIS/TimescaleDB domain (segmentby assumption); Published artifacts / display identity (the latest cycle chosen)
Evidence floor: see tasks.md
```
