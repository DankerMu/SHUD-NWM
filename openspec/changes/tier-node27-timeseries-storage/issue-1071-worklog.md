# Worklog: #1071 — node-27 retention Step B preparation

**execution_mode**: live preparation; no production retirement
**Node**: node-27 (`210.77.77.27`, active primary DB + ingest + display)
**Date**: 2026-07-19 UTC
**Deployed baseline SHA**: `8485f36d66e4aaf4219f76a42089b530ff2f4d4f`

## Goal

Prepare node-27 for the first archive rebuild drill and retention dry-run on
2026-07-25 while keeping every mutation switch disabled. Resolve any live gate
that would otherwise make the scheduled Step B run impossible.

## Preparation completed

- Verified the canonical archive root and current inventory receipt:
  1,769 complete windows (825 forcing, 944 runs), including 228 verified
  db-export forcing windows.
- Confirmed no product-archive forcing object is eligible yet. The earliest
  live forcing package ends on 2026-06-24, so the 30-day safety invariant first
  permits it after 2026-07-24.
- Changed node-27's private product-archive env from 45 to the allowed 30-day
  minimum; the recurring service remains dry-run because its `ExecStart`
  carries no `--enforce`.
- Explicitly pinned retention to `NODE27_TIMESERIES_RETENTION_ENFORCE=0`,
  retained the five-chunk bound, and configured the future drill receipt path.
- Created a mode-0600 archive-rebuild-drill env with production readonly
  (`nhms_display_ro`), isolated `nhms_archive_drill`, and `postgres` admin DB
  identities. The wrapper boot/config check passes; no staging DB was created.
- Verified user-timer order: product archive 03:20 UTC, inventory audit 03:40,
  compression 04:25, retention 05:15.
- A 30-day product-archive dry-run found 471 non-forcing candidates (180 runs,
  291 states), 1,512,451,378 source bytes, zero discovery failures, and zero
  archived bytes. This was read-only.

## Live gate corrected before Step B

The first Timescale chunks physically start at 2026-05-28 while the truthful
inventory coverage begins at the first real data window, 2026-05-31T06:00Z.
The former global bounds check therefore refused every future tick with
`COMPLETENESS_RECEIPT_BOUNDS_INSUFFICIENT`, even though later chunks were fully
evidenced.

The runner now keeps a boundary-partial chunk in `deferred_remainder`, advances
only chunks wholly inside completeness bounds, and bounds each `drop_chunks`
call with both `newer_than` and `older_than`. Verified db-export tuples also
participate in the forcing recovery union while retaining their independent
gate. A disposable Timescale database proved that the bounded call dropped the
single target middle chunk and preserved the older chunk; the scratch database
was removed immediately.

## Verification

- `uv run pytest -q tests/test_node27_timeseries_retention.py` — 105 passed,
  1 skipped.
- Targeted storage/governance suite — 261 passed, 1 skipped.
- `uv run ruff check .` — PASS.
- `openspec validate tier-node27-timeseries-storage --strict --no-interactive`
  — PASS.
- Disposable node-27 Timescale exact-drop probe — PASS (`before=3`,
  `dropped=1`, `after=2`, `oldest_preserved=true`); scratch DB removed.

## Remaining gate

No production product was archived or retired and no production DB chunk was
dropped. On 2026-07-25 the scheduled workflow must first review a fresh
product-archive dry-run, then produce real forcing/runs archives, refresh the
completeness receipt, execute the isolated drill, and stop after retention
dry-run for human review. #1072 enforce remains prohibited until that later
review receives explicit human go.
