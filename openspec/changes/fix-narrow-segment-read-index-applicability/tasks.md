# Tasks — keep the segment key in the segment read's index condition (#2451)

Evidence Floor is the Verify clause on each task. A task is complete only when its Verify clause has
been run and its output recorded. Revised after fixture review round 1.

## 1. Extend the oracle until it can actually discriminate

The oracle is `tests/test_river_timeseries_stats_index_choice_integration.py`. As it stands it measures
one predicate shape, one statistics state, the narrow branch only, and uncompressed chunks only. Every
gap below is a way a wrong implementation would go green.

- [ ] 1.1 Add the **`issue_time=latest`** predicate shape. It binds
  `rt.run_key = ANY(%(pushdown_run_keys)s)` (`packages/common/forecast_store.py:516-524`, `:859-866`,
  `:82-85`) and production shows it flipping a *different* chunk than the run-bound shape (design.md
  F8), so one shape's result does not carry to the other. Note that `_capture_fact_statement`
  (`tests/test_river_timeseries_stats_index_choice_integration.py:505-513`, assertion at `:510`) filters
  on `"hydro.river_timeseries" in statement["sql"]` and demands exactly one hit. The `latest` shape
  yields **two** through that filter, not one: `_per_source_latest_cycles`
  (`packages/common/forecast_store.py:759`) and the cycle-window segment read (`:898`) both inline
  `_segment_rows_source_sql`. The relaxation is therefore to select the statement carrying
  `pushdown_run_keys` / `_CYCLE_WINDOW_PUSHDOWN_SQL`; `_per_source_latest_cycles`'s nodes are §4.3's
  business, not §1.1's.
  Verify: both shapes reproduce the failure against today's code on a no-statistics chunk, and both go
  green after `ANALYZE`. Record the two failure ratios.
- [ ] 1.2 Add the **stale**-statistics condition, in the shape production actually has (design.md F9b):
  `ANALYZE`, **then insert the target run's rows**, then do not re-analyse — so the target `run_key` is
  absent from the column's MCV list and histogram. Merely adding rows for runs already present will not
  reproduce it. Verify: the stale condition either reproduces the failure — in which case §2.2 and §3.1
  treat it as a hard gate — or it does not, in which case record plainly that production's `latest`
  failure on `_hyper_9_170_chunk` is **not** explained by staleness alone, and leave the question open
  rather than dropping it.
- [ ] 1.3 Add the **legacy branch**. `_segment_rows_source_template` renders one template for both
  stores (`packages/common/forecast_store.py:121-132`) and `river_timeseries_legacy` still carries
  `river_ts_selected_identity_key_valid_time_idx` — same column order, same missing segment key, never
  dropped (design.md F1b). Seed a legacy-routed run with legacy fact rows for the target segment, and
  extend the node extractor (`_narrow_access_nodes`, def at `:554`) — its filter is a caller-supplied
  allowlist (`:562`, applied at `:566-567`) populated at the single call site `:785` — past its
  narrow-chunk-name restriction so
  legacy nodes are measured rather than silently excluded. Verify: the legacy branch's nodes are
  present in the extract for both shapes; record whether the failure reproduces there (design.md Q4).
- [ ] 1.4 Add the **compressed-chunk** condition. Seed a second chunk and `SELECT compress_chunk(...)`;
  000059 already configures the compression settings. The extractor must map `compress_hyper_*_chunk`
  relations to the measured chunk, or the new condition runs empty and passes for nothing. The current
  test asserts the measured chunk is **not** compressed (`:376-379`); that assertion belongs to the
  uncompressed condition only. Verify: the compressed condition yields a node with
  `Index Cond ((run_key = …) AND (river_segment_key = …))` and reddens if that pruning is lost.
- [ ] 1.5 Add the third pass criterion: **node `Shared Hit Blocks` within a fixed multiple of the
  post-`ANALYZE` primary-key baseline**. Criteria 1 and 2 alone are satisfiable by a bad plan
  (design.md, "How the selection is made"); the field is already collected at `:587`, it just has
  no gate. Verify: a synthetic plan that carries `river_segment_key` late in the `Index Cond` and
  removes nothing at the heap layer is **rejected** by the extended criteria.

## 2. Select the mechanism by measurement

- [ ] 2.1 Implement **C1** and **C2** (design.md) far enough to be measured. These are throwaway spikes;
  only the selected one is committed. C4 is withdrawn — a branch-level `ORDER BY` is a syntax error
  against `packages/common/forecast_store.py:135-138`, which concatenates the branches bare. C3 is a
  gated fallback, attempted only if both C1 and C2 fail, and it carries §5. Verify: for each candidate,
  a recorded result for every cell of the condition cross product in design.md — shape × statistics
  state × branch × chunk state — giving criterion 1 judged **per branch**
  (`river_segment_key` in the `Index Cond` on narrow nodes, `river_segment_id` on legacy nodes including
  the `compress_hyper_7_*` child), plus per-node ratio and node shared hits. Written into `design.md` as
  a table.
- [ ] 2.2 Select one candidate and record why, **citing the measurements, not the design argument**.
  Rejected by definition: any candidate that holds only after `ANALYZE`; and, if §1.2 showed staleness
  reproduces the failure, any candidate that fails the stale condition. If **neither** C1 nor C2 holds
  across the cross product, stop and report to the user before attempting C3 — do not ship the
  least-bad one. Verify: the selection and its rejected alternatives are in `design.md` with the
  measured numbers beside each.

## 3. Implement the selected mechanism

- [ ] 3.1 Apply the selected change to `packages/common/forecast_store.py`. Verify:
  `uv run ruff check .`; the §1 oracle green across the **whole** condition cross product on a throwaway
  database on node-27 — including the stale condition if §1.2 showed it reproduces.
- [ ] 3.2 If the selected candidate changes the spelling or position of the `basin_version_key` or
  `river_network_version_key` conjuncts, the oracle that must be updated is
  **`tests/test_river_ts_text_identity_cleanup.py:939-953`**, which asserts the literal substrings
  `"rt.basin_version_key = ("` and `"rt.river_network_version_key = ("` across all eight segment blocks.
  `_assert_key_predicates_retained` (`packages/common/river_ts_render.py:2556-2611`) is a **relative**
  check — template versus a rendering derived from that same template — so a symmetric rewrite leaves it
  silent and needing no edit. **Its silence is not evidence that the predicate survived.** Update the
  literal pin to the new spelling and add an assertion that reverting the spelling to a plain `=`
  **reddens**, so the pin cannot be satisfied by loosening it to a substring match. Do not weaken the
  guard's exact-equality character — that hole was closed by review #1996 C8. Verify:
  `uv run pytest -q tests/test_river_ts_render.py tests/test_river_ts_text_identity_cleanup.py`; state
  the census delta and show the new red-proof.
- [ ] 3.3 If the selected candidate changes the index set, update `tests/test_migrations.py:278-282` and
  `:1498-1499` and `tests/test_river_identity_normalization_integration.py:270-296` (design.md F5), and
  note that two archived window tools also pin the three index names
  (`openspec/changes/archive/2026-09-15-refresh-node27-window-admission/tools/window_execute.py:1019-1025`,
  `.../2026-09-15-node27-post-d12-reforward/tools/window_execute.py:1167-1173`) — archived and therefore
  non-blocking, but they would refuse on reuse. Verify: `uv run pytest -q tests/test_migrations.py`;
  real-DB identity-normalisation test on node-27.
- [ ] 3.4 Row identity: every measured shape returns byte-identical rows, by the receipt digest
  `sha256("\n".join(repr(sorted(row.items()))))[:16]`. Verify: the throwaway digest regression passes
  for both shapes and both branches, and §4's node-27 probe reports the same digests as
  `receipts/2026-09-17-i8-explain-gate/explain-1987.json` and `explain-1987-latest.json`.

## 4. Live evidence on node-27

- [ ] 4.1 Re-run the receipt probes
  (`openspec/changes/timeseries-narrow-store-expand-contract/receipts/2026-09-17-i8-explain-gate/probe1987.py`,
  `probe1987latest.py`) **as a same-session A/B against two worktrees** — the merge-base and this change
  — exactly as the 2026-09-17 receipt did. A cross-day comparison against that receipt is **not**
  acceptable evidence: `_analyze_frontier_chunks` (`scripts/node27_autopipeline.py:1628`, called at `:1761`) refreshes them, so by then an *unfixed* tree may also measure green (design.md F11). Record, for **every chunk the plan touches**, in the same read-only
  transaction: `pg_stat_all_tables.last_analyze`, **`last_autoanalyze`** (a chunk refreshed by
  autoanalyse has a NULL `last_analyze` and would otherwise read as "no statistics" — the very confound
  this task exists to remove), `n_mod_since_analyze`, **the target run's write time** (staleness in the
  sense that matters is "the target run was written after the last analyse", design.md F9b, not "many
  modifications"), and **the state of `NODE27_AUTOPIPE_STATS_GUARD`**
  (`scripts/node27_autopipeline.py:1742-1753`). Verify: on SHJ-NJ, both shapes show `river_segment_key` in the
  `Index Cond` on every narrow fact node, **legacy fact nodes still binding `river_segment_id` in their
  `Index Cond` exactly as the 2026-09-17 receipt shows** (the legacy branch keeps its text aid conjuncts
  and text segmentby, so requiring `river_segment_key` there would be a permanent false red, and forcing
  it would be the regression must-preserve #4 forbids), per-node ratio within 10 on both branches, node
  shared hits within the §1.5 multiple, digests unchanged. **If no touched chunk is in an absent-or-stale statistics
  state during the run, the live leg is `inconclusive`, not `PASS`.** The terminating condition is
  explicit: the **throwaway oracle of §1-§3 is the primary evidence** and this live leg is confirmatory.
  Record `inconclusive` and move on; do not loop waiting for production to present the failing state. The narrow-compressed leg of the 2026-09-17 receipt is **not** reproducible
  (`_hyper_9_126_chunk` sat at the retention boundary); if it is gone, say so rather than substituting
  another chunk — §1.4 is what carries that property now.
- [ ] 4.2 No regression on the three discovery-index consumers (design.md F3, F6): warm
  `EXPLAIN (ANALYZE, BUFFERS)` before/after, same-session A/B, for
  `apps/api/routes/hydro_display.py:1123-1164`, `services/tiles/mvt.py:702-730` and
  `packages/common/display_coverage.py:139-160`. Verify: shared hits and warm P95 for each, before and
  after, any regression stated in absolute terms. Mandatory regardless of candidate; for C3 it is the
  admission gate, per `db/migrations/000049_...`.
- [ ] 4.3 No regression on the other call sites of the shared template. There are eight
  (`packages/common/forecast_store.py:729, 759, 792, 825, 898, 950, 986, 1024`); only `:898` and `:950`
  bind `rt.run_key` into the fact scan, `:792`/`:825` push `h.scenario_id` and `:986`/`:1024` push
  `h.run_type` (`:97-98`, `:101-102`), and `:729`/`:759` push nothing — so no exposed call site is
  unmeasured. Measure in particular
  `_per_source_latest_cycles` (`packages/common/forecast_store.py:759`) at 631 496 shared hits — 98.8 %
  of the `latest` shape's cost. `probe1987latest.py` already captures it. Verify: its shared hits and
  warm P95, before and after, recorded as a pass/fail criterion rather than as background.
- [ ] 4.4 The D11 capture path is unmodified and still accepts the statement: `_REQUIRED_EQUALS`
  (`packages/common/node27_pgdata_workload_query.py:57-63`) matches the captured statement. Verify: the
  D11 live receipt runs and reports `status: PASS`, with the buffer count recorded. This is the gate
  #1987 task 5.2 and #1988 both depend on.

## 5. C3 admission (only if §2.2 selects C3)

- [ ] 5.1 C3 rebuilds an index on a 504 GB hypertable, and `CREATE INDEX CONCURRENTLY` is refused on it
  (`db/migrations/000051_river_ts_surrogate_key_read_index.sql:67-74`), so the rebuild is a plain
  `CREATE INDEX` holding a SHARE lock. Produce a maintenance-window plan with a measured duration
  estimate from a throwaway cluster, an abort criterion, and a rollback receipt in the shape of
  `db/migrations/000049_...`. Verify: the plan is reviewed and the window is separately authorised by
  the user before any production execution. **No production execution without an explicit GO.**

## 6. Close out

- [ ] 6.1 Record the result on #2451, including whether the sub-mechanism question (design.md F9 — why
  the discovery index wins without statistics) was answered by the candidate measurements or remains
  open. Do not close it as answered if it was not.
- [ ] 6.2 State explicitly whether remedy (b) — keeping the write frontier's statistics fresh — is still
  wanted after this change, and if so leave #2451 open for it or file the follow-up. The user chose (a);
  that choice does not by itself decide that (b) is unnecessary.
- [ ] 6.3 If §1.3 showed the legacy branch reproduces the failure and the selected candidate does not
  reach it, file that as its own issue rather than folding it in silently. It expires with #1988's DROP,
  which is a reason to record it, not a reason to ignore it.
- [ ] 6.4 Note the #2417 deploy hold: the live tree `/home/nwm/NWM` was at `7ecc46be` on 2026-09-17,
  older than #2417's merge-base, so the defect this change fixes is latent in production and activates
  on the next ff-only pull. Verify: the PR body states which commit the live tree carries at merge time.
