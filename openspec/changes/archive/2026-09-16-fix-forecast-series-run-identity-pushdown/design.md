# Design

**Change surface**: `packages/common/forecast_store.py` — `_SEGMENT_ROWS_SOURCE_SQL` (`:29-54`),
`_segment_rows_source_template` (`:57-63`), `_segment_rows_source_sql` (`:65-68`), and the eight call
sites at `:564, :594, :627, :660, :718, :758, :793, :831`; plus the two capture harnesses that drive
`forecast_series` — `packages/common/node27_pgdata_workload_query.py` and
`scripts/node27_timeseries_compression_benchmark.py` (see "The two production capture harnesses"); and
`scripts/node27_timeseries_compression_live_evidence.py`, the offline verifier that re-derives the
benchmark's curve query and every binding from the public owner by exact equality — two of the bindings
become database facts it cannot recompute, so it must change too (task 2.12). Test surface:
`tests/test_forecast_store_routing.py`, `tests/test_river_ts_text_identity_cleanup.py`,
`tests/test_river_ts_template_golden.py`, `tests/test_sql_shape_helpers.py`,
`tests/test_river_ts_read_path_surrogate_keys.py`, `tests/river_ts_template_registry.py`.

**Governing invariant**: for a given `run_key`, the `hydro_run` row seen inside a UNION branch and the one
seen by the outer join are the same row, so pushing a conjunctive outer `h.*` predicate into the branch is
an equivalence-preserving rewrite. Basis: `db/migrations/000050_river_identity_normalization.sql:178` —
`run_key INTEGER GENERATED ALWAYS AS IDENTITY UNIQUE`; both joins are `h.run_key = rt.run_key` on that
unique key.

Two consequences the implementer must not get wrong:

- **A filter fragment is pushed whole or not at all.** `_scenario_filter` (`:2681`) emits
  `AND (LOWER(h.source_id) = ANY(%(scenario_tokens)s) OR LOWER(h.scenario_id) = ANY(%(scenario_ids)s))`
  — a single conjunct whose *interior is a disjunction*. Pushing the whole parenthesised fragment is
  sound; pushing one side of the `OR` is not. `_run_identity_filter` (`:2699`) is pure conjunction.
- No call site applies an `h.*` predicate through an OUTER JOIN or inside a disjunction with a non-`h`
  term, so there is no other unsound case.

## The mechanism: resolve once, then push constants

Two of the three measured paths are served by one pattern; `:758`-bound keeps its in-SQL form because a
capture harness depends on it (next section). The template still needs one new slot, not three:

1. Run one `hydro.hydro_run` query for the call site's own run constraints, selecting `run_key` and
   `run_id`. Measured cost on node-27: **265 blocks / 1.1–1.7 ms**.
2. Push `rt.run_key = ANY(%(...)s)` into **both** branches, and a legacy-only
   `rt.run_id = ANY(%(...)s)` aid into the legacy branch, from those constants.
3. Where the call site's time window is expressed against the outer `h.cycle_time`, also push it as two
   constants.

| path | before | after | measured |
|---|---|---|---|
| `:758`, `run_id` bound | 18 621 | **560** | in-SQL scalar `run_key` subquery, **no resolve statement** — required by the D11 capture path, see below |
| `:758`, `run_id` unbound | 18 621 | **2 548** + 265 resolve | the live shape from `apps/frontend/src/stores/forecast.ts:356-369`, which never sends `run_id` |
| `:718` | 684 115 | **3 049** + 265 resolve | needs the `valid_time` envelope as well |

All row-identical to the unpushed query: `sha256` over `"\n".join(repr(sorted(row.items())))`, truncated
to 16 hex characters, equals `b3b3f1bdd79a5e6c` over 168 rows in every variant. That is the exact
serialisation used by the node-27 probes `/home/nwm/tmp/2410/explain718{,c,d,e}.py` and `explain758g.py`;
reproduce it, do not invent another.

**Resolve-query predicate discipline**: the resolve query's `h.*` predicate set MUST be a *subset* of the
outer layer's, so its result is a *superset* of the runs the outer layer keeps. Adding anything the outer
layer does not apply — `h.basin_version_id`, `h.status` — silently drops rows. It must also not collapse
to one run: a single `(scenario_id, cycle_time)` can match several runs, and all of them must be returned.

**The `valid_time` envelope at `:718`** (this is the one that silently loses data if done naively):
`cycle_times_by_scenario` is a **dict with one cycle per scenario** (`forecast_store.py:696` iterates
`sorted(cycle_times_by_scenario)`; `_per_source_latest_cycles` groups by `h.scenario_id` at `:600`), and
the outer window is **per run**: `rt.valid_time >= h.cycle_time AND rt.valid_time <= h.cycle_time +
INTERVAL '7 days'` (`:726-727`). The pushed constants must therefore be the **union envelope**
`min(cycle_time)` and `max(cycle_time) + 7 days` — a superset that the unchanged outer per-run predicate
then narrows. Pushing one scenario's cycle as both bounds deletes the other scenario's rows entirely. The
production measurement pinned a single scenario, so **the digest assertion cannot catch this**; task 3.1
carries a dedicated multi-scenario case.

## Where the resolve query lives

`tests/test_river_ts_text_identity_cleanup.py:571` asserts `len(cursor.statements) == 1` per owner against
an unseeded `_CaptureCursor` (`:528-548`, `fetchall()` returns `[]`). A second statement inside
`_fetch_forecast_segment_rows` therefore breaks the shared harness, and worse: with zero resolved rows the
early return leaves the *resolve* SQL as the captured statement, so `assert_spanning_route`
(`tests/test_forecast_store_routing.py:186`, `sql.count("UNION ALL") == 1`) fails and every consumer of
`FORECAST_STORE_EXECUTIONS` (`test_forecast_store_routing.py:222`, `test_river_ts_template_golden.py:114`
and `:403`) reads the wrong text.

**Decision: the resolve query runs in `forecast_series` and the resolved keys are passed down as
parameters.** The eight segment-block methods each keep executing exactly one statement, so
`tests/test_river_ts_text_identity_cleanup.py` and `tests/test_forecast_store_routing.py` need no harness
change.

### The two production capture harnesses, and why `:758`-bound is NOT resolve-then-push

`forecast_series` is also driven by two recording adapters that subclass `PsycopgForecastStore` over a
cursor whose `fetchall()` returns `[]`:

- `packages/common/node27_pgdata_workload_query.py` — `_RecordingCursor` (`:90-105`),
  `_CaptureForecastStore` (`:108-120`), `record_explicit_cycle_curve` (`:395-450`). **This is the D11
  receipt path** that task 4.5 and #1987 task 5.2 depend on. It drives `:758` with `run_id` **bound**.
- `scripts/node27_timeseries_compression_benchmark.py:241-272`, whose own comment (`:254-255`) states it
  relies on the owner raising **after** issuing the production query. It drives `:758` **unbound**.

A resolve-first `forecast_series` resolves zero runs under both, and every outcome loses the receipt:
early return → `primary` empty at `node27_pgdata_workload_query.py:447-453` → `QUERY_PRIMARY_INVALID`;
empty `ANY(ARRAY[])` → `PLAN_NO_ROWS` (`node27_pgdata_workload_plan.py:502`); fall back to no pushdown →
`PLAN_BUFFERS_EXCEEDED` as today. `measure --evidence-kind live` would merely swap one refusal code for
another. (`PLAN_NO_ROWS` and `PLAN_CANDIDATES_EMPTY` (`:426`) do mean a *vacuous* D11 pass is not
reachable — the gate fails closed. The cost is the receipt, not a false green.)

The CLI cannot seed the resolve either: `capture_workload_query` runs at
`scripts/node27_pgdata_workload.py:127`, **before** `open_readonly_connection` at `:140`. At capture time
it holds `run_id` and `model_id` from argv and no database, so it cannot produce a `run_key`.

**Decision: `:758` with `run_id` bound keeps the pushdown entirely inside the SQL** —
`rt.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = %(run_id)s)`. That is the shape actually
measured at **560**, it is self-contained, and the D11 CLI captures it with no harness change at all.
Resolve-then-push is used **only** where no capture harness reaches: `:758` unbound and `:718`.

The benchmark script is the one remaining consumer of `:758` unbound. It gets an overridable
`_resolve_run_identity(...)` seam on `PsycopgForecastStore` — the same shape as the existing
`_validate_series_target` override (`node27_pgdata_workload_query.py:119-120`) — fed from the live
connection its capture phase already holds (`:444-457`). Its placeholder check at `:262-272` is an
**exact set equality**, so it must be updated for the new bindings; `_REQUIRED_PRESENT_KEYS`
(`node27_pgdata_workload_query.py:64-74`) is a subset check and is not affected by additive bindings.

### Pushdown is additive: no outer predicate is removed

Pushing a predicate into the branches **adds** a conjunct; the outer layer keeps every predicate it has
today. Two independent reasons:

- `_REQUIRED_EQUALS` (`node27_pgdata_workload_query.py:57-63`) requires the captured statement to still
  contain `h.cycle_time = %(issue_time)s`, `h.run_id = %(run_id)s` and `h.model_id = %(model_id)s`;
  `:372-378` refuses `QUERY_IDENTITY_UNBOUND` if any is gone. Removing one kills the D11 receipt.
- The `:718` envelope is deliberately a **superset**; it is the unchanged per-run outer window at
  `:726-727` that narrows it back. Delete that outer window and the read silently returns up to seven
  extra days of rows.

Consequently `OUTER_CLAUSES` in `tests/test_forecast_store_routing.py:172-186` does **not** change.

## Per-call-site pushdown plan

| line | method | run set | push |
|---|---|---|---|
| `:564` | `_latest_issue_time` | all runs | **unchanged.** No production caller (`_latest_issue_time` appears only at its definition outside `tests/`); changing dead code adds risk without evidence. |
| `:594` | `_per_source_latest_cycles` | spans runs, no cycle to pin | **unchanged — #2424** |
| `:627` | `_latest_analysis_issue_time` | all analysis runs | literal `h.scenario_id = 'analysis_true_field'` |
| `:660` | `_fetch_analysis_segment_rows` | all analysis runs, **spliced** | literal `scenario_id` **only**. Its `DISTINCT ON (rt.valid_time) … ORDER BY rt.valid_time, h.end_time DESC` (`:653`, `:665`) deliberately splices across analysis runs. |
| `:718` | `_fetch_forecast_segment_rows`, cycle-per-scenario | bounded | resolved `run_key` set + legacy `run_id` aid + `valid_time` **envelope** |
| `:758` | `_fetch_forecast_segment_rows`, explicit cycle | single when `run_id` bound, bounded otherwise | **bound**: in-SQL scalar `run_key` subquery + legacy `run_id` aid, no resolve statement. **unbound**: resolved `run_key` set + legacy `run_id` aid |
| `:793` | `_latest_run_type_valid_time` | **all runs, deliberately** (`:795` has only `LOWER(h.run_type::text) = ANY(...)`) | `run_type` set only |
| `:831` | `_fetch_run_type_segment_rows` | **all runs, deliberately** (`:835-837`, no DISTINCT) | `run_type` set only |

Deferred with reason: `:660` (`:663-664`) and `:831` (`:836-837`) each already carry a constant-conjunct
`rt.valid_time` window in the outer layer — the same chunk-exclusion lever this change exploits elsewhere.
Neither is on the D11 path and neither has a production measurement, so pushing them is out of scope here
rather than done unmeasured.

## The golden-SQL suites need a pushed-variant owner

`_segment_block_executions` (`tests/test_river_ts_text_identity_cleanup.py:563-621`) calls
`_fetch_forecast_segment_rows` and its siblings **directly**, bypassing `forecast_series`. So with the
resolve one layer up:

- a required keyword argument makes that helper raise `TypeError`;
- an argument defaulting to `None` makes it capture the **un-pushed** SQL — and then every negative
  assertion in task 3.3 ("the `:793` branch must not contain `run_key =`") passes for a reason unrelated
  to the change, while `:718` and `:758` gain no text oracle at all.

The second is the dangerous one, because it is green. The harness therefore gains **pushed-variant
owners** that seed an explicit key set, and task 3.3's negative assertions run against those, not against
the default-argument rendering. `assert_spanning_route`'s legacy aid count
(`tests/test_forecast_store_routing.py:208`) changes only for owners that were actually seeded.

## Rejected shapes — do not re-attempt

| shape at `:718` | shared hit | verdict |
|---|---|---|
| `(h.scenario_id, h.cycle_time) IN (SELECT … FROM selected_cycles)` | **22 820 183** / 95 886 ms | 33× worse than doing nothing. A semi-join against a CTE gives the planner no constant, so chunk exclusion is lost entirely. |
| `h.cycle_time = ANY(…)` + `h.scenario_id = ANY(…)` only | 601 572 | −12%, insufficient |
| `rt.run_key = ANY(…)` only | 596 240 | −13%, insufficient |
| all three | 3 049 | accepted |

At `:758` with `run_id` unbound, pushing only the `rt.valid_time` window measured **18 621 → 18 621** —
no change, because that path already reaches only the 8 in-window chunks. Its cost is entirely the
un-memoized per-row `hydro_run` lookups, which need run identity to collapse.

Why all three are needed at `:718`: `rt.run_key` matches the narrow pkey prefix
`(run_key, river_segment_key, variable_e, valid_time)` but **cannot** prune the legacy compressed chunks,
whose `segmentby` is `(run_id, river_network_version_id, river_segment_id)`. The `rt.valid_time` envelope
is what removes those chunks from the plan at all, because today the window predicate at `:726-727`
references the outer `h.cycle_time` and no chunk exclusion is possible inside the branch.

**Do not extrapolate between call sites.** The rejected and accepted shapes differ by **7 484×** on the
same call site with identical semantics. Any call site whose shape changes needs its own measurement.

## Renderer constraints

`render_river_ts_sql` (`packages/common/river_ts_render.py:2604`) for `store == "narrow"` deletes each
`-- transitional compressed-chunk pushdown aid` marker line **and the single conjunct on the physical line
below it**, then runs `assert_structurally_intact`, `_assert_no_fact_text_identity` and
`_assert_key_predicates_retained`. So:

- The legacy `rt.run_id` aid must be written as a triple-quoted constant with the marker and
  `AND rt.run_id = ANY(%(...)s)` on **adjacent physical lines**. `assert_marker_census`
  (`tests/river_ts_template_registry.py:530-537`) scans the Python source line by line; an escaped
  `"\n"` single-line string fails it.
- `_AID_PREDICATE` / `_AID_KEYWORDS` (`river_ts_render.py:2280-2313`) accept `rt.run_id = ANY(%(x)s)`.
- The new predicate slot must not sit directly below an existing marker line, or narrow rendering deletes
  it.
- `_segment_rows_source_template(store)` must remain callable with the store argument alone and render an
  **empty** slot in that form, because `tests/river_ts_template_registry.py:255-264` and the single-valued
  `expected_aids` assertion (`tests/test_sql_shape_helpers.py:826-836`) cannot express legacy=4 / narrow=3.

## Required evidence

- node-27 warm `EXPLAIN (ANALYZE, BUFFERS)` × 3 on all three measured paths: `shared hit <= 5000`
  including the resolve step, with `hydro.river_timeseries_legacy` still present. Validated targets 560,
  2 548 + 265, 3 049 + 265.
- Digest equivalence before/after on all three, using the serialisation named above.
- A multi-scenario (≥ 2 distinct `cycle_time`) row-equivalence case at `:718`.
- A regression that **fails** when a call site pushes more than it should.
- The golden-SQL and census assertions updated in lockstep, not loosened.
- `uv run ruff check .`

## Review focus

1. Is the `:718` `valid_time` push an envelope over all scenarios, and is there a multi-scenario test?
2. Does the resolve query's predicate set stay a subset of the outer layer's, and does it avoid collapsing
   to a single run?
3. Do all eight segment-block methods still execute exactly one statement, **and** can the D11 capture
   path still produce a receipt — with a non-empty run-key parameter, so the EXPLAIN is not vacuous?
4. Is the legacy `rt.run_id` aid marker-guarded on adjacent physical lines, and is the narrow branch still
   free of fact-table text identity columns?
5. Is row equivalence asserted by digest rather than by row count, and is `:660`'s multi-run splice intact?
6. Are the marker/mention censuses re-pinned to the new counts rather than relaxed?
