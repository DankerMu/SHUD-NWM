## Risk packs

- **Public API / CLI / script entry: selected.** The default forecast-series request path (D1). Covered by the equivalence tests (2.1), the node-27 regression (3.3) and the post-deploy receipt (5.2).
- **Schema / columns / units / field names: selected.** The receipt `server` block and `SCHEMA_VERSION` 1.1 (D4), covered by 2.3.
- **Legacy compatibility / examples: selected.** Archived 1.0 receipts stay untouched (D4). The legacy forcing legs stay unchanged (D3).
- **Resource limits / large input: selected.** Buffer gates: `<= 5000` for the D1 statement (3.2); Index Cond plus rows per loop for D3 (3.4).
- **Release / operational: selected.** node-27 display deploy and receipt (5.x).
- **Auth / permissions / secrets: selected.** D4 depends on `nhms_display_ro` being able to execute `pg_control_system()` (1.1, live) and on the `server` block carrying no credential (2.3 redaction test).
- **Error handling / rollback / partial outputs: selected.** `SERVER_IDENTITY_MISSING` writes no PASS receipt, and an isolated identity failure must not abort the sampling transaction (2.3).
- **Documentation / migration notes: selected.** Runbook §4.10 `server` block and the physical-clone limitation (2.3); narrow-store D13 pointer (2.4).
- **PostGIS / TimescaleDB domain behavior: selected.** D1's cost rests on compression `segmentby (run_key, river_segment_key)` (design Context). The 3.2 gate is measured against that layout.
- **Published NHMS artifacts / display identity: selected.** The chosen latest cycle drives which curve is displayed. Covered by the I1 equality (2.1, 3.3) and the post-deploy request check (5.2).
- **Not selected:**
  - Concurrency (read-only statements).
  - File IO (the receipt write path is unchanged).
  - Config.
  - Migration (no DDL; the `hydro_run` index follow-up goes to #2048 / batch M).

## 1. Baselines

- [x] 1.1 Live pre-state recorded (design Context):
  - `/home/nwm/tmp/l2/base-explain.out`, `siblings.out`.
  - The D1 probe: 4131 buffers / 8.7 ms, same cycle.
  - The `hydro_run` index predicates, read live.
  - `pg_control_system()` executable by `nhms_display_ro`.
- [x] 1.2 Pre-deploy request baseline (node-27 local, warm ×8, `.workplans/l2/pre-timing.txt`):
  - issue URL (`issue_time=latest`, IFS pin) p50 1.38 s;
  - latest-product (`source=gfs&basin_id=basins_huaiyss`) p50 0.048 s.

## 2. Implementation

- [ ] 2.1 #2424 D1: rewrite `_per_source_latest_cycles`.
  - The EXISTS body is rendered through `render_river_ts_sql(..., 'narrow')` and registered in `tests/river_ts_template_registry.py` (design, sibling surfaces).
  - The shape test pins the #2451 non-sargable `IS NOT NULL AND IS NOT DISTINCT FROM` basin/network key spelling inside the EXISTS body.
  - The pins listed there go red and are updated deliberately.
  - The shape test pins the `OFFSET 0` fence and the outer `ORDER BY o.cycle_time DESC` before `LIMIT 1`.
  - **Local tests over a SQL-shape double:** no `hydro.river_timeseries` scan outside the correlated EXISTS; the `h.basin_version_id` predicate is present; no `status` predicate; scenario and identity filters are rendered once, in the `MATERIALIZED` candidate CTE; `hydro_run` is referenced exactly once.
  - **Real-DB integration test (seeded PostgreSQL + TimescaleDB, node-27 disposable DB)** comparing the old SQL (kept as a test-only oracle string) with the new SQL. Seeded cases:
    - multiple scenarios;
    - a superseded run with rows still selected;
    - a failed run with rows still selected;
    - a newer run of another basin with no rows for this segment, not selected;
    - a newest run of this basin without rows for the segment, so the older cycle with rows is selected;
    - `cycle_time NULL`;
    - model_id and run_id identity filters;
    - an unknown basin or network, giving `{}`;
    - duplicate max cycle across two runs of one scenario;
    - an unrelated segment's rows.
  - Every case must give equal dicts.
- [ ] 2.2 #2516 D3: both narrow legs use the de-duplicated `req(variable)` relation. Pin and golden tests are updated deliberately. Tests:
  - duplicate bound variables do not duplicate rows;
  - an unknown variable raises the same error class as before;
  - a real-DB result-equality test (old narrow SQL vs new) over seeded forcing rows with 4 variables per station, for **both** templates: the latest-product narrow leg (`forecast_store.py`) and the display-coverage narrow leg (`display_coverage.py`).
  - The unknown-variable error test binds a non-empty fact side, because the per-row cast can otherwise be skipped. Production always binds `MVP_STATION_VARIABLES`.
- [ ] 2.3 #2418 D4:
  - Add `prove_server_identity`, the receipt `server` block and `SCHEMA_VERSION = "1.1"`.
  - `live` refuses with `SERVER_IDENTITY_MISSING`.
  - Update the runbook §4.10.
  - Call site: inside `measure_workload`, after `prove_readonly_session` and before any sample. The probe is non-raising (privilege check first).
  - Tests: the block is present with the exact key set; `system_identifier` is a decimal string when present, and `null` only for `isolated`; redaction leaves no credential (a DSN with a password in the session must not appear); refusal when the function raises or returns NULL on `live`; `isolated` records `null`, and after an isolated identity miss the samples still run with the `SET LOCAL` timeouts in force; a test that the archived 1.0 receipts are unchanged.
- [ ] 2.4 Selector / CI routing for new tests; tracked-tree guards green. File a follow-up for the non-partial `hydro_run` index (D1 growth caveat) against #2048 / batch M. `timeseries-narrow-store-expand-contract/design.md` gains a D13 pointer to D1 (the #2424 acceptance wants the shape decision in that change).

## 3. Verification

- [ ] 3.1 Local: `uv run ruff check .`; targeted pytest; `openspec validate display-latest-cycle-and-receipt-server-identity --strict --no-interactive` and `timeseries-narrow-store-expand-contract`.
- [ ] 3.2 node-27 live EXPLAIN (ANALYZE, BUFFERS), read-only, warm ×3 on the issue pin, with the new SQL rendered by the branch code. The `_per_source_latest_cycles` statement must be `shared hit <= 5000` and return the same dict as master.
- [ ] 3.3 node-27 equivalence regression (read-only). For every registered basin with a river network, the pin set is its first and last reach, × `scenarios` in {GFS}, {IFS} and {GFS,IFS}, × no identity filter plus a model_id filter on one pin per basin.
  - The old statement and the new one must return equal dicts on every pin.
  - Record the pin count, zero mismatches, and the maximum and p95 new-statement buffers.
  - The old statement runs under `statement_timeout` of 30 s, and its time is recorded.
  - A timeout is recorded as such and never counted as equal.
  - **Direct invariant check (read-only, one-off), per run:** for every `hydro_run` row with `run_type='forecast'` and `cycle_time IS NOT NULL`, take one fact row by `run_key` seek (`LATERAL … LIMIT 1`) and compare its `basin_version_key` with the key of `hydro_run.basin_version_id`.
    - This catches a whole run labelled with the wrong basin. A complete every-row check would require decompressing the whole hypertable (`basin_version_key` is not a `segmentby` column), which is not acceptable on production. This limit is recorded.
    - Any mismatch withdraws the D1 basin narrowing and is reported.
    - **Done 2026-09-24 (pre-implementation, `.workplans/l2/basin-invariant.txt`):** 10051 forecast runs; 4744 with fact rows; **0 mismatched**; 5307 without rows (retention-dropped); 0 runs without a `core.basin_version` row.
- [ ] 3.4 node-27 live EXPLAIN for #2516.
  - **Input:** the production latest-product request `source=gfs&basin_id=basins_huaiyss` (and the issue's `dg_0883…` GFS identity if still present). The real `PsycopgForecastStore` latest-product call is driven through a recording cursor, the narrow station statement it executes is captured, and EXPLAIN (ANALYZE, BUFFERS) is run warm ×3 on it. The same is done on master and on the branch. `model_id`, `forcing_version_id` and `source` are recorded.
  - **Failure branch:** if the branch plan's `interp_weight` Index Cond still lacks `variable`, the plan is recorded, #2516 is not claimed done, and the result is reported with the next candidate (A/B).
  - Pass criteria:
    - the `interp_weight` probe's Index Cond includes `variable`;
    - no `Join Filter ... variable_e)::text = iw.variable`;
    - rows per loop is 1;
    - the node's buffers are of the same order as the legacy leg's 173376;
    - old and new narrow SQL give the same row count and row hash;
    - whole-statement `shared hit` and execution time, master vs branch, are recorded. A whole-statement regression (the `req` join changing the `fst` access path) fails 3.4 even if the `interp_weight` node improves.
- [ ] 3.5 node-27 `scripts/node27_pgdata_workload.py` run (isolated or live kind as the runbook permits for a read-only probe), showing the `server` block with node-27's `system_identifier`.
- [ ] 3.6 node-27 full pytest on the frozen SHA (disposable DB). The failure set must equal master's (#2615 only).

## 4. Review / CI

- [ ] 4.1 Review rounds recorded with fix_gate. CI green.

## 5. After merge

- [ ] 5.1 node-27 `git pull --ff-only`, display restart, C1 checks.
- [ ] 5.2 Live receipt:
  - the issue URL (`issue_time=latest`, IFS pin) curl warm ×8, p50 compared with the pre value;
  - latest-product warm ×8 p50;
  - the post-deploy `EXPLAIN` of statement 2;
  - p50 and p95 compared with the 1.2 baseline. With 8 samples, a comparison against the D11 500 ms P95 is indicative only;
  - production log since the restart has no new 500s;
  - recorded in the archive PR.
  - End-to-end D11 (500 ms P95) is reported with its attribution; not reaching it because of statement 3 (#2417) is reported, not hidden.

## Evidence Floor

1. `_per_source_latest_cycles` has no fact-table scan outside the correlated EXISTS, and has no `status` predicate. Old and new give equal per-scenario dicts on every seeded case (2.1) and on every live pin (3.3), with zero mismatches.
2. On the issue pin, statement 2 is `shared hit <= 5000` warm (3.2); the pre value is 409069.
3. On the #2516 narrow leg, the probe's Index Cond includes `variable`, rows per loop is 1, and the result sets are identical (3.4). The display-coverage copy is changed identically.
4. Every workload receipt has `server.{database, system_identifier, server_version, server_addr, server_port}`, and a `live` receipt without `system_identifier` is refused. `SCHEMA_VERSION` is `1.1`, and archived 1.0 receipts are unchanged (2.3, 3.5).
5. The node-27 full pytest failure set equals master's (3.6). The post-deploy receipt (5.2) shows the default request's timing before and after.
