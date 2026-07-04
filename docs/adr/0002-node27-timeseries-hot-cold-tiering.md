# ADR 0002: Node-27 Timeseries Hot/Cold Storage Tiering

Date: 2026-07-03

## Status

Accepted

## Context

Live measurements on node-27 (2026-07-04 CST; governance receipt
`resource-governance-20260704T012644Z.json` + direct `psql` via the `nhms-db`
container) established:

- `nhms` database: **146 GB** after the redundant-index prune
  (db/migrations/000041-000042 landed that morning; it was 228 GB before).
- `hydro.river_timeseries`: 98 GB total = 29 GB heap + **69 GB index**
  (132M rows; index share ~70%). Remaining index families are functional
  (pkey 30 GB, MVT identity lookup 32 GB) and cannot be pruned further.
- `met.forcing_station_timeseries`: 48 GB total = 14 GB heap + 34 GB index
  (91M rows; `qhh_latest_window_idx` alone is 20 GB).
- Everything else in the database sums to under 300 MB. The two detail
  hypertables ARE the size problem.
- TimescaleDB **2.10.2** / PostgreSQL 15.2. `compression_enabled = false` on
  both hypertables. No `drop_chunks`/retention policy exists anywhere.
- DB chunk coverage starts 2026-05-28 (7-day chunks). The hot object-store
  (`/home/ghdc/nwm/object-store/`) retains `forcing/` only since 2026-06-16
  (a mid-June ad-hoc reset; no code routinely rotates `forcing/` or `runs/`),
  `runs/` since 2026-05-31, and `raw/` is pruned at 14 days by
  `nhms-node27-raw-retention.timer`. **Forcing station series before
  2026-06-16 exist only as DB rows** — the DB is currently the sole copy.
- Display read paths do not scan the big hypertables: latest-product reads
  `hydro.run_display_coverage`; station forcing curves read retained
  object-store CSV (ADR 0001). A prior incident (docs/bugs.md, 21.4 s → 413 ms)
  proved ad-hoc scans of the 92M+ row table are a production hazard.

An externally proposed redesign ("demote the DB to control plane + hot cache,
v2 star schema with surrogate keys, object-store as full source of truth") was
reviewed against these facts. Its end-state direction is sound; its ordering
(retention before a durable archive exists) would destroy sole-copy data, and
it omits TimescaleDB native compression entirely.

## Decision

1. **Source of truth for cold data is node-22-produced cycle products**
   (forcing packages, SHUD run outputs, state snapshots) — not a DB
   re-export. Aged products move to a rotation-exempt archive root on the
   shared volume: `/home/ghdc/nwm/archive/` (node-22 view:
   `/ghdc/data/nwm/archive/`), stored as per-cycle `tar.zst` + manifest with
   sha256 checksums.
2. **One-time DB-export salvage** only for windows whose upstream products
   already rotated away (verified scope; notably forcing station series
   before 2026-06-16): `COPY` to `csv.zst` with manifest, provenance-marked
   `db-export`, stored in the same archive root. This is a salvage lane, not
   a steady-state mechanism.
3. **DB rebuild path is the existing node-27 ingest/reingest from products.**
   No parallel COPY-FROM restore lane is built. An archive rebuild drill must
   prove hot-window reconstruction before any DB deletion is enabled.
4. **Enable TimescaleDB native compression** on both hypertables (terminal
   chunks only; the active chunk stays uncompressed). Segment/order choices
   must cover the existing primary keys (river: segmentby
   `run_id, river_network_version_id, river_segment_id`, orderby
   `variable, valid_time`; forcing: segmentby
   `forcing_version_id, station_id`, orderby `variable, valid_time`).
   Compress-after lag is configurable (default one chunk width, 7 days).
   Reingest into a compressed chunk requires an explicit, documented
   decompress step; tooling must fail closed with instructions rather than
   corrupt or silently skip.
5. **Retention is script-driven `drop_chunks` with a 30-day window**, not
   `add_retention_policy`: dry-run default, enforce mode, JSON receipts,
   flock, bounded deletions per tick, wired into the node-27 user-level
   systemd governance family. **Hard gate**: enforcement refuses to run
   unless archive completeness and rebuild-drill receipts cover the window
   being dropped. Coverage/metadata tables (`hydro_run`,
   `run_display_coverage`, `forcing_version`, QC/lineage) are retained
   indefinitely.
6. **Deferred: v2 star schema** (surrogate-key dimension tables + narrow hot
   fact tables). With indexes at ~70% of hypertable size, compression of
   terminal chunks removes most of what the star schema would save. It is
   re-evaluated only against measured growth curves when expanding toward
   national scale (~100 basins), with compression receipts as the baseline.
7. **Out of scope**: archiving `raw/` GRIB (refetchable upstream; forcing
   packages carry the rebuild value — existing 14-day prune stays), the
   station history API surface (ADR 0001 owns that boundary), and
   `met.best_available_selection` (currently 0 chunks).

## Consequences

- Steady-state DB size becomes bounded (30-day window, mostly compressed)
  instead of growing ~24 GB/week at 13 basins; the archive grows by
  compressed product tarballs (estimated single-digit GB/month at current
  scale) on a volume with 839 GB free.
- The mid-June reset failure mode ("delete products, DB silently becomes the
  only copy") is eliminated: deletion anywhere is gated on archive receipts.
- Rollback: compression is reversible per chunk (`decompress_chunk`);
  retention is preceded by archive + drill receipts; the salvage lane keeps
  provenance so `db-export` data is distinguishable from product-derived
  archives forever.
- Risk: the forcing/ rotation mechanism was an ad-hoc reset, so archive-lane
  completeness auditing (products ⟷ DB coverage inventory) must land before
  retention enforcement; this ordering is encoded as a hard gate in the
  change tasks.
- Node-22 keeps writing products exactly as today; all new machinery runs on
  node-27 (mover, salvage, compression, retention, drill), matching the
  current "node-27 owns data plane" topology.
