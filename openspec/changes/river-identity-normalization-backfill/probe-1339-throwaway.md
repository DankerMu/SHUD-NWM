# Probe log: tasks 1.0(a)-(d) throwaway experiments (issue #1339)

Fixture-adjacent evidence file for `tasks.md` 1.0. **Verdict: 1.0(a) and 1.0(b)
CONFIRM the fixture; 1.0(c) and 1.0(d) REFUTE three load-bearing D4
mechanisms.** Implementation of tasks 1.1-1.7 was stopped after this file was
written, per design D2's "任一结果推翻假设 → 回炉 fixture 而非硬写".

## Environment

- Host: node-27 (`210.77.77.27:32099`, user `nwm`), container `nhms-db`.
- `PostgreSQL 15.2 (Ubuntu 15.2-1.pgdg22.04+1)`, `timescaledb 2.10.2`,
  `postgis 3.3.2`, `timescaledb_toolkit 1.16.0`.
- Throwaway database `nhms_1339_probe` (`CREATE DATABASE` from `postgres`;
  `timescaledb` arrived pre-installed from the instance template, `postgis` and
  `pg_trgm` created explicitly). Peak size 1559 MB. Dropped at the end of the
  session; every experiment script is reproduced verbatim below so the database
  can be rebuilt in ~6 minutes.
- The live `nhms` database was touched with **read-only catalog / `pg_stats` /
  size-function queries only**. No DDL, no DML.

### Live read-only baseline captured for the same session (2026-08-15)

| Measurement | Value |
|---|---|
| `approximate_row_count('hydro.river_timeseries')` | 459,914,080 |
| `hypertable_detailed_size` table / index / toast / total | 112 GB / 137 GB / 1696 kB / **249 GB** |
| Chunks | 6 (`_hyper_3_63/32/51/55/58/62`), 2 compressed (`_32`, `_51`) |
| Per-chunk total size | 63: 9939 MB · 55: 136 GB · 58: 91 GB · 62: 2005 MB · 32/51: 40 kB (origin truncated) |
| `chunk_compression_stats` ratio | `_hyper_3_32` 268 GB → 6096 MB = **45.09x**; `_hyper_3_51` 215 GB → 4924 MB = **44.63x** |
| `core.river_segment` rows / total / heap+toast / index | 209,126 / 355 MB / 278 MB / 77 MB |
| `hydro.hydro_run` rows | 3,609 |
| `core.basin_version` / `core.river_network_version` rows | 20 / 20 |
| `df -h /home` | 1.7T size, 999G used, **576G avail**, 64% |

Live distinct value sets for the three enum candidates, read from
`pg_stats.most_common_vals` over all six `hydro.river_timeseries` chunks
(`n_distinct` was 1, 1, 2 respectively on every chunk, so the MCV list is the
complete value set):

- `variable` → `{q_down}`
- `unit` → `{m3/s}`
- `quality_flag` → `{ok, qc_warning}`

Two fixture inputs have drifted upward since the fixture was written and the
migration header / ADR amendment must use the measured numbers, not the fixture
text: `core.river_segment` is **209,126** rows (fixture said "~9 万"),
`hydro.hydro_run` is **3,609** rows (fixture said "~704"), and the fact table is
**459.9M rows / 249 GB** (fixture said 449M / 264 GB — rows up, bytes down
because `_hyper_3_51` was compressed between the two captures).

---

## 1.0(a) — nullable no-default ADD COLUMN on a hypertable with a compressed chunk

**Verdict: CONFIRMED**, with one correction to the *rationale* recorded in
design D2 (the deliverable is unaffected).

Object: `probe.fact_a`, the `hydro.river_timeseries` column shape with its text
primary key, `chunk_time_interval => 7 days`, **1,008,000 rows spanning three
chunk intervals** (`2026-01-01`, `2026-01-08`, `2026-01-15`), compression
enabled with the production text settings, and the oldest chunk
(`_hyper_1_1_chunk`) compressed via `compress_chunk`.

| Statement | Time |
|---|---|
| `ALTER TABLE probe.fact_a ADD COLUMN run_key INTEGER` | 0.939 ms |
| `... ADD COLUMN river_network_version_key INTEGER` | 0.815 ms |
| `... ADD COLUMN basin_version_key INTEGER` | 0.808 ms |
| `... ADD COLUMN river_segment_key INTEGER` | 0.896 ms |
| `... ADD COLUMN variable_e probe.river_variable` | 1.103 ms |
| `... ADD COLUMN unit_e probe.river_unit` | 1.050 ms |
| `... ADD COLUMN quality_flag_e probe.river_quality_flag` | 1.068 ms |

`pg_attribute` for all 7 columns, on the parent **and on all three chunks
including the compressed one** — 28 rows, every one of them:

```
atthasmissing = f, atthasdef = f
```

### Correction to design D2's stated reason for omitting the default

D2 says a defaulted `quality_flag_e` "带默认值加列在含压缩 chunk 的 2.10 上非
metadata-only". **That is false as measured.** The control statement on the same
compressed-chunk hypertable:

```
ALTER TABLE probe.fact_a ADD COLUMN control_defaulted TEXT NOT NULL DEFAULT 'ok';
ALTER TABLE
Time: 1.532 ms
```

```
         relname          |      attname      | atthasmissing | atthasdef
--------------------------+-------------------+---------------+-----------
 _compressed_hypertable_2 | control_defaulted | f             | f
 _hyper_1_1_chunk         | control_defaulted | t             | t
 _hyper_1_2_chunk         | control_defaulted | t             | t
 _hyper_1_3_chunk         | control_defaulted | t             | t
 compress_hyper_2_4_chunk | control_defaulted | f             | f
 fact_a                   | control_defaulted | t             | t
```

The defaulted add is also metadata-only (PG 11+ fast default, 1.532 ms, no
rewrite). The real, measurable difference is only `atthasmissing`: it is `t` for
the defaulted column and `f` for the seven no-default columns. So the
`atthasmissing = false` pin required by tasks 2.2 / the spec is still a valid
mechanical pin **of the no-default choice**, but the migration comment must not
claim the defaulted variant would rewrite the table.

---

## 1.0(b) — IDENTITY surrogate-key add on the authority tables

**Verdict: CONFIRMED** (rewrite + AEL as predicted; cost is seconds, not
minutes).

`probe.river_segment` reproduces the live `core.river_segment` shape as of
2026-08-15 (000004 + 000037 MultiLineString + 000048 STORED generated
`stream_type`) with **209,126 rows** and the complete live index set: pkey,
`gist(geom)`, three `gin(... gin_trgm_ops)`, and two btrees.

| Object | Rows | Total size | `ADD COLUMN <k> INTEGER GENERATED ALWAYS AS IDENTITY UNIQUE` |
|---|---|---|---|
| `probe.river_segment` (3-point lines) | 209,126 | 106 MB | **5347.776 ms** |
| `probe.river_segment_big` (30-point lines) | 209,126 | 228 MB | **6617.702 ms** |
| `probe.hydro_run` | 3,609 | — | **61.121 ms** |
| `probe.basin_version` | 20 | — | **46.270 ms** |
| `probe.river_network_version` | 20 | — | **46.746 ms** |

Live `core.river_segment` is 355 MB total, i.e. 1.56x the 228 MB replica, so the
production `ADD COLUMN ... IDENTITY UNIQUE` should be budgeted at **~10 s of
ACCESS EXCLUSIVE** (the two measured points are 5.35 s @ 106 MB and 6.62 s @
228 MB — the cost is dominated by rebuilding the seven indexes, not by row
count). The three other authority tables are sub-100 ms.

Post-conditions on the rewritten column:

```
      attname      | attnotnull | atthasmissing | attidentity
-------------------+------------+---------------+-------------
 river_segment_key | t          | f             | a
```

`count = 209126, min = 1, max = 209126, count(DISTINCT) = 209126` — dense and
unique, IDENTITY implies NOT NULL as D2 assumed.

**Idempotency:** a second
`ALTER TABLE probe.basin_version ADD COLUMN IF NOT EXISTS basin_version_key INTEGER GENERATED ALWAYS AS IDENTITY UNIQUE;`
emits `NOTICE: column "basin_version_key" of relation "basin_version" already
exists, skipping` and returns in **0.563 ms** — no rewrite on replay.

**`lock_timeout` guidance, measured.** With a concurrent session holding
ACCESS SHARE on the target (`BEGIN; SELECT count(*) ...; SELECT pg_sleep(30);`):

```
SET lock_timeout = '2s';
ALTER TABLE probe.river_segment_big ADD COLUMN lock_probe_key INTEGER GENERATED ALWAYS AS IDENTITY UNIQUE;
ERROR:  canceling statement due to lock timeout
Time: 2001.546 ms (00:02.002)
```

`lock_timeout` bounds only the *wait for the lock*, not the rewrite that follows
— so the migration should pair a short `lock_timeout` (fail fast rather than
queue in front of MVT readers) with a low-peak window sized for the ~10 s hold.

---

## 1.0(c) — does TSDB 2.10.2 require FK columns in segmentby ∪ orderby?

**Verdict: REFUTED. Yes, it does. Design D4's "既有文本 FK 保留" is
incompatible with the integer segmentby.**

Object: a hypertable carrying the exact 000006:57-58 two-column text FK to the
`river_segment` replica, all seven normalized columns present and filled, and
**no** primary key (so nothing but the FK can be responsible for the outcome):

```sql
ALTER TABLE probe.fact_c SET (
  timescaledb.compress = true,
  timescaledb.compress_segmentby = 'run_key, river_network_version_key, river_segment_key',
  timescaledb.compress_orderby = 'variable_e, valid_time'
);
```

```
ERROR:  column "river_segment_id" must be used for segmenting
DETAIL:  The foreign key constraint "fact_c_river_segment_id_river_network_version_id_fkey"
         cannot be enforced with the given compression configuration.
```

`timescaledb_information.compression_settings` stayed at 0 rows — nothing was
applied. Corroborating live evidence that the rule is real and currently
satisfied by accident: `\d core.river_segment` on live `nhms` shows the FK
replicated onto `_timescaledb_internal.compress_hyper_7_53_chunk` as well as the
uncompressed chunks, i.e. the FK is enforced against the compressed relation,
which is exactly why its columns must be segmentby columns. Today's production
segmentby (`run_id, river_network_version_id, river_segment_id`) happens to be a
superset of the FK columns; the target segmentby is not.

Design D4 anticipated this branch ("若被纳入校验，cutover 增'drop FK'步骤并
回炉本节") — the branch has fired.

Two remedies were probed for the fixture-repair decision (neither is
implemented here):

- **Remedy A — drop the text FK at cutover.** Works (see 1.0(d) step e.6), but
  it retires the FK ahead of the text columns it belongs to, which D4 explicitly
  routed to the column-retirement issue.
- **Remedy B — keep the FK and add its two text columns to segmentby**
  (`run_key, river_network_version_key, river_segment_key, river_segment_id,
  river_network_version_id`). Not measured to completion: the probe run that
  attempted it was blocked earlier in the chain by the compression-enabled DDL
  lock described in 1.0(d), so its viability is **unknown** and would need one
  more experiment. Note it would keep the two widest text columns in the
  compressed representation, working against the change's stated purpose,
  though as segmentby columns they are stored once per segment rather than once
  per row.

---

## 1.0(d) — PK USING INDEX, CHECK NOT VALID/VALIDATE, transaction_per_chunk

Object: `probe.fact_d`, production column shape + text pkey + text FK,
`chunk_time_interval => 7 days`, **3,024,000 rows across three chunks**, one
chunk compressed then decompressed.

### (d-1) `ADD CONSTRAINT ... PRIMARY KEY USING INDEX` — REFUTED

```sql
ALTER TABLE probe.fact_d DROP CONSTRAINT fact_d_pkey;                   -- 127.848 ms, OK
ALTER TABLE probe.fact_d ADD CONSTRAINT fact_d_pkey PRIMARY KEY USING INDEX fact_d_target_pkey_idx;
```

```
NOTICE:  ALTER TABLE / ADD CONSTRAINT USING INDEX will rename index "fact_d_target_pkey_idx" to "fact_d_pkey"
ERROR:  hypertables do not support adding a constraint using an existing index
```

This is the central AEL-minimisation mechanism of design D4.3 ("drop 旧 pkey →
`ADD CONSTRAINT ... PRIMARY KEY USING INDEX`"). It does not exist on TimescaleDB
2.10.2. A pre-built unique index cannot be adopted as the primary key; the pkey
must be built by `ADD CONSTRAINT ... PRIMARY KEY (cols)`, which builds its own
index inside the ACCESS EXCLUSIVE window. On the live 459.9M-row table that puts
the full index build (plain `CREATE UNIQUE INDEX` on the target shape measured
2.969 s for 3.024M rows, so order-of-hours at 459.9M) *inside* the cutover
window rather than outside it.

The pre-built index is not merely unusable — it is dead weight: after the
failure, `pg_index` had **zero** primary-key indexes on `fact_d` or any of its
chunks.

### (d-2) `timescaledb.compress = true` blocks nearly all cutover DDL — REFUTED

This was not anticipated by the fixture at all. With the setting in force —
**even with zero compressed chunks** (all had been decompressed first;
`compression_enabled = t`) — TimescaleDB 2.10.2 rejects:

| Statement | Result |
|---|---|
| `ALTER TABLE ... ADD CONSTRAINT ... CHECK (col IS NOT NULL) NOT VALID` | `ERROR: operation not supported on hypertables that have compression enabled` |
| `ALTER TABLE ... VALIDATE CONSTRAINT ...` | same error |
| `ALTER TABLE ... ALTER COLUMN ... SET NOT NULL` | same error |
| `CREATE UNIQUE INDEX ...` | same error |
| `CREATE UNIQUE INDEX ... WITH (timescaledb.transaction_per_chunk)` | same error |
| `CREATE UNIQUE INDEX CONCURRENTLY ...` | same error |
| `ALTER TABLE ... DROP CONSTRAINT <fkey>` | same error |
| `ALTER TABLE ... DROP CONSTRAINT <pkey>` | same error |

Not blocked (isolated control run on `probe.fact_a`, compression enabled **and**
a compressed chunk present):

| Statement | Result |
|---|---|
| `ALTER TABLE ... ADD COLUMN` (nullable, and with a constant default) | allowed, ~1 ms — see 1.0(a) |
| plain non-`UNIQUE` `CREATE INDEX ...` | allowed, 239.495 ms / 401.265 ms |
| `CREATE UNIQUE INDEX` on the same shape | `ERROR: operation not supported on hypertables that have compression enabled` |

So the discriminator is `UNIQUE`, not `CREATE INDEX` as such — consistent with
000049's record that plain `CREATE INDEX` is the available rollback path on the
live hypertable.

The unlock is `ALTER TABLE ... SET (timescaledb.compress = false)`, which
requires zero compressed chunks and took **42.502 ms** on the toy. After it, the
`DROP CONSTRAINT <fkey>` that had just failed succeeded in **2.347 ms**.

**Consequence for design D4.2.** The fixture's shape — run `verify`, then build
the unique index and the `CHECK NOT VALID` / `VALIDATE` pair *outside* the
window while the table stays compressed and only a minutes-scale `cutover_*`
holds ACCESS EXCLUSIVE — is not executable. Every prepare step requires
compression to be **disabled first**, and disabling compression requires
**decompressing the entire hypertable** (live: `_hyper_3_32` 268 GB and
`_hyper_3_51` 215 GB pre-compression, against 576G free on `/home`). The
compression lane is therefore off for the whole prepare+cutover span, not just
the window, and the disk-headroom precheck that D6 assigns to the backfill
applies to the cutover too — at a much larger magnitude.

### (d-3) `transaction_per_chunk` as a lighter unique-index variant — REFUTED

With compression disabled:

```
CREATE UNIQUE INDEX fact_d_tpc_idx ON probe.fact_d (...) WITH (timescaledb.transaction_per_chunk);
ERROR:  cannot use timescaledb.transaction_per_chunk with UNIQUE or PRIMARY KEY
```

Not available for unique indexes. There is no lighter variant.

### (d-4) CIC — re-confirmed rejected (not retried against a fresh assumption)

```
CREATE UNIQUE INDEX CONCURRENTLY fact_d_cic_idx ON probe.fact_d (...);
ERROR:  hypertables do not support concurrent index creation
```

Matches 000049:37-40 verbatim.

### (d-5) CHECK NOT VALID → VALIDATE → SET NOT NULL — CONFIRMED (scan-free)

All with compression disabled, on 3,024,000 rows:

| Stage | Time |
|---|---|
| `ADD CONSTRAINT ... CHECK (col IS NOT NULL) NOT VALID` ×7 | 0.778 - 1.443 ms each |
| `VALIDATE CONSTRAINT` ×7 | 464.742 - 611.898 ms each |
| `SET NOT NULL` ×7 (after the validated CHECK) | **0.591 - 0.788 ms each** |
| CONTROL: `SET NOT NULL` on `value`, no validated CHECK | **1161.166 ms** |

A ~1500x gap on 3M rows — the fixture's "借 validated CHECK 免扫" claim is
solidly confirmed. Propagation to chunks is automatic in both stages:

```
      relname      |         conname         | convalidated
-------------------+-------------------------+--------------   after NOT VALID:
 _hyper_6_11_chunk | fact_d_run_key_not_null | f
 _hyper_6_12_chunk | fact_d_run_key_not_null | f
 _hyper_6_13_chunk | fact_d_run_key_not_null | f
 fact_d            | fact_d_run_key_not_null | f
                                                              after VALIDATE:
 _hyper_6_11_chunk | fact_d_run_key_not_null | t
 _hyper_6_12_chunk | fact_d_run_key_not_null | t
 _hyper_6_13_chunk | fact_d_run_key_not_null | t
 fact_d            | fact_d_run_key_not_null | t
```

### (d-6) The sequence that DOES work, and the compression round trip — CONFIRMED

Executed end to end on `probe.fact_d` after the failures above:

1. `decompress_chunk` every compressed chunk (4761.776 ms for one 1M-row chunk)
2. `ALTER TABLE ... SET (timescaledb.compress = false)` — 42.502 ms
3. `ALTER TABLE ... DROP CONSTRAINT <text fkey>` — 2.347 ms
4. `ADD CONSTRAINT ... CHECK ... NOT VALID` ×7 → `VALIDATE CONSTRAINT` ×7 →
   `SET NOT NULL` ×7
5. `ALTER TABLE ... DROP CONSTRAINT fact_d_pkey` — 127.848 ms
6. plain `CREATE UNIQUE INDEX` on the target shape — 2969.238 ms
   (**cannot** then be adopted as the pkey — see (d-1); in a repaired design
   this step has to become `ADD CONSTRAINT ... PRIMARY KEY (cols)`)
7. `ALTER TABLE ... SET (timescaledb.compress = true, compress_segmentby =
   'run_key, river_network_version_key, river_segment_key', compress_orderby =
   'variable_e, valid_time')` — **6.310 ms, accepted**:

```
          attname          | segmentby_column_index | orderby_column_index
---------------------------+------------------------+----------------------
 run_key                   |                      1 |
 river_network_version_key |                      2 |
 river_segment_key         |                      3 |
 variable_e                |                        |                    1
 valid_time                |                        |                    2
```

8. Round trip on a 1,008,000-row chunk under the new settings:
   `compress_chunk` 3073.881 ms (**182 MB → 20 MB**), `decompress_chunk`
   3523.119 ms, and a full-row `EXCEPT` in both directions against a
   pre-compression snapshot:

```
 lost_rows | extra_rows
-----------+------------
         0 |          0
```

The AC-4 round-trip property holds under the target settings. What does not hold
is the *shape* of the sequence the fixture prescribed to reach them.

---

## Assumption-by-assumption verdict

| # | Fixture assumption | Verdict |
|---|---|---|
| a | 7 nullable no-default columns are metadata-only on a hypertable with a compressed chunk; `atthasmissing = false` | **CONFIRMED** (≤1.1 ms each; `atthasmissing=f` on parent + all chunks) |
| a' | D2: a defaulted column would *not* be metadata-only on 2.10 | **REFUTED** (1.532 ms, fast-default; only `atthasmissing` differs). Deliverable unchanged; the migration comment must be reworded. |
| b | IDENTITY add on the authority tables is a rewrite + AEL, cost small enough for a low-peak window; `lock_timeout` fails fast | **CONFIRMED** (~10 s extrapolated on live `core.river_segment`; `lock_timeout` errors at exactly the configured wait) |
| c | The existing text FK can be **retained** while segmentby moves to the integer keys | **REFUTED** — `ERROR: column "river_segment_id" must be used for segmenting` |
| d | `ADD CONSTRAINT ... PRIMARY KEY USING INDEX` adopts a pre-built index and pushes down to chunks | **REFUTED** — `ERROR: hypertables do not support adding a constraint using an existing index` |
| d | `CHECK ... NOT VALID` + `VALIDATE` makes `SET NOT NULL` scan-free, propagating to chunks | **CONFIRMED** (0.59-0.79 ms vs 1161 ms control) |
| d | `WITH (timescaledb.transaction_per_chunk)` may be a lighter unique-index variant | **REFUTED** — `ERROR: cannot use timescaledb.transaction_per_chunk with UNIQUE or PRIMARY KEY` |
| d | CIC unavailable (000049:37-40) | re-confirmed verbatim |
| — | *(unanticipated)* prepare steps can run while `timescaledb.compress = true` | **REFUTED** — every constraint/unique-index/NOT-NULL statement raises `operation not supported on hypertables that have compression enabled`, with zero compressed chunks present |

The refutations are confined to **design D4 / tasks 1.5 and the ordering half of
1.6, plus the fourth spec scenario**. Design D1, D2 (schema shape), D3 (backfill
runner), D6's backfill-orchestration obligations and D7's test obligations are
untouched by them.

## Reproduction

The four scripts run, in order, are `exp_a.sql`, `exp_b.sql` + `exp_b2.sql` +
the two-session `lock_timeout` probe, `exp_c.sql` + `exp_c2.sql`, and
`exp_d.sql` + `exp_e.sql`. Each was copied into the container and run as:

```bash
ssh -p 32099 nwm@210.77.77.27
docker cp /tmp/exp_X.sql nhms-db:/tmp/exp_X.sql
docker exec nhms-db psql -U nhms -d nhms_1339_probe -f /tmp/exp_X.sql
```

Teardown:

```bash
docker exec nhms-db psql -U nhms -d postgres -c 'DROP DATABASE nhms_1339_probe;'
```
