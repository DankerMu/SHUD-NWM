# Proposal: river-backfill-receipt-observability

## Why

Issue #1408 (PR #1403 round-1 DEFER + Phase 7 note): the river identity
backfill runner's receipt/observability surface misleads operators in
three ways — none corrupts data (the write path is fail-closed and
re-entrant), all misdirect diagnosis:

1. **Shortfall double-snapshot race**: `execute_batch` runs the candidate
   count and the UPDATE as two statements in one READ COMMITTED
   transaction (two snapshots). A concurrent committed DELETE between
   them (real channel: `workers/output_parser/parser.py:747-755`
   re-parse deletes on terminal chunks — the runner's main workload)
   fabricates `shortfall > 0`, stopping the whole invocation, and the
   runbook routes shortfall to "data corruption, escalate". The race has
   a cheap discriminating signature: `shortfall > 0 ∧ unmatched == 0 ∧
   unmappable == 0` (both diagnostic counts run AFTER the update, so
   deleted rows are already out of range; genuine rot has at least one
   non-zero).
2. **Lock waits aliased into duration_wall**: only `statement_timeout`
   is set per batch; a lock-blocked UPDATE and a slow UPDATE both
   surface as SQLSTATE 57014 → `BatchDurationExceeded` → halving →
   `BackfillStop("duration_wall")`, whose remediation advice (lower
   batch_pages / raise the wall) is wrong in both directions for lock
   problems. Deadlock 40P01 today escapes as an unclassified exception.
3. **`totals.pending_rows` folds null to 0**: `_accumulate` reads a
   skipped chunk's `pending_rows = None` as 0, so a receipt can show
   `totals.pending_rows == 0` while skipped chunks still hold NULLs —
   the per-chunk schema explicitly forbids fabricating 0 for unmeasured
   chunks, but the totals field has no such semantics.

## What Changes

Issue's recommended (cheapest) route for all three, with one recorded
carve-out:

- **Shortfall**: no predicate change — fail-closed stays. The
  `BackfillStop("shortfall")` reason text appends the concurrent-DELETE
  signature note when (and only when) `unmatched == 0 ∧ unmappable == 0`;
  runbook §4.6.2 stop table documents the signature and the "check the
  parser re-parse window before treating as corruption" first step.
  Tests pin both the double-zero and the non-zero message shapes.
- **Lock attribution**: SQLSTATE `55P03` (lock_not_available) and
  `40P01` (deadlock_detected) are classified into a NEW stop stage
  `lock_contention` (schema enum + runner + runbook fifth cause), with
  remediation advice "pause the ingest writer / wait for an idle window"
  (the --final-sweep quiescence gate noted as enforcing that pause on
  the ACTIVE chunk only — round-1 A wording) — distinct from
  `duration_wall`.
  No halving retry on lock contention (halving does not reduce lock
  waits). The existing 57014 path is byte-unchanged.
- **Carve-out (recorded, NOT delivered here)**: adopting `SET LOCAL
  lock_timeout` — the half that makes lock waits actually surface as
  55P03 — changes live-batch behavior and the issue's own acceptance
  requires a node-27 dry-run for it; this batch is local-only, so the
  adoption is routed to a follow-up issue labeled node-27 instead of
  faking a local PASS. Until then `55P03` classification is dormant
  (fires only if lock_timeout is set externally) and `40P01` is live.
- **Totals**: schema `description` on `totals.pending_rows` stating it
  sums only chunks measured this invocation (skipped chunks contribute
  nothing; 0 does not mean the table is clean), plus a receipt-shape
  test for "all eligible chunks zeroed + skipped chunk present". No
  type change (null-propagation alternative rejected by the issue:
  nearly every invocation would be null, less useful).

## Capabilities

- `river-identity-normalization`: MODIFIED requirement "river_timeseries
  identity columns SHALL have integer surrogate-key targets with an
  idempotent, bounded, receipted backfill" — appended observability
  sentences + new scenarios (shortfall signature, lock_contention stage,
  totals semantics). Byte-faithful otherwise.

## Impact

- `scripts/node27_river_identity_backfill.py`,
  `schemas/river_identity_backfill_receipt.schema.json`,
  `docs/runbooks/tier-node27-timeseries-storage.md` §4.6.2,
  `tests/test_node27_river_identity_backfill.py`,
  `tests/test_node27_river_identity_backfill_receipt.py`
  (`tests/river_identity_backfill_fakes.py` already supports raising
  arbitrary Exception instances from handlers — a sibling of
  `QueryCancelled` with pgcode 55P03/40P01 suffices, no new knob).
- Out of scope (issue boundary): batch transaction/isolation model
  (REPEATABLE READ / FOR UPDATE); `workers/output_parser/parser.py`
  delete semantics; shortfall predicate widening (alternative 1 —
  explicitly NOT adopted, fail-closed stance preserved); totals null
  propagation (alternative 2); #1340/#1341/#1342; `SET LOCAL
  lock_timeout` adoption (follow-up, node-27).
