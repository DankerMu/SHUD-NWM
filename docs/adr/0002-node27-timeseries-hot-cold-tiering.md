# ADR 0002: Node-27 Timeseries Hot/Cold Storage Tiering

Date: 2026-07-03

Policy amendment: 2026-07-21 (archive/DB retention window reduced from 30 to 14 days;
the receipt gates and 7-day compression lead remain unchanged)

Policy clarification: 2026-07-21 (all 7/14-day lifecycle ages are anchored to
the latest node-27 displayable forecast cycle, not host wall time; wall time is
used only for receipt generation and gate freshness)

Layout amendment: 2026-07-26 (archive root moved off the `/home` hot
filesystem to `/data/GHDC/nwm-archive`; see "Amendment (2026-07-26)" below,
which supersedes Decision item 1's path wording)

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
   re-export. Aged products move to a rotation-exempt archive root: an
   archive directory on node-27's local `/home` filesystem (original path
   literal, and the "shared volume"/node-22-view framing around it, are
   superseded — see "Amendment (2026-07-26)"), stored as per-cycle
   `tar.zst` + manifest with sha256 checksums.
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
   Compress-after lag is configurable (default one chunk width, 7 days) and
   is evaluated against the node-27 display business-time watermark.
   Reingest into a compressed chunk requires an explicit, documented
   decompress step; tooling must fail closed with instructions rather than
   corrupt or silently skip.
5. **Retention is script-driven `drop_chunks` with a 14-day window**, not
   `add_retention_policy`: dry-run default, enforce mode, JSON receipts,
   flock, bounded deletions per tick, wired into the node-27 user-level
   systemd governance family. **Hard gate**: enforcement refuses to run
   unless archive completeness and rebuild-drill receipts cover the window
   being dropped. Coverage/metadata tables (`hydro_run`,
   `run_display_coverage`, `forcing_version`, QC/lineage) are retained
   indefinitely.
   The product mover, existing raw cleanup, compression runner, and retention
   runner all resolve one shared watermark as the maximum forecast
   `cycle_time` accepted by the display catalog (`succeeded`, `parsed`, or
   `published`). Missing/unreadable watermark truth blocks mutation; pipeline
   stalls therefore do not age data merely because the host clock advances.
6. **Deferred: v2 star schema** (surrogate-key dimension tables + narrow hot
   fact tables). With indexes at ~70% of hypertable size, compression of
   terminal chunks removes most of what the star schema would save. It is
   re-evaluated only against measured growth curves when expanding toward
   national scale (~100 basins), with compression receipts as the baseline.
7. **Out of scope**: archiving `raw/` GRIB (refetchable upstream; forcing
   packages carry the rebuild value — existing 14-day prune stays), the
   station history API surface (ADR 0001 owns that boundary), and
   `met.best_available_selection` (currently 0 chunks).

## Amendment (2026-07-26)

The Decision text above is preserved as history, with one exception: Decision
item 1's archive path literal was replaced by a pointer to this section,
because that literal was both wrong and stale (below). For the archive tier's
physical location this section supersedes Decision item 1 entirely: **the
current layout is whatever this amendment says**. The exact pre-migration path
survives verbatim in the committed receipt JSONs, in
`openspec/changes/tier-node27-timeseries-storage/`, and in git history.

### What changed

The archive root moved from the Decision item 1 location (a directory on
node-27's local `/home` filesystem) to **`/data/GHDC/nwm-archive`**, backed by
the node-27-local RAID
`/dev/md0` (15 TB total, ~14 TB free at migration). 2.2 GB / 1850 files were
rsync-verified into the new root; the old directory's contents were removed
(an empty directory shell remains as residue — operator cleanup is optional
and has no functional effect). The change is env-only, and no runner code or
receipt schema changed. Deployed state (verified 2026-08-01): four of the five
node-27 archive-lane env files set `NHMS_ARCHIVE_ROOT` to the new path, and
the fifth (`node27-resource-governance.env`) carries the service-level
override `NODE27_GOVERNANCE_ARCHIVE_ROOT` set to the same value. All five repo
`.example` templates set `NHMS_ARCHIVE_ROOT`.

### Premise correction (the original description was wrong from day one)

Decision item 1's original "shared volume" path wording named a node-27 path
and paired it with a claimed node-22 view of the same bytes. That was never
true on node-27: the path it named lived on the **local `/home` filesystem**,
not on the NFS share node-22 mounts. The claimed shared/node-22 view of the
archive never functioned. It also never mattered: `grep -rn
"ghdc/data/nwm/archive"` has zero hits across all code, scripts, and env files
— node-22 has no runtime consumer of any archive path. This amendment
therefore retires the "shared volume / node-22 view" wording entirely; the
archive tier is node-27-local storage, and node-22 remains unaffected by the
migration.

### Anti-pattern this amendment encodes

**The archive tier MUST NOT share a filesystem with the hot tier (pgdata /
object-store) it exists to relieve.** When it does, the free-space guard that
protects the archive lane becomes a self-lock: as the hot tier fills the shared
filesystem, the mover refuses with `refused_free_space`, so the mover frontier
stops advancing, so the archive-completeness receipt stays pending, so
`scripts/node27_timeseries_retention.py` refuses to drop chunks — the one
mechanism able to free the disk is deadlocked by the very disk it protects.

That is exactly the 2026-07-26 incident: node-27 `/home` filled
(`hydro.river_timeseries` at 816 GB, growing ~43 GB/day during July), the mover
frontier had been stuck at 2026-06-20, PostgreSQL crashed twice, and the
display surfaced as "Layer is not registered by the API". Moving the archive
root to its own filesystem restored `outcome=success` on the mover and a clean
`free_space` band. Any future relocation of `NHMS_ARCHIVE_ROOT` must preserve
the separate-filesystem property; the env templates carry this as an inline
warning.

### Disposition of the superseded path wording

`openspec/changes/tier-node27-timeseries-storage/` intentionally keeps the
original archive path wording (`proposal.md:22`,
`specs/timeseries-product-archive/spec.md:8-9`), and the committed
pre-migration receipts under
`docs/runbooks/receipts/tier-node27-timeseries-storage/product-archive/` keep
it too. Both are point-in-time records of what was delivered and are not
rewritten. **This ADR amendment is the authoritative source for the current
archive layout**; the operator-facing current state lives in
[`docs/runbooks/tier-node27-timeseries-storage.md`](../runbooks/tier-node27-timeseries-storage.md).

## Amendment (2026-08-06): the archive filesystem now also carries part of the DB

The 2026-07-26 amendment separated the archive tier onto `/dev/md0` precisely
so that a full `/home` could not deadlock the mover ↔ retention loop. That
separation is now partially undone, knowingly, in the opposite direction.

### What changed

PostgreSQL tablespace **`ghdc`** was created at host path
`/data/GHDC/nwm-archive/nhms-tablespace` (container
`/home/postgres/pgdata/tablespaces/ghdc`), on the same `/dev/md0` that backs
`/data/GHDC/nwm-archive`. Four chunks — `_hyper_3_10_chunk` and
`_hyper_3_14_chunk` of `hydro.river_timeseries`, `_hyper_1_12_chunk` and
`_hyper_1_13_chunk` of `met.forcing_station_timeseries` — plus all 18 of their
indexes were moved there, roughly 502 GB decompressed. The `nhms-db` container
was recreated to add the bind mount; `pg_default` and the object store stay on
`/home`.

### Why

The six-basin production replay (#1164) had to decompress those four chunks to
lift the compressed-chunk write guard, and `/home` had ~357 GB free against a
~502 GB requirement. Sequencing could not avoid it: each backfilled cycle's
forecast window spans both the 07-02…07-09 and 07-09…07-16 chunks, so both had
to be uncompressed simultaneously. On `/data/GHDC` the only `nwm`-writable
location is `nwm-archive/` — the rest of that mount is `root`/`ghdcadmin`
owned and a sibling mount point needs root. The alternatives were rejected:
`/srv` has 434 GB free (below the requirement), and dropping the replay's
older 17 cycles would have failed the issue's "controlled replacement of the
six basins' historical runs" requirement.

### What this costs, and the terms it is accepted on

This re-opens the 2026-07-26 deadlock with the operands swapped: **database**
growth can now push the archive filesystem below the mover's refuse threshold,
freezing the mover frontier, leaving the archive-completeness receipt pending,
and making retention refuse to drop. Accepted because `/dev/md0` holds ~19 GB
of archive against 15 TB, and the archive grows at single-digit GB/month —
i.e. headroom, not a structural fix. Terms:

- The tablespace's working set must be bounded (re-compress promptly, never
  hold more uncompressed than the chunks actively being reingested). This is
  the only lever that protects `/dev/md0`. The archive free-space band is
  **not** such a lever: it is the mover's entry gate, it reserves nothing,
  and PostgreSQL enforces no tablespace quota — raising it makes the mover
  refuse at higher free space, i.e. it advances the deadlock above instead of
  preventing it.
- Governance visibility is partial, and distorted in both directions.
  `scripts/node27_resource_governance.py` *does* report `/dev/md0` free/total
  and warn/refuse recommendations through its `archive_root` block (live only
  when both `NHMS_ARCHIVE_FREE_SPACE_{WARN,REFUSE}_BYTES` are set). But its
  `pgdata_root` `du` covers only `/home/nwm/nhms-pgdata`, so the DB footprint
  under-reports by the migrated bytes; and `archive_root.used_bytes` is a `du`
  of the whole archive root, which now *contains* the tablespace — so the
  archive's reported size absorbed ~502 GB of database and the
  "single-digit GB/month" growth signal this exception relies on is no longer
  readable from it. Read `df -h /home /data/GHDC` manually, and size the
  archive alone with `du -s --exclude=nhms-tablespace`. Issue #1290.
- `/dev/md0` is not NHMS-exclusive (`root`/`ghdcadmin` trees share it, ~1 TB
  in use at the 2026-07-26 migration), so free space can fall without any
  NHMS growth.
- Prefer a dedicated filesystem for `ghdc` the next time root-level
  provisioning is available on node-27. This amendment is an exception with a
  reason, not a new rule; the 2026-07-26 separation principle still stands for
  everything else, and `NHMS_ARCHIVE_ROOT` must still never be pointed at a
  filesystem carrying pgdata or the object store.

Operator-facing detail: `docs/runbooks/tier-node27-timeseries-storage.md`
section "Recorded exception (2026-08-06)".

## Consequences

- Steady-state DB size becomes bounded (14-day window, mostly compressed)
  instead of growing ~24 GB/week at 13 basins; the archive grows by
  compressed product tarballs (estimated single-digit GB/month at current
  scale) on a volume with 839 GB free (pre-migration figure; since 2026-07-26
  the archive volume is the 15 TB RAID — see "Amendment (2026-07-26)").
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

## Implementation

Delivered under OpenSpec change
[`tier-node27-timeseries-storage`](../../openspec/changes/tier-node27-timeseries-storage/proposal.md)
across a family of node-27 user-level systemd oneshot + timer scripts,
plus receipt schemas and a single operator runbook. Every write path is
gated on a signed receipt so the "no deletion without archive receipt"
invariant is enforceable from operator tooling, not honor-based.

Runbook — the single operator entrypoint for all sections below:
[`docs/runbooks/tier-node27-timeseries-storage.md`](../runbooks/tier-node27-timeseries-storage.md).

| Sub-issue | Scope | Code | Receipt schema | Runbook |
|---|---|---|---|---|
| #846 | Storage-source foundation | `packages/common/runtime_storage_source.py` | — | §1 |
| #849 | Product archive mover + inventory audit systemd + capacity guards | `scripts/node27_product_archive.py`, `scripts/node27_storage_inventory_audit.py` | [`schemas/archive_completeness_receipt.schema.json`](../../schemas/archive_completeness_receipt.schema.json) | Install / Operation / Rollback (top of runbook) |
| #850 | DB-export salvage exporter + manual restore | `scripts/node27_db_export_salvage.py` | [`schemas/db_export_salvage_receipt.schema.json`](../../schemas/db_export_salvage_receipt.schema.json) | §3 (including §3.2 manual restore) |
| #851 | Hypertable compression migration + runner | `db/migrations/000047_hypertable_compression.sql`, `scripts/node27_timeseries_compression.py` | [`schemas/timeseries_compression_receipt.schema.json`](../../schemas/timeseries_compression_receipt.schema.json) | §4 (including §4.3 decompress procedure) |
| #852 | Fail-closed compressed-chunk write guard | `packages/common/timescale_write_guard.py` + wired at 3 write paths | — (in-process exception) | §4.3 |
| #853 | Compression systemd + governance registration | `infra/systemd/nhms-node27-timeseries-compression.{service,timer}` | — | §4 install / cadence / rollback |
| #854 | Archive rebuild drill (isolated staging) | `scripts/node27_archive_rebuild_drill.py` | [`schemas/archive_rebuild_drill_receipt.schema.json`](../../schemas/archive_rebuild_drill_receipt.schema.json) | §7 (including §7.5 coverage rule + §7.6 recovery) |
| #855 | Gated retention runner (`drop_chunks`) + systemd | `scripts/node27_timeseries_retention.py`, `infra/systemd/nhms-node27-timeseries-retention.{service,timer}` | [`schemas/timeseries_retention_receipt.schema.json`](../../schemas/timeseries_retention_receipt.schema.json) | §8 (including §8.5 dry-run semantics + §8.6 recovery + §8.7 salvage cross-link) |
| #856 | Node-27 live dry-run + first enforce | committed receipts under [`docs/runbooks/receipts/tier-node27-timeseries-storage/`](../runbooks/receipts/tier-node27-timeseries-storage/) | consumes retention schema | §8.4 how to run |

The two gate receipts consumed by retention enforce are the audit's
archive-completeness receipt (from the archive/audit section at the top
of the runbook) and the drill PASS receipt (§7);
compression state is never a retention gate. All new node-27 systemd
units are registered in the resource-governance audit unit list
([`scripts/node27_resource_governance.py`](../../scripts/node27_resource_governance.py)
`DEFAULT_SERVICES`).
