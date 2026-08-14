---
status: archived
current_authority:
  - path: docs/runbooks/tier-node27-timeseries-storage.md
    section: "Retirement record: the cold archive lane is gone (2026-08-11)"
    reason: current node-27 timeseries storage-tier procedures
superseded_by: none
status_since: 2026-08-14
archive_scope: whole-document
retained_for: immutable live evidence of the retired archive lane
---

# Archive rebuild drill live receipts (task §5.2)

**Retired lane — read as history, not as procedure.** The node-27 cold archive
tier was permanently retired on 2026-08-11
([`docs/adr/0002-node27-timeseries-hot-cold-tiering.md`](../../../../adr/0002-node27-timeseries-hot-cold-tiering.md)
Revision 2026-08-11) after the `/dev/md0` double-disk failure, and #1370
deleted `scripts/node27_archive_rebuild_drill.py` together with its wrapper and env template.
The receipts in this directory are immutable evidence of runs that happened;
they cannot be regenerated, and no command described below is runnable today.
Retention now deletes chunks with no archive backstop — see
[`docs/runbooks/tier-node27-timeseries-storage.md`](../../../tier-node27-timeseries-storage.md)
§8.

This directory holds committed live receipts from
`scripts/node27_archive_rebuild_drill.py` on node-27 (staging DB
`nhms_archive_drill` on the primary Postgres at `127.0.0.1:55432`,
production opened read-only-pinned).

## Receipts

### `first-live-pass-20260725T053420Z.json`

First live PASS (2026-07-25, code `2b3ee31b`). Manifest set targets the
retention drop window `[2026-06-04, 2026-06-18]` (21-day window, cutoff
`2026-06-24T12Z`):

- **runs lane**: 15 daily gfs qhh cycles `2026060400..2026061800`,
  restored and reingested via `OutputParser.parse_run` against staging;
  every cycle matched the file-derived expected count exactly
  (274,344 rows = 1,633 output segments x 168 timesteps).
- **forcing lane**: 3 product cycles `2026061600..2026061800`, verified
  in **file-integrity mode** (counts items tagged `(file-integrity)`):
  legacy SHUD-package-only archives carry no domain-handoff payloads
  (never archived; source object-store dirs since removed by
  raw-retention), so a DB replay into `met.forcing_station_timeseries`
  is impossible for them — restore-time per-member sha256 verification
  plus a restored-file-set re-attestation is the whole product-lane
  claim. DB-truth coverage for the table comes from the db-export lane
  (the retention gate's `_drill_covers` unions db-export tuples into
  the forcing window check by design). See issue #1124; domain-bundle
  replay for post-cutover archives is tracked there and the drill
  fails closed if it meets a bundle archive.
- **db-export lane**: 4 salvage selectors
  (`forc_gfs_2026060100/2026060600/2026061106/2026061400_basins_qhh_shud`),
  verified by sha256 + decompressed per-selector row count.

Coverage union: runs `[2026-06-04, 2026-06-25]`; forcing product
`[2026-06-16, 2026-06-25]` + db-export `[2026-06-01, 2026-06-21]` —
both lanes cover the planned drop window.

Production isolation: the prod connection is pinned
`default_transaction_read_only = on` and asserted at open; the staging
database is DROPped + CREATEd + migrated from zero per run and dropped
after (verified absent post-run). Production hypertables are never
written.

## Live-only defects found and fixed on the way to PASS

Runs 1–6 of the drill each exposed a defect invisible to §5.1 unit
fixtures; all are fixed on master:

1. `#1121` (`1bfb1305`) — registry lift bound GENERATED ALWAYS columns
   (migration 000048 `stream_type`).
2. `#1122` (`5842d88b`) — expected segment count used physical
   `core.river_segment` rows (2x the declared contract on every
   network — duplicate seed imports, see #1123) instead of the
   production parser's `shud_output_river` subset oracle.
3. `#1124` (`f920fd68`, `2b3ee31b`) — forcing adapter signature drift
   (keyword-only `staging_conn` vs positional dispatch) and the
   handoff-payload archive-format assumption described above.
4. (`3f608321`) — staging parser repository used the 60s online
   statement timeout; full-run INSERTs under live-ingest contention
   need the 300s batch timeout.

## Reproduction

Env file at `/home/nwm/NWM/infra/env/node27-archive-rebuild-drill.env`
(mode 0600). Invocation wrapper `/home/nwm/run_drill.sh` builds the 44
manifest flags above and runs
`env PYTHONPATH=/home/nwm/NWM .venv/bin/python
scripts/node27_archive_rebuild_drill.py "${args[@]}"` under the drill
flock. Runtime ~30 minutes alongside live ingest.
