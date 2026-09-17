# Push run identity into the forecast-series UNION branches

## Triage

```text
Issue type: bugfix
Fixture level: expanded
Upstream suggested level: absent (orchestrator selects expanded)
Blast radius: every forecast-series display read. The shared template feeds eight call sites on the
  only public entrypoint for river forecast curves; a wrong pushdown silently returns the wrong run's
  data on a production chart. The same query is the D11 gate that guards an irreversible DROP of a
  732 GB table (#1988).
Selected risk packs: Public API / CLI / script entry; Resource limits / large input / discovery;
  Legacy compatibility / examples; Schema / columns / units / field names
Evidence floor: node-27 production warm EXPLAIN (ANALYZE, BUFFERS) on :758 and :718 with shared_hit
  <= 5000 while hydro.river_timeseries_legacy still exists; sha256 row equivalence before/after on
  both paths; uv run pytest on the forecast-store routing and text-identity suites; uv run ruff check .
```

## Why

`packages/common/forecast_store.py:29-54` (`_SEGMENT_ROWS_SOURCE_SQL`) joins
`hydro.hydro_run h ON h.run_key = rt.run_key` inside each UNION branch but constrains the run side with
**only** `h.timeseries_store = 'legacy'|'narrow'`. Every run-identity predicate — `h.cycle_time`,
`h.run_id`, `h.model_id`, and the 7-day `rt.valid_time` window, which is expressed against the outer
`h.cycle_time` — is applied in the **outer** layer. So each branch first materialises every run's rows for
the pinned segment inside the retention window, then the outer layer discards almost all of them.

Measured on node-27 production (2026-09-16, read-only `nhms_display_ro`, worktree `/home/nwm/tmp/2410-wt`
at `652b520739db0785549b126509de45bb472cab9b`):

| path | statement | warm shared hit | warm exec_ms | rows |
|---|---|---|---|---|
| explicit `issue_time`, `run_id` bound | `_fetch_forecast_segment_rows` `:758` | 18 621 | 18.5 / 19.8 / 45.2 | 168 |
| explicit `issue_time`, `run_id` unbound | `_fetch_forecast_segment_rows` `:758` | 18 621 | 18.4 / 18.6 / 20.8 | 168 |
| default `issue_time=latest` | `_fetch_forecast_segment_rows` `:718` | 684 115 | 2519 / 2520 / 2522 | 168 |

Both `:758` shapes are live. `apps/frontend/src/lib/hydroMet/riverForecast.ts:134-135` (the map panel)
sends `run_id` and `model_id`; `apps/frontend/src/stores/forecast.ts:356-369` never does, and that store
is mounted (`apps/frontend/src/components/ScenarioSelector.tsx:14-19`).

The D11 bound is `shared hit <= 5000`. The default request shape is also user-visible: two consecutive
warm calls to the production display API returned **HTTP 200 in 9.296 s and 9.171 s** against a D11 API
warm P95 of 500 ms. The frontend reaches it — `apps/frontend/src/stores/forecast.ts:364` sends
`issue_time: options.issueTime ?? 'latest'` and `:440` leaves `issueTime` undefined on the first click of
a segment.

This is pre-existing, introduced by `e8b30893c feat(narrow-store): route forecast facts by run store
(#1981)`: routing facts by the run's `timeseries_store` required the `hydro_run` join inside the branch,
but identity convergence stayed outside, splitting one decision across two layers.

Compression makes the same shape latently worse on the narrow table. `hydro.river_timeseries_legacy` is
`segmentby = (run_id, river_network_version_id, river_segment_id)`; with `run_id` unconstrained its
compressed chunks read **589 280 blocks to return 82 rows**. `hydro.river_timeseries` is
`segmentby = (run_key, river_segment_key)` and is equally unconstrained by the current shape, so a
compressed narrow chunk would behave the same way.

It does not today only because narrow compression currently yields nothing durable: measured 2026-09-16,
`hydro.river_timeseries` has **0 of 30 chunks compressed** (496 GB). The daily runner compresses the four
oldest chunks and the retention timer drops them ~44 minutes later. That is tracked separately — it is not
this change's problem, and this change must not be justified by it. The point here is only that the
current query shape has no protection against compressed narrow chunks whenever that ordering is fixed.

## What changes

`_SEGMENT_ROWS_SOURCE_SQL` gains a **per-call-site, per-store** predicate slot alongside the existing
`{store_predicate}`. Each call site supplies the conjunctive subset of its outer `h.*` / `rt.*` constraints
that is correct for it to push. Call sites that deliberately span every run keep pushing nothing beyond
their `run_type` set.

Where `run_id` is bound (`:758`), the push is a self-contained in-SQL scalar `run_key` subquery — the D11
workload CLI captures that statement and has no database at capture time, so it must stay self-contained.
The other two paths resolve the call site's runs once against `hydro.hydro_run` (**265 blocks /
1.1–1.7 ms**), then push `rt.run_key = ANY(…)` into both branches plus a legacy-only `rt.run_id = ANY(…)`
aid. `:718` additionally pushes its `valid_time` window as two constants. Every push is **additive**: no
outer-layer predicate is removed.

Validated on production, every variant row-identical
(`sha256("\n".join(repr(sorted(row.items()))))[:16] = b3b3f1bdd79a5e6c`, 168 rows):

| path | before | after |
|---|---|---|
| `:758`, `run_id` bound | 18 621 | **560** |
| `:758`, `run_id` unbound | 18 621 | **2 548** + 265 resolve |
| `:718` | 684 115 | **3 049** + 265 resolve |

All three clear the 5000 ceiling **with the legacy table still present**; #1988 is not a prerequisite.

## Non-goals

- `_per_source_latest_cycles` (`:594`) — a different root cause with a different fix. Filed as **#2424**.
- Dropping `hydro.river_timeseries_legacy` — **#1988**. This change removes the need to wait for it.
- Re-deriving the D11 buffer bound. Explicitly rejected: 9 864 (let alone 684 115) blocks for 168 rows is
  pathological regardless of where the line sits, and relaxing the bound would weaken the only gate
  guarding an irreversible 732 GB DROP.
- The 336-block `Seq Scan river_network_version` (×168 loops) in the outer layer.
- Disposing of `_latest_issue_time` (`:564`), which has no production caller — reported, not fixed here.
- Cold-read measurability under `STATEMENT_TIMEOUT_MS = 5000`
  (`packages/common/node27_pgdata_workload_io.py:34`).

## Deviations from the issue's acceptance list

Two items in #2417's acceptance list are superseded by evidence found after it was written. Both are
deliberate and must be restated in the PR body.

- **`OUTER_CLAUSES` does not change.** The issue says "谓词从 outer 移入分支会让 `outer.count(clause)`
  从 1 变 0". Pushdown is **additive** — see design.md, "Pushdown is additive". `_REQUIRED_EQUALS`
  (`packages/common/node27_pgdata_workload_query.py:57-63`) requires the outer equalities to survive, and
  the `:718` envelope is only sound because the outer per-run window still narrows it. Moving a predicate
  out would kill the D11 receipt and silently widen `:718`.
- **Cold-read measurability is resolved by the second of the issue's two options**: D11 does not take cold
  evidence through this CLI. `STATEMENT_TIMEOUT_MS` is left at 5000 and unparameterised. This change makes
  the *warm* path clear the bound with the legacy table present; a cold read of a 496 GB hypertable is not
  something a 5 s statement timeout can measure, and relaxing that timeout would weaken a guard on the
  same gate that protects the irreversible 732 GB DROP.
