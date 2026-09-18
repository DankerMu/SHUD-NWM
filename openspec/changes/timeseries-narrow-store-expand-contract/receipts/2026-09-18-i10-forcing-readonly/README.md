# I10 (#1989) — forcing read-only receipt, node-27, 2026-09-18

Task 7.1. Production is **read only throughout**, every query as `nhms_display_ro` from
`infra/env/display.env`, each inside a `READ ONLY` transaction. The IDENTITY timings run on throwaway
databases that the scripts create and drop; the live cluster was never altered. DSN only via env, never
argv, never printed. No production schema change, no write of any kind.

## 1. Authority table row counts and index footprint

| table | rows | heap | indexes | total |
|---|---|---|---|---|
| `met.met_station` | **42 029** | 325 MB | 18 MB | 343 MB |
| `met.forcing_version` | **8 653** | 15 MB | 1040 kB | 16 MB |

| table | index | size | definition |
|---|---|---|---|
| `forcing_version` | `forcing_version_pkey` | 1040 kB | `(forcing_version_id)` UNIQUE |
| `met_station` | `met_station_pkey` | 3912 kB | `(station_id)` UNIQUE |
| `met_station` | `met_station_geom_gix` | 11 MB | GiST `(geom)` |
| `met_station` | `met_station_basin_idx` | 1528 kB | `(basin_version_id)` |
| `met_station` | `met_station_active_basin_station_idx` | 432 kB | `(basin_version_id, station_id) WHERE active_flag` |
| `met_station` | `met_station_name_trgm_idx` | 432 kB | GIN trgm on `coalesce(station_name,'')` `WHERE active_flag` |

**`met.met_station` is roughly 8× bloated, and that is a migration-window input, not trivia.** Its 325 MB
is heap pages, not data: TOAST holds 8 kB, `properties_json` averages 928 B (37 MB across the table),
`geom` 29 B and the text columns 86 B — about 40 MB of live column data in 325 MB of pages. §3 measures
what that costs.

## 2. Distinct value vocabulary — exact, whole table

`met.forcing_station_timeseries`, **259 255 326 rows**, full aggregate in **3 min 08 s**
(`distinct-full.sh` → `full-distinct.out`). Not a sample and not a lower bound.

| column | distinct values | counts |
|---|---|---|
| `variable` | **6** — `PRCP`, `Press`, `RH`, `Rn`, `TEMP`, `wind` | 43 209 221 each, exactly uniform |
| `unit` | **6** — `mm/day`, `Pa`, `0-1`, `W/m2`, `degC`, `m/s` | 43 209 221 each, 1:1 with `variable` |
| `quality_flag` | **1** — `ok` | 259 255 326 |
| `native_resolution` | **2** — `3h` (249 196 518), `6h` (10 058 808) | **no NULLs**, though the column is `TEXT NULL` |
| `source_id` | **2** — `gfs` (133 197 360), `IFS` (126 057 966) | — |

Three facts worth carrying into I11/I12:

- **There is no CHECK constraint on any of these four columns** — they are free `TEXT` today, while the
  river side already has `hydro.river_variable`, `river_unit` and `river_quality_flag` as enums. The
  forcing narrow store's enum columns therefore have to *introduce* a vocabulary, not mirror one.
- **`variable` and `unit` are perfectly 1:1** at 43 209 221 rows each, so `unit` is a function of
  `variable` in every row that exists today.
- **The task text says "三源并集" (three-source union); production has two sources.** `met.data_source`
  holds exactly `gfs` and `IFS`, both `enabled`. Recorded as a discrepancy in the task's framing rather
  than silently answered for two.

## 3. IDENTITY column add — wall time and lock, throwaway cluster

`ALTER TABLE … ADD COLUMN … INTEGER GENERATED ALWAYS AS IDENTITY UNIQUE` carries a volatile default
(`nextval`), so PostgreSQL rewrites the whole table under `AccessExclusiveLock` and then builds the
UNIQUE index. Cost tracks **pages scanned**, which is why §1's bloat matters.

| table | seeded heap | live heap | wall time | lock |
|---|---|---|---|---|
| `forcing_version` | 17 MB | 15 MB | **689 ms** | `AccessExclusiveLock` |
| `met_station`, live **data** constitution | 47 MB | (40 MB of data) | **1 141 ms** | `AccessExclusiveLock` |
| `met_station`, live **page** constitution | 375 MB | 325 MB | **1 863 ms** | `AccessExclusiveLock` |

**The rewrite de-bloats as a side effect**: the 375 MB table came back at 47 MB after the ALTER. So the
migration window does not need a separate `VACUUM FULL` for these two tables, and de-bloating beforehand
would only save the ~700 ms difference between the two `met_station` rows.

### A rejected measurement, kept rather than deleted

`identity.out` is a **first attempt whose numbers must not be used** (734 ms / 625 ms). Its seed padded
`properties_json` with `repeat('x', 7400)`, which jsonb compressed away: the table came out at 11 MB
instead of ~325 MB, so the measurement understated the work by roughly 30×. It is archived because a
timing that looks plausible and is wrong by 30× is exactly the failure this receipt should make visible.
`identity-add.sh` (v2) fixes it by padding from `encode(gen_random_bytes(...), 'base64')`, which TOAST
compression cannot shrink, and by building the bloat deliberately with `autovacuum_enabled = false`.

`identity-add.sh`'s own `forcing_version` leg failed with `ERROR: Length not in range` —
`gen_random_bytes` caps at 1024 — leaving that table empty and its 284 ms reading meaningless.
`identity-add-forcing-version.sh` re-measures it properly; that is where the 689 ms above comes from.

## 4. Per-`forcing_version` existence probe — the D9 asymmetry, measured

The river side routes per run through a column on `hydro.hydro_run`. Forcing has no run, so the routing
backfill must ask the fact table, per `forcing_version`, whether legacy rows exist. Measured over all
8 653 versions in one statement (`existence-probe.sql`):

| | cold | warm |
|---|---|---|
| execution time | **21 849 ms** | **238 ms** |
| buffers | 146 170 hit / 6 523 read | — |

Result: **4 764 versions have rows, 3 889 do not.** Each probe is an index descent per chunk — the
compressed chunks answer from `compress_hyper_8_*__compressed_hypertable_8_forcing_ver`, the uncompressed
ones from the chunk primary key, and `ChunkAppend` stops at the first hit, so later chunks see fewer
loops (8 653 → 7 157 → 6 093 → 5 029 → 3 965 → 3 889).

**So the backfill is affordable** — a one-time 22 s pass at worst — and the D9 asymmetry costs a full
fact-table probe pass rather than a column read. Two consequences for I11/I12: the probe must run *after*
the last legacy write or it can misroute a version that gains rows later, and 3 889 of 8 653 versions
(45 %) have no legacy rows at all, so routing them is a no-op that the backfill can skip.

## Files

| file | what it is |
|---|---|
| `authority-footprint.sql`, `column-composition.sql` | §1's `READ ONLY` queries |
| `distinct-full.sh`, `full-distinct.out` | §2, exact whole-table aggregate |
| `distinct.out` | the earlier per-chunk pass (oldest + newest), kept as the cheap cross-check |
| `identity-add.sh`, `identity2.out` | §3 v2, both `met_station` constitutions |
| `identity-add-forcing-version.sh` | §3's `forcing_version` re-measurement |
| `identity.out` | §3 v1 — **rejected**, see above |
| `existence-probe.sql`, `existence-probe-warm.out` | §4 |
