# I8 task 5.2 — curve EXPLAIN gate, partial receipt (2026-09-17)

**Status: the gate does NOT pass. No configuration measured today clears all three
bounds on the largest network. This receipt records why, with the numbers.**

Covers these task 5.2 items and no others:

- curve EXPLAIN gate for SHJ-NJ and a small network on narrow uncompressed,
  narrow compressed and legacy — **measured, verdict red, see §3**
- one compression tick and one retention tick covering both tables — **captured, §5**
- first narrow chunk size / compression ratio — **partial, §4**

Everything else in task 5.2 (per-statement expand wall time, identity-existence
probe miss branch, registry counts, governance receipt with working-set fields,
`/` click screenshots, display deny-write C1–C4, `/ops`) is **not** in this
receipt and #1987 stays open.

## 1. What was measured, and against which code

All read-only: role `nhms_display_ro` from `infra/env/display.env`, every statement
inside a `READ ONLY` transaction. No `systemctl start`, no `compress_chunk`, no
`drop_chunks`, no `ANALYZE`, no `reset-failed`. DSN only via env, never argv.

`probe1987.py` drives the **real `PsycopgForecastStore`** of a given checkout through a
recording pass-through cursor, then `EXPLAIN (ANALYZE, BUFFERS)` each fact-reading
statement **five times warm**. Every number below is what `forecast_series` itself
issues, not a hand-rebuilt statement.

Three trees are in play and the receipt distinguishes them throughout:

| tree | commit | what it is |
|---|---|---|
| `/home/nwm/tmp/2417-wt` | `fd3d4869a` | **master** — the merged #2417 pushdown |
| `/home/nwm/tmp/2417-base` | `b40d0015a` | the merge-base, pre-pushdown |
| `/home/nwm/NWM` (live) | `7ecc46be` | **what node-27 actually serves today** — older than both |

**The live display API is running neither tree measured here.** `7ecc46be` predates
`b40d0015a`, so the `:8080` service serves pre-pushdown code. The master column below
is what production will do **after the next `git pull --ff-only` in the live tree**,
not what it does now.

Pinned shape (the run-bound `:758` shape, deliberately **not** `issue_time=latest` —
see §6):

```
forecast_series(basin_version_id=…, segment_id=…, river_network_version_id=…,
                variables=['q_down'], scenarios=['GFS'], run_types=['forecast'],
                include_analysis=False, issue_time=<cycle>, run_id=…, model_id=…)
```

Networks: the spec names only "SHJ-NJ" and "a small network" and pins no concrete
identifier, so they were resolved from `core.river_segment` by segment count:

| role | `river_network_version_id` | segments |
|---|---|---|
| SHJ-NJ (largest with live runs) | `basins_shj_nj_rivnet_vbasins` | 32 018 |
| small | `basins_tailanhe_rivnet_vbasins` | 126 |

(`basins_hhe_rivnet_vbasins` is larger at 87 598 segments but its newest run ended
2026-07-28 and it is outside the retention window, so it cannot be measured.)

## 2. Headline numbers

Warm `shared hit` / warm P95 over five samples, per storage state:

| case | base `b40d0015a` | master `fd3d4869a` |
|---|---|---|
| shj_nj / narrow **compressed** | 362 451 · 1621.9 ms | **549 · 2.342 ms** |
| shj_nj / narrow uncompressed | 14 875 · 31.513 ms | **2 749 · 43.335 ms** |
| shj_nj / legacy | 132 328 · 588.9 ms | **300 · 1.246 ms** |
| small / narrow **compressed** | 362 470 · 1526.8 ms | **498 · 1.835 ms** |
| small / narrow uncompressed | 14 708 · 26.634 ms | **517 · 2.108 ms** |
| small / legacy | 132 638 · 563.8 ms | **324 · 1.265 ms** |

**Row identity holds on all six shapes**: the probe digest
(`sha256("\n".join(repr(sorted(row.items()))))[:16]`) is byte-identical between the
two trees for every case —
`b6e858bc2cb73818`, `3e5b320d680975fc`, `0a5aea3665695eae`,
`b1ab8bc8b921ac23`, `062d2e994eb5c48f`, `0b6e670db0abad25`.
That is six more independent corroborations of #2417's must-preserve claim.

## 3. Gate verdict: RED on SHJ-NJ narrow-uncompressed, on **both** trees

The spec bound is three-part (`specs/timeseries-narrow-store/spec.md`):
`river_segment_key` in an `Index Cond`, `Rows Removed by Filter / rows returned ≤ 10`,
`shared hit ≤ 5000`, plus SQL warm P95 ≤ 300 ms.

| tree | shared hit ≤ 5000 | ratio ≤ 10 | `river_segment_key` in Index Cond |
|---|---|---|---|
| master `fd3d4869a` | 2 749 ✓ | **1143.69 ✗** | **✗** (one chunk) |
| base `b40d0015a` | **14 875 ✗** | 0.524 ✓ | ✓ |

**No tree passes all three.** `2749 < 5000` on master must not be read as a pass.

The whole failure is one plan node — `_hyper_9_175_chunk`, the newest narrow chunk
(range 2026-09-23 → 2026-09-24):

| | base `b40d0015a` | master `fd3d4869a` |
|---|---|---|
| index | `…_river_ts_segment_time_key_idx` | `…_river_ts_run_discovery_key_idx` |
| `river_segment_key` | first column of `Index Cond` | demoted to `Filter` |
| `Rows Removed by Filter` | 0 | **192 096** |
| `actual_rows` | 24 | 12 |
| `Shared Hit Blocks` | 27 | **2 204** (81×) |

`192 096 = 32 018 segments × 6 hourly steps` — the node now reads every segment of
the run for that chunk's slice. Every **other** narrow chunk in the same plan
(`_hyper_9_132/133/134/135/136/142/170`) correctly uses
`NNN_river_timeseries_narrow_pkey` with `(run_key, river_segment_key, variable_e,
valid_time)` in the `Index Cond` and `Rows Removed by Filter: 0`.

Mechanism: #2417 pushed `run_key` into the fact scan, which made
`…_river_ts_run_discovery_key_idx` **newly matchable** for this shape. On a chunk with
no statistics the planner mis-costs it and takes it.

Correlation, **not proven**: `_hyper_9_175_chunk` is the only one of the three newest
chunks that has never been analyzed.

```
      relname       | n_live_tup | n_mod_since_analyze |         last_analyze          | last_autoanalyze
 _hyper_9_142_chunk |   48611281 |                   0 | 2026-09-17 07:04:57.588902+00 | 2026-09-15 06:10:51+00
 _hyper_9_170_chunk |   27006197 |            10802448 | 2026-09-17 00:16:11.701765+00 | (null)
 _hyper_9_175_chunk |    5401224 |             5401224 | (null)                        | (null)
```

All three carry all three indexes, so it is not a missing index. `ANALYZE` was **not**
run (production, read-only role), so the causal link is unverified here. It is filed
separately; the proof belongs on a throwaway database, not on the live primary.

### The narrow-**compressed** leg, by contrast, passes cleanly

This is the leg the spec calls "segmentby batch pruning on narrow compressed chunks",
and it is the one that was unobtainable until today (see §5). On master:

```
Index Scan on compress_hyper_10_174_chunk
  using compress_hyper_10_174_chunk__compressed_hypertable_10_run_key_r…
  Index Cond: ((run_key = $7) AND (river_segment_key = $5))
  Filter: ((_ts_meta_max_2 >= '2026-08-26 12:00:00+00') AND (_ts_meta_min_2 <= '2026-09-02 12:00:00+00'))
  Rows Removed by Filter: 0    actual_rows: 1
```

`run_key` first, `river_segment_key` second, one compressed batch touched, nothing
removed. That is exactly the segmentby pruning the gate asks for.

Read the 549 honestly: it is the **statement** total for a mixed read — the compressed
chunk `_hyper_9_126` plus uncompressed `_hyper_9_150…156` plus two legacy chunks that
return 0 rows. The compressed-chunk evidence is the plan node above, not the 549.

## 4. Compression ratio on the one real compressed narrow chunk

| | value |
|---|---|
| `_hyper_9_126_chunk` uncompressed remnant | 24 kB |
| `compress_hyper_10_174_chunk` (its compressed form) | 3677 MB |
| compressed batches | 4 219 397 |
| compression receipt `before_bytes` → `after_bytes` | 14 540 062 720 → 3 855 196 160 |

3 855 196 160 bytes = 3677 MB — the catalog and the lane receipt agree exactly.
**Ratio 3.77×** (14.54 GB → 3.86 GB) on a real production narrow chunk.

Task 5.2's "first narrow chunk size after one full cycle" is only partly answered: a
full daily narrow chunk is ~21 GB / ~48.6M rows uncompressed (`_hyper_9_142_chunk`);
`_hyper_9_126_chunk` is a full day and compressed to 3.86 GB. Narrow table total
**504 GB** across 29 chunks, 1 of them compressed.

## 5. The compression / retention tick pair, and why this window existed at all

`fixtures/I8b-1987-live.md` forbids forcing production compression to manufacture a
compressed-state receipt. No compression was triggered. The compressed chunk measured
here is the natural output of the scheduled 04:25Z lane, and it survived the 05:15Z
retention lane **for the first time** — the narrow table was 0/30 compressed on
2026-09-16 and is 1/29 today.

Both ticks are archived here and cover both tables:

- `compression-receipt-0917.json` — `generated_at 2026-09-17T05:08:45Z`, `mode enforce`,
  `outcome clean`, selected `_hyper_9_149/124/125/126`, 46 837 194 752 → 12 318 957 568
  bytes, deferred 17, skipped 14.
- `retention-20260917T051532Z.json` — `cutoff 2026-08-26T00:00:00Z`,
  `reference_time 2026-09-16T00:00:00Z`, `window_days 21`, dropped
  `_hyper_9_149/124/125`, `deferred_remainder []`.

Three of the four chunks compressed at 04:25Z were dropped at 05:15Z the same day.
That is #2425; today's data also **refutes** two of that issue's stated facts (both
lanes share one business-time watermark; retention does not use wall clock), recorded
in its comment thread, not here.

`_hyper_9_126_chunk` is the oldest narrow chunk and therefore the next retention
target: on the same arithmetic it is dropped as soon as the watermark reaches
2026-09-17T00:00:00Z. **The compressed-state evidence in this receipt is not
reproducible on demand** — it was taken in the window it existed.

## 6. Two spec gaps this receipt had to work around

1. **No concrete network identifiers.** The spec and both fixtures name "SHJ-NJ", "one
   medium" and "one small" network and pin no `river_network_version_id`. Resolved by
   segment count as recorded in §1. If the intended pins differ, the gate must be
   re-measured.
2. **The `forecast-series` warm P95 ≤ 500 ms bound names no request shape.** The spec
   says only "the node-27 local single-source `forecast-series` warm P95 ≤ 500 ms".
   The production default is `issue_time=latest` (`apps/api/routes/forecast.py:47`),
   which costs 650 589 shared blocks end to end because of #2424 and would fail. This
   receipt measures the **run-bound** shape, which is what the D11 gate measures, and
   records the ambiguity rather than resolving it. The API-level P95 is **not** in this
   receipt; it cannot be measured against master until the live tree is updated (§1).

## Files

| file | what it is |
|---|---|
| `probe1987.py` | the probe (real store + recording cursor, 5 warm EXPLAIN rounds) |
| `gate.py` | offline gate evaluation over a bundle |
| `explain-1987.json` | master `fd3d4869a` bundle, six cases, full plan nodes |
| `explain-1987-base.json` | base `b40d0015a` bundle, same six cases |
| `run1987.out` / `run1987base.out` | the probes' console output |
| `compression-receipt-0917.json` | the 04:25Z compression tick |
| `retention-20260917T051532Z.json` | the 05:15Z retention tick |
