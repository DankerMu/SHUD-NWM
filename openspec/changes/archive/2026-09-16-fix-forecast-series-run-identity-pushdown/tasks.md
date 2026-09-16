# Tasks

## Risk packs

| pack | selected | reason |
|---|---|---|
| Public API / CLI / script entry | **yes** | `GET /api/v1/basin-versions/{b}/river-segments/{s}/forecast-series` is the only production route; the same SQL is what `scripts/node27_pgdata_workload.py measure` captures. |
| Resource limits / large input / discovery | **yes** | The defect *is* a resource bound (D11 `shared hit <= 5000`), and the fix's value is measured in blocks. |
| Legacy compatibility / examples | **yes** | The legacy branch must keep routing correctly while `hydro.river_timeseries_legacy` exists, and its compressed chunks need a different predicate column than the narrow branch. |
| Schema / columns / units / field names | **yes** | The narrow branch forbids fact-table text identity columns; the legacy-only aid introduces `rt.run_id`, which `render_river_ts_sql` must strip for narrow. |
| Concurrency / shared state / ordering | no | Read-only display path, no shared mutable state. |
| Error handling / rollback / partial outputs | no | No writes, no partial output; the one new failure mode (empty resolve result) is covered by task 3.4. |
| File IO / path safety / overwrite | no | No file IO. |
| Auth / permissions / secrets | no | Unchanged; already runs as `nhms_display_ro`. |
| Config / project setup | no | No config surface. |
| Release / packaging / dependency compatibility | no | No dependency change. |
| Documentation / migration notes | no | No user-facing doc contract; the spec delta carries the behaviour. |

## Evidence Floor

- node-27 production, read-only `nhms_display_ro`, warm `EXPLAIN (ANALYZE, BUFFERS)` × 3 on all three
  measured paths — `:758` with `run_id` bound, `:758` with `run_id` unbound, `:718` — each
  `shared hit <= 5000` **including the resolve step** and **with `hydro.river_timeseries_legacy` still
  present**.
- Digest equivalence pre/post on all three. Digest is
  `sha256("\n".join(repr(sorted(row.items())))).hexdigest()[:16]`; baseline `b3b3f1bdd79a5e6c` over 168
  rows for the pinned segment. Same serialisation as `/home/nwm/tmp/2410/explain718e.py` on node-27.
- `uv run pytest -q tests/test_forecast_store_routing.py tests/test_river_ts_text_identity_cleanup.py tests/test_river_ts_template_golden.py tests/test_sql_shape_helpers.py tests/test_river_ts_read_path_surrogate_keys.py tests/test_direct_grid_display_cutover_history.py tests/test_node27_pgdata_workload.py tests/test_node27_timeseries_compression_benchmark.py tests/test_river_ts_render.py tests/test_node27_timeseries_compression_live_evidence.py tests/test_forecast_api.py`
  (run on the committed tree — `test_before_and_after_slices_merge_into_exact_live_evidence_contract`
  compares the working tree against the `HEAD` blob and is red for any uncommitted source change)
- `uv run ruff check .`
- `openspec validate fix-forecast-series-run-identity-pushdown --strict --no-interactive`

## 1. Template

- [x] 1.1 Add a per-call-site, per-store predicate slot to `_SEGMENT_ROWS_SOURCE_SQL`
      (`packages/common/forecast_store.py:29-54`), threaded through `_segment_rows_source_template`
      (`:57-63`) and `_segment_rows_source_sql` (`:65-68`). Keep it one template constant so
      `tests/river_ts_template_registry.py:255-264`'s `mentions=1` still holds.
- [x] 1.2 `_segment_rows_source_template(store)` must stay callable with the store argument alone and
      render an **empty** slot in that form — `tests/test_river_ts_template_golden.py:139-156` calls it
      that way, and the single-valued `expected_aids` assertion
      (`tests/test_sql_shape_helpers.py:826-836`) cannot express legacy=4 / narrow=3.
- [x] 1.3 The slot must not sit on the physical line directly below an existing aid marker, or the narrow
      renderer deletes it.

## 2. Call sites

- [x] 2.1 Add the run-identity resolution query to `forecast_series`
      (`packages/common/forecast_store.py:347`), **not** to the segment-block methods — see task 3.6. It
      selects `run_key` and `run_id` from `hydro.hydro_run`. Its `h.*` predicate set MUST be a subset of
      the consuming call site's outer predicates, and it MUST NOT reduce to a single run: one
      `(scenario_id, cycle_time)` can match several runs and all must be returned.
- [x] 2.2 `:758` explicit-cycle `_fetch_forecast_segment_rows`, **`run_id` bound**: push
      `rt.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = %(run_id)s)` — entirely in-SQL,
      **no resolve statement**. This is the shape measured at 560 and the only one
      `packages/common/node27_pgdata_workload_query.py` can capture (it has no database at capture time:
      `scripts/node27_pgdata_workload.py:127` runs before `:140`). Do not convert it to resolve-then-push.
      The scalar form is safe because `hydro.hydro_run.run_id` is `TEXT PRIMARY KEY`
      (`db/migrations/000006_hydro.sql`), so the subquery returns at most one row and cannot raise
      "more than one row returned by a subquery" on a production display route.
- [x] 2.2b `:758`, **`run_id` unbound**: resolved `rt.run_key = ANY(...)` into both branches plus the
      legacy-only `rt.run_id = ANY(...)` aid. `apps/frontend/src/stores/forecast.ts:356-369` never sends
      `run_id` — and the store is mounted (`apps/frontend/src/components/ScenarioSelector.tsx:14-19`) — so
      this is a live production shape, measured at 18 621 blocks today.
- [x] 2.3 `:718` cycle-per-scenario `_fetch_forecast_segment_rows`: the same push, **plus** the
      `rt.valid_time` window as two constants. Those constants MUST be the union envelope
      `min(cycle_time)` / `max(cycle_time) + INTERVAL '7 days'` across every scenario in
      `cycle_times_by_scenario`, because the outer window at `:726-727` is per-run. Using one scenario's
      cycle as both bounds deletes the other scenarios' rows.
- [x] 2.4 `:627` / `:660`: push the literal `h.scenario_id = 'analysis_true_field'` only. `:660`'s
      `DISTINCT ON (rt.valid_time)` splice across multiple analysis runs must be preserved.
- [x] 2.5 `:793` / `:831`: push the `run_type` set only — these deliberately span every run.
- [x] 2.6 `:564` and `:594`: leave unchanged. `:564` has no production caller; `:594` is #2424.
- [x] 2.7 Any filter fragment is pushed whole. `_scenario_filter` (`:2681`) emits a single conjunct whose
      interior is an `OR`; pushing one side of that disjunction is unsound.
- [x] 2.8 **Pushdown is additive.** No outer-layer predicate is removed. `_REQUIRED_EQUALS`
      (`packages/common/node27_pgdata_workload_query.py:57-63`, enforced `:372-378`) requires the captured
      statement to still carry `h.cycle_time`/`h.run_id`/`h.model_id` equalities, and the `:718` envelope
      is only safe because the per-run outer window at `:726-727` still narrows it.
- [x] 2.9 Make the resolve an overridable seam on `PsycopgForecastStore` — the same shape as the existing
      `_validate_series_target` override at `packages/common/node27_pgdata_workload_query.py:119-120`.
      `packages/common/node27_pgdata_workload_query.py` itself needs **no change**, because 2.2 keeps its
      only path (`:758` bound) self-contained; confirm that with task 3.9 rather than assuming it.
- [x] 2.10 `scripts/node27_timeseries_compression_benchmark.py:241-272` drives `:758` **unbound**, so it
      does need the 2.9 seam. Its own comment (`:254-255`) says it relies on the owner raising **after**
      issuing the production query; resolve-first inverts that ordering.
      **Do not assume the render site has a connection** — `_curve_query_and_binding` is called at `:657`
      from a function that only holds the `connect` factory (`:650`); connections are acquired later, in
      `_capture_with_connections` (`:596-625`). Either move the render to after acquisition or resolve
      through `connect`; state which in the PR. This is the same stage-ordering trap as the workload CLI,
      in a second file.
- [x] 2.11 That script's placeholder check (`:262-272`) is an **exact set equality** — extend it with the
      new bindings. `_REQUIRED_PRESENT_KEYS` (`node27_pgdata_workload_query.py:64-74`) is a subset check
      and needs nothing.
- [x] 2.12 `scripts/node27_timeseries_compression_live_evidence.py::_validate_benchmarks` re-derives the
      curve query and **every** binding from the public owner by exact equality. Two bindings become
      database facts it cannot recompute offline. Replay the bundle's recorded run set back into the owner
      rather than excluding those names from the comparison, and keep every other binding exactly
      re-derived. The recorded set must be non-empty, aligned and well-typed, so an
      `= ANY('{}')` ghost measurement is refused rather than measured.
- [x] 2.13 Bind the recorded run set to something the bundle already proves. The bundle retains the full
      `EXPLAIN (ANALYZE, BUFFERS, VERBOSE, FORMAT JSON)` plan and already recomputes `shared_hit_blocks`
      from it (`:2535-2578`); psycopg2 interpolates client-side
      (`scripts/node27_timeseries_compression_benchmark.py:488,676`), so the plan's `Index Cond` / `Filter`
      text contains the literal `= ANY ('{…}'::integer[])`. Cross-check `pushdown_run_keys` against that
      plan text. Without it those two bindings are the only values in the bundle that reconcile against
      nothing.
- [x] 2.14 `_resolve_run_identity` must carry a deterministic `ORDER BY h.run_key`. The benchmark lists
      `binding` in `static_keys` and compares before/after for equality
      (`scripts/node27_timeseries_compression_benchmark.py:897-902`); an unordered result makes the
      binding non-deterministic across the compression window, so heap-order drift or a new run rejects
      the whole round as `benchmark query identity drift`. Before this change the binding was a pure
      function of the request and could not drift.

## 3. Tests

- [x] 3.1 Row-equivalence regression asserting the digest defined in the Evidence Floor, not a row count.
      **Executed on node-27 2026-09-16: `2 passed, 2 deselected in 25.49s`, rc 0** (legacy and narrow
      parameterisations), on a throwaway database created and dropped per test by
      `tests/conftest.py::throwaway_database_url`, DSN supplied only through the environment. It lives in
      `tests/test_forecast_series_run_identity_pushdown_integration.py::test_run_identity_pushdown_returns_byte_identical_rows_on_every_forecast_shape`
      and is gated on `NHMS_RUN_INTEGRATION=1` + `NHMS_INTEGRATION_DATABASE_URL`, so it skips locally.
      It only counts once run on node-27.
      **It is a different digest from the Evidence Floor's.** `_series_row_digest` (`:960-974`) hashes
      *response* fields — `scenario_id`, `source_id`, `cycle_time`, `valid_time`, `value` — while the
      node-27 probe baseline `b3b3f1bdd79a5e6c` hashes the raw fact rows (`run_key`,
      `river_network_version_key`, `valid_time`, `value`, `unit_e`). This test is a sound
      pushed-vs-unpushed equivalence oracle on its own terms; it does **not** satisfy task 4.4, which
      stays a probe-side comparison against `b3b3f1bdd79a5e6c`.
- [x] 3.2 Multi-scenario `:718` case with **≥ 2 distinct `cycle_time`s**, asserting every scenario's rows
      survive. The production measurement pinned one scenario, so the digest alone cannot catch a
      non-envelope window push.
- [x] 3.3 Over-pushdown regression. With no real-DB fixture in this suite, the executable form is a
      per-branch negative assertion on the rendered SQL: the `:793` and `:831` branches may contain
      `run_type` but MUST NOT contain `run_key =`, `rt.run_id`, `cycle_time` or `scenario_id`; the `:660`
      branch MUST NOT contain `run_key =` or `rt.run_id`. Spell out the forbidden-substring list so the
      test cannot pass vacuously. Run these against the **pushed-variant owners** of task 3.10 — against
      the default rendering they would pass for a reason unrelated to this change.
- [x] 3.4 An empty resolve result must produce the **same outcome the current code produces**.
      `forecast_series` MUST NOT short-circuit: pass the (possibly empty) key set down and let the existing
      flow produce today's response — explicit `issue_time` with no rows still raises 404
      `RUN_NOT_PUBLISHED` (`packages/common/forecast_store.py:482-493`), `latest` still returns an empty
      200, and the `include_analysis` branch (`:440-468`) still fetches and splices the analysis curve even
      when the forecast side is empty. Short-circuiting turns a 404 into a 200 and deletes the analysis
      series.
- [x] 3.9 Assert the D11 capture still works end to end against the unseeded `_RecordingCursor`:
      `record_explicit_cycle_curve` yields exactly one primary statement, that statement contains the
      in-SQL `run_key` subquery of task 2.2, and `validate_captured_explicit_cycle_query` still accepts it
      (`_REQUIRED_EQUALS`, `_REQUIRED_PRESENT_KEYS`). This is the test that proves task 4.5 is reachable.
      It belongs in `tests/test_node27_pgdata_workload.py`, where `record_explicit_cycle_curve` is already
      covered; there is no `tests/test_node27_pgdata_workload_query.py`.
- [x] 3.10 Add pushed-variant owners to `_segment_block_executions`
      (`tests/test_river_ts_text_identity_cleanup.py:563-621`), which calls the segment-block methods
      **directly** and so never sees what `forecast_series` resolves. Each seeds an explicit key set.
      Tasks 3.3 and 3.5 assert against these owners; the default-argument rendering stays as the
      un-pushed baseline.
- [x] 3.5 `OUTER_CLAUSES` (`tests/test_forecast_store_routing.py:172-186`) does **not** change — pushdown
      is additive (task 2.8), so every outer clause still appears exactly once. Only
      `assert_spanning_route`'s per-branch predicate list and aid counts change, and only for owners whose
      capture actually seeds a key set (task 3.10).
- [x] 3.6 Confirm every one of the eight segment-block methods still executes exactly one statement —
      `tests/test_river_ts_text_identity_cleanup.py:571` asserts this against an unseeded
      `_CaptureCursor`. A second statement inside a segment-block method makes `assert_spanning_route`
      (`tests/test_forecast_store_routing.py:186`) read the wrong SQL rather than fail cleanly.
- [x] 3.7 Re-pin **only** the source-scanning censuses, and do not relax them:
      `MARKER_AID_CENSUS["packages/common/forecast_store.py"]` (`tests/test_river_ts_text_identity_cleanup.py:216`)
      6 → 7 and its total (`:1590`) 8 → 9 **if** one shared aid constant is used. Two constants (a scalar
      `rt.run_id = %(run_id)s` aid for the bound case plus an `= ANY` aid for the unbound one) is an
      equally acceptable implementation and makes it 6 → 8. Re-pin to the **actual** count, never relax,
      and state the count in the PR.
      **Leave alone** `expected_aids` / `sum == 28` (`tests/test_sql_shape_helpers.py:833-834/845`) and the
      narrow/legacy line-count difference (`tests/test_river_ts_read_path_surrogate_keys.py:990`): those
      read `entry.source(store)`, which under task 1.2's empty-slot default still renders 3 aids. Changing
      them turns passing tests red.
- [x] 3.8 The legacy aid must be a triple-quoted constant with the marker and
      `AND rt.run_id = ANY(%(...)s)` on adjacent physical lines — `assert_marker_census`
      (`tests/river_ts_template_registry.py:530-537`) scans the source line by line.

## 4. Production evidence

Measured 2026-09-16 on node-27, read-only `nhms_display_ro`, against the **shipped code** of both
checkouts — `/home/nwm/tmp/2417-wt` at `fd3d4869a5b8dbf5370ddfc12c21e6eccf8ab5fd` and
`/home/nwm/tmp/2417-base` at `b40d0015a`. The probe drives the real `PsycopgForecastStore` through a
recording pass-through cursor, so every number below is what `forecast_series` itself issues, not a
hand-rebuilt statement. Probe and raw output: `/home/nwm/tmp/2417/probe2417.py`, `probe-{before,after}.json`.

| shape | before | after | fact stmt | resolve | rows | digest |
|---|---|---|---|---|---|---|
| `:758` `run_id` bound | 18 621 | **560** | 560 | — | 168 | `b3b3f1bdd79a5e6c` |
| `:758` `run_id` unbound | 18 621 | **2 813** | 2 548 | 265 | 168 | `b3b3f1bdd79a5e6c` |
| `:718` `issue_time=latest` | 1 331 390 | **650 589** | 3 049 | 265 | 168 | `b3b3f1bdd79a5e6c` |

- [x] 4.1 node-27 warm EXPLAIN × 3, `:758` with `run_id` bound, in-SQL scalar subquery form:
      `shared hit <= 5000` — **560**, no resolve step. Independently confirmed by the 4.5 receipt.
- [x] 4.2 node-27 warm EXPLAIN × 3, `:758` with `run_id` unbound: `shared hit <= 5000` — **2 548** on the
      fact statement plus **265** resolve, 2 813 total.
- [x] 4.3 node-27 warm EXPLAIN × 3, `:718`: the forecast-series fact read is **3 049** plus **265**
      resolve, from 684 115. It clears 5000.
      **But the shape as a whole does not**: `_per_source_latest_cycles` (`:594`) still costs
      **647 275** blocks to return one row, so the end-to-end `latest` request is 650 589. That is #2424
      and this change cannot fix it. The D11 gate measures the `:758`-bound statement, which is why 4.5
      passes; nothing here should be read as "the default path now clears the bound".
- [x] 4.4 Digest equivalence pre/post, probe-side, raw-row serialisation: **all three shapes return
      `b3b3f1bdd79a5e6c` over 168 rows, before and after, identical.** `:594`'s own one-row statement is
      likewise unchanged at `2add3314ffd65bc7`.
- [x] 4.5 `scripts/node27_pgdata_workload.py measure --evidence-kind live` → `{"ok": true, "status":
      "PASS", "evidence_kind": "live"}`, rc 0. `sql/buffers` **560** of 5000, `sql/p95_ms` **5.97** of 300,
      `api/p95_ms` **204.86** of 500, 168 rows, 0 shared reads, 20 accepted samples. Receipt at
      `/home/nwm/tmp/2417/measure-live.json`. This is the receipt #1987 task 5.2 needs, and it also
      settles the two gates that could not be judged statically — `PLAN_FILTER_RATIO`
      (`node27_pgdata_workload_plan.py:503-515`) and the segment-bound index requirement (`:475-490`)
      both pass on the new plan shape.
- [x] 4.6 API warm P95 on the shape D11 measures (`run_id` bound) is **204.86 ms**, inside the 500 ms
      bound. The **default** `issue_time=latest` shape is *not* re-timed as passing: its 650 589 blocks
      are dominated by #2424's `:594`, and the honest statement is that this change removes 680 000 of
      the 1 331 390 blocks it used to cost while leaving it above the API bound.
