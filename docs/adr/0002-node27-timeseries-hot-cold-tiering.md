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

Context amendment: 2026-08-14 (the Context claim that the remaining
`hydro.river_timeseries` index families "cannot be pruned further" is
superseded by new measurements; see "Amendment (2026-08-14)" below)

Amendment: 2026-08-29 (DB-only `nhms_cold` successor; archive retirement
stands; isolated 2.10.2 probe freezes shell-first movement — see
"Amendment (2026-08-29)" below)

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
  everything else. Concretely: the exception is bounded to the `ghdc`
  tablespace directory already present under the archive root — do not point
  `NHMS_ARCHIVE_ROOT` at the filesystem carrying `pg_default`
  (`/home/nwm/nhms-pgdata`) or the object store, and do not place further DB
  data on `/dev/md0`.

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

## Revision 2026-08-11 — archive lane retired; delete-without-archive authorized

`/dev/md0` suffered a double-disk failure (#1309) and the operator decision
(2026-08-11) is that the array will **not** be rebuilt. The archive lane —
product-archive mover, the storage-inventory audit's archive leg, db-export
salvage, and the archive-rebuild drill (`/data/GHDC/nwm-archive`) — is
permanently retired; #1310, #1177 and #1228 were closed on that basis.

Consequently the operator explicitly amends this ADR's core invariant:
**"no deletion without archive receipt" no longer holds** — node-27
timeseries retention is authorized to delete hot chunks without archive
coverage. The change is deliberate and auditable, not a silent bypass:
the retention runner keeps its fail-closed default and only skips the
completeness/drill gates in an explicit gate-disabled mode whose receipt
records the mode and cites this revision. Implementation is tracked in
issue #1369. The display carve-out window and the compression sections
of this ADR are unchanged (compression was never a retention gate).

## Amendment (2026-08-14): the river index families could be pruned further

The Context above records, from the 2026-07-04 measurements, that
`hydro.river_timeseries`'s "remaining index families are functional (pkey 30 GB,
MVT identity lookup 32 GB) and cannot be pruned further". That forward-looking
claim is **superseded by #1338**; the original Context text stays as written
(it is an accurate record of what was measured in July), and the Status is
unchanged — nothing in the Decision depended on the claim.

### What the new measurement shows

Live node-27 measurement 2026-08-14 (read-only, aggregated across all 8
`hydro.river_timeseries` chunks; inventory in the #1338 pre-drop receipt posted
on PR #1377 (2026-08-14)):

- `river_timeseries_mvt_identity_lookup_idx`: **162 GB** (up from 32 GB in July)
  for **5,571** `idx_scan`, against **796,096,944** `river_timeseries_pkey` scans
  over the same window. Its column *set* is identical to the pkey's — only the
  order differs — but the same set is not the same coverage: with `variable`
  single-valued table-wide it behaves as `(run_id, valid_time, rnv, segid)`, a
  run-scoped time prefix the pkey cannot offer (the pkey orders `rnv`/`segid`
  ahead of `variable`/`valid_time`). The in-repo query shapes measured to use
  this index are two reads — the #1338 pre-drop baseline Q1/Q8 shape captures:
  the national-tile `typed_values` ts-access leg
  (`services/tiles/mvt.py:603-651`) and the source-identity stats probe
  (`mvt.py:530-553`) — both binding `run_id` + `variable` + `valid_time` +
  `rnv` with **no** `basin_version_id`, a shape the retained
  `..._mvt_selected_identity_valid_time_discovery_idx` cannot serve (its 2nd
  column is `basin_version_id`); the 5,571 cumulative `idx_scan` are **not**
  individually attributed to queries. Both baseline captures used this index,
  but Q1 is a single-table shape proxy rather than the real query: the real
  `typed_values` CTE projects `value`/`unit`/`quality_flag`/`basin_version_id`,
  none of which this index carries, so the real query can never be index-only
  on it (even the proxy's Index Only Scan reported `Heap Fetches: 216`). The
  stats probe's real ts leg takes no payload columns, so index-only is feasible
  there. The statically expected
  post-drop successor for those two reads was the pkey (usable prefix `run_id` +
  `rnv`, remaining predicates as in-index filters), and the post-drop receipt
  (PR #1377 comments, 2026-08-14) **confirmed** it: both shapes fell to a
  `river_timeseries_pkey` Index Only Scan — no Seq Scan fallback, no other
  retained index picked up — at Q1 warm **10.4 ms** vs **2.5 ms** pre-drop
  (a **4.2x** residual cost: the disclosed tradeoff, not an order-of-magnitude
  regression) and Q8 **2.9 ms**. All other captured shapes' plans were unchanged
  against the same-compression-state immediate pre-drop baseline. The ingest
  window `DELETE` already planned on the pkey pre-drop
  (baseline Q7, all four predicates pushed into the pkey `Index Cond`), so the
  write path is not a consumer.
- `river_timeseries_valid_time_discovery_idx`: 4663 MB for 10,864 `idx_scan`, a
  strict prefix of the index above; those 10,864 cumulative `idx_scan` are
  **not** individually attributed to queries. No in-repo migration created it;
  it was created out-of-band on node-27.

### Disposition

Both are dropped by
[`db/migrations/000049_drop_redundant_river_mvt_identity_and_valid_time_discovery_idx.sql`](../../db/migrations/000049_drop_redundant_river_mvt_identity_and_valid_time_discovery_idx.sql)
(~167 GB at the 2026-08-14 05:29Z pre-drop baseline sizes; the node-27 apply
receipt (PR #1377 comments, 2026-08-14) records the actual before/after:
**293.6 GB → 193.2 GB, ~100 GB reclaimed**. The delta against the ~167 GB
estimate is not a mis-measurement of either number — a compression drift
between capture and apply had already dropped part of the chunk btrees, so the
dated capture and the dated apply are each valid for their own moment, and the
receipt records the drift). This is a
**tradeoff, not a redundancy removal**: 162 GB of
carrying cost weighed against 5,571 scans, with a real residual coverage loss
on the two read shapes above — now priced at the 4.2x recorded there. The
pre-merge before/after `EXPLAIN (ANALYZE, BUFFERS)` gate measured the
before/after latency of those two **predicate shapes** — the gate's Q1/Q8 are
single-table proxies, so real-query latency was **not** measured by the gate.
The rollback trigger was a Seq Scan fallback or an order-of-magnitude slowdown
in the shape plans; the receipt showed neither (pkey Index Only Scan, 4.2x), so
the drop stands. Should a later measurement cross that line, re-creating the
index rolls it back (the re-create DDL for both is preserved verbatim in that
migration's comments).
Re-creating the index is not by itself a durable rollback: as that migration's
rollback-procedure note records, the build takes a `SHARE` lock on
`hydro.river_timeseries` (blocking ingest writes) and that lock is unavoidable —
the #1338 live-leg pre-check (node-27, 2026-08-14) had
`CREATE INDEX CONCURRENTLY` **rejected** on this hypertable
(`ERROR: hypertables do not support concurrent index creation`, TimescaleDB
2.10.2) while plain `CREATE INDEX` was accepted, so plain `CREATE INDEX` under a
`SHARE` lock for the full build is the only rollback path — and the full filename
`000049_drop_redundant_river_mvt_identity_and_valid_time_discovery_idx.sql` must
also be recorded in `public.schema_migrations.version` (or the file reverted) —
inserting the shorthand `000049` leaves `migrate.py` still treating the
migration as unapplied and silently re-dropping the rebuilt index — or the next
unattended `migrate.py` run silently re-drops the rebuilt index. The
general lesson for this ADR: "cannot be pruned further" is a measurement, not a
property — index redundancy claims expire and must be re-measured against
growth, not carried forward.

## Amendment (2026-08-15): Decision 6 re-evaluated EARLY; Decision 4's column list is superseded at cutover

This amendment covers two Decisions at once because issue #1339 touches both:
it revives the star-schema idea Decision 6 deferred, and the mechanism it uses
rewrites the specific segmentby column list Decision 4 pins.

### This is an early trigger, and the literal condition is NOT met

Decision 6 defers the v2 star schema and names its own re-evaluation condition:
"re-evaluated only against measured growth curves when expanding toward
national scale (~100 basins), with compression receipts as the baseline."

**The literal condition is not satisfied.** The deployment still carries **18**
business basins (`docs/runbooks/current-production-ops.md:182`), the same count
as when this ADR was accepted. Nothing here should be read as claiming
otherwise, and the display/MVT capacity work of the intervening weeks was
rendering scale, not basin expansion.

The re-evaluation is triggered early, on two grounds:

1. **The growth curve moved without the basin count moving.**
   `hydro.river_timeseries` went from 132M rows (2026-07-04, this ADR's Context)
   to **459.9M rows** (2026-08-15, read-only measurement on node-27) — 3.5x in
   six weeks, driven by cycle accumulation rather than by new basins. Decision 6
   names "measured growth curves" as a trigger in its own right; that curve is
   the trigger being invoked.
2. **Epic #1336 carries a new profile** of the per-row identity cost that did
   not exist when Decision 6 was written.

Deferring to ~100 basins while the row count triples every six weeks would mean
re-evaluating a decision about storage cost only after the cost had already
compounded — which is what the 2026-08-14 amendment above already recorded as
this ADR's recurring lesson.

### The compression-receipt baseline, stated in full

Decision 6 set the baseline itself: "with compression receipts as the
baseline." Honouring that means reporting the numbers that **weaken** the case
alongside the ones that support it.

Measured read-only on node-27, 2026-08-15 (`chunk_compression_stats`,
`hypertable_detailed_size`):

| Measurement | Value |
|---|---|
| Compression ratio, `_hyper_3_32` | 268 GB → 6096 MB = **45.09x** |
| Compression ratio, `_hyper_3_51` | 215 GB → 4924 MB = **44.63x** |
| Total hypertable size | **249 GB** (112 GB heap, 137 GB index, 1696 kB toast) |
| Index share, post-#1338 | **137 GB index vs 112 GB heap = 55%** of total (this ADR's Context recorded ~70%; #1338's pruning brought it down) |
| Chunks | 6, of which 2 compressed |
| Rows | 459,914,080 |

**The 45x ratio supports Decision 6's original argument, and that is stated
plainly:** for a chunk that has been compressed, columnar compression really
does capture most of what a star schema would have saved, and normalizing those
chunks would add little. Decision 6 was right about compressed data.

**What Decision 6's argument does not cover** is the data that is *not*
compressed. Of the 249 GB live footprint, roughly **239 GB** sits in the four
uncompressed chunks and in indexes; only ~10 GB is compressed storage. The
compression lane compresses terminal chunks only — by construction the active
and recent chunks stay in plain form, and they are also the chunks display
reads actually hit. Indexes are the sharper point: at 137 GB they are **larger
than the 112 GB heap**, they are not compressed by the hypertable's compression
at all, and their width is driven directly by the repeated text identity
columns (`run_id` 56 B, `river_segment_id` 37 B per row) that #1339 replaces
with `int4`.

**And one measurement cuts against the urgency, which is also stated plainly:**
total size went **down**, 264 GB → 249 GB, between the fixture's first capture
and this one — because another chunk was compressed in between. Bytes are not
growing monotonically; the compression lane is doing its job. The pressure this
amendment responds to is visible in the **row curve and the index share**, not
in the total-bytes curve. Presenting only the row growth, or only the 45x
ratio, would each be a distortion; both are recorded.

### Disposition for Decision 6

Decision 6's "deferred" status is **narrowed, not reversed**. What issue #1339
delivers is not the v2 star schema Decision 6 described: no dimension tables are
created. Integer surrogate keys are added to the four authority tables that
already exist (`hydro.hydro_run`, `core.basin_version`,
`core.river_network_version`, `core.river_segment`), which avoids the duplicate
identity source and the DISTINCT-scan seeding phase that made the dimension-table
shape unattractive. Narrow hot fact tables remain deferred and remain governed
by Decision 6 as written.

### Decision 4's segmentby column list is superseded at cutover

Decision 4 pins the river hypertable's compression configuration as segmentby
`run_id, river_network_version_id, river_segment_id`, orderby
`variable, valid_time`, chosen to cover the then-current primary key. That
column list is rewritten by
`hydro.cutover_river_identity_normalization()` to segmentby
`run_key, river_network_version_key, river_segment_key`, orderby
`variable_e, valid_time`, simultaneously with the primary key it covers.

The two cannot be separated: TimescaleDB 2.10 requires segmentby ∪ orderby to
cover every unique/primary-key column, so the key swap and the settings swap
are one atomic act. Decision 4's *principle* — "segment/order choices must cover
the existing primary keys" — is unchanged and is exactly what forces this. Only
the literal column names are superseded, and only once the cutover has been
executed in a maintenance window. **As of this amendment the production
configuration is still Decision 4's original list**; `000050` changes no
compression setting and the migration chain never calls the cutover function.

### A cost this amendment records rather than hides

The cutover **drops the two-column text foreign key** from
`hydro.river_timeseries` to `core.river_segment` (000006_hydro.sql:57-58). This
is forced, not chosen: TimescaleDB 2.10.2 refuses a compression configuration
that does not cover a foreign key's columns
(measured: `ERROR: column "river_segment_id" must be used for segmenting`), and
the target segmentby is integer-only. No integer replacement FK can be added
either — `basin_version_key` is not in the target segmentby, so an FK on it
would hit the same rule.

The consequence: between this cutover and the text-column-retirement issue, the
fact table has **no database-enforced referential integrity** against
`core.river_segment`. It is guarded instead by the backfill's four-way join,
the runner's fail-closed unmatched counter, the read-only verify function's
equality audit, and the seven `NOT NULL` constraints the cutover installs.
Those are real checks, but they are point-in-time checks, not a continuously
enforced constraint. Anyone reading this ADR later and wondering where the
foreign key went: it was traded for a 45x-compressible integer segmentby, and
the trade is recorded here rather than discovered in the catalog.

### Provenance

All figures above are read-only measurements taken on node-27 on 2026-08-15
against the live `nhms` database (catalog, `pg_stats`, and size functions only —
no DDL, no DML). The TimescaleDB behavioural findings come from a dedicated
throwaway database (`nhms_1339_probe`, since dropped); the full experiment log,
including the exact error texts and the measurements that refuted two earlier
designs, is
`openspec/changes/river-identity-normalization-backfill/probe-1339-throwaway.md`.

## Amendment (2026-08-29): DB-only `nhms_cold` successor; archive retirement stands

The 2026-08-11 revision retired the **product archive / salvage / rebuild**
lanes after the `/dev/md0` double-disk failure (#1309/#1370). That retirement
still stands. This amendment does **not** restore those lanes, does **not**
revive the historical `ghdc` tablespace as a live operator target, and does
**not** re-authorize archive-gated retention.

What it does add is a narrower, DB-only successor: a fresh PostgreSQL
tablespace named `nhms_cold` that may later hold **terminal compressed chunk
residency groups** after #1894/#1895. #1892 only freezes the TimescaleDB 2.10.2
contract on an isolated disposable cluster. Live `nhms-db` is read-only during
Issue #1892; no production CREATE/DROP TABLESPACE, chunk move, or container recreate
is authorized here.

### What is live vs historical

- `pg_default` on `/home/nwm/nhms-pgdata` remains the live hot tablespace.
- The historical `ghdc` tablespace path
  (`/data/GHDC/nwm-archive/nhms-tablespace`, container
  `/home/postgres/pgdata/tablespaces/ghdc`) is a **retired exception** from
  the 2026-08-06 decompression overflow. It is not the successor, not an
  installer target, and not a live cold-residency name. Operators must not
  treat leftover `ghdc` catalog/bind residue as the #1891 cold tier.
- `nhms_cold` is the only DB-only successor name. Production host path
  `/data/GHDC/nhms-cold-tablespace` and container path
  `/home/postgres/pgdata/tablespaces/nhms_cold` are owned by #1894. #1892
  must not create them on the live cluster.

### Frozen 2.10.2 movement sequence

Pinned image identity is the live `nhms-db` image
`sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e`
(`timescale/timescaledb-ha:pg15-latest`), PostgreSQL 15.2, TimescaleDB 2.10.2.
The isolated probe on the pinned 2.10.2 image freezes **exactly one**
accepted sequence: `shell_first_decompress_recompress_atomic`.

In one explicit transaction with finite `SET LOCAL lock_timeout` /
`statement_timeout`. The isolated probe uses `2s` / `30s` only because its
fixture is tens of kilobytes; those values are not production budgets for
gigabyte relations. #1893 owns configurable lock/statement/wall budgets sized
for a full cold rewrite plus WAL.

1. lock origin and compressed heaps in OID order and revalidate;
2. `ALTER TABLE <origin> SET TABLESPACE nhms_cold` plus every origin
   index in OID order (never ALTER the compressed heap or TOAST);
3. `decompress_chunk(origin)` and prove the expanded origin/index/TOAST
   group is entirely cold;
4. `compress_chunk(origin)` and prove the **new** complete compressed
   group is entirely cold with unchanged data parity;
5. commit, then a **fresh** connection readback. Test rollback also uses
   a fresh observer.

Measured properties of this sequence:

- It is a full cold-device rewrite, not metadata-only. Preflight must
  reserve cold expansion/rollback headroom and hot PGDATA/WAL headroom
  while the original compressed hot bytes remain until commit.
- After the shell move, a transient mixed state is legal **only inside
  the transaction** (the compressed index may remain hot until
  `decompress_chunk` drops the old sibling).
- Recompression creates a new compressed sibling OID/name. Durable
  identity is hypertable + origin OID/name + range; sibling identities
  are recorded before/after separately.
- Expanded uncompressed bytes land in `nhms_cold`, not `pg_default`.
- Direct `ALTER` of the compressed heap remains
  `FeatureNotSupported: changing tablespace of compressed chunk is not
  supported` and is not needed.
- Data parity is scoped to the durable origin window
  `[range_start, range_end)` over every business column. #1892 proves this on
  its four-column disposable fixture; that token is not the production
  inventory. #1893 must derive and validate every real business column for
  both production hypertables from the live schema before mutation. A
  replacement compressed sibling at source is `unknown`, not
  `complete_source`.
- WAL observations are instance-level `pg_wal_lsn_diff` from `0/0`, not
  per-group WAL volume. Catalog relation bytes remain the primary
  group-accounting unit.
- Capacity preflight is `required_cold = before_compression_total_bytes +
  operator cold reserve` and `required_hot = operator WAL reserve`; the
  original compressed source remains allocated until commit and is not
  added to free bytes. #1893 owns production reserve values; the probe
  does not invent production defaults.

Rejected alternatives (not fallback lanes):

1. `timescaledb_experimental.move_chunk` is a procedure, cannot run
   inside a transaction, and on this single-node image errors with
   `function must be run on the access node only`.
2. Direct compressed-heap / TOAST ALTER or LOCK is refused.
3. `attach_tablespace('nhms_cold', internal compressed hypertable)` does
   not route new compressed chunks and must not attach either business
   hypertable.
4. Decompress-first is atomic but expands the origin on the hot device.
5. Two-transaction move-then-recompress leaves a committed uncompressed
   window.

A group is the origin heap + compressed heap + every index + every owned
TOAST heap/index reachable from both. Moving only the origin shell does not
prove compressed bytes left `/home` at a **terminal** boundary. `already_cold`
is a lock-only no-op. Mixed residency is never a terminal success.

`CREATE TABLESPACE` / `DROP TABLESPACE` are cluster-scoped. A throwaway
database inside live `nhms-db` is not isolation. The probe must refuse live
container `nhms-db`, port 55432, PGDATA `/home/nwm/nhms-pgdata`, and any
production checkout/data root.

Do **not** `attach_tablespace('nhms_cold')` to either business hypertable.
New chunks stay in `pg_default`.

### RAID, SMART, and backup gates (production, not this probe)

Re-admitting `/dev/md0` for terminal compressed DB storage requires
root-generated `mdadm --detail` plus SMART PASS evidence for **both** member
devices, with no degraded/rebuild/recovering/unknown state. `/proc/mdstat
[UU]` or a successful mount alone is insufficient. A PGDATA-only backup is
incomplete once any external `pg_tblspc` target exists; backup readiness
must cover PGDATA and every tablespace location. #1894 owns the installer
and those gates; this amendment only freezes the policy.

### Lifecycle contract measured by the isolated probe

Legal state flow is `hot-uncompressed -> hot-compressed -> cold-compressed
-> cold-uncompressed-replay -> cold-compressed`. Cold decompression writes
into the origin's tablespace. Replay stays there. Recompression placement
is engine-defined and must be measured; if any member lands hot, the same
serialized tick converges the group or reports mixed/recovery. `drop_chunks`
must leave no origin/compressed/index/TOAST catalog or files.
