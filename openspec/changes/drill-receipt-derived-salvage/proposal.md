# Proposal: drill-receipt-derived-salvage

Issue: #1177 · Fixture level: standard · Suggested by issue readiness: implementation-ready

## Why

The retention gate's demand set is mechanically derived from the completeness
receipt (`derive_salvage_backed_windows`: every `coverage=="db-export" and
verdict=="complete"` subject window intersecting the drop window — 19 windows
in the 2026-07-27 live run), but the drill's evidence set is an
operator-typed CLI whitelist (`--salvage-manifest`, 5 manifests that day).
The two sets share no source and are never cross-checked, so the drill emits
`verdict=PASS` receipts that say nothing about the demand set. Each miss
surfaces only later as a bare `DRILL_COVERAGE_DB_EXPORT_MISSING` at the
retention gate, and because the gate reads a single `latest-pass-receipt.json`
(no cross-run accumulation) while a drill run is expensive (staging DB
drop/create/migrate + full restore), unlocking retention degenerates into
guess-a-manifest retry loops. Live evidence: the 2026-07-27 rerun added the
`forc_ifs_2026061406` manifests while the actual gap was `forc_gfs_2026061406`
— one full drill wasted and a false "permanently unsatisfiable" conclusion.

## What Changes

- `scripts/node27_archive_rebuild_drill.py` gains `--completeness-receipt
  <path>`: derive the salvage-manifest set from the receipt's
  `coverage=="db-export" and verdict=="complete"` subjects, mapped to
  `<archive_root>/db-export/<lane>/<identity>/manifest.json`, following the
  receipt-as-sole-source precedent of `scripts/node27_db_export_salvage.py`.
- Optional `--drop-window <start> <end>` (or equivalent) filters derived
  subjects to those overlapping the drop window using the gate's own
  closed-interval `_overlaps` convention (boundary-touching and zero-length
  intersections stay in scope), so a 228-manifest archive does not force a
  full-set drill. The receipt records the drop window used for derivation.
- A **new** bound on the derived set (cardinality and aggregate decompressed
  bytes) is added and enforced fail-closed — the drill today only has the
  per-object `MAX_SALVAGE_OBJECT_BYTES` cap, nothing bounds set size.
- Fail-closed derivation gaps: a derived subject whose manifest file is
  missing or unreadable fails the drill (FAIL receipt naming the paths), as
  does a manifest whose selector window differs from the receipt subject's
  window (stale-manifest divergence) — never a silent PASS on a narrower or
  drifted set. Receipt identities are path-safety-guarded before joining into
  archive paths (mirroring `_refuse_unsafe_identity` in the salvage sibling);
  the `runs` lane is handled alongside `forcing`, and `states`-lane subjects
  (no db-export lane exists for them) are explicitly refused/skipped with
  evidence, never silently dropped.
- `--salvage-manifest` remains supported; explicit paths are unioned with the
  derived set (deduped by resolved path) and the receipt records each input's
  provenance (`derived` vs `explicit`).
- `scripts/node27_archive_rebuild_drill_once.sh` passes the new flags through
  (env-configurable like its existing knobs).
- Runbook `docs/runbooks/tier-node27-timeseries-storage.md` §7.5 and the
  invocation example replace "full set of salvage manifests" with the
  executable receipt-derived procedure.

## Non-Goals

- Gate semantics from #1162/PR #1174 (`derive_salvage_backed_windows`,
  `_drill_covers`, `_clip_to_drop`) are untouched.
- Retention refusal payload locatability is #1175, not here.
- Completeness-audit db-export matching
  (`scripts/node27_storage_inventory_audit.py`) is correct as-is per the
  issue's falsification record — the subject window equals the manifest
  selector window by full-JSON canonical equality; do not "fix" it to actual
  row extents.
- The archive-manifest (product-archive) drill leg is unchanged.

## Impact

- Affected specs: `archive-rebuild-drill` (delta: salvage input derivation).
- Affected code: `scripts/node27_archive_rebuild_drill.py` (CLI/config
  assembly ~:1918-1945 — note :1941-1944 refuses invocations with no
  manifest args, which a receipt-only invocation must satisfy; salvage
  traversal ~:1480-1500),
  `schemas/archive_rebuild_drill_receipt.schema.json` (top-level
  `additionalProperties: false`, validated before write at drill :1241 — any
  new receipt field requires a schema change),
  `scripts/node27_archive_rebuild_drill_once.sh` (likely zero change: the
  wrapper is a bare `exec "$@"` passthrough and config knobs are env vars;
  the sibling already reads `NHMS_ARCHIVE_COMPLETENESS_RECEIPT_PATH`),
  `tests/test_node27_archive_rebuild_drill.py`,
  `docs/runbooks/tier-node27-timeseries-storage.md` (~:1424, :1493-1498).
- Ops: drill invocations gain a mechanical, receipt-anchored input procedure;
  retention unlock stops depending on operator luck.
