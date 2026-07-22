# Tier Node-27 Timeseries Storage (Archive → Compress → Retain)

> Policy revision (2026-07-21): archive retirement and DB retention use a
> 14-day window; TimescaleDB compression remains at its independent 7-day lag.

## Why

Live measurement on node-27 (2026-07-04 receipts + direct psql) shows the
`nhms` database at 146 GB after the redundant-index prune (228 GB that same
morning), with `hydro.river_timeseries` (98 GB, 132M rows, ~70% index) and
`met.forcing_station_timeseries` (48 GB, 91M rows) accounting for effectively
all of it — growing ~24 GB/week at 13 basins with TimescaleDB compression
disabled and no retention anywhere. Worse, forcing station series before
2026-06-16 exist **only** as DB rows (their upstream products were removed by
an ad-hoc object-store reset), so any retention-before-archive ordering would
destroy sole-copy data. Decision record: `docs/adr/0002-node27-timeseries-hot-cold-tiering.md`.

## What Changes

- Add a rotation-exempt **archive tier** `/home/ghdc/nwm/archive/` (node-22
  view `/ghdc/data/nwm/archive/`) holding per-cycle `tar.zst` + manifest +
  sha256 of node-22-produced cycle products (forcing packages, SHUD run
  outputs, states) as the durable full-history source of truth.
- Add a **one-time DB-export salvage** lane for windows whose upstream
  products already rotated (forcing before 2026-06-16; river gaps if the
  inventory audit finds any), provenance-marked `db-export`.
- **Enable TimescaleDB native compression** on both detail hypertables
  (terminal chunks only; hot chunks stay writable for reingest), with a
  documented decompress path for reingest conflicts.
- Add **script-driven `drop_chunks` retention** (14-day window) with
  dry-run/enforce JSON receipts, flock, bounded deletion per tick, wired into
  the node-27 user-level systemd governance family; **hard-gated** on the
  recurring inventory audit's archive-completeness receipt (which folds in
  salvage coverage) + a rebuild-drill PASS receipt covering the window being
  dropped.
- Add an **archive rebuild drill** proving the DB hot window is
  reconstructable from archive: product cycles restore + reingest via the
  existing ingest path into an isolated staging schema (parity against
  counts parsed from the restored files); `db-export` salvage objects are
  verified by checksum + manifest row count — their only restore path is a
  documented manual `COPY FROM` procedure (no parallel automated COPY-FROM
  restore lane is built).
- **Deferred (ADR 0002 §6)**: the externally proposed v2 star schema
  (surrogate-key dims + hot facts) — re-evaluated only against measured
  growth after compression receipts, toward national scale.

## Capabilities

### New Capabilities

- `timeseries-product-archive`: archive-tier layout (forcing, runs, and
  state products), mover semantics (idempotent, same-volume staged atomic
  writes, checksum-verified before any source deletion, explicit minimum-age
  eligibility ≥ the DB retention window), rotation exemption, governance
  capacity watermark, and the recurring inventory audit that emits the
  archive-completeness receipt consumed by the retention gate and the
  salvage lane.
- `db-export-salvage`: one-time export of DB-only forcing/river timeseries
  windows to `csv.zst` +
  manifest with `db-export` provenance; scope fixed by the inventory audit's
  archive-completeness receipt; restore is a documented manual `COPY FROM`
  runbook procedure only.
- `hypertable-compression`: compression settings covering existing primary
  keys, terminal-chunk-only policy, timer-scheduled receipted runner
  registered in the governance audit, size receipts, and reingest
  decompress-or-fail-closed interplay.
- `timeseries-db-retention`: gated `drop_chunks` enforcement with receipts,
  bounds, and metadata-table exemptions; the gate set is exactly
  archive-completeness + drill-PASS receipts covering the drop window.
- `archive-rebuild-drill`: restore-from-archive + isolated staging reingest
  parity receipt with declared window coverage that unlocks retention
  enforcement.

### Modified Capabilities

- `runtime-storage-source-canonicalization`: the "durable artifacts use
  object-store root" requirement gains an archive storage class — aged cycle
  products MAY relocate to `NHMS_ARCHIVE_ROOT` with manifest + checksum, and
  the non-display rebuild/reingest/ops tooling this change builds (inventory
  audit, rebuild drill, salvage tooling) resolves rotated cycles via archive
  provenance instead of silently missing. Display routes governed by ADR
  0001 are explicitly carved out: they keep disk-only 404 semantics
  (`STATION_FORCING_FILE_NOT_FOUND` for rotated cycles) and never read the
  archive as a silent fallback.

## Impact

- `scripts/`: new `node27_storage_inventory_audit.py`,
  `node27_product_archive.py`, `node27_db_export_salvage.py`,
  `node27_timeseries_compression.py`, `node27_timeseries_retention.py`,
  `node27_archive_rebuild_drill.py` (+ `_once.sh` wrappers), following the
  existing `node27_raw_retention` / `node27_resource_governance` patterns.
- `infra/systemd/` + `infra/env/`: new user-level timer/service units and
  `.example` env files; audit/archive/compression/retention units all
  registered in the resource governance audit unit list.
- `schemas/`: JSON Schemas pinning the archive manifest,
  archive-completeness receipt, salvage manifest, drill receipt, and
  retention receipt shared by the new scripts (json-schema-validate CI gate).
- `db/migrations/`: one migration enabling compression settings
  (`ALTER TABLE ... SET (timescaledb.compress, ...)`); no schema change to
  row layout, no API contract change.
- `docs/`: ADR 0002 (committed with this change), archive/retention runbook,
  DOC_STATUS touchpoints.
- `tests/`: pytest for manifest/verify, salvage export, retention gating and
  dry-run logic; real-DB assertions run on the node-27 oracle per repo
  verification routing.
- Display read paths untouched (`run_display_coverage`, object-store CSV per
  ADR 0001); node-22 compute plane untouched.
