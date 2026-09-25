## Context

- **Master** is `64f47adee`.
- **Live pre-state** (node-27, 2026-09-24, read-only `nhms_display_ro`, scripts under `/home/nwm/tmp/l2/`):
  - `base-explain.out`: the issue's production default pin (`basins_wj_vbasins` / `basins_wj_rivnet_vbasins` / `basins_wj_shud_reach_000001`, `scenarios=IFS`, `include_analysis=false`, `issue_time=latest`) resolves `issue_time=2026-09-24T00:00:00Z`. Statement 2 (`_per_source_latest_cycles`) costs **409069 hit / ~1.1 s**.
  - `siblings.out`:
    - The two-scenario `GFS,IFS` request costs 409237 for statement 2 plus 246964 for the fetch.
    - The `include_analysis=true` request resolves the same cycle. The analysis leg with the `analysis_true_field` pushdown costs 1299.
- **`hydro.hydro_run`:** 10025 rows / 1299 blocks. All runs are `forecast`: published 7435, superseded 2297, succeeded 276, parsed 15, failed 2.
- **Indexes on `hydro.hydro_run`, read live on 2026-09-24 (not from the migration ledger, per #2048):**
  - `hydro_run_display_ready_candidate_idx`, `hydro_run_qhh_latest_candidate{,_parsed}_idx`: `(lower(source_id), run_type, basin_version_id, cycle_time DESC, run_id DESC)`, partial on `status IN (...)`, which includes the live-only `frequency_done`.
  - `hydro_run_run_key_key`, unique on `(run_key)`.
- **`hydro.river_timeseries`:**
  - Compression `segmentby`: `(run_key, river_segment_key)`.
  - `orderby`: `(variable_e, valid_time)`.
  - 29 chunks, 15 of them compressed.
  - Uncompressed-chunk indexes:
    - `river_timeseries_narrow_pkey (run_key, river_segment_key, variable_e, valid_time)`;
    - `river_ts_segment_time_key_idx (river_segment_key, variable_e, valid_time DESC)`;
    - `river_ts_run_discovery_key_idx (run_key, basin_version_key, river_network_version_key, variable_e, valid_time DESC)`. #2451 C1 keeps this one to its `run_key` prefix through the non-sargable key spelling.
- **Probes of the D1 shape** (same pin, warm):
  - First draft (LATERAL re-reads `hydro_run`, sorts after EXISTS): 4131 buffers.
  - `MATERIALIZED` candidates without the fence: 2826 (IFS) / 4344 (GFS+IFS), because EXISTS still ran for all 57 candidates per scenario.
  - **Final shape with the `OFFSET 0` sort fence: 1386 hit / 5.0 ms (IFS) and 1464 hit / 3.9 ms (GFS+IFS).** It returns `forecast_{gfs,ifs}_deterministic → 2026-09-24 00:00+00`, the same as production.
- **Pre-deploy request timing** (node-27 local, warm ×8, `.workplans/l2/pre-timing.txt`):
  - the issue URL (`issue_time=latest`, IFS pin): p50 1.38 s (1.349–1.392);
  - latest-product (`source=gfs&basin_id=basins_huaiyss`): p50 0.048 s.
- **Domain assumption that D1's probe cost rests on:** `hydro.river_timeseries` compression `segmentby (run_key, river_segment_key)`, and uncompressed-chunk `river_timeseries_narrow_pkey (run_key, river_segment_key, …)`. Each EXISTS is one `run_key` + segment seek per chunk. A change of `segmentby` invalidates the 3.2 gate and requires re-measuring.
- **`pg_control_system()`:** executable by `nhms_display_ro`, giving `system_identifier = 7651116524569100322`. `inet_server_addr()` / `inet_server_port()` return the container-internal `127.0.0.1:5432` over TCP and NULL over a unix socket.

## Goals / Non-Goals

- **Goals:**
  - The default `issue_time=latest` discovery statement meets `shared hit <= 5000` with the same per-scenario result.
  - The narrow membership probe regains the `variable` Index Cond.
  - Live workload receipts carry the cluster identity.
- **Non-goals:**
  - #2417 statement 3.
  - A D11 end-to-end latency sign-off beyond reporting it.
  - Changing D11 constants.
  - Any migration or DDL.
  - Candidate C (the `interp_weight` column type).

## Governing invariants

- **I1:** for a given pin and parameters, `_per_source_latest_cycles` returns exactly the same `dict[scenario_id -> cycle_time]` as before.
  - Per scenario, this is the maximum `cycle_time` over forecast runs (`cycle_time IS NOT NULL`, plus the scenario and identity filters) that have at least one `q_down` fact row for the requested basin, network and segment.
  - It is never the global latest cycle, and never a status-filtered set.
- **I2:** latest-product and display-coverage narrow legs return the same rows as before.
- **I3:** every `live` receipt names the cluster that produced its samples, and never carries a credential.

## Decisions

### D1 (#2424): drive latest-cycle discovery from `hydro_run`, per scenario

Shape:

```sql
WITH seg AS (<the three existing scalar key lookups: basin_version_key, river_segment_key, river_network_version_key>),
cand AS MATERIALIZED (
  SELECT h.run_key, h.scenario_id, h.cycle_time FROM hydro.hydro_run h
  WHERE h.run_type = 'forecast' AND h.cycle_time IS NOT NULL
    AND h.basin_version_id = %(basin_version_id)s
    {scenario_filter} {identity_filter}
),
scen AS (SELECT DISTINCT scenario_id FROM cand)
SELECT scen.scenario_id, pick.cycle_time
FROM scen CROSS JOIN seg
CROSS JOIN LATERAL (
  SELECT o.cycle_time FROM (
    SELECT c.run_key, c.cycle_time FROM cand c
    WHERE c.scenario_id = scen.scenario_id
    ORDER BY c.cycle_time DESC OFFSET 0) o          -- fence: sort first, then probe lazily
  WHERE EXISTS (SELECT 1 FROM hydro.river_timeseries rt
                WHERE rt.run_key = o.run_key AND rt.river_segment_key = seg.rsk
                  AND rt.basin_version_key IS NOT NULL AND rt.basin_version_key IS NOT DISTINCT FROM seg.bvk
                  AND rt.river_network_version_key IS NOT NULL AND rt.river_network_version_key IS NOT DISTINCT FROM seg.rnvk
                  AND rt.variable_e = 'q_down'::hydro.river_variable)
  ORDER BY o.cycle_time DESC LIMIT 1) pick           -- correctness does not depend on the plan
ORDER BY scen.scenario_id
```
- The fact-side predicates are exactly those of `_SEGMENT_ROWS_SOURCE_SQL`, including the #2451 C1 spelling (`forecast_store.py:83-125`):
  - basin and network keys are compared as `IS NOT NULL AND IS NOT DISTINCT FROM`, which is deliberately non-sargable, so `river_ts_run_discovery_key_idx` stays confined to its `run_key` prefix and never takes `river_segment_key` into a Filter;
  - segment and variable are compared with `=`.
  - "Has a fact row" therefore means the same thing as the old join. When a scalar lookup is NULL (unknown basin or network), the result is `{}` in both shapes.
  - Re-measured live with this spelling: 1386 / 1464 hit, the same as the `=` draft, with compressed-chunk probes on the `run_key` segmentby index. The 2.1 shape test pins the spelling inside the EXISTS body.
- The `scenario_filter` and `identity_filter` fragments are the same objects the old statement used. They reference `h.` and are rendered once, in `cand`.
- **Status (user decision (a)):** no `status` predicate. A run with fact rows is eligible whatever its status, exactly as before. The rewrite therefore cannot use the status-partial candidate indexes; `hydro_run` access is a sequential scan (1299 blocks per layer today).
  - Rows removed by the scan filter are not a gate metric; the gate is total `shared hit`.
  - `hydro_run` is read exactly once, through the `MATERIALIZED` candidate CTE, whatever the number of scenarios. The probe measured two sequential scans (1299 each) because it re-read `hydro_run` in the LATERAL layer, and this shape removes that.
  - **Growth caveat, recorded:** `hydro_run` grows about 1100 rows (~143 blocks) per week; the live weekly counts for 2026-08..09 are 477–1306. The probes cost about 90 blocks per scenario, so the statement stays under 5000 while the single scan is below about 4800 blocks: roughly 37k rows, or about 24 weeks from 2026-09-24.
  - The durable fix is a non-status-partial `(basin_version_id, scenario_id, cycle_time DESC) WHERE run_type = 'forecast' AND cycle_time IS NOT NULL` index. It belongs with #2048's forward migration, which reconciles `hydro_run` index predicates (batch M), and is filed as a follow-up. No DDL is in this batch.
- **Basin narrowing (user decision (b)):** `h.basin_version_id = %(basin_version_id)s` narrows candidates.
  - It depends on the invariant "a run's `hydro_run.basin_version_id` is the basin of its fact rows". Nothing enforces this in the DB: `rt.basin_version_key` was back-filled from the fact row's own text column (`db/migrations/000050_river_identity_normalization.sql:296`).
  - The node-27 equivalence regression (tasks 3.3) is the evidence. If any pin differs, the narrowing is withdrawn and the result is reported, never shipped silently.
- **Correctness vs plan.** The outer `ORDER BY o.cycle_time DESC` before `LIMIT 1` makes the result correct under any plan: it cannot depend on a semi-join keeping the inner order. It was added after the fixture review, and live the pathkeys still propagate from the fenced subquery, so the plan stays lazy: 1386 (IFS) / 1464 (GFS+IFS) hit, same result. A test pins both the fence and the outer `ORDER BY`.
- The `OFFSET 0` fence keeps the planner from flattening the ordered subquery. Candidates are therefore sorted by `cycle_time DESC` first, and EXISTS is evaluated lazily until the first hit. A test pins the fence.
- `LIMIT 1` per scenario keeps early termination safe: a candidate is probed only after every later-cycle candidate of that scenario has failed its EXISTS. This does not inherit the "candidate limit is 1" soundness condition of `_qhh_latest_candidate_runs_sql`: every candidate is still checked in order, and the pick is the first one that has rows.
- Ties: several runs of one scenario may share the maximum `cycle_time`. The old statement returned that `MAX`, and the new one returns the same `cycle_time` whichever run wins. Run selection stays in `_resolve_run_identity`, which is unchanged.
- `issue_time=latest` with no candidates or no rows returns `{}`, as today. `_latest_cycle_time`, the `include_analysis` fallback and the empty-response branches are unchanged.

### D2 (#2424 siblings)

- **`_latest_issue_time`:** no production caller. `grep` finds only `tests/test_direct_grid_display_cutover_history.py`, `test_river_ts_text_identity_cleanup.py` and `test_forecast_store_routing.py`. Left unchanged; retiring it is out of scope.
- **`_latest_analysis_issue_time`:** reached only when `include_analysis` is set and no forecast cycle exists. Its source carries the `analysis_true_field` pushdown (#2417), and the sibling analysis fetch with the same pushdown costs 1299 on the pin. Left unchanged, with this measurement recorded. It is measured again on the post-deploy pin (tasks 5.2).
- **`_latest_run_type_valid_time`** (hindcast, `MAX(rt.valid_time)`): it genuinely needs fact values, so it does not have the same shape. Out of scope.

### D3 (#2516): fence the membership EXISTS with `OFFSET 0` (revised after the live 3.4 run)

- **Outcome of candidate D (the first implementation, `cc2cf177c`):** live on node-27 it did **not** restore the index column.
  - Input: the production latest-product request `source=gfs&basin_id=basins_huaiyss`, model `dg_fed73a15ec9ff9b7c4cba8dd644d5458`, forcing `forc_gfs_2026092400_dg_fed73a15…`, with the CTE fallback forced.
  - The join filter merely moved from `(fst.variable_e)::text = iw.variable` to `(u.v = iw.variable)`.
  - rows per loop stayed 4 (52080 loops); the node stayed at 326648 hit; the whole statement stayed at 517544 (master 517524).
  - The planner pulls the EXISTS up into a semi-join and never builds a path parameterised by the variable. Per the 3.4 failure branch, candidate D is dropped.
- **Chosen shape (zero DDL, measured):** keep the text comparison `iw.variable = fst.variable_e::text` and add `OFFSET 0` as the last clause inside the membership EXISTS of the **narrow** latest-product station leg (`forecast_store.py`) and of the narrow display-coverage station leg (`display_coverage.py`).
  - The fence stops the pull-up, so the probe runs as a correlated SubPlan whose outer values, including `fst.variable_e::text`, are parameters of the index scan.
  - Live, on the same identity, with master SQL plus the fence:
    - Index Cond `((model_id = cr_1.model_id) AND (station_id = ms.station_id) AND (variable = (fst_1.variable_e)::text) AND (lower(source_id) = lower(cr_1.source_id)))`;
    - rows per loop **1**; no join filter on the variable;
    - node 275464 hit, which is 5.3 blocks per probe against the legacy leg's 7.3 (173376 / 23856);
    - whole statement 466284 hit against 517524;
    - the station leg is the same 52080 rows with sha256 `0db1b694…4bd1376` on master and fenced.
- The bound `%(variables)s::met.forcing_variable[]`, the parameter name, every caller and the legacy variants are unchanged. The only template change is the fence line. No #1990 forcing pin or golden references this text. The fence is pinned by `tests/test_station_membership_fence.py`, which compares against master's text frozen in `tests/station_membership_fence_oracle.py`.
- **Precondition:** none (no DDL, no enum superset, unknown-variable behaviour unchanged).
- `best_available.py:67` (`fvc.variable = fst.variable_e::text` against `met.forcing_version_component`) has not been measured. It is reported in the PR, not changed.

### D4 (#2418): the receipt `server` block

- **Capture:** `prove_server_identity(connection)` (in `node27_pgdata_workload_io.py`) is called inside `measure_workload`, after `prove_readonly_session` and before any warmup or accepted sample. It runs on the same connection and transaction, which carries `autocommit=False` and the `SET LOCAL` timeouts.
  - It runs inside a `SAVEPOINT`. On any inner error it does `ROLLBACK TO SAVEPOINT` then `RELEASE`, which reverts only settings made after the savepoint, so the earlier `SET LOCAL` timeouts survive.
  - It must never raise inside that transaction. It first checks `has_function_privilege('pg_catalog.pg_control_system()', 'EXECUTE')`; only if that is true does it call the function, so a missing privilege yields `null` without aborting the transaction.
  - Other probe failures (for example the function returning NULL) are handled as values, not exceptions.
  - The query:
  - `current_database()`, `(SELECT system_identifier::text FROM pg_control_system())`, `current_setting('server_version')`, `host(inet_server_addr())`, `inet_server_port()`.
- **Receipt:** `document["server"]` holds `{database, system_identifier, server_version, server_addr, server_port}`.
  - `system_identifier` is a decimal string when present. `null` is allowed only when `evidence_kind=isolated`. JSON integers overflow 2^53, which is why it is not stored as a number.
  - `server_addr` and `server_port` are nullable.
  - The block is added under `redact_payload`; no DSN, user or password field exists in it.
- **Refusal:** when `system_identifier` is missing, or the function is not executable, `refuse(code="SERVER_IDENTITY_MISSING", stage="performance")` applies for `live`, and no PASS receipt is written. `isolated` records it when available and otherwise records `null`; the samples then proceed with the timeouts still in force.
- **Limitation:** `system_identifier` is set by `initdb` and copied by physical clones (pg_basebackup, PGDATA copy or snapshot, streaming standby). It distinguishes independently initialised clusters, not a physical copy from its origin. The archived node-22 :55433 cluster may share node-27's lineage. The runbook states this; the block is evidence of cluster lineage, not of host.
- **Version:** `SCHEMA_VERSION = "1.1"`. The archived artifacts stay as they are: historical, never re-validated by code, and byte-pinned by sha256 in `tests/test_node27_pgdata_workload_server_identity.py`.
  - `openspec/changes/fix-narrow-segment-read-index-applicability/receipts/2026-09-18-live-ab/d11-live-receipt.json` is a 1.0 workload receipt.
  - `openspec/changes/archive/2026-09-16-compressed-chunk-cold-tablespace-tiering/evidence/receipts/retirement-pgdata-workload-smoke.json` is a smoke wrapper that embeds workload documents; it has no top-level `artifact` / `schema_version`.
- The runbook §4.10 receipt description adds the block and one sentence: a live receipt's `server.system_identifier` must equal node-27's.

## Sibling surfaces

- **#2424:**
  - Every `_segment_rows_source_sql()` caller (`_latest_issue_time`, `_latest_analysis_issue_time`, `_fetch_analysis_segment_rows`, `_fetch_forecast_segment_rows`, `_latest_run_type_valid_time`, `_fetch_run_type_segment_rows`) keeps its SQL.
  - `packages/common/forecast_curve_capture.py` / the D11 capture path uses an explicit `issue_time`, so it is unaffected.
  - **River render registry (`river_ts_render.py`, decided):** the EXISTS body is written as a narrow-store template and rendered through `render_river_ts_sql(<template>, 'narrow')`, like `_SEGMENT_ROWS_SOURCE_SQL`. That way the scanner and registry guards own it. It **gets** a `tests/river_ts_template_registry.py` entry: an unregistered read site is red there (`:16-17`).
  - These pins go red and must be updated deliberately, not loosened:
    - `tests/test_forecast_store_routing.py:143-150` (`OUTER_CLAUSES["per_source_latest_cycles"]`) and the `PROJECTION` count check at `:216`;
    - `tests/test_river_ts_stats_harness_offline.py:305-314` (the `MAX(h.cycle_time)` companion assertion);
    - `tests/test_river_timeseries_stats_index_choice_integration.py:~840` (the must-preserve #5 companion baseline). This is an unasserted companion record, so it did not go red. Its comments at `:32` / `:840` ("98.8 % of the latest shape's cost") are now stale. The file is 1056 lines and not excluded from the large-file guard, so the comment refresh is deferred and recorded here;
    - `tests/test_river_ts_text_identity_cleanup.py:495,1097`;
    - the segment-block census or golden (`tests/fixtures/river_ts_templates_51f9d273.json`) if it covers this statement.
- **#2516:** latest-product and display-coverage narrow legs; `_STATION_SERIES_ROWS_TEMPLATES` (already on the relation shape, and the precedent).
- **#2418:** `scripts/node27_pgdata_workload.py` CLI output; `tests/test_node27_pgdata_workload*.py`; runbook `docs/runbooks/tier-node27-timeseries-storage.md` §4.10.

## Risks / Trade-offs

- **D1 EXISTS is not fenced.** Laziness (probe candidates in order and stop at the first hit) relies on the planner keeping the EXISTS as a per-candidate probe. At thousands of estimated candidates it could choose a hashed semi-join, which rescans the segment's rows. Correctness is unaffected, because of the outer `ORDER BY`. An `OFFSET 0` inside the EXISTS would force the SubPlan; it is not added (review round 1 note; the live plan is lazy).

- **The `server` block covers the SQL-sample cluster only.** API samples go through the display API's own DSN, so the receipt does not name the API's backing cluster. This is recorded, not fixed.

- **The basin narrowing could hide a mislabelled run.** Mitigated by the full per-basin live regression. A mismatch is a finding that withdraws the narrowing.
- **Sequential-scan growth on `hydro_run`.** Recorded with its threshold, and a follow-up issue is filed.
- **Nested EXISTS probing when the newest candidates lack rows** (for example a run still being parsed). Each probe is bounded by one `run_key` seek per chunk. The worst case is the number of candidate runs of that basin and scenario (57 on the pin).
