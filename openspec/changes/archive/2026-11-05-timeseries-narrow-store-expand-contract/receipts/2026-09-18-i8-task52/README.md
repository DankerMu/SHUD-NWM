# I8 task 5.2 — the live receipt, node-27, 2026-09-18

**Reviewed sha `258b06ec`.** That is not the tree's HEAD, it is what the serving process
loaded: `nhms-display-api.service` `MainPID=1058434`, `ActiveEnterTimestamp=Fri
2026-09-18 13:56:11 CST`, and `/home/nwm/NWM` has been at `258b06ec` since before that
restart. This receipt states the process sha rather than the tree sha because reading
runtime state off tree state produced two wrong claims in this issue's history
(recorded in `../../../fix-narrow-segment-read-index-applicability/receipts/2026-09-18-live-ab/README.md`).
The service was **not** restarted for this receipt; every measurement below is a read.

All DB access is read-only: role `nhms_display_ro` from `infra/env/display.env`, every
statement inside a `READ ONLY` transaction, DSN via env only — never argv, never printed,
and no DSN appears in any file in this directory. No `compress_chunk`, no `drop_chunks`,
no `ANALYZE`, no `systemctl start`, no `reset-failed`, nothing on node-22.

This receipt supersedes the RED verdict of `../2026-09-17-i8-explain-gate/` §3. That
receipt stays as written; it is the record of the defect, and its §4–§8 are still the
authority for the compression ratio, the tick pair and the causal proof.

## Verdict per task 5.2 item

| task 5.2 item | verdict | where |
|---|---|---|
| curve EXPLAIN gate, SQL bounds, 2 networks × 3 storage states | **GREEN** | §1 |
| the same gate's local API bound (P95 ≤ 500 ms) | **GREEN** — 6/6 quiescent, worst 202.7 ms. Under full-cycle ingest 4/6 miss it, legacy included; by the 2026-09-18 decision that is a capacity item (#2486), not this bound | §2 |
| identity-existence probe miss branch before/after + coverage loss | **GREEN, no loss** | §3 |
| registry counts (active/runnable/selected/excluded) | **38 / 38 / 38 / 0** | §4 |
| governance receipt with the working-set fields | **captured, not critical** | §5 |
| one compression tick and one retention tick covering both tables | **the 0917 pair; today's pair failed** | §6 |
| first narrow chunk size after one full cycle | **21 GB uncompressed / 3677 MB compressed** | §7 |
| display deny-write receipt (checklist C1–C4) | **C2 deny-write 23/23 PASS — the item this task asks for; C1/C4 partial, C3 out of scope and blocked; validator overall BLOCKED on C3** | §8 |
| `/` clicks on three networks with screenshots | **3/3, GFS+IFS both 200** | §9 |
| `/ops` reachable | **200** | §9 |
| the regression criterion recorded | **recorded, not tripped** | §10 |
| per-statement expand wall time | **NOT OBTAINABLE — waived by the user 2026-09-18; window wall clock stands** | §11 |

## 1. The curve EXPLAIN gate — six cells, all four SQL bounds

`probe0918.py` drives the **real `PsycopgForecastStore` of the live tree** through a
recording pass-through cursor, then `EXPLAIN (ANALYZE, BUFFERS)` each fact-reading
statement five times warm. Same probe as 2026-09-17, one change: the compressed leg
resolves its cycle live (the oldest narrow forecast cycle inside the oldest currently
compressed chunk) instead of a literal date, because retention moves that window daily
and a pinned date silently stops measuring the compressed state.

Pinned shape, the run-bound one (the 2026-09-17 user decision recorded in that receipt's
§6): `forecast_series(variables=['q_down'], scenarios=['GFS'], run_types=['forecast'],
include_analysis=False, issue_time=<cycle>, run_id=…, model_id=…)`.

Networks: SHJ-NJ `basins_shj_nj_rivnet_vbasins` (32 018 segments) and
`basins_tailanhe_rivnet_vbasins` (126), the same two the 2026-09-17 receipt resolved.

| cell | run | `shared hit` | SQL warm P95 | rows | digest |
|---|---|---|---|---|---|
| shj_nj / narrow **compressed** | `fcst_gfs_2026082600_dg_6f9ed937…` | **465** | **2.028 ms** | 168 | `f3ec8146ad2f5b4c` |
| shj_nj / narrow uncompressed | `fcst_gfs_2026091700_dg_6f9ed937…` | **569** | **3.156 ms** | 168 | `5b5d910d88ad8526` |
| shj_nj / legacy | `fcst_gfs_2026081800_dg_6f9ed937…` | **300** | **1.252 ms** | 120 | `0a5aea3665695eae` |
| small / narrow **compressed** | `fcst_gfs_2026082600_dg_510fbd39…` | **441** | **1.873 ms** | 168 | `e27a54074ca37441` |
| small / narrow uncompressed | `fcst_gfs_2026091700_dg_510fbd39…` | **513** | **1.696 ms** | 168 | `22ab180675efc60a` |
| small / legacy | `fcst_gfs_2026081812_dg_f14b5404…` | **324** | **1.299 ms** | 132 | `0b6e670db0abad25` |

Both legacy digests are **byte-identical to the 2026-09-17 run** (`0a5aea3665695eae`,
`0b6e670db0abad25`) — the legacy leg returns the same rows it did before the fix. The
narrow digests differ from 0917's because the probe resolves `newest`, and the newest
narrow cycle moved from 2026-09-16 to 2026-09-17.

`gate0918.py` judges **every** fact plan node, not the statement total, and prints the
number of index nodes it judged so that a zero walk shows up as a zero instead of as a
green. (Two earlier comparators in this family reported "0 violations" while walking zero
nodes, from wrong JSON keys; that is the failure this print exists to make visible.) It
also accepts `river_segment_id` as the segment identity on legacy relations — the legacy
hypertable has no `river_segment_key`, and judging it by the narrow column name alone
turned this gate red on the first pass, incorrectly.

```
PASS shj_nj/narrow_compressed:     hit=465 p95=2.028ms fact_nodes=16 index_nodes_judged=10
PASS shj_nj/narrow_uncompressed:   hit=569 p95=3.156ms fact_nodes=8  index_nodes_judged=8
PASS shj_nj/legacy:                hit=300 p95=1.252ms fact_nodes=2  index_nodes_judged=1
PASS small_tailanhe/narrow_compressed:   hit=441 p95=1.873ms fact_nodes=16 index_nodes_judged=10
PASS small_tailanhe/narrow_uncompressed: hit=513 p95=1.696ms fact_nodes=8  index_nodes_judged=8
PASS small_tailanhe/legacy:              hit=324 p95=1.299ms fact_nodes=2  index_nodes_judged=1

SQL GATE: GREEN
```

### The node that was the whole 2026-09-17 failure

`_hyper_9_175_chunk`, the newest narrow chunk and the only one the statistics guard had
never analyzed:

| | 2026-09-17, master `fd3d4869a` | today, live `258b06ec` |
|---|---|---|
| index | `…_river_ts_run_discovery_key_idx` | `175_407_river_timeseries_narrow_pkey` |
| `river_segment_key` | demoted to `Filter` | back in the `Index Cond` |
| `Rows Removed by Filter` | **192 096** | **0** |
| `actual_rows` | 12 | 24 |
| `Shared Hit Blocks` | **2 204** | **27** |
| per-node ratio | **16 008** (limit 10) | **0** |

Every other narrow chunk in both statements is on `…_river_timeseries_narrow_pkey` with
`Rows Removed by Filter: 0`, 19–28 buffers each.

### The compressed leg — segmentby batch pruning, which is what the spec asks for

Four compressed narrow chunks are live today (`_hyper_9_126/150/151/152`, up from one
yesterday — §6). Each answers through its compressed form with **one batch touched and
nothing removed**:

```
Index Scan on compress_hyper_10_174_chunk / _177 / _178 / _179
  using …__compressed_hypertable_10_run_key_r…
  actual_rows: 1   Rows Removed by Filter: 0   Shared Hit Blocks: 4
```

`run_key` then `river_segment_key` in the `Index Cond` — that is the segmentby pruning
the spec names, now measured on four chunks rather than the single one that existed when
the 2026-09-17 receipt was written.

## 2. The API bound — local single-source `forecast-series` warm P95 ≤ 500 ms: **GREEN**

Measured twice with the same script against the same six runs, once under full-cycle
ingest and once quiescent. **Quiescent: GREEN, all six cells, worst 202.7 ms. Contended:
four of six over, legacy included.** The cause is established — contention, not the read
path.

**The bound this verdict is read against was decided and made explicit on 2026-09-18.**
When this receipt was first posted the spec line carried no machine-state qualifier, so
§2 gave both readings and refused to pick. The user took the decision: it is a
**quiescent, read-path bound**, and the text now says so in all three places that carry it
(`specs/timeseries-narrow-store/spec.md`, `design.md`, `tasks.md` 5.2) — autopipe unit
inactive and 1-minute load < 2.0, over at least 30 samples. The decisive reason is in the
contended table below: `shj_nj/legacy`, the **pre-change** read path, misses the same
bound at 623.7 ms. A bound the legacy store also fails cannot discriminate this change,
and read that way it would have blocked the migration before it began. The number was not
moved; a measurement condition of the same kind as `warm` was stated.

The contention itself is not waived — it is **#2486**, re-scoped as a production capacity
item whose guard is the user-facing river-click P95, one tier above this bound. Both
measurements are below, contended first, because that is the order they were taken in and
the contended one is what forced the question.

### The contended measurement

`api0918.py`, `http://127.0.0.1:8080`, two warm-ups then 30 timed requests per cell,
against the same six runs §1 resolved, so the API and SQL legs measure the same work.

**A retracted earlier reading, kept rather than replaced.** The first two passes of this
script used **n = 8** and reported a comfortable green — worst cell 233.4 ms, then 699.3 ms
on the same cell minutes later. At n = 8 the P95 index lands on the **maximum**, so a
single blip *is* the P95 and two passes can disagree by 3×. Neither 8-sample pass is
evidence. The table below is n = 30, where P95 is the 29th value and is a percentile
again, and all 30 samples per cell are in `api-0918.json` so the distribution is
re-readable.

| cell | http | points | min | median | p90 | **P95** | max | samples > 500 ms |
|---|---|---|---|---|---|---|---|---|
| shj_nj / narrow compressed | 200 | 168 | 121.4 | 160.4 | 420.3 | **422.2** | 423.7 | 0 / 30 |
| shj_nj / narrow uncompressed | 200 | 168 | 108.2 | 185.3 | 731.5 | **761.3** | 861.2 | 8 / 30 |
| shj_nj / **legacy** | 200 | 120 | 108.7 | 174.8 | 559.6 | **623.7** | 682.5 | 3 / 30 |
| small / narrow compressed | 200 | 168 | 125.1 | 179.0 | 800.2 | **955.7** | 1251.7 | 4 / 30 |
| small / narrow uncompressed | 200 | 168 | 108.1 | 156.6 | 429.7 | **576.0** | 587.9 | 2 / 30 |
| small / **legacy** | 200 | 132 | 100.6 | 132.6 | 370.9 | **436.5** | 710.1 | 1 / 30 |

**Four of six cells exceed 500 ms at P95, and one of the four is the legacy baseline.**
The bound is not met in this state and this receipt does not claim it is.

Three facts that say what the tail is and is not:

1. **It is not the narrow store.** `shj_nj/legacy` misses the bound at 623.7 ms and
   `small/legacy` has a 710.1 ms sample. Whatever produces the tail produces it on the
   pre-change read path too. Against the **regression** criterion — which is relative to
   legacy, §10 — narrow/legacy is 1.22× on SHJ-NJ and 2.19× on the small network, both
   far inside one order of magnitude.
2. **It is not the fact read.** §1 measures those exact statements at 1.252–3.156 ms warm
   over five `EXPLAIN (ANALYZE, BUFFERS)` rounds each. Three orders of magnitude separate
   the SQL from the tail.
3. **It is not general server slowness** — though see below for what this control does
   *not* cover. `api_tail_control.py` runs the same 30-sample
   shape against two routes that issue no forecast query: `/health` **2.0 / 2.0 / 2.4 /
   2.4 ms** (min/median/P95/max) and `/api/v1/runtime/config` **2.6 / 2.7 / 3.1 / 3.2 ms**.
   Flat, no tail at all.

Every distribution is bimodal — a tight body at 100–220 ms and a separate upper cluster
at 350–1250 ms. Not a uniform slowdown: a fraction of requests falls into a second mode.

### The cause, established by a controlled re-measure

`api-0918.json` was written at **14:40:46**, and `nhms-node27-autopipe.service` had been
`activating` continuously since **14:30:32**, finishing only at **15:11:47** — **41
minutes**, against a normal idle tick of **11 seconds** (`13:58:40 Starting` → `13:58:51
Finished`). That run was not stuck; it was the **whole `2026-09-17 12z` cycle**: 38 GFS +
38 IFS runs through `node27_autopipeline.py --workers 6`, five `output_parser.cli parse`
children at 50–100 % CPU each with five `nhms_ingest_rw … INSERT` backends, load average
4.44 → 6.92. **The entire 30-sample run above sat inside that saturated window.**

The flat `/health` does not rule contention out — `/health` touches no database, no
connection pool and no serialization, so it stays flat under exactly this kind of
contention with `--workers 2`. So the control was re-run properly instead:
`quiet_remeasure.sh` waits for the service to go `inactive` **and** for the 1-minute load
to fall below 2.0, then runs the **same `api0918.py`, unchanged**, against the **same six
runs**. The only variable is the machine state. It measured 15:30:28–15:30:57 at load
**1.84 → 2.33**, with **no tick of either pipeline inside the window** (the next
`nhms-` and `yd-node27-autopipe` both started at 15:31:40, and that nhms tick finished in
11 s — back to the idle shape).

| cell | min | median | p90 | **P95** | max | > 500 ms |
|---|---|---|---|---|---|---|
| shj_nj / narrow compressed | 139.3 | 151.1 | 167.4 | **167.5** | 167.8 | 0 / 30 |
| shj_nj / narrow uncompressed | 125.3 | 158.2 | 180.0 | **183.6** | 202.9 | 0 / 30 |
| shj_nj / **legacy** | 115.7 | 150.0 | 166.6 | **174.2** | 175.3 | 0 / 30 |
| small / narrow compressed | 114.1 | 158.3 | 180.9 | **183.3** | 188.2 | 0 / 30 |
| small / narrow uncompressed | 122.2 | 160.8 | 184.2 | **202.7** | 212.8 | 0 / 30 |
| small / **legacy** | 108.4 | 136.8 | 159.8 | **173.9** | 176.4 | 0 / 30 |

**Quiescent: all six cells PASS, worst P95 202.7 ms against 500 ms — 2.5× margin, and
0 of 180 samples over the bound** (contended: 18 of 180). `api-0918-quiet.json`.

The distribution shape is the evidence, not the P95 alone. Contended it is **bimodal** —
a tight body at 100–220 ms plus a separate cluster at 350–1250 ms. Quiescent it is
**unimodal**, every one of the 180 samples inside 108.4–212.8 ms: the upper cluster does
not shrink, it **disappears**. So that cluster is not a cost of this read path. It is
requests losing CPU and pool to the ingest workers — on legacy exactly as on narrow.

### What the verdict is, stated as two facts rather than one

1. **Quiescent, the bound holds** on all six cells with 2.5× margin.
2. **Under full-cycle ingest, it does not** — four of six cells over, worst 955.7 ms.

Neither state is contrived: the ingest window is ordinary production, and it occupies
roughly **40 minutes of every forecast cycle**, not the "11 seconds every ten minutes"
that an idle tick suggests. The spec line (`specs/timeseries-narrow-store/spec.md`) says
`warm P95 ≤ 500 ms` with **no machine-state qualifier**, so whether this gate reads green
depends on whether the bound is meant to hold while ingest is co-located — and that
reading is what gates I9 (#1988). **This receipt does not pick for you**; it is listed
with the other open decision at the end of §11. The residual work — what exactly the two
processes contend for (CPU, pool, or a sync call blocking the event loop under
`--workers 2`) and how the bound should be written — is **#2486**.

The `issue_time=latest` shape is **not** folded into this verdict and is not hidden: it
is still red at ~3.9 s warm, its cause is `_per_source_latest_cycles` scanning the fact
table, and it is tracked as **#2424**. The 2026-09-17 receipt §7 is its measurement.

## 3. Identity-existence probe, interior-gap miss branch — before and after

The narrow store creates no single-column `valid_time` index
(`000059`; `design.md:57`), so `timeseries-index-hygiene` requires the branch that used
it to be measured, not presumed, with every coverage loss enumerated
(`specs/timeseries-narrow-store/spec.md:176`).

`probe_identity.py` renders the probe with the **real** `mvt.py` functions
(`_hydro_national_identity_source_template` through `render_river_ts_sql`), so the plan
is the plan the `hydro-national` route issues. The miss branch is the interior gap the
spec names: a `valid_time` strictly inside a run's `run_display_coverage` window — so the
discovery sub-select yields candidates and the 424/200 decision really rests on the fact
table — for which no row exists. The series is hourly, so `:30` past the hour is a
genuine interior gap.

| | store | branch | `valid_time` | answer | P95 | buffers |
|---|---|---|---|---|---|---|
| **before** | legacy | hit | `2026-08-21 22:00Z` | 1 | 0.8 ms | 273 |
| **before** | legacy | **miss** | `2026-08-21 22:30Z` | 0 | **3 194.4 ms** | **1 523 851** |
| **after** | narrow | hit | `2026-09-20 22:00Z` | 1 | 0.8 ms | 233 |
| **after** | narrow | **miss** | `2026-09-20 22:30Z` | 0 | **4.6 ms** | **1 656** |

Both branches answer identically before and after (1 and 0), so the 424/200 decision is
unchanged. The miss branch is **694× faster and 920× fewer buffers** on the narrow store.
An earlier pass of the same script, resolving a different newest narrow run, read
3 061.4 ms / 1 523 851 and 10.8 ms / 5 143 — same two orders of magnitude, same verdict;
the table above is the pass archived in `identity-probe-0918.json`.

Why, from the plans:

- **legacy/miss** — `_hyper_3_62_chunk` is a `Custom Scan` (DecompressChunk) with
  `run_key`, `river_network_version_key`, `variable`, `variable_e` and `valid_time` all
  as **filters**: 20 loops, 525 914 rows removed, 1 523 604 buffers. The retained
  single-column `river_timeseries_valid_time_idx` is not reachable on a compressed chunk.
- **narrow/miss** — `_hyper_9_136_chunk` is an **`Index Only Scan`** on
  `…_river_ts_run_discovery_key_idx` with
  `(run_key, river_network_version_key, variable_e, valid_time)` **all four in the
  `Index Cond`**: 14 loops, 0 rows returned, 0 removed, 1 435 buffers.
- The same narrow/miss plan still contains the legacy leg
  (`_hyper_3_113_chunk`), and there the planner *does* pick
  `_hyper_3_113_chunk_river_timeseries_valid_time_idx` with `valid_time` alone in the
  cond and five heap filters — the exact #1596 E4 shape. `actual_loops: 0`: the store
  predicate fences it, so it costs nothing.

**Coverage-loss list — the index that disappears and what it cost:**

| index removed on the narrow store | shape that used it | replacement | measured loss |
|---|---|---|---|
| `river_timeseries_valid_time_idx` (single column `valid_time`) | identity-existence probe, interior-gap miss branch, uncompressed chunks | `river_ts_run_discovery_key_idx`, as an Index **Only** Scan binding 4 of 5 columns | **none** — a 1-column cond with 5 heap filters becomes a 4-column cond with 0 filters, 3 061 ms → 10.8 ms |

No other shape in the repo binds `valid_time` alone against the fact table. The loss list
is therefore one row, and that row is a gain.

## 4. Registry counts

`registry_counts.py` calls the scheduler's own `discover_models`
(`services/orchestrator/scheduler_models.py:36`, evidence keys at `:103-109`) against the
real registry reader, so these are the scheduler's definitions rather than a hand-written
SQL approximation. Read-only: the reader issues `SELECT`s only, no scheduler pass starts,
nothing is submitted, node-22 is untouched.

| counter | value |
|---|---|
| `active_model_count` | **38** |
| `runnable_model_count` | **38** |
| `selected_model_count` | **38** |
| `excluded_model_count` | **0** |

`exclusion_reasons: {}` — no model is inactive, not runnable, not a SHUD model, carrying
incomplete metadata, or a duplicate identity. `selected` is reported under the
**no-operator-filter** configuration (`model_ids=None`, `basin_ids=None`), stated
explicitly because `selected` is the only one of the four that depends on operator
configuration; `operator_filters.expression` is `null` and `excluded_runnable_count` is 0.

These are node-27's registry. The 76 that node-22's scheduler passes carry
(`tests/test_operator_action_listing.py:203-208`) is that plane's count and is out of this
issue's scope by its own "不改 node-22 任何东西".

## 5. Governance receipt — the working-set fields

`governance-20260918T041032Z.json`, produced by the **timer**
(`nhms-node27-resource-governance.timer`, `OnCalendar=*-*-* 04:10:00 UTC`, last run
2026-09-18 12:10:32 CST). Nothing was triggered by hand; `node27_resource_governance.py`
is a read-only audit (`"execution_mode": "read_only_audit"`).

| field | value |
|---|---|
| `uncompressed_bytes` | 1 333 551 579 136 (≈ 1.21 TiB) |
| `daily_ingest_bytes` | 42 342 158 921 (≈ 39.4 GiB/day) |
| `next_compressible_at` | `2026-08-30T00:00:00Z` |
| `watermark` | `2026-09-17T00:00:00Z` |
| `projection_status` | `ok` |
| `projected_peak_bytes` | 1 333 551 579 136 |
| `working_set_free_bytes` | 12 935 214 493 696 (≈ 11.77 TiB) |
| `working_set_filesystem` | `/data/GHDC/nhms-primary/pgdata`, device `9:0:7539273700150526131`, `status: ok`, `blockers: []` |

`projected_peak_bytes` equals `uncompressed_bytes` because `next_compressible_at`
(2026-08-30) is **behind** the display watermark (2026-09-17), so the
`max(0, days(next_compressible_at − watermark))` term is zero — there is no future
ingest to add before the next compression is due; it is already due. Peak 1.21 TiB
against 11.77 TiB free less the 100 GiB default margin: **not critical**, 9.7× of headroom.

The field is the configured PGDATA device, not `/home` — the distinction the spec
insists on (`specs/timeseries-working-set-governance/spec.md:12`, `:60`).

## 6. The compression and retention ticks

**The pair that satisfies this item is the 2026-09-17 one**, archived in
`../2026-09-17-i8-explain-gate/` (`compression-receipt-0917.json`,
`retention-20260917T051532Z.json`) and unchanged. "Both tables" means the two D3 detail
hypertables — `hydro.river_timeseries` and `met.forcing_station_timeseries`
(`packages/common/node27_timeseries_discovery.py:5`,
`scripts/node27_timeseries_retention.py:164-169`) — **not** narrow plus legacy: the
transitional cold tier deliberately does not cover `_legacy`, which is task 5.1's own
note. The 0917 compression receipt's `per_table_totals` names all three
(`hydro.river_timeseries` 4 chunks, `hydro.river_timeseries_legacy` 0,
`met.forcing_station_timeseries` 0); the 0917 retention receipt dropped
`_hyper_9_149/124/125` with `deferred_remainder: []`.

**Today's pair failed, and that is a finding, not a gap in this receipt.** Both lanes are
external systemd timers; TimescaleDB's own job catalog carries only `policy_telemetry`
and `policy_job_error_retention`, so there is no native lane to read.

- **Compression** started 2026-09-18 12:25:32 CST and exited at 13:30:32 with
  `status=124/n/a` — exactly **3 900 s**, the wrapper wall
  (`infra/env/node27-timeseries-compression.example:64`), and `124` is `timeout(1)`'s
  code from `scripts/node27_timeseries_budget_preflight.py:228-231`, not systemd's
  `TimeoutStartSec`. The budget chain did its job. It **did** do work: the narrow table
  went from 1 compressed chunk yesterday to 4 today (`_hyper_9_150/151/152`;
  `_hyper_9_150` was the chunk the 0917 receipt had deferred with
  `per-tick bound reached`). But it was killed before writing its receipt, so
  `/home/nwm/NWM/.nhms-issue1069-live/scheduled-receipt.json` is still the 2026-09-17
  file, `generated_at 2026-09-17T05:08:45Z`.
- **Retention** was invoked at 13:15:00 CST (05:15 UTC) — **inside** that compression run
  — hit the shared lock and refused: `{"mode": "enforce", "outcome": "refused",
  "refusal_reason": "RETENTION_CONCURRENT_INVOCATION"}`, `rc=1`, `OnFailure=` triggered.
  `retention-20260918T051500Z.json` is archived here as the evidence.

**Corrected attribution.** A first draft of this section called the overlap a structural
schedule collision. It is not: `infra/systemd/nhms-node27-timeseries-retention.timer:5`
has said **`06:36:00 UTC`** since `59a26eca6` (2026-08-30), chosen precisely to clear
compression's worst case (`docs/runbooks/tier-node27-timeseries-storage.md:3299`; `05:15`
is marked historical at `:3404-3405`). The committed schedule leaves 66 minutes of slack.
What actually fired at 05:15 UTC is an **installed unit 19 days behind the repo** —
`nhms-node27-timeseries-retention.service:8-9` says in its own header that installation is
a manual step and `git pull` updates only the ExecStart payload. Two separate defects,
both filed as **#2483**: (a) the stale installed timer, sibling of #2285; (b) a
compression tick killed mid-run publishes no receipt at all — `publish_receipt` is called
once after a complete `build_receipt()` (`scripts/node27_timeseries_compression.py:1315`),
the per-chunk loop has no remaining-wall check, and there is no SIGTERM handler, so the
three `except` layers at `:1282-1313` are unreachable on this path. #2349 is the same
symptom on 2026-09-14, closed by incident recovery without the root cause — which is why
this is a recurrence, not a one-off.

No compression and no retention was triggered by hand; the I8b rule against manufacturing
a compressed-state fixture is intact.

## 7. First narrow chunk size after one full cycle

Measured today, read-only, from `timescaledb_information.chunks` joined to
`chunks_detailed_size`:

| chunk | day | compressed | total |
|---|---|---|---|
| `_hyper_9_132_chunk` | 2026-09-16 | no | **21 GB** |
| `_hyper_9_133_chunk` | 2026-09-17 | no | 21 GB |
| `_hyper_9_126_chunk` | 2026-08-26 | **yes** | **3677 MB** |

A full daily narrow chunk is **21 GB uncompressed**; its compressed form is **3677 MB**,
a **3.77×** ratio (the 0917 receipt §4 derives the same ratio from the lane receipt's
`before_bytes`/`after_bytes`, 14 540 062 720 → 3 855 196 160, and the catalog agrees
exactly). The newest chunks are smaller only because they are forward-dated forecast
windows still filling: 09-23 at 2565 MB, 09-22 at 5529 MB, 09-21 at 8770 MB.

Whole tables today: `hydro.river_timeseries` **478 GB** over 29 chunks, 4 compressed;
`hydro.river_timeseries_legacy` **732 GB** over 5 chunks, 3 compressed.

## 8. Display deny-write receipt (C1–C4)

`scripts/validate_readonly_db_boundary.py`, the canonical C2 lane, run twice against the
live cluster with the DSN in `NHMS_DISPLAY_READONLY_DATABASE_URL` — never on argv. Both
runs agree exactly on the boundary:

| | value |
|---|---|
| `role.current_user` / `session_user` | `nhms_display_ro` / `nhms_display_ro` |
| `role.role_type` | `readonly_candidate` |
| `validation_provenance` | `{"mode": "live", "live_readonly_proof": true, "injected_components": []}` |
| `permission_probe_summary` | `operation_count 23`, **`passed_denial_count 23`**, `failed_mutating_count 0`, `target_count 12` |
| `role.mutating_privilege_findings` / `reachable_role_findings` | `[]` / `[]` |

Every mutating probe was **denied by PostgreSQL**, SQLSTATE `42501`, and rolled back
before commit — INSERT/UPDATE/DELETE on `hydro.hydro_run`, `hydro.river_timeseries`,
`met.forecast_cycle`, `met.forcing_station_timeseries`, `ops.pipeline_job`,
`ops.pipeline_event`; `CREATE TABLE` in `hydro`, `met` and `ops`; `CREATE` on database
`nhms`; sequence `USAGE`/`UPDATE` across all six audited sequences. Both control-plane
mutations returned **409 `CONTROL_PLANE_MANUAL_ACTION_REQUIRED`** with
`write_executed: false` and `write_dependency_constructed: false`.

Read routes under the read-only role: `/health`, `/api/v1/runtime/config`,
`/api/v1/models`, `/api/v1/met/stations` all **200 PASS**.

**The validator's overall verdict is `BLOCKED`, and not because a write succeeded.** It is
the route-identity-echo lane. With a real published identity (`source=GFS`,
`cycle_time=2026-09-17T00:00:00+00:00`, `run_id=fcst_gfs_2026091700_dg_0883c7e9c…`), four
routes return **200** and are still blocked — but for **two different reasons**, and the
distinction matters:

| route | blocker | field | expected | observed |
|---|---|---|---|---|
| `latest_product` | `…IDENTITY_MISMATCH` | `cycle_time` | `2026-09-17T00:00:00+00:00` | `2026-09-17T00:00:00Z` |
| `jobs` | `…IDENTITY_MISSING` ×4 | all four | — | `None` |
| `pipeline_status` | `…IDENTITY_MISSING` ×4 | all four | — | `None` |
| `pipeline_stages` | `…IDENTITY_MISSING` ×4 | all four | — | `None` |

**Corrected attribution.** A first draft of this section blamed the `data` envelope and
the `source_id` name. That is wrong: `services/production_closure/readonly_db_route_smoke.py:221-231`
already drills into `data`, and `:244-245` already aliases `source_id` → `source`.
`latest_product` gets all four fields out and fails on a **timezone spelling** —
`packages/common/forecast_store.py:4148-4151` emits `Z`, `readonly_db_route_smoke.py:290`
compares strings exactly. The other three genuinely have no such fields in their response
schema (`apps/api/routes/pipeline.py:311-331`, `:2022-2035`, `:2292-2314`). A
diagnosability defect compounds it: `:246-248` requires all four fields in one mapping or
returns `{}`, which is why a single missing field reports as four, and why the first draft
of this section reasoned from the wrong symptom. Filed as **#2484**.

`job_logs` is `BLOCKED` for a third, structural reason: it needs a `job_id`, and node-27
holds no `ops.pipeline_job` rows for a display identity — jobs are node-22's control
plane. That is **#2420**, already open; note that #2420's stated cause is partly wrong
too, since `jobs` returns rows here and still yields `{}`.

All of these are C3 cross-plane items, not the deny-write boundary this task asks for.

**What this section does and does not evidence, per checklist item.** The verdict row is
labelled C1–C4; read literally that would claim four passes, and it is not four passes:

| item | state here | evidence |
|---|---|---|
| **C1** service reachable under the read-only role | **partial** | the route smoke's read lane: `/health`, `/api/v1/runtime/config`, `/api/v1/models`, `/api/v1/met/stations` all 200 under `nhms_display_ro`. The full C1 checklist (unit state, worker count, restart survival) is not re-run here — it is the 2026-09-17 deploy receipt's. |
| **C2** deny-write boundary | **PASS** | 23/23 mutating probes denied, SQLSTATE `42501`, `failed_mutating_count 0`, twice. This is the item task 5.2 actually asks for. |
| **C3** cross-plane identity echo | **out of scope here, and blocked** | needs node-22's control plane, which this task must not touch. Blocked on #2484 (`cycle_time` spelling + the four-field diagnosability defect) and #2420 (`job_logs` has no `job_id` on node-27). |
| **C4** `/ops` control surface | **partial** | `/ops` 200 (§9) and both control-plane mutations returned 409 `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` with `write_executed: false`. The **browser-level** check that retry/cancel controls are hidden for the read-only role was **not** run. |

So: one PASS (C2, the asked-for one), two partials, one out of scope. The `BLOCKED`
overall verdict comes from C3, not from any write getting through.

Evidence bundles: `artifacts/issue1987-52-c2/issue1987-52-c2-20260918/` and
`…-c2b-20260918/` on node-27 (gitignored; the summary above is the archived form).

## 9. `/` clicks and `/ops`

`/ops` and `/` are reachable on both the local API and the public entry point:

| url | status | time |
|---|---|---|
| `http://127.0.0.1:8080/` | 200 | 5.2 ms |
| `http://127.0.0.1:8080/ops` | 200 | 6.5 ms |
| `https://test.nwm.ac.cn/` | 200 | 103.6 ms |
| `https://test.nwm.ac.cn/ops` | 200 | 111.3 ms |

Networks, resolved by segment count from `core.river_segment` (the spec names "SHJ-NJ,
one medium and one small" and pins no identifiers — the same gap the 0917 receipt §6.1
recorded):

| role | `river_network_version_id` | segments |
|---|---|---|
| large | `basins_shj_nj_rivnet_vbasins` | 32 018 |
| medium | `basins_qhh_rivnet_vbasins` | 3 266 |
| small | `basins_tailanhe_rivnet_vbasins` | 126 |

`clicks.mjs` drives headless Chromium against the **public** entry point
`https://test.nwm.ac.cn` and performs a **real mouse click** on a rendered river feature
of each network, at the feature's own projected geometry. It is not a synthetic request:
the map instance is located through the React fiber tree, `queryRenderedFeatures` picks a
feature out of the `m11-national-river-line` layer, and the click goes to that feature's
screen position through `page.mouse.click`.

| network | segments | clicked segment | click → settled | `forecast-series` calls | statuses | sources | non-2xx |
|---|---|---|---|---|---|---|---|
| large `basins_shj_nj_rivnet_vbasins` | 32 018 | `basins_shj_nj_shud_shud_riv_007727` | 5 704 ms | 2 | 200, 200 | gfs + ifs | none |
| medium `basins_qhh_rivnet_vbasins` | 3 266 | `basins_qhh_shud_shud_riv_000950` | 4 769 ms | 2 | 200, 200 | gfs + ifs | none |
| small `basins_tailanhe_rivnet_vbasins` | 126 | `basins_tailanhe_shud_shud_riv_000001` | 4 570 ms | 2 | 200, 200 | gfs + ifs | none |

**One click loads both curves.** The panel issues two `forecast-series` requests, one per
source, and the receipt records the `run_id` each carried — so identity does not cross
between the two curves:

| network | GFS run | IFS run |
|---|---|---|
| SHJ-NJ | `fcst_gfs_2026091700_dg_6f9ed9377bd5e52240d3ebfc17291e28` | `fcst_ifs_2026091700_dg_cb042bbe7af7a3ad4fd0b2448ad6fb49` |
| QHH | `fcst_gfs_2026091700_dg_0883c7e9c1006c6fd347df500315e9df` | `fcst_ifs_2026091700_dg_9ccb261a39d51c24f4de9173fb4461b6` |
| tailanhe | `fcst_gfs_2026091700_dg_510fbd39433e9cd2ee948980efd523e1` | `fcst_ifs_2026091700_dg_7e247591d8fae09dc8db71482c73efee` |

Each pair is two distinct `model_id`s under one `river_network_version_id`, which is
exactly the sibling-merge hazard `run_id`+`model_id` binding exists to prevent. Zero
non-2xx responses across all three networks, `shots/` has the screenshots: the panel
header names the segment (`河段 ID basins_…_shud_shud_riv_…`), the legend shows both GFS
and IFS series, and `起报 09-17 00:00 UTC` with `GFS + IFS 同步切换`.

**Two measurement mistakes, recorded because the wrong version looked like a red:**

1. The first attempt clicked once per source, expecting one curve per click. The second
   click lands on an already-selected segment and fires nothing, so GFS read 2 calls and
   IFS read 0 — which looks like "IFS is broken" and is not. One click is the correct
   instrumentation.
2. `Escape` does **not** close the curve panel. A left-over panel from the previous
   network covered the next network's click point, so the small network measured zero.
   `shots/` from that run showed the QHH panel still open over the tailanhe map — that
   screenshot is what diagnosed it.

**And one real fact the small network exposed:** `basins_tailanhe_rivnet_vbasins` has 126
segments but its runs' `run_display_coverage.segment_count` is **63**. Half its segments
carry no series, so an arbitrary rendered feature can legitimately open no curve. The
click above targets a segment proven to have rows for that run. This is a property of the
model's output, not of the display path, and it is recorded so the next reader does not
mistake it for a regression.

## 10. The regression criterion

The criterion, from the issue's 验收标准 and `fixtures/I8b-1987-live.md:135`: **any pinned
curve / MVT / display shape that regresses by an order of magnitude against the legacy
baseline, or any Seq Scan on a fact table, blocks the contract (I9) and must be recorded
as a missing access path.**

Recorded for every shape this receipt measures, narrow against its own legacy baseline:

| shape | legacy baseline | narrow | ratio |
|---|---|---|---|
| curve SQL, SHJ-NJ, buffers | 300 | 569 (uncompressed) / 465 (compressed) | 1.9× / 1.6× |
| curve SQL, SHJ-NJ, P95 | 1.252 ms | 3.156 ms / 2.028 ms | 2.5× / 1.6× |
| curve SQL, small, buffers | 324 | 513 / 441 | 1.6× / 1.4× |
| curve SQL, small, P95 | 1.299 ms | 1.696 ms / 1.873 ms | 1.3× / 1.4× |
| API, SHJ-NJ, P95 (n=30) | 623.7 ms | 761.3 ms / 422.2 ms | 1.22× / **0.68×** |
| API, SHJ-NJ, median (n=30) | 174.8 ms | 185.3 ms / 160.4 ms | 1.06× / 0.92× |
| API, small, P95 (n=30) | 436.5 ms | 576.0 ms / 955.7 ms | 1.32× / 2.19× |
| API, small, median (n=30) | 132.6 ms | 156.6 ms / 179.0 ms | 1.18× / 1.35× |
| API, SHJ-NJ, P95 (n=30, **quiescent**) | 174.2 ms | 183.6 ms / 167.5 ms | 1.05× / 0.96× |
| API, small, P95 (n=30, **quiescent**) | 173.9 ms | 202.7 ms / 183.3 ms | **1.17×** / 1.05× |
| identity probe, miss branch | 3 194.4 ms | 4.6 ms | **0.0014×** |

Worst ratio **2.19×**, well inside one order of magnitude, and the narrow curve legs
return **168 points against legacy's 120–132** — they are not the same amount of work, so
even that overstates the regression. **Fact-table Seq Scans: zero.** `gate0918.py` judged
58 fact plan nodes across the six cells and every one that touches a fact relation is an
Index Scan, Index Only Scan, or a Custom Scan over a compressed chunk whose child is an
index scan. **Criterion not tripped; it does not block I9.**

Note the separation this table makes explicit and §2 depends on: under ingest the API
**absolute** bound is missed on four cells, but the API **relative** ratio never exceeds
2.19× because the legacy baseline carries the same tail — and quiescent, where the tail is
gone, the worst API ratio falls to **1.17×**. The regression criterion is the relative one
and it is not tripped in either state. The absolute 500 ms bound is a separate spec bound
whose reading depends on the machine state; §2 gives both numbers and does not pick.

## 11. Per-statement expand wall time — not obtainable, stated rather than faked

Task 5.2 asks for the per-statement wall time of the expand migration. **It does not exist
for the live production application of `000059` and cannot be produced now.**

- The only per-statement timing in the repo for
  `000059_river_timeseries_narrow_expand.sql` is
  `../2026-09-12-i8-rollback/receipt.json:868-873` — `"expand_runner"`,
  `"single_statement_wall_seconds": 0.026955`. That is the **R1–R5 rollback rehearsal on a
  disposable node-27 database** (task 5.1's evidence), not production.
- The production reforward receipt
  (`../2026-09-15-i8-reforward/production-reforward-receipt.json`) shows
  `"Skipped migration: … 000059_river_timeseries_narrow_expand.sql"` and
  `"Migrations complete: 0 applied, 53 skipped, 53 total."` — `000059` was already applied
  before that window, so it recorded no fresh statement timings either. It records the
  window's overall wall clock instead: T0 02:28:31Z, drain 02:28:32Z, display validated
  02:29:22Z, `WINDOW_VALIDATED` 02:29:30Z.

Re-running `000059` to manufacture the number is forbidden (no implicit rerun) and would
be meaningless against an already-migrated table.

**WAIVED by the user, 2026-09-18.** The window-level wall clock stands in its place: T0
02:28:31Z, drain 02:28:32Z, display validated 02:29:22Z, `WINDOW_VALIDATED` 02:29:30Z.
The item is recorded here as waived rather than deleted, so that the gap stays a stated
fact and no number was invented to fill it.

## The two decisions this receipt handed back, and how they were taken

Both were put to the user on 2026-09-18 and both were decided; 5.2 is checked on that
basis. Neither was a measurement question.

1. **The per-statement expand wall time (§11) — WAIVED.** The window-level wall clock
   stands in its place. No number was fabricated.
2. **What machine state the API bound is written against (§2) — QUIESCENT.** It is a
   read-path bound, not a capacity bound, and the spec text now says so with a
   reproducible condition (autopipe unit inactive, 1-min load < 2.0, ≥ 30 samples). The
   decisive reason: under ingest the legacy path misses the same bound, so the measurement
   stops discriminating this change. The SQL bounds and the relative regression criterion
   are green in **both** states regardless.

**Carried forward rather than dropped:** #2486 stays open as a production capacity item
(≈40 min per cycle at up to 955 ms P95 is user-visible), and the `@live-river-click`
oracle — which is now the explicit home of the user-facing guarantee — has produced **no
receipt against node-27** and is on I9's pre-GO checklist for that reason.

## Files

| file | what it is |
|---|---|
| `probe0918.py`, `explain-0918.json` | §1, the six-cell EXPLAIN probe and its raw plans |
| `gate0918.py` | §1, the per-node judge (prints the node count it judged) |
| `api0918.py`, `api-0918.json` | §2, the local API warm P95, 30 samples per cell — the **contended** run (14:40, inside the 41-minute cycle ingest) |
| `quiet_remeasure.sh`, `api-0918-quiet.json` | §2, the **quiescent** re-run of the same script: waits for the ingest unit to go inactive *and* 1-min load < 2.0, then measures. It waits; it never starts or stops anything. |
| `api_tail_control.py`, `api-tail-control-0918.json` | §2's control — `/health` and `/api/v1/runtime/config`, same shape, no tail |
| `probe_identity.py`, `identity-probe-0918.json` | §3, the existence-probe before/after |
| `registry_counts.py`, `registry-counts-0918.json` | §4 |
| `governance-20260918T041032Z.json` | §5, the timer's own read-only audit |
| `retention-20260918T051500Z.json` | §6, today's refused retention tick |
| `clicks-0918.json`, `clicks.mjs`, `shots/` | §9 |
