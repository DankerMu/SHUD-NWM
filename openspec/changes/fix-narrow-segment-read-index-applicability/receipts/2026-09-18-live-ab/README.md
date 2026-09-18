# #2451 §4 — live evidence on node-27, 2026-09-18

All read-only. Role `nhms_display_ro` from `infra/env/display.env` for the live A/B; role `nhms` on a
throwaway database (created and dropped per test) for the bench. DSN only via env, never argv, never
printed. No `ANALYZE`, no `compress_chunk`, no `drop_chunks`, no write of any kind. Nothing was deployed.

## The two arms

| arm | tree | commit |
|---|---|---|
| base | `/home/nwm/tmp/2451-base` | `011098d88` — this branch's merge-base, which already carries #2417 |
| head | `/home/nwm/tmp/2451-wt` | `40496a2b3` — C1 |

The merge-base is deliberately **not** `/home/nwm/tmp/2417-wt` (`fd3d4869a`), which the 2026-09-17
receipt used: that is an older master, and comparing this branch against it would let unrelated changes
into the diff. The live display API serves neither arm — `/home/nwm/NWM` is still at `7ecc46be`.

## §4.1 — PASS on no-regression, **INCONCLUSIVE on defect reproduction**

Same session, both probes, both arms, five warm rounds each.

**Every judged node is identical between arms**: 38 index-scan nodes on the run-bound probe and 84 on the
`latest` probe, each binding its branch's segment identity in the `Index Cond`, each at
`Rows Removed / Actual = 0.0`, each with the same `Shared Hit Blocks`. Every statement digest is
identical across arms. Legacy nodes still bind `river_segment_id` and the `compress_hyper_*` children
still bind `(run_key, river_segment_key)`, exactly as must-preserve #4 and #7 require.

C1's rendering is visible in the production plans, which is the point of doing this live:

```
Filter: ((NOT (basin_version_key IS DISTINCT FROM $4))
     AND (NOT (river_network_version_key IS DISTINCT FROM $6)))
Index Cond: ((run_key = $7) AND (river_segment_key = $5) AND (variable_e = 'q_down'…) AND (valid_time …))
```

**But the base arm does not reproduce the defect today, so this leg cannot confirm the fix.** On
2026-09-17 the same probe on master measured `_hyper_9_175_chunk` at ratio 16 008 (run-bound) and
`_hyper_9_170_chunk` at 16 008 (`latest`). Today both are at ratio 0.0 **in the base arm**. The chunk
statistics say why (`narrow-chunk-stats.sql`, same read-only session):

| chunk | `last_analyze` | `n_mod_since_analyze` |
|---|---|---|
| `_hyper_9_175` (yesterday's run-bound breach) | 2026-09-17 19:15:16Z | **0** |
| `_hyper_9_170` (yesterday's `latest` breach) | 2026-09-17 18:36:24Z | 9 685 896 |

This is precisely the confound `design.md` F11 predicted and `tasks.md` §4.1 wrote its `inconclusive`
rule for: `_analyze_frontier_chunks` refreshed the frontier, so an **unfixed** tree also measures green.
Per that rule the leg is recorded `inconclusive` and not looped on. The throwaway oracle of §1–§3 is the
primary evidence; this leg is confirmatory and confirms only the no-regression half.

**A negative result worth keeping**: `_hyper_9_142`, `_136` and `_133` carry 10 802 448 modifications
since their last analyse, and `_hyper_9_141` has a NULL `last_analyze` outright — yet all of them plan
correctly in **both** arms. So "stale by modification count" is not the trigger. What matters is F9b's
condition — the target run written *after* the last analyse — and today's target runs were not. This
independently corroborates §1.2's finding that the fixture's staleness is not the operative variable, and
it leaves the sub-mechanism behind production's `latest` breach still unproven.

**The narrow-compressed leg is reproducible after all.** `tasks.md` §4.1 expected `_hyper_9_126_chunk` to
have aged out; it is still present and still measured, alongside `_hyper_9_150`…`_156`.

## §4.2 — no regression on the three discovery-index consumers

The whole non-test diff between the arms is two files:

```
packages/common/forecast_store.py | 35 +++++++++++-----------
scripts/select_ci_tests.py        | 32 ++++++++++++++++++++
```

`apps/api/routes/hydro_display.py`, `services/tiles/mvt.py` and `packages/common/display_coverage.py` are
**byte-identical between the arms**, and C1 changes no index. The three consumers therefore present the
planner with identical SQL against an identical index set, and a warm A/B of them would compare a
statement against itself. The diff is the evidence; the F6 precedent that made this task mandatory exists
because `000049` *dropped* an index, which C1 does not do.

## §4.3 — no regression on the other call sites, `_per_source_latest_cycles` included

Twelve statements across **eight** cases, both arms (`statement-deltas.py`): the run-bound probe's six
cases carry one fact statement each, and the `latest` probe's two carry three each.

| | base | head | delta |
|---|---|---|---|
| `_per_source_latest_cycles`, SHJ-NJ | 631 833 hits | 631 833 hits | **0** |
| `_per_source_latest_cycles`, small | 632 045 hits | 632 045 hits | **0** |
| every other statement | — | — | **0** |

**Every statement's shared-hit delta is exactly 0 and every digest is identical.** The p95 figures move
between **-39.2 % and +1.1 %** on the head arm — ten statements lower, **two higher**
(`shj_nj/latest` stmt 1 at +0.1 %, `small_tailanhe/latest` stmt 1 at +1.1 %). That spread is run order,
not a speedup: the base arm ran first and the head arm second on a warmer cache, and a zero buffer delta
on every node means no plan changed. It is recorded as no-regression, in neither direction.

(An earlier revision of this file said "six cases" and "0.1 %–39 % lower", both wrong; caught in
cross-review by recomputing from the committed probe JSON.)

## §4.4 — capture path unaffected; the live D11 receipt is **not run**, and needs a GO

The offline half is decisive. `_REQUIRED_EQUALS`
(`packages/common/node27_pgdata_workload_query.py:57-63`) pins `h.cycle_time`, `h.run_id`, `h.model_id`,
`rt.river_segment_id` and `rt.river_network_version_id`. C1 touches none of them — it changes
`rt.basin_version_key` and `rt.river_network_version_key` — and `_NAMED_EQUALS_RE` (`:53-56`) only matches
the `= %(key)s` form, which those two conjuncts never had: they are subselects. Measured:
`tests/test_node27_pgdata_workload{,_io,_plan}.py` are `55 passed` under base, C1 and C2 alike.

The live half is **not run and must not be run on the orchestrator's own authority.**
`scripts/node27_pgdata_workload.py measure` drives `--api-origin`, i.e. the live display API on
`/home/nwm/NWM`, which is at `7ecc46be`. Producing a D11 receipt *for this change* would require pulling
the live tree to this branch — a production deployment, which needs an explicit GO, and which would also
carry #2417 into production (`tasks.md` §6.4's deploy hold). Recorded as blocked, not as passed.

## Files

| file | what it is |
|---|---|
| `probe1987-{base,head}.json` | run-bound probe bundles, both arms |
| `probe1987latest-{base,head}.json` | `issue_time=latest` probe bundles, both arms |
| `matrix-baseline-20260918.json` | §1's 24-cell baseline against the unfixed tree |
| `matrix-3variant-20260918.json` | §2.1's base/C1/C2 cross product — the selection evidence |
| `matrix-shipped-20260918.json` | §3's 24-cell run against the shipped C1 |
| `run-ab.sh` | the A/B runner; refuses any DSN whose role is not `nhms_display_ro` |
| `compare-arms.py`, `statement-deltas.py` | the comparators whose output is quoted above; lint-formatted after the run, so their text is not byte-identical to what executed — the archived `.txt` output is |
| `narrow-chunk-stats.sql`, `legacy-chunk-stats.sql` | the `READ ONLY` statistics snapshots |
| `chunk-stats-output.txt` | their result sets, plus the `pg_attribute.attnotnull` reading — the tables quoted above |
| `bench-shipped-run.out` | the `1 passed in 33.29s` pytest output for §3's run |
| `compare-arms-output.txt` | `compare-arms.py`'s console output |

The probes themselves are **not copied here**: they are
`openspec/changes/timeseries-narrow-store-expand-contract/receipts/2026-09-17-i8-explain-gate/probe1987*.py`,
run unmodified. They select fact statements by table-name marker (`FACT_MARKERS`) and pin no conjunct
literal, so they capture the head arm's rewritten SQL correctly — checked before the run, because a probe
that silently captures nothing reports no violation and reads as PASS.
