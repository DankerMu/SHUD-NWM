# §8.6 item 5 tick-bracket criteria + derived grep anchor (#1215)

## Why

Runbook §8.6 item 5 is the operator's ONLY decision procedure for a
`freed_bytes: 0` in an `enforced` receipt, and both of its legs are half a
notch off (found by PR #1212 round-5 verifier side-sweep, both CONFIRMED,
deferred as non-blocking):

- R1 (doc precision): the bracket-selection criterion — "the `start` line
  naming the receipt under investigation" — cannot select a unique bracket
  under the shipped configuration, because
  `infra/env/node27-timeseries-retention.example` pins
  `NODE27_TIMESERIES_RETENTION_RECEIPT_PATH` to one fixed path and the
  wrapper echoes that same path in every tick's start/done lines. The text
  gives no tick-correlation key at all. Additionally the second criterion
  (chunk in `dropped_chunks[]`) does not block the refuse-then-retry window:
  tick N warns about chunk X then refuses; tick N+1 genuinely measures 0 and
  drops X — the stale tick-N warning satisfies both criteria. Misread
  direction is conservative (an extra reconciliation pass), a precision
  defect, not a safety one.
- R2 (anchor hardening): `_MEASURE_WARNING_GREP_FENCE` in the retention test
  file is an independent literal, not derived from
  `_MEASURE_WARNING_GREP_TOKEN`. A full rename campaign (warning string in
  the runner, `_MEASURE_WARNING`, the token, §8.2.1) is forced through by
  existing red tests at every step — except the fence and §8.6's grep line,
  which go stale together and stay self-consistent, so
  `test_measure_warning_byte_identical_with_runbook` stays green while the
  operator gets a grep the runner no longer emits.

Fixture-level note: triaged **compact / low** — docs two sentences plus a
one-line test derivation; hard acceptance criterion that `scripts/` has zero
diff (both the retention runner and the wrapper). The DROP/refuse vocabulary
in the issue lives in unchanged production code and in documentation OF that
code. `design.md` exempt at this level.

## What Changes

- `docs/runbooks/tier-node27-timeseries-storage.md` §8.6 item 5:
  - replace "the `start` line naming the receipt under investigation" with
    a `generated_at` correlation rule: pick the bracket whose `start`/`done`
    timestamps (wrapper `ts()`, UTC ISO-8601) contain the receipt's
    `generated_at` (schema-required, `format: date-time`); state explicitly
    that the shipped env pins the receipt path
    (`infra/env/node27-timeseries-retention.example`) so the path alone
    CANNOT discriminate ticks, and warn that a `start` line without a
    matching `done rc=` (tick in flight / wrapper died) or a receipt-less
    `rc=2` config-refused tick brackets a tick that wrote no receipt —
    fixture-review P1: an unconditional LAST-bracket rule would point at
    those and the misread there would NOT be conservative;
  - add the refuse-then-retry caveat: a prior tick that refused with
    `RETENTION_DROP_FAILED` may have warned about a chunk that THIS tick
    genuinely measures as 0; the second criterion does not exclude it, and
    the misread direction is conservative (one extra reconciliation).
- `tests/test_node27_timeseries_retention.py`:
  `_MEASURE_WARNING_GREP_FENCE = f"grep '{_MEASURE_WARNING_GREP_TOKEN}'"` —
  derived, keeping the existing comment about why the quotes matter.

## Non-goals

- Zero production change: `scripts/node27_timeseries_retention.py` and
  `scripts/node27_timeseries_retention_once.sh` zero diff.
- No change to §8.2.1, receipt schema, gate logic, or receipt structure.
- The receipt-path convention split (fixed path vs per-run basename in the
  receipts README) is explicitly out of scope — pre-existing, needs its own
  decision (issue #1215 records it; do not resolve here).
- No byte-identity anchor for the wrapper start/done wording. The
  `generated_at` correlation clause relies on the wrapper's `ts()`
  timestamps, but the runbook already cites
  `scripts/node27_timeseries_retention_once.sh:143,151` unanchored today, so
  the clause adds no NEW unanchored surface — remains individually
  trackable (recorded in issue #1215's out-of-scope list).
