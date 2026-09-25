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

- [x] 2.1 #2424 D1: rewrite `_per_source_latest_cycles`.
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
  - **Done:** `tests/test_latest_cycle_discovery_shape.py` (shape), `tests/test_latest_cycle_discovery_integration.py` (15 seeded cases, old == new == hand-worked cycles; frozen oracle `tests/latest_cycle_discovery_oracle.py`), green on a disposable PG 15.2 / TimescaleDB 2.10.2 container and in the node-27 full run (3.6).
- [x] 2.2 #2516 D3 (revised): both narrow legs fence the membership EXISTS with `OFFSET 0`; the candidate-D `req` relation from `cc2cf177c` is reverted. No #1990 pin or golden references this text; the fence is pinned by `tests/test_station_membership_fence.py` against master's sha256-pinned text. Tests:
  - A shape test pins the fence inside the membership EXISTS of both narrow templates, and pins that the legacy templates are unchanged.
  - A real-DB result-equality test (old narrow SQL vs new) over seeded forcing rows with 4 variables per station, for **both** templates: the latest-product narrow leg (`forecast_store.py`) and the display-coverage narrow leg (`display_coverage.py`).
  - A real-DB plan test on the seeded DB that the `interp_weight` probe's Index Cond includes `variable`, if the seeded planner reproduces the SubPlan. If it does not, 3.4 on node-27 is the evidence, and the test states which one it is.
- [x] 2.3 #2418 D4:
  - Add `prove_server_identity`, the receipt `server` block and `SCHEMA_VERSION = "1.1"`.
  - `live` refuses with `SERVER_IDENTITY_MISSING`.
  - Update the runbook §4.10.
  - Call site: inside `measure_workload`, after `prove_readonly_session` and before any sample. The probe is non-raising (privilege check first).
  - Tests: the block is present with the exact key set; `system_identifier` is a decimal string when present, and `null` only for `isolated`; redaction leaves no credential (a DSN with a password in the session must not appear); refusal when the function raises or returns NULL on `live`; `isolated` records `null`, and after an isolated identity miss the samples still run with the `SET LOCAL` timeouts in force; a test that the archived 1.0 receipts are unchanged.
- [x] 2.4 Selector / CI routing for new tests; tracked-tree guards green. File a follow-up for the non-partial `hydro_run` index (D1 growth caveat) against #2048 / batch M. `timeseries-narrow-store-expand-contract/design.md` gains a D13 pointer to D1 (the #2424 acceptance wants the shape decision in that change).
  - **Done here:** `scripts/select_ci_tests.py` routes `tests/test_latest_cycle_discovery_shape.py` (forecast_store.py + river_ts_template_registry.py rules), `tests/test_station_membership_fence.py` (forecast_store.py + display_coverage.py rules, plus the support-module rule for `tests/station_membership_fence_oracle.py`; `tests/latest_cycle_discovery_oracle.py` has its own support-module rule and anchor, routing to `tests/test_latest_cycle_discovery_shape.py`; both oracles are sha256-pinned to master `64f47adee`) and `tests/test_node27_pgdata_workload_server_identity.py` (`NODE27_PGDATA_WORKLOAD_TESTS` + its own changed-test rule); anchors in `tests/test_select_ci_tests.py`; full selector suite green. D13 pointer landed with the fixture (`86bcacc5e`).
  - Follow-up for the non-partial `hydro_run` index: #2626.

## 3. Verification

- [x] 3.1 Local: `uv run ruff check .`; targeted pytest; `openspec validate display-latest-cycle-and-receipt-server-identity --strict --no-interactive` and `timeseries-narrow-store-expand-contract`.
  - **Result (node-27, 2026-09-25):** ruff clean. Targeted pytest: 1363 passed / 2 skipped. After fix pass 1, shape + fence + select_ci_tests: 914 passed. `openspec validate --strict`: both changes valid.
- [x] 3.2 node-27 live EXPLAIN (ANALYZE, BUFFERS), read-only, warm ×3 on the issue pin, with the new SQL rendered by the branch code. The `_per_source_latest_cycles` statement must be `shared hit <= 5000` and return the same dict as master.
  - **Result (node-27, 2026-09-25):** on `3597f960d`, warm ×3, the issue pin gives 1386 hit / ~4.6 ms (IFS) and 1464 hit / ~3.8 ms (GFS+IFS), against 409069 before. The result is the same, `2026-09-24T00:00Z` per scenario. `/home/nwm/tmp/l2/d1-branch.out`.
- [x] 3.3 node-27 equivalence regression (read-only). For every registered basin with a river network, the pin set is its first and last reach, × `scenarios` in {GFS}, {IFS} and {GFS,IFS}, × no identity filter plus a model_id filter on one pin per basin.
  - **Result (node-27, 2026-09-25):** **440 pins** (every basin's first/last reach × {GFS},{IFS},{GFS,IFS}, plus one model-filter pin per basin), **440 equal, 0 mismatches, 0 unresolved timeouts**.
    - The first pass had 33 pins where the old statement timed out at 30 s, while node-27 was loaded by a starting full pytest. Re-run with a 600 s old-statement timeout, all 33 were equal, and the old statement took 1.1–3.8 s.
    - New statement: pins with data (364) max 3301, p95 2530 hit. Empty-result pins (76) max 25447 / 68 ms, recorded in design Risks.
    - Old statement p50 1156 ms; new statement p50 48 ms.
    - `/home/nwm/tmp/l2/equivalence{,-rerun}.out`.
  - The old statement and the new one must return equal dicts on every pin.
  - Record the pin count, zero mismatches, and the maximum and p95 new-statement buffers.
  - The old statement runs under `statement_timeout` of 30 s, and its time is recorded.
  - A timeout is recorded as such and never counted as equal.
  - **Direct invariant check (read-only, one-off), per run:** for every `hydro_run` row with `run_type='forecast'` and `cycle_time IS NOT NULL`, take one fact row by `run_key` seek (`LATERAL … LIMIT 1`) and compare its `basin_version_key` with the key of `hydro_run.basin_version_id`.
    - This catches a whole run labelled with the wrong basin. A complete every-row check would require decompressing the whole hypertable (`basin_version_key` is not a `segmentby` column), which is not acceptable on production. This limit is recorded.
    - Any mismatch withdraws the D1 basin narrowing and is reported.
    - **Done 2026-09-24 (pre-implementation, `.workplans/l2/basin-invariant.txt`):** 10051 forecast runs; 4744 with fact rows; **0 mismatched**; 5307 without rows (retention-dropped); 0 runs without a `core.basin_version` row.
- [x] 3.4 node-27 live EXPLAIN for #2516.
  - **Result (node-27, 2026-09-25):** identity model `dg_fed73a15ec9ff9b7c4cba8dd644d5458`, forcing `forc_gfs_2026092400_dg_fed73a15…`, source gfs, CTE fallback forced.
    - **Master:** Index Cond `(model_id, station_id, lower(source_id))`, join filter `(fst.variable_e)::text = iw.variable`, 4 rows/loop × 52080, node 326648, statement 517524 hit / ~4.2 s.
    - **Branch `3597f960d`:** Index Cond includes `variable = (fst_1.variable_e)::text`, 1 row/loop × 52080, node 275464 (5.3 blocks/probe vs the legacy leg's 7.3), statement 466291 hit / ~4.07 s.
    - The station leg is 52080 rows with sha256 `0db1b694…4bd1376`, and the product sha is `ae784639…`, identical on both.
    - The identity differs from the issue's (`dg_0883…`, 23856 rows); it is the current production latest-product identity.
    - Candidate D failed this gate first (see design D3).
  - **Input:** the production latest-product request `source=gfs&basin_id=basins_huaiyss` (and the issue's `dg_0883…` GFS identity if still present). The real `PsycopgForecastStore` latest-product call is driven through a recording cursor, the narrow station statement it executes is captured, and EXPLAIN (ANALYZE, BUFFERS) is run warm ×3 on it. The same is done on master and on the branch. `model_id`, `forcing_version_id` and `source` are recorded.
  - **Failure branch:** if the branch plan's `interp_weight` Index Cond still lacks `variable`, the plan is recorded, #2516 is not claimed done, and the result is reported with the next candidate (A/B).
  - Pass criteria:
    - the `interp_weight` probe's Index Cond includes `variable`;
    - no `Join Filter ... variable_e)::text = iw.variable`;
    - rows per loop is 1;
    - the node's buffers are of the same order as the legacy leg's 173376;
    - old and new narrow SQL give the same row count and row hash;
    - whole-statement `shared hit` and execution time, master vs branch, are recorded. A whole-statement regression fails 3.4 even if the `interp_weight` node improves.
- [x] 3.5 node-27 `scripts/node27_pgdata_workload.py` run (isolated or live kind as the runbook permits for a read-only probe), showing the `server` block with node-27's `system_identifier`.
  - **Result (node-27, 2026-09-25):** the CLI isolated receipt on `3597f960d` passes: `schema_version` 1.1, `server` = `{database: nhms, system_identifier: "7651116524569100322", server_version: 15.2, server_addr: 172.17.0.2, server_port: 5432}`, which equals a direct `pg_control_system()` read. No password or DSN in the file. The first attempt failed with `API_REQUEST_FAILED` while node-27 was loaded; the retry passed. `/home/nwm/tmp/l2/workload-isolated-3597f96.json`.
- [x] 3.6 node-27 full pytest on the frozen SHA (disposable DB). The failure set must equal master's (#2615 only).
  - **Result (node-27, `3597f960d`, 2026-09-25):** 2 failed, 20778 passed, 62 skipped in 2:29:02.
    - `test_canonical_precip_copyback_backfill::test_backfill_module_launch_outside_the_repo_root_fails_with_no_summary` is #2615, as on master.
    - `test_entropy_audit_report_contract::test_entropy_audit_current_repo_hard_gate_has_zero_production_topology_findings` was caused by this change: the §4.10 runbook paragraph named the node-22 :55433 cluster without the archived / do-not-connect boundary. The paragraph now carries it, and the contract file passes (12 passed). The fix-delta rerun on the final SHA is recorded in the PR body.
    - No new test file of this change is in the skip list; the 62 skips are the opt-in docker/e2e markers and one pre-existing `test_forcing_read_path_store_routing.py:1114` skip.

## 4. Review / CI

- [x] 4.1 Review rounds recorded with fix_gate. CI green.
  - **Result:** round 1 not clean (`3597f960d`), fix pass 1 `413760f92`, round 2 clean. The first ready run failed SQL Migration Dry Run on an unrelated SQLAlchemy 2.1.0 release (default driver moved to psycopg v3); #2631 capped `sqlalchemy<2.1` (follow-up #2632), and the re-run on the fresh merge ref was green (279 passed). Merged as `2024a5e4e`.

## 5. After merge

- [x] 5.1 node-27 `git pull --ff-only`, display restart, C1 checks.
  - **Result:** clean checkout, `0c75aba8` → `2024a5e4`; restart OK (main_pid 1736309, 2 workers, smoke passed); C1 `/health` 200, `runtime/config` `display_readonly`, `/api/v1/slurm/health` 404.
- [x] 5.2 Live receipt: `evidence/node27-live-receipt.md` in the archived change. Issue URL p50 1.381 s → 0.237 s (max 0.283 s); latest-product p50 0.049 s → 0.049 s; statement 2 1381 / 1454 hit warm; 0 500s since the restart.
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
