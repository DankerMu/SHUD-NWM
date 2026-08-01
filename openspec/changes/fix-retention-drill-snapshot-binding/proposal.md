# Drill/completeness snapshot binding before irreversible DROP CHUNK (#1220)

## Why

`check_drill_gate`'s requirement set (salvage-backed windows) comes from the
completeness receipt the GATE loads at judgment time, while its evidence set
comes from a drill receipt up to 30 days old — and nothing binds the two.
The completeness receipt is rewritten in place daily (03:40 UTC audit
timer); a db-export subject B added after the drill (backfill/rerun
selector) enters the requirement set without ever having been
restore-verified, and because coverage tuples carry no subject identity and
the db-export leg judges against the tuple union, an old subject A's wide
tuple vouches for B: empty reasons → PASS → irreversible DROP CHUNK on rows
whose salvage manifest was never verified. The issue's in-memory replay
pins the exact flip (v1 snapshot: A only; v2 at gate time: A + B; un-narrowed
drill; gate returns `[]` against v2, and the two controls isolate the cause
to snapshot drift). This is the third false-PASS path in the family:
#1162 closed window-scoping, #1207 closed operator narrowing (its D5-b
explicitly records THIS defect as out of scope), and this change closes
snapshot drift.

## What Changes

Requirement-set binding (issue's recommended option, with the triage
decision resolved in design D1 — strict digest/`generated_at` binding is
REJECTED by arithmetic, it would collapse the 30-day drill budget to <1 day):

- `scripts/node27_archive_rebuild_drill.py` (emit side): when the drill
  derives salvage inputs from a completeness receipt, `salvage_derivation`
  additionally records
  - `db_export_windows`: the full universe of `{start, end}` windows of
    subjects with `coverage == "db-export"` AND `verdict == "complete"` in
    the consumed completeness receipt — normalized exactly like
    `derive_salvage_backed_windows` (string endpoints, deduped, sorted),
    but NOT filtered by any drop window (conservative superset, design D2);
  - `completeness_generated_at`: the consumed receipt's `generated_at` —
    diagnostics only, never a refusal input (design D1).
- `schemas/archive_rebuild_drill_receipt.schema.json`: both fields OPTIONAL
  inside `salvage_derivation` (`additionalProperties: false` block gains
  two properties; `required` unchanged) — receipts written between #1206
  and this change stay schema-valid and keep current gate behavior.
- `scripts/node27_timeseries_retention.py` (gate side): new wire code
  `DRILL_COMPLETENESS_SNAPSHOT_UNBOUND` + helper `_drill_snapshot_binds`.
  Inside the db-export leg, after the existing empty-derivation refusal and
  before the per-target coverage loop: every salvage-backed target window
  derived from the CURRENT completeness receipt must be a member (exact
  normalized `{start, end}` pair) of the drill's recorded
  `db_export_windows`; any target the drill never saw refuses fail-closed.
  Section absent → skip (explicit-manifest / pre-#1206 receipts, unchanged);
  section present but `db_export_windows` key absent → skip (post-#1206
  pre-binding receipts, recorded residual); key present but shape unusable →
  refuse (defence-in-depth, symmetric with #1207's D2).
- Wire code four-surface sync in the same commit: constant + `WIRE_CODES`,
  registry tests (`_EXPECTED_WIRE_CODES` + count 16→17), runbook §8.2 table
  + priority chain (between `DRILL_COVERAGE_RUNS_MISSING` and
  `DRILL_COVERAGE_DB_EXPORT_MISSING`), design fixture #855 block + H2.
- `docs/runbooks/tier-node27-timeseries-storage.md` §7.5: record the
  binding rule and its operator consequence (a drill receipt stops binding
  when a NEW db-export window appears — rerun the drill; daily completeness
  regeneration WITHOUT new windows does not invalidate it); residual
  paragraph updated (D5 of `fix-retention-drill-window-guard` (b) is now
  closed at window granularity; what remains is layer-2 subject identity).

## Non-goals

- Layer-2 per-subject tuple attribution (root fix for the whole family) —
  separately scheduled per #1207/#1220 meta.
- Refusing on `completeness_generated_at` newer / file-digest inequality —
  rejected, design D1 (would force daily drill reruns).
- #1175 locatability (same code region; this change adds one guard and
  does not reshape existing reasons).
- `forcing`/`runs` whole-window union semantics (correct by design).
- Completeness-receipt schema changes (none needed).
