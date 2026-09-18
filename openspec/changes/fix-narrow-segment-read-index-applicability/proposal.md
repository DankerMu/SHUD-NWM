# Keep the segment key in the segment read's index condition

## Triage

```text
Issue type: bugfix
Fixture level: expanded
Upstream suggested level: absent (orchestrator selects expanded; issue #2451 is needs-triage with the
  remedy direction chosen by the user: (a), the read path, not (b), statistics freshness)
Blast radius: every forecast-series display read on the narrow store. The defect is latent on master
  and activates on the next ff-only pull of node-27's live tree. It breaks the D11 gate
  (PLAN_FILTER_RATIO) on the largest river network, and D11 guards an irreversible DROP of a 732 GB
  table (#1988). The index this change touches is also read by three other display paths.
Selected risk packs: Public API / CLI / script entry; Resource limits / large input / discovery;
  Schema / columns / units / field names; Legacy compatibility / examples
Evidence floor: the throwaway oracle must show river_segment_key in the node's Index Cond, a per-node
  filter ratio within D11's bound, and node shared hits within a fixed multiple of the post-ANALYZE
  primary-key baseline, across the full cross product - run-bound AND issue_time=latest shapes, absent
  AND stale statistics, narrow AND legacy branches, uncompressed AND compressed chunks; node-27
  same-session A/B (merge-base vs this change) warm EXPLAIN (ANALYZE, BUFFERS) recording each touched
  chunk's last_analyze and n_mod_since_analyze, inconclusive if no chunk is in an absent-or-stale state;
  no regression on the three discovery-index consumers per the 000049 precedent, nor on
  _per_source_latest_cycles; sha256 row equivalence before/after on every measured shape; D11 live
  receipt PASS; uv run pytest on the render, text-identity-cleanup, forecast-store routing, migration
  and identity-normalisation suites; uv run ruff check .
```

## Why

`hydro.river_timeseries` carries three indexes
(`db/migrations/000059_river_timeseries_narrow_expand.sql:23-35`):

```
river_timeseries_narrow_pkey        (run_key, river_segment_key, variable_e, valid_time)
river_ts_segment_time_key_idx       (river_segment_key, variable_e, valid_time DESC)
river_ts_run_discovery_key_idx      (run_key, basin_version_key, river_network_version_key,
                                     variable_e, valid_time DESC)
```

The third does not contain `river_segment_key`. #2417 began binding `run_key` into the fact scan of
`_SEGMENT_ROWS_SOURCE_SQL` (`packages/common/forecast_store.py:29-54`, the `{run_pushdown}` slot), which
made that index newly matchable for the segment read. When the planner takes it, the segment predicate
falls out of the `Index Cond` into a `Filter` and the node reads **every segment of the run** for that
chunk's slice.

Measured on node-27 (2026-09-17, read-only; receipt
`openspec/changes/timeseries-narrow-store-expand-contract/receipts/2026-09-17-i8-explain-gate/`):

| shape | offending node | `Rows Removed / Actual` | node shared hits |
|---|---|---|---|
| run-bound | `_hyper_9_175_chunk` | 192 096 / 12 = **16 008** | 2 204 |
| `issue_time=latest` | `_hyper_9_170_chunk` | 384 192 / 24 = **16 008** | 4 480 |

D11 evaluates that ratio **per plan node** against `filter_ratio_limit = 10`
(`packages/common/node27_pgdata_workload_plan.py:503-515`, default `filter_ratio_limit = 10` at `:405`,
refuse code `PLAN_FILTER_RATIO`), so the gate
refuses on this network today. The ratio equals the network's segment count, so the breach scales with
the network: the same node on a 126-segment network shows 62.

The trigger is statistics. Proven on a throwaway database
(`tests/test_river_timeseries_stats_index_choice_integration.py`, `1 passed in 18.53s`): with
`ANALYZE` as the only variable, the index flips from the discovery index to the primary key,
`river_segment_key` moves from `Filter` into the `Index Cond`, the per-node ratio goes 2 999 → 0 and
execution time 22.254 ms → 0.255 ms.

**The remedy direction is (a), not (b).** Keeping statistics fresh would work — but the chunk that
mis-plans is always the one being actively written, whose statistics drift by construction. The
run-bound shape flipped a never-analysed chunk; the `latest` shape flipped one that had been analysed
and then accumulated 10 802 448 modifications. A read path that is correct only while an ingest-side
guard keeps up is not a fixed read path.

## What changes

The segment read - on **both** UNION branches, since one template renders them
(`packages/common/forecast_store.py:121-138`) and both physical tables carry a same-shaped index missing
the segment key - stops being satisfiable by an index that does not bind `river_segment_key`, under any
statistics state.

**The mechanism is selected by measurement, not by argument.** Two candidates survive as peers in
`design.md` — C1 and C2 — with C3 a gated fallback that needs a maintenance window, and C4 **withdrawn**
because a branch-level `ORDER BY` is a syntax error against
`packages/common/forecast_store.py:135-138`, which concatenates the two UNION branches bare. Claiming a
four-way measured choice the code cannot support would be theatre. The extended throwaway oracle decides
between C1 and C2 before either is committed to, across the full condition cross product in `design.md`
— predicate shape × statistics state × store branch × chunk compression state.

## What does not change

- **The discovery index is not dropped** — and neither are the legacy table's **two** same-shaped twins.
  `river_ts_selected_identity_key_valid_time_idx`
  (`db/migrations/000051_river_ts_surrogate_key_read_index.sql:100`, surrogate keys) and
  `river_timeseries_mvt_selected_identity_valid_time_discovery_idx`
  (`db/migrations/000021_latest_ready_run_discovery_idx.sql:15`, text identities) each carry the
  identical column order with the identical missing segment column, and **neither was ever dropped**:
  `000042:5` and `000049:52,84` dropped three *different* indexes, and `000059` renames the table and
  drops no index. The same template renders both branches
  (`packages/common/forecast_store.py:121-138`), so the legacy branch's exposure is structurally
  analogous — and it is now **measured**: the 2026-09-18 bench run reproduces the breach on legacy at
  ratio 999.0, through the key twin under absent statistics and through the text twin under stale
  statistics (`design.md`, "What the bench measured"; Q4 answered). C1 and C2 move only
  `basin_version_key` and `river_network_version_key`, which are not columns of the text twin, so a
  candidate that fixes narrow may not reach legacy; `tasks.md` §6.3 governs that, and the legacy
  exposure is closed in production today by three independently measured facts recorded in `design.md`.
  The narrow index has
  three live consumers that bind `run_key`,
  `basin_version_key` and `river_network_version_key` **without** a segment key:
  `apps/api/routes/hydro_display.py:1123-1164` (MVT source-identity existence probe),
  `services/tiles/mvt.py:702-730` (`valid_times_for_layer` named-identity discovery), and
  `packages/common/display_coverage.py:139-160` (per-run coverage refresh scan). Its purpose is stated
  at `openspec/changes/timeseries-narrow-store-expand-contract/design.md:57`.
- **No identity predicate is dropped.** `basin_version_key` and `river_network_version_key` are
  logically redundant once `run_key` and `river_segment_key` are bound — `hydro.hydro_run` has one
  `basin_version_id` per run (`db/migrations/000006_hydro.sql:2-6`) and `core.river_segment` one
  `river_network_version_id` per segment (`db/migrations/000004_core.sql:33-42`) — but they are pinned
  as permanent, non-aid conjuncts. **The guard that catches a violation is not the obvious one:**
  `_assert_key_predicates_retained` (`packages/common/river_ts_render.py:2556-2611`) is a *relative*
  check between a template and a rendering derived from it, so a symmetric rewrite leaves it silent and
  needing no edit - its silence is not evidence. The oracle that actually bites is
  `tests/test_river_ts_text_identity_cleanup.py:939-953`. Both exist because of
  #2050/#2086/#2112/#2114/#2141/#2148. Dropping either conjunct outright is out of scope here and would
  need its own issue.
- **Row identity.** Every measured shape must return byte-identical rows before and after.
- **Statistics.** This change makes no claim about, and takes no action on, autovacuum or
  `_analyze_frontier_chunks` (`scripts/node27_autopipeline.py:1628`). Whether the write frontier's
  statistics should also be kept fresh remains open on #2451 and is not settled here.

## Deviations from the issue's framing

#2451 was filed with the mechanism "the discovery index binds more columns, so its estimate is smaller
and it wins". The throwaway run **falsifies the clean form of that**: before `ANALYZE` the node reports
`Plan Rows = 1` at `Total Cost = 2.53`, i.e. the estimate clamped to the minimum rather than merely
being low. What the primary-key path would have cost without statistics was not measured, so whether
that choice was a near-tie broken by index size or OID order is undetermined. **No candidate mechanism
in this change may be justified by that unproven mechanism**; each must be selected on measured
behaviour.
