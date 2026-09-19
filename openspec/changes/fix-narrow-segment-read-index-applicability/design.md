# Design: keep the segment key in the segment read's index condition

Revised after fixture review round 1 (verdict `revise`, 5×P1, 3×P2, 9 citation errors). The review's
corrections are applied here, not argued with; where it changed the shape of the design — the legacy
branch, the collapse of the candidate set, the guard that actually bites — the change is called out.

## Context (facts, each cited; nothing here is inferred)

**F1 — the three indexes on `hydro.river_timeseries`.**
`db/migrations/000059_river_timeseries_narrow_expand.sql:23-35`:
`river_timeseries_narrow_pkey (run_key, river_segment_key, variable_e, valid_time)`;
`river_ts_segment_time_key_idx (river_segment_key, variable_e, valid_time DESC)`;
`river_ts_run_discovery_key_idx (run_key, basin_version_key, river_network_version_key, variable_e,
valid_time DESC)`.

**F1b — the legacy table carries TWO same-shaped indexes, and neither was ever dropped.**
`db/migrations/000051_river_ts_surrogate_key_read_index.sql:100` creates
`river_ts_selected_identity_key_valid_time_idx (run_key, basin_version_key,
river_network_version_key, variable_e, valid_time DESC)` on what is now
`hydro.river_timeseries_legacy`. `000059` renames the table (`:8-9`) and **drops no index**; a
repository-wide grep finds no `DROP INDEX` for it.

**Corrected 2026-09-18 by measurement (§1.3's run):** there is a *second* surviving twin, the **text**
one. `db/migrations/000021_latest_ready_run_discovery_idx.sql:15` creates
`river_timeseries_mvt_selected_identity_valid_time_discovery_idx (run_id, basin_version_id,
river_network_version_id, variable, valid_time DESC)` — the same column order, the same missing segment
column, expressed in text identities. The later DROPs targeted **different** indexes:
`000042_drop_redundant_river_selected_identity_lookup_idx.sql:5` drops
`river_timeseries_mvt_selected_identity_lookup_idx` (the one that *did* carry `river_segment_id`, as its
6th column), and `000049:52,84` drop `river_timeseries_mvt_identity_lookup_idx` and
`river_timeseries_valid_time_discovery_idx`. This matters for the candidate set: **C1 and C2 move
`basin_version_key` and `river_network_version_key`, which are not columns of the text twin.** Neither
candidate can make the text twin non-matchable, and the bench measured the planner taking it
(§1.3 cell `run_bound/stale/legacy/uncompressed`).

Two details worth pinning, since a grep for the legacy table name finds neither index: both are created
`ON hydro.river_timeseries` and reach the legacy table by inheritance through `000059:9`'s `RENAME`. And
they carry **separate** oracles — `tests/test_migrations.py:385` pins the surrogate twin's columns, `:400`
the text twin's — so a change touching only one of them does not go silently green on the other.
`tests/test_river_identity_normalization_integration.py:270` comments that the legacy text indexes
remain on the renamed table. **Both have the same column order as F1's discovery index and the same
missing column.** The narrow index is therefore the key-column *analogue* of 000051's, not its
successor: `openspec/changes/timeseries-narrow-store-expand-contract/design.md:57` says "替代 000051",
but that replacement is scoped to the narrow table only.

**F1c — one template renders both branches.** `packages/common/forecast_store.py:136-143`
(`_segment_rows_source_template`) formats the **same** `_SEGMENT_ROWS_SOURCE_SQL` for `legacy` and
`narrow`, and `:146-148` (`_segment_rows_source_sql`) concatenates them as
`f"({legacy}\nUNION ALL\n{narrow})"`. Any change to the template lands on both branches, and by F1b the
legacy branch has the same exposure.

**F2 — the segment read already binds the segment key on every call site.** Every caller of
`_SEGMENT_ROWS_SOURCE_SQL` — `_latest_issue_time` (`packages/common/forecast_store.py:740`),
`_per_source_latest_cycles` (`:770`), `_latest_analysis_issue_time` (`:803`),
`_fetch_analysis_segment_rows` (`:836`), and the forecast/run-type segment fetches — takes a
`segment_id` and goes through `_segment_identity_params`. **The defect is index choice, not a missing
predicate.**

**F3 — the discovery index has three live consumers that bind no segment key.**
`apps/api/routes/hydro_display.py:1123-1164` (called from `:1093`), `services/tiles/mvt.py:702-730`,
`packages/common/display_coverage.py:139-160` (whose comment at `:64-65` names the index's column tuple
as its intended plan). Purpose stated at
`openspec/changes/timeseries-narrow-store-expand-contract/design.md:57`. A fourth historical consumer,
the hydro tile point lookup, was rewritten as a per-segment `CROSS JOIN LATERAL` and no longer uses it
(`db/migrations/000051_river_ts_surrogate_key_read_index.sql:44-57`, Round-3 amendment).

**F4 — `basin_version_key` / `river_network_version_key` are redundant but pinned.** Redundant:
`hydro.hydro_run` carries one `basin_version_id` per run (`db/migrations/000006_hydro.sql:2-6`),
`core.river_segment` one `river_network_version_id` per segment
(`db/migrations/000004_core.sql:33-42`), surrogate keys added by
`db/migrations/000050_river_identity_normalization.sql:185,192`. Pinned: neither conjunct carries a
`remove with #1342` aid marker in `packages/common/forecast_store.py:44-69`.

**F4b — but `_assert_key_predicates_retained` will not catch a symmetric rewrite.** It is a *relative*
check: `render_river_ts_sql` calls it at `packages/common/river_ts_render.py:2641` comparing the
template against the rendering derived from that same template, with aid lines removed. Rewriting a
conjunct in `_SEGMENT_ROWS_SOURCE_SQL` changes both sides, so the guard stays silent and needs no edit.
**Its silence is not evidence.** The oracle that actually bites is
`tests/test_river_ts_text_identity_cleanup.py:939 (helper), :981 (the pin), :989 (its red proof)`, which asserts the literal substrings
`"rt.basin_version_key = ("` and `"rt.river_network_version_key = ("` across all eight segment blocks.
`tests/test_river_ts_template_golden.py` does **not** bite: `forecast_store:segment_rows_source` is in
`ROUTED_SOURCE_KEYS` (`:95-107`, `:124-128`) and is excluded from the golden chain comparison.

**F5 — the index name is pinned by two live oracles and two archived tools.** Live:
`tests/test_migrations.py:278-282` (`RETAINED_RIVER_TIMESERIES_INDEXES`) and `:1498-1499`;
`tests/test_river_identity_normalization_integration.py:270-296` ("exactly three indexes" by name and
column list). Archived, non-blocking but would refuse on reuse:
`openspec/changes/archive/2026-09-15-refresh-node27-window-admission/tools/window_execute.py:1019-1025`
and `openspec/changes/archive/2026-09-15-node27-post-d12-reforward/tools/window_execute.py:1167-1173`.
No hard-coded index-name list exists in `packages/common/node27_pgdata_workload_*.py`,
`scripts/node27_timeseries_*.py`, or `validate_current_d3`
(`scripts/node27_timeseries_compression_supervisor.py:1718`), which pins compression settings.

**F6 — dropping or restructuring an index of this family has a precedent that demands live evidence.**
`db/migrations/000049_drop_redundant_river_mvt_identity_and_valid_time_discovery_idx.sql` proceeded only
behind a measured before/after `EXPLAIN (ANALYZE, BUFFERS)` receipt on node-27. A design-time argument
was not accepted then and is not accepted here. Note also
`db/migrations/000051_river_ts_surrogate_key_read_index.sql:67-74`: `CREATE INDEX CONCURRENTLY` is
refused on this hypertable, so any index rebuild is a plain `CREATE INDEX` holding a SHARE lock.

**F7 — there is no plan-steering mechanism in this read path.** The only `SET LOCAL` in it is
`statement_timeout` at `packages/common/display_coverage.py:740` (its value comes from
`_refresh_statement_timeout_ms()` at `:646`). No `enable_indexscan`, `enable_seqscan` or `pg_hint_plan`
usage exists in the repo's Python read path; `ForecastStore._transaction`
(`packages/common/forecast_store.py:2810-2811`) issues no session GUCs.

**F8 — the measured failure.** Receipt
`openspec/changes/timeseries-narrow-store-expand-contract/receipts/2026-09-17-i8-explain-gate/`:
run-bound flips `_hyper_9_175_chunk` (192 096 / 12 = 16 008 per-node ratio, 2 204 node hits);
`issue_time=latest` flips `_hyper_9_170_chunk` (384 192 / 24 = 16 008, 4 480 node hits). Different
chunks, so the defect is not chunk-specific. D11 evaluates the ratio per node: the loop starts at
`packages/common/node27_pgdata_workload_plan.py:503`, compares at `:514` and refuses at `:515` with
`PLAN_FILTER_RATIO`; `filter_ratio_limit = 10` is the default at `:405`.

**F9 — the trigger, proven.** `tests/test_river_timeseries_stats_index_choice_integration.py` on a
throwaway database: with `ANALYZE` as the only variable, the index flips, `river_segment_key` moves from
`Filter` into the `Index Cond`, ratio 2 999 → 0, 22.254 ms → 0.255 ms. **Sufficient cause established.**
The sub-mechanism (*why* the discovery index wins without statistics) is **not**: before `ANALYZE` the
node reports `Plan Rows = 1` at `Total Cost = 2.53` — the estimate clamped — and the primary-key path's
no-statistics cost was not measured.

**F9b — production staleness has a specific shape.** `_hyper_9_170_chunk` was analysed at
2026-09-17T00:16Z and then accumulated 10 802 448 modifications. The relevant staleness is not "more
rows": it is that **the target run was written after the last `ANALYZE`**, so its `run_key` is absent
from the column's MCV list and histogram. A reproduction that only adds rows for runs already present
will not reproduce it.

**F10 — D11's segment-bound check cannot catch this.** `_segment_identity_bound` reads a node's `Filter`
and `Index Cond` concatenated (`packages/common/node27_pgdata_workload_plan.py:240` `_node_predicate_raw`),
so the offending node satisfies it from its own `Filter`. Only `PLAN_FILTER_RATIO` catches it.

**F11 — statistics on the production chunks are refreshed continuously.**
`_analyze_frontier_chunks` (`scripts/node27_autopipeline.py:1628`, called at `:1761`) refreshes
`hydro.river_timeseries` candidates. It is **not** unconditional: the stats guard is opt-out via
`NODE27_AUTOPIPE_STATS_GUARD` (`:1742-1753`) and the frontier leg additionally requires
`ingested_runs >= 1` (`:1758`) — consistent with `_hyper_9_175_chunk` having never been analysed. By the time this change reaches live measurement, the two chunks
that failed on 2026-09-17 may carry fresh statistics — in which case an **unfixed** tree would also
measure green. Any cross-day before/after comparison is therefore confounded.

## The decision this design must make

Given F3 (the discovery index stays) and F4 (no identity predicate may be dropped here), the segment read
must become un-servable by an index lacking `river_segment_key` — on **both** branches, by F1b/F1c.

### The candidate set, after review

**C4 is withdrawn: it cannot be written.** The proposed branch-level `ORDER BY` is a syntax error —
`_segment_rows_source_sql` (`packages/common/forecast_store.py:146-148`) concatenates the two branches
bare, `f"({legacy}\nUNION ALL\n{narrow})"`, with no per-branch parentheses. And even if it could be
written, `river_ts_run_discovery_key_idx` ends in `valid_time DESC`, so after its equality prefix is
bound it yields the same `valid_time` order the primary key does; an `ORDER BY` cannot discriminate.

**C3 — reorder the discovery index so it also binds the segment key.**
`(run_key, basin_version_key, river_network_version_key, variable_e, river_segment_key,
valid_time DESC)`: even when the planner picks it, the segment predicate is an index condition and the
failure mode cannot occur. Its cost is that the three F3 consumers lose `valid_time` as an index
condition — a column after an unbound one cannot be used — and two of them bind it
(`apps/api/routes/hydro_display.py:1123-1164`, `packages/common/display_coverage.py:139-160`);
`services/tiles/mvt.py:702-730` does not. **It is a gated fallback, not a peer.** It requires an index rebuild on a 504 GB hypertable without
`CREATE INDEX CONCURRENTLY` (F6), i.e. a maintenance window, and a `000049`-style before/after receipt
for all three consumers in F3. It is considered only if C1 and C2 both fail the oracle, and it carries
its own admission tasks.

**So the live selection is C1 versus C2.** Saying otherwise would keep the appearance of a four-way
measured choice that the code does not support.

**C1 and C2 are not independent, and the fixture should not pretend they are.** Both work through the
same lever — shortening `river_ts_run_discovery_key_idx`'s usable prefix to `run_key` — so one planner
property decides both. If it does not hold, they fail **together** and the change lands on C3 and its
maintenance window. §2.2's stop-and-report exists for exactly that outcome.

**A tension to hold consciously.** `proposal.md` forbids justifying any candidate by the unproven
"more bound columns → smaller estimate → wins" mechanism. C1's and C2's rationale above is that
mechanism read backwards. That is why it is written as a *hypothesis to be measured*, not as a reason to
believe: §2.1 measures the cross product and §2.2 decides on the numbers. If a candidate passes, the
fixture records that it passed — not that the mechanism was thereby proven.

#### C1 — make the two redundant conjuncts non-sargable

Keep `basin_version_key` and `river_network_version_key` as enforced predicates but express them so they
cannot form an index condition on the discovery index's 2nd and 3rd columns (for example
`IS NOT DISTINCT FROM`). The discovery index's usable prefix collapses to `run_key`, while the primary
key still binds four columns.

**The equivalence argument this originally gave was wrong, and the correction matters.** It said "both
columns are `NOT NULL`, so the predicate is equivalent". That holds for the narrow table
(`000059:14-15`) and **not** for the legacy one, which the same template renders: `000050:203` adds the
seven surrogate columns as nullable and the only `SET NOT NULL` lives inside
`hydro.cutover_river_identity_normalization()` (`000050:452-458`), whose own `COMMENT` at `:492-499`
says it is never invoked by the migration chain. Confirmed live on node-27 — `pg_attribute.attnotnull`
is true for all three narrow key columns and **false** for all three legacy ones. `=` and
`IS NOT DISTINCT FROM` diverge only when **both** sides are NULL, so on legacy a row with a
never-backfilled `basin_version_key`, read with a `basin_version_id` that matches nothing, would be
excluded by `=` and **returned** by `IS NOT DISTINCT FROM`.

That divergence is **not reachable through `forecast_series`**, for two reasons that are neither of them
the one originally cited: `_validate_series_target`
(`packages/common/forecast_store.py:611-629`) raises 404 `SOURCE_NOT_FOUND` for an unknown
`basin_version_id` before any of the eight blocks run, in the same transaction (`:461`); and the network
leg is gated by `rt.river_segment_key = (…)`, which deliberately keeps `=` and is keyed on the same
`river_network_version_id`, with `core.river_segment.river_network_version_id` declared
`NOT NULL REFERENCES core.river_network_version` (`db/migrations/000004_core.sql:35`) — so a missing
network version makes the segment subselect NULL first and the retained `=` hard-gates the row.

An explicit `IS NOT NULL` is nevertheless added beside each rewritten conjunct, and **it was measured
before being believed**: the same 24 cells re-run against the guarded template differ from the unguarded
run in **nothing** — same index, same shared hits, same ratio, same digest, same verdict in every cell
(`receipts/2026-09-18-live-ab/matrix-guarded-20260918.json`). That mattered because the guard is not
obviously free: `IS NOT NULL` is btree-indexable, and on node-27's PG 15.2 there is no PG17
redundant-`IS NOT NULL` elimination, so `nulltestsel`'s `DEFAULT_NOT_UNK_SEL = 0.995` perturbs the
discovery index's estimate by ×0.995 under absent statistics. The structural reason it cannot reinstate
the defect — a `NullTest` with `IS_NOT_NULL` never sets `eqQualHere` in `btcostestimate`, so it cannot
carry the bound-qual prefix past `basin_version_key` — is confirmed rather than assumed.

The precise claim is **WHERE-equivalence, not truth-table identity**: with either side NULL, `=` yields
UNKNOWN while the guarded pair yields FALSE, and those are indistinguishable only because the conjunct
is always a top-level `AND` term of the branch's `WHERE`. That is pinned by a test that executes all
five NULL/non-NULL cells rather than arguing them.

The guard is there not because the hole is
live — it is not — but because without it the branch predicate depends on an upstream
validator that two subclasses override to a no-op (`packages/common/forecast_curve_capture.py:82`,
`packages/common/node27_pgdata_workload_query.py:119`). §2.2 rejected C2 for moving identity
verification out of the branch scan; accepting an equivalent dependency inside C1 would be the same
mistake wearing a different hat.

Costs: the conjunct text changes, so `tests/test_river_ts_text_identity_cleanup.py:939 (helper), :981 (the pin), :989 (its red proof)` goes red and
must be updated deliberately (F4b). Depends on a planner property that must be measured, never assumed.
Lands on the legacy branch too (F1c) — which is desirable here, since by F1b the legacy branch has the
same exposure, but it must be measured there rather than hoped for.

#### C2 — verify the two redundant conjuncts in the outer layer instead of the branch scan

The fact scan binds `run_key`, `river_segment_key`, `variable_e`, `valid_time`; the outer layer, which
already joins `hydro.hydro_run` and `core.river_network_version`, carries the identity verification.

Costs: runs against #2417's "pushdown is additive" rule. Moving predicates outward is the reverse motion
and must be justified on its own evidence, including the cycle-window envelope argument that change
relied on (`packages/common/forecast_store.py:98-104` `_CYCLE_WINDOW_PUSHDOWN_SQL`, applied at
`:873-876`). Also weakens the per-branch predicate set, which is closer to the fail-open class of F4
than C1 is — the predicate survives, but no longer inside the scan it was protecting.

**Measured costs of the spike, 2026-09-18 — C2 is materially worse than C1, before any plan is read.**

| | C1 | C2 |
|---|---|---|
| `test_river_ts_text_identity_cleanup.py` | 1 red, the F4b-predicted pin at `:942` | 5 red |
| `test_forecast_store_routing.py` (`:197`, `:214`) | 11 red, all text pins | 11 red, all text pins |
| nature of the extra reds | none | **2 semantic, 2 fixture** |

The two extra *semantic* reds are the finding: `…segment_blocks_carry_only_their_sanctioned_aids` and its
pushed twin report `text aid rt.river_network_version_id is not AND-ed with rt.river_network_version_key`.
C2 leaves the legacy branch's transitional **text** aid as that branch's *only* network identity
conjunct — which is the fail-open class of F4, the class #2050/#2086/#2112/#2114/#2141/#2148 closed. That
is a different order of cost from C1's single spelling pin, and §2.2 weighs it as such. The two fixture
reds are `_union_branches` pinning `_PROJECTION` verbatim, which C2's SELECT-list change breaks.

**F4b was incomplete**: it named only the text-identity oracle. `tests/test_forecast_store_routing.py:214`
(`assert_spanning_route`) pins the same literals and reddens 11 times under *either* candidate — every one
a text pin, none a behaviour difference (its parameter-set assertion still passes). Conversely the golden
fixture is **weaker** than F4b implied: it stores the eight `forecast_store:<label>` chains but no test
compares them, so `test_river_ts_template_golden.py` stays green under both candidates and is not a guard
here.

**must-preserve #6 / §4.4 is not at risk from either candidate**: `_REQUIRED_EQUALS`
(`packages/common/node27_pgdata_workload_query.py:57-63`) pins the two legacy text aids
`rt.river_segment_id` and `rt.river_network_version_id`, not `basin_version_key` /
`river_network_version_key`. Measured: the three `test_node27_pgdata_workload*` suites are `55 passed`
under all three variants.

**A trap when reading C2's plans.** C2 cannot be an outer `WHERE`: `pull_up_simple_union_all` promotes the
`UNION ALL` subquery and `set_append_rel_size` redistributes a single-relation qual back into each child,
returning the conjunct to the branch scan and making C2 a no-op. The spike therefore writes it as a JOIN —
but a JOIN can be defeated too, by a NestLoop with `spike_bv` as outer handing
`basin_version_key = spike_bv.basin_version_key` back to the chunk scan as a parameterised `Index Cond`.
**Read the `Index Cond`**: `spike_bv.` / `spike_rnv.` means the planner pushed back what C2 moved out and
the cell is measuring base; only `$N` is the intended C2 shape. This is decided on node-27, not locally.

### Ruled out, with reasons

- **Dropping either discovery index** — F3: three live consumers on the narrow one; the legacy one is
  retained by an explicit oracle (F1b) and its removal belongs to #1342/#1988, not here.
- **Dropping the redundant conjuncts outright** — F4: reopens the fail-open identity class closed across
  #2050/#2086/#2112/#2114/#2141/#2148. A separate issue if ever wanted.
- **`SET LOCAL enable_indexscan` or similar** — F7: nothing to extend, far too blunt, and PostgreSQL has
  no per-index disable.
- **Anything relying on statistics being fresh** — that is remedy (b), which the user did not choose,
  and F8/F11 show the mis-planned chunk is always the one being actively written.

## How the selection is made

The oracle is `tests/test_river_timeseries_stats_index_choice_integration.py`, extended per `tasks.md`
§1. A candidate is selected only if it holds under **every** condition below. A candidate that holds
only after `ANALYZE` has fixed nothing: that is the state the defect already has.

Conditions (the cross product is the gate):

- predicate shape: run-bound **and** `issue_time=latest` — F8 shows they flip different chunks;
- statistics state: absent **and**, if §1.2 shows it reproduces, stale-in-the-F9b-sense;
- branch: narrow **and** legacy — F1b/F1c, each judged against its own branch's segment
  identity column (see criterion 1);
- chunk state: uncompressed **and** compressed.

Pass criteria, per fact-reading node (three, not two — the third closes the hole the review found):

1. **The node's segment identity is bound in its `Index Cond`** — `river_segment_key` on a narrow node,
   `river_segment_id` on a legacy node, including the `compress_hyper_7_*` child of a legacy
   `DecompressChunk`. **The two branches are not the same predicate and requiring the key on legacy
   would be a permanent false red**: `render_river_ts_sql(..., "legacy")` retains the text aid conjuncts
   (`packages/common/river_ts_render.py:2634-2637`, which does not call the key-predicate assertion at
   all) and the legacy compression segmentby is text-based, so in every measured legacy plan the text
   primary key or text segmentby index wins and `river_segment_key` sits in the `Filter`. Verified in
   `receipts/2026-09-17-i8-explain-gate/explain-1987.json`, case `shj_nj/legacy`:
   `compress_hyper_7_104_chunk` binds `river_segment_id` in its `Index Cond` while
   `_hyper_3_62_chunk` carries `river_segment_key` only as a filter. Forcing legacy onto the key index
   would be exactly the regression must-preserve #4 forbids;
2. the node's `Rows Removed by Filter / Actual Rows` is within `filter_ratio_limit`, read off
   `evaluate_explain_json_plan`'s signature so the test reddens if D11 moves the bound;
3. the node's `Shared Hit Blocks` is within a fixed multiple of the post-`ANALYZE` primary-key baseline
   **and above an absolute floor of 256 buffers** — it must exceed both to fail (see "Criterion 3 was
   miscalibrated"; the multiple alone put 50 hits on the failing side of a line that 51 hits passed).
   **Criteria 1 and 2 alone are satisfiable by a bad plan**:
   PostgreSQL lists every qual on an indexed column in `Index Cond`, including non-boundary quals, and
   rows discarded in the index layer are not counted in `Rows Removed by Filter`, which D11 reads from
   the heap layer. An index shape carrying `river_segment_key` after `valid_time` would pass 1 and 2
   while still traversing every index entry for the run on that chunk.

## What the bench measured — baseline run, 2026-09-18, node-27

`tests/test_river_timeseries_stats_index_choice_integration.py` against unfixed code, 24 measured cells
(12 of them `fresh@*` post-`ANALYZE` controls, which carry no criterion-3 baseline and so are scored on
criteria 1–2 only). Evidence: `/home/nwm/tmp/2451/matrix.json`. **Three cells reproduce the defect**, and
all three are the **run-bound** shape on an **uncompressed** chunk:

| cell | index taken | segment bound? | ratio | node shared hits (baseline) |
|---|---|---|---|---|
| `run_bound/absent/narrow/uncompressed` | `river_ts_run_discovery_key_idx` | no (`river_segment_key` in `Filter`) | 999.0 | 5 977 (3) |
| `run_bound/absent/legacy/uncompressed` | `river_ts_selected_identity_key_valid_time_idx` | no | 999.0 | 12 841 (4) |
| `run_bound/stale/legacy/uncompressed` | `river_timeseries_mvt_selected_identity_valid_t…` | no | 999.0 | 12 841 (4) |

The ratio equals the fixture network's segment count, as production's 16 008 equals its own — the same
signature.

**Q4 is answered: the legacy branch is exposed, through two different indexes.** The absent-statistics
cell takes the **key** twin via `run_key = $3`; the stale cell takes the **text** twin via
`run_id = '…'::text AND river_network_version_id AND variable AND valid_time`, i.e. matched by the legacy
rendering's **text aid conjuncts**, not by `run_key` at all. C1 and C2 touch neither text column
(F1b, corrected). A candidate that fixes narrow is therefore *predicted* to leave
`run_bound/stale/legacy` red; §2.1 measures rather than assumes this, and §6.3 governs what happens if
the prediction holds.

**Production closure for the legacy branch (READ ONLY, node-27, 2026-09-18).** The exposure the bench
proves needs three conditions that are all false in production today and cannot become true before
#1988's DROP:

1. *a run routed to legacy* — #1988 gate 6.1 measured **0** legacy-routed runs in the retention window;
2. *a legacy chunk with absent or stale statistics* — all five chunks carry a `last_analyze`
   (2026-08-28 … 2026-09-14) and `n_mod_since_analyze = 0`;
3. *rows for the pinned run inside such a chunk* — with #1988's 0 legacy-routed runs, the run a
   segment read pins has no legacy rows to be mis-scanned, and no modification has landed on any chunk
   since the latest analyze (2026-09-15 03:21Z).

**Correction, same day — a fourth condition was claimed and is false.** An earlier revision of this
section asserted that outside `tests/` and `openspec/` no source file references
`hydro.river_timeseries_legacy`. That came from a `grep` truncated at 30 lines and is wrong. The legacy
branch is rendered **unconditionally into every forecast-series read** —
`packages/common/forecast_store.py:146-148` builds `f"({legacy}\nUNION ALL\n{narrow})"` with no
condition, and `services/tiles/mvt.py:910,1014,1017,2003` does the same — and the table name is a source
constant in two places: `packages/common/river_ts_render.py:101` (`RIVER_TABLE_LEGACY`) and
`packages/common/node27_pgdata_workload_plan.py:34` (`LEGACY_HYPERTABLE`). **Legacy plan nodes are
present on every read.** The closure therefore rests on conditions 1–3 above, which name what the defect
additionally needs, and not on an absence of references that does not exist.

This is a reason to record the legacy exposure, not to ignore it, and not a licence to widen the
candidate set: extending C1's non-sargable rule to the legacy text aids would touch the conjuncts the
#2050/#2086/#2112/#2114/#2141/#2148 family installed, which is a fixture amendment, not a spike.

### Criterion 3 was miscalibrated, and the baseline run proves it

Two cells sit on opposite sides of the verdict with the **same healthy plan** — `river_segment_key` (resp.
`river_segment_id`) in the `Index Cond`, ratio 0.0, ~48 rows returned:

- `latest/absent/legacy/uncompressed`: 51 hits, baseline 8, limit 64 → **pass**
- `latest/absent/narrow/uncompressed`: 50 hits, baseline 6, limit 48 → **fail**

The line fell between 50 and 51 because the post-`ANALYZE` baseline happened to be 6 on one branch and 8
on the other. Meanwhile the genuine defect cells sit at 5 977 and 12 841. The multiple alone therefore
cannot separate "the planner picked a different but healthy index" from "the node reads the whole
network". Criterion 3 gains an absolute floor, `SHARED_HIT_ABSOLUTE_FLOOR = 256` (2 MB at 8 kB pages):
a node must exceed **both** the multiple and the floor to fail. The rationale is geometric, not
empirical — below 2 MB a node cannot have read a full network's segments at any geometry this bench
seeds. Criteria 1 and 2 are untouched. Each node records the raw multiple verdict alongside the floored
one, so this baseline run is re-readable under the new rule without re-running it.

### Two gaps between the bench and production, which bound what the bench can select

- **The `latest` shape never reproduces in the bench**, while production breached at 16 008 on exactly
  that shape (`receipts/2026-09-17-i8-explain-gate/`). Every `latest` cell here binds the segment and
  shows ratio 0. So **the bench cannot select between candidates for the `latest` shape**; §4's live A/B
  on node-27 is the only gate for it, and §2.2 must not claim otherwise.
- **`narrow/stale` does not reproduce**, but not because staleness is safe: the cell's node reports
  `Plan Rows = 48`, a real estimate, whereas the reproducing cells report the clamped `Plan Rows = 1`.
  The fixture's "stale" is a **milder** state than production's, whose `latest` breach was on a chunk
  carrying 10 802 448 modifications since its last analyze. Recorded as: staleness reproduces on legacy
  through a different index; on narrow this fixture's staleness is insufficient to clamp the estimate.
  That is a limit of the fixture, not a finding about production.

## §2.2 Selection — C1, on the measurements

Three-variant run, one process, node-27, 2026-09-18 (`/home/nwm/tmp/2451/matrix.json`; each variant's
natural statistics state measured before the single shared `ANALYZE`):

| variant | cells passed | cells failed | row-digest mismatches |
|---|---|---|---|
| base | 21 / 24 | 3 | 0 |
| **C1** | **23 / 24** | 1 | 0 |
| C2 | 23 / 24 | 1 | 0 |

Both candidates close the two cells this change exists to close, and both leave exactly the same one:

| cell | base | C1 | C2 |
|---|---|---|---|
| `run_bound/absent/narrow/uncompressed` | `run_discovery_key_idx`, ratio 999.0, 5 977 hits | `segment_time_key_idx`, ratio 1.0, 50 hits | same as C1 |
| `run_bound/absent/legacy/uncompressed` | `selected_identity_key_valid_time_idx`, ratio 999.0, 12 841 hits | `segment_time_idx`, ratio 1.0, 51 hits | same as C1 |
| `run_bound/stale/legacy/uncompressed` | text twin, ratio 999.0, 12 841 hits | **unchanged** | **unchanged** |

**C2 was not a no-op**, so the tie is real and not an artefact: had the planner defeated its JOIN by
NestLoop parameterisation, C2 would have reproduced base's three failures; it reproduces one.

**So the plan outcomes do not discriminate, and the selection is made on the measured costs**, which do:

- **C1 reddens one oracle** — the F4b-predicted conjunct-spelling pin at
  `tests/test_river_ts_text_identity_cleanup.py:981` — plus 11 text pins in
  `tests/test_forecast_store_routing.py` that F4b did not name. **Corrected while implementing**: those
  11 all land on the single literal at `:214`, not on `:197` as recorded here earlier — `:197`
  (`sql.count(PROJECTION) == 2`) is a C2 red only, since C1 does not touch the SELECT list. Each of the
  11 was classified before being touched and all 11 are spelling, not behaviour; three of them assert the
  exact response payload *before* reaching the SQL pin, so the payload had already passed.
  **A twelfth red was masked behind them**: `:215`'s `\bDISTINCT\b` matches `IS NOT DISTINCT FROM`, so
  fixing `:214` would have moved all 11 onto `:215` rather than clearing them. Narrowed to
  `(?<!NOT )DISTINCT`, with `SELECT DISTINCT` and `MAX(` verified still caught. This is why the count in
  a review note is not a substitute for running the suite after the fix.
- **C2 reddens five**, and two of them are semantic, not spelling: it leaves the legacy branch's
  transitional **text** aid as that branch's only network identity conjunct, which is the F4 fail-open
  class that #2050/#2086/#2112/#2114/#2141/#2148 closed. C2 also depends on a JOIN the planner is free to
  push back into the scan, so its effect would have to be re-verified on every future plan change.

Both preserve row identity exactly (0 digest mismatches across 24 cells each). **C1 is selected.** The
mechanism by which C1 works is *not* claimed: F9's sub-mechanism is still unproven, and the selection
rests on the table above, not on "more bound columns → smaller estimate".

**What this selection may not claim** (per §2.2's bound): nothing about the `issue_time=latest` shape,
which no variant discriminates here because base already passes it — §4's live A/B on node-27 is its only
gate. And `run_bound/stale/legacy` remains open by construction: it is reached through the text twin,
which C1 has no lever on, so §6.3 files it rather than folding it in.

### §3 verified — shipped code, node-27, 2026-09-18

`1 passed in 33.29s`. 24 cells measured, 23 passed, 1 excused, **0 row-digest mismatches**, findings
empty. The two cells this change exists to close:

| cell | before | after |
|---|---|---|
| `run_bound/absent/narrow/uncompressed` | `run_discovery_key_idx`, ratio 999.0, 5 977 hits | `segment_time_key_idx`, ratio 1.0, 50 hits |
| `run_bound/absent/legacy/uncompressed` | `selected_identity_key_valid_time_idx`, ratio 999.0, 12 841 hits | `segment_time_idx`, ratio 1.0, 51 hits |

The excused cell still carries its real verdict in `matrix.json` — `passed: false`,
`defect_reproduced: true`, ratio 999.0, 12 841 hits — so the evidence says what happened, and only the
gate's assertion is relaxed. Filed as #2471.

### What the cross product does NOT cover — four of the eight blocks

Found in cross-review, and the PR's original Evidence Floor claim overstated this. The bench drives
`forecast_series` with `include_analysis: False` and no `run_types`
(`tests/test_river_timeseries_stats_index_choice_integration.py:369-380`), so the cross product varies
statistics state, branch and compression across **two** of the read path's predicate shapes — run-bound
and `issue_time=latest`. It never issues the other two: the `_ANALYSIS_SCENARIO_PUSHDOWN_SQL` blocks
(`packages/common/forecast_store.py:803`, `:836`) and the `_RUN_TYPE_PUSHDOWN_SQL` blocks (`:997`,
`:1035`). Those are live `/series?include_analysis` and hindcast paths and they have neither plan nor
digest evidence here.

Why that is recorded as acceptable rather than measured — and it is an **argument, not a measurement**:
the defect requires `run_key` to be bound into the fact scan, because that is what makes the discovery
index's leading column matchable. Those four blocks push `h.scenario_id` and `h.run_type`, not
`run_key`, so `river_ts_run_discovery_key_idx` and `river_ts_selected_identity_key_valid_time_idx` were
reachable for them only through a nestloop parameterised on `h.run_key`, and collapsing the usable
prefix raises that path's cost rather than lowering it. The risk direction is favourable. It is still
unmeasured, and the PR says so.

The one input class where the rewrite is **not** a no-op — a legacy row with a NULL surrogate key read
against a subselect that yields NULL — is also unexercised: the bench seeds legacy keys from
`bv.basin_version_key` (`tests/river_ts_stats_matrix_seed.py:676`, `:720`), so they are never NULL. The
"0 digest mismatches across 24 cells" result is therefore strong evidence for the shapes it covers and
**silent** on the only divergent one. The `IS NOT NULL` guard is what closes that class, not the digests.

### What criterion 3's floor costs, and what it is not tied to

Raised in cross-review, and it is a fair charge against the recalibration. On the **shipped** run the
floor is load-bearing in five cells — the multiple is exceeded and only the floor carries them — and two
of those five are the headline cells this change exists to close:

| cell | hits | baseline | 8× limit | multiple | verdict |
|---|---|---|---|---|---|
| `run_bound/absent/narrow/uncompressed` | 50 | 3 | 24 | 16.7 | passes on the floor |
| `run_bound/absent/legacy/uncompressed` | 51 | 4 | 32 | 12.8 | passes on the floor |
| `latest/absent/narrow/uncompressed` | 50 | 6 | 48 | 8.3 | passes on the floor |
| `latest/stale/narrow/uncompressed` | 50 | 6 | 48 | 8.3 | passes on the floor |
| `run_bound/stale/narrow/uncompressed` | 50 | 3 | 24 | 16.7 | passes on the floor |

So **after the fix, criterion 3 contributes no independent signal on the two cells it was meant to
back**: its verdict there reduces to "under 256 buffers, so nothing pathological". Criteria 1 and 2 carry
those cells. That is acceptable — a node reading 50 buffers for 24 rows is not scanning a 1 000-segment
network by any arithmetic — but it must be stated, because the adjacency pair used to justify the floor
(50 vs 51 hits) came from the **baseline** run's `latest` cells while the cells that actually benefit are
the **shipped** run's `run_bound` ones. The argument and its beneficiaries are not the same rows.

Two further limits of the floor, recorded rather than papered over:

- **It is not tied to the seed's geometry.** `SEGMENTS_PER_SCENARIO = 1000`
  (`tests/river_ts_stats_matrix_seed.py:67`) is the only reason the defect cells land at 5 977 and
  12 841, safely above 256. No assertion couples the two: shrinking the seed would silently make
  criterion 3 unfalsifiable on the uncompressed cells too, and
  `test_the_genuine_defect_cells_still_fail_criterion_3_under_the_floor` pins transcribed constants, not
  seed-derived ones.
- **It is untestable on the compressed cells.** Their healthy readings are 3 buffers (run-bound) and 102
  (`latest`), so any degenerate compressed plan under 256 buffers is invisible to criterion 3. No receipt
  has ever measured a degenerate plan on a compressed chunk, so the floor's stated rationale — "cannot
  represent a full-network scan at any geometry this bench seeds" — has empirical support only on the
  two uncompressed geometries.

### Two residual risks found in cross-review, neither blocking

- **`IS NOT DISTINCT FROM` is invisible to the renderer's comparison-position stripper.**
  `_COMPARISON_TAIL` (`packages/common/river_ts_render.py:212`) matches only `=`, `<`, `>`, `<>`, `!=`,
  `<=`, `>=`, so `_comparison_position_scalar_bodies` drops from 3 to 1 and `basin_version_id` now
  survives `strip_scalar_subqueries`. Harmless here — attribution is alias-scoped and
  `text_fact_columns(sql, "rt")` is still empty — and the failure direction is **fail-closed**, a false
  refusal rather than a false accept. But the module's "authority sub-select text is stripped before
  attribution" promise is now spelling-dependent, and a future template using this spelling against an
  *unaliased* fact table would hit the bare-column fallback and be refused.
- **On the legacy branch the rewrite also truncates the primary key's usable prefix to `run_key`.** The
  legacy pkey is `(run_key, river_network_version_key, river_segment_key, variable_e, valid_time)`
  (`db/migrations/000050_river_identity_normalization.sql:381-382`). Not a regression — criterion 1
  already records that legacy nodes never bound `river_segment_key` in an `Index Cond`, and the live A/B
  shows both arms identical — but it leaves `river_ts_segment_time_idx`, reachable only through the
  `#1342` transitional text aid, as the legacy branch's **only** segment-binding index. **If #1342
  removes the legacy text aids before #1988 drops the table, the legacy branch loses every sargable
  segment binding.** Recorded on #2471.

## Must-preserve behaviour

1. Row identity: every measured shape returns byte-identical rows, by the receipt's digest
   (`sha256("\n".join(repr(sorted(row.items()))))[:16]`).
2. `basin_version_key` and `river_network_version_key` remain enforced predicates of the segment read.
   If their spelling or position changes, `tests/test_river_ts_text_identity_cleanup.py:939 (helper), :981 (the pin), :989 (its red proof)` is
   updated **deliberately and visibly** (F4b), never loosened to a weaker match.
3. The three discovery-index consumers (F3) do not regress. Per F6 this is a measured claim.
4. **The legacy branch does not regress.** F1b/F1c: the same template renders it and the legacy table
   carries a same-shaped index. The receipt's three legacy cases (300 / 324 shared hits) are the
   baseline.
5. **The other four call sites of the shared template do not regress** — in particular
   `_per_source_latest_cycles` (`packages/common/forecast_store.py:770`), which is 631 496 shared hits
   and 98.8 % of the `latest` shape's cost. `probe1987latest.py` already captures it, so this is a
   missing criterion, not missing evidence.
6. `_REQUIRED_EQUALS` in `packages/common/node27_pgdata_workload_query.py:57-63` still matches the
   captured D11 statement; the D11 capture path is not modified.
7. The compressed-chunk access path stays as measured: `Index Cond ((run_key = …) AND
   (river_segment_key = …))` on the compressed chunk's segmentby index, one batch, nothing removed.

## Open questions to settle during implementation

- **Q1** Does the selected candidate hold for `issue_time=latest`, where the binding is
  `rt.run_key = ANY(%(pushdown_run_keys)s)` (`packages/common/forecast_store.py:527-535`, `:870-877`,
  `:82-85`)? A `ScalarArrayOpExpr` matches a btree leading column, and F8 shows it is taken.
- **Q2** For C3 only: what do the three F3 consumers cost with `valid_time` demoted to a filter,
  measured on node-27 per F6?
- **Q3** Does the selected candidate hold under F9b staleness, not only under absent statistics?
- **Q4 — ANSWERED 2026-09-18 by the baseline bench run: yes, the legacy branch is exposed.** The
  2026-09-17 production plans showed `river_ts_selected_identity_key_valid_time_idx` in no plan, which
  was read here as "empirically different". Under the run-bound pushdown with absent statistics the
  planner does take it, and under stale statistics it takes the **text** twin instead
  (F1b, corrected). Both breach at ratio 999.0. See "What the bench measured". What remains open is not
  whether legacy is exposed but whether the selected candidate reaches it — §2.1 measures that, §6.3
  governs the answer, and the production-closure facts recorded above bound the urgency.
