# Design: river-backfill-receipt-observability

## Change surface

`scripts/node27_river_identity_backfill.py` @ origin/master 5188a32e
(issue cites 5f7af41d; verified unchanged): `execute_batch` :744-803
(SET LOCAL statement_timeout :773, count+UPDATE :774-775, except → 57014
check :778-783); `_is_query_cancelled` :824-829; shortfall stop
`_run_enforce` :1143-1174 (reason text :1159-1165; diagnostic counts
already in stop detail); halving/duration_wall `_run_one_batch_with_retry`
:1282-1339; `_guarded_batch` :1273-1279 (wraps execute_batch);
`_accumulate` :1506-1516 (`int(descriptor.get(key) or 0)` folds None→0);
`_chunk_descriptor` :962-985 (pending_rows None default + honest-null
docstring). Schema: stop.stage enum
`schemas/river_identity_backfill_receipt.schema.json:268-276` (4 stages,
description says "Only the stages the runner can actually emit");
per-chunk pending_rows :167-171 (null = NOT MEASURED); totals.pending_rows
:253 (bare integer, no description). Runbook stop table
`docs/runbooks/tier-node27-timeseries-storage.md:1950-1960` ("Four
causes, distinguished in stop.stage").

Risk triage: compact fixture. All three are observability-surface edits;
the only control-flow change is the new lock-contention classification
path (a NEW stop reason on error classes that today either alias to
duration_wall (55P03, dormant until lock_timeout exists) or escape
unclassified (40P01, live)). Highest risks: (1) disturbing the existing
57014 → halving → duration_wall path (must be byte-unchanged), (2) stop
receipts failing schema validation if the runner emits a stage the enum
lacks (add enum member and runner arm together), (3) the shortfall
message change breaking existing message-anchored assertions.

## Key decisions

1. **Shortfall signature is message+doc only; predicate untouched.** In
   `_run_enforce` :1157-1174, when `outcome.unmatched_rows == 0 and
   outcome.unmappable_rows == 0`, append a sentence to the reason text:
   the double-zero signature most likely indicates a concurrent DELETE
   between the count and the UPDATE (two READ COMMITTED snapshots) —
   re-check the parser re-parse window before escalating as data
   corruption. Non-double-zero message unchanged. The structured stop
   fields (`unmatched_rows`/`unmappable_rows`) already carry the
   signature machine-readably; no new receipt field.
   CHECK FIRST: grep existing tests for assertions on the shortfall
   reason text — if any assert on the full string, the fixture-review
   rule from #1320 applies (enumerate and get authorization; prefer
   substring assertions that survive the append).
2. **Lock contention: classify in `execute_batch`'s except as an `elif`
   arm AFTER the existing 57014 check at :778-783** (order is safe —
   disjoint SQLSTATEs, and 55P03/40P01 messages never contain the 57014
   fallback string — but after-placement keeps the 57014 arm literally
   byte-unchanged); raise a new `BatchLockContention(RuntimeError)`
   carrying the pgcode; `_run_one_batch_with_retry` catches it (both
   attempt sites: initial and halved), rolls back, and raises
   `BackfillStop("lock_contention", ...)` WITHOUT halving — halving
   reduces scan width, not lock wait; retrying against a held lock is
   the exact anti-pattern the duration-wall docstring warns about.
   Reason text carries pgcode + the distinct remediation (round-1 A
   final wording: pause the ingest writer and wait for an idle window —
   that pause is the whole remedy on terminal chunks, the only chunks a
   plain --enforce lock stop can occur on; --final-sweep's quiescence
   gate enforces the pause for the ACTIVE chunk only and is not a
   terminal-chunk remedy; lowering batch_pages or raising the duration
   wall will not help).
   New `_is_lock_contention(error)`: `getattr(error, "pgcode", None) in
   {"55P03", "40P01"}` (no message-string fallback — unlike 57014 there
   is no ambiguity channel to paper over; keep it narrow).
3. **57014 path byte-unchanged**: `_is_query_cancelled` body untouched;
   the new classification is an additional `elif` arm. Existing tests
   :266/:356/:375 (per issue) must stay green unmodified.
4. **Schema enum gains `lock_contention`**; stage description sentence
   stays true ("only the stages the runner can actually emit" — it now
   can). Runbook stop table: "Four causes" → "Five causes" + new bullet.
5. **Totals**: schema-only description on totals.pending_rows :253 —
   "Sums pending_rows over chunks MEASURED this invocation only; skipped
   chunks contribute nothing (their per-chunk value is null), so 0 here
   does not mean the table has no NULL sentinels left. Cross-check
   chunks_skipped_compressed / chunks_skipped_active." `_accumulate`
   body untouched (behavior already matches this description).
6. **lock_timeout NOT adopted** (recorded carve-out, follow-up issue
   labeled node-27): adoption changes live-batch behavior and requires a
   node-27 dry-run per the issue's own acceptance; local-only batch
   cannot honestly verify it. Consequence stated in runbook bullet: until
   lock_timeout is set, a pure lock WAIT still exhausts
   statement_timeout and reports duration_wall; only deadlocks (40P01)
   are classified today.

## Must preserve

- Shortfall fail-closed predicate and rollback/cursor-rewind semantics
  (:1143-1156) untouched; only the reason STRING gains a conditional
  sentence.
- 57014 → BatchDurationExceeded → one halved retry → duration_wall stop:
  byte-identical behavior and messages.
- Receipt schema backward compatibility: enum widening only (old
  receipts remain valid); no field type changes.
- `_accumulate` arithmetic unchanged (totals fix is description-only).
- Existing tests: zero modified assertions beyond the TWO fixture-review
  enumerated authorizations — (a) the exact stage-set guard
  tests/test_node27_river_identity_backfill_receipt.py:105-122 gains
  `lock_contention` (exactness NOT loosened; reachability leg untouched),
  (b) the receipt-shape test :86-102 is extended with totals/skip-counter
  assertions. Shortfall message anchors: fixture review verified NO test
  asserts on the reason string (grep zero), so the conditional append
  needs no authorization.
- `stop` object is schema-closed (`additionalProperties: false`; splat
  `{**stop.detail}` at :1468): pgcode lives in the reason string, never
  as a detail kwarg.

## Seams under test

Existing fakes in `tests/river_identity_backfill_fakes.py` — the
mechanism ALREADY exists (fixture-review verified): `FakeCursor.execute`
raises any handler return value that is an Exception instance
(:136-138), and `QueryCancelled` (:108-114) is just a class carrying
`pgcode = "57014"`. The 55P03/40P01 tests need only a sibling class (or
one parametrized by pgcode), used exactly like
tests/test_node27_river_identity_backfill.py:348/:367. Receipt shape
tests in `tests/test_node27_river_identity_backfill_receipt.py`.
Implementer notes (fixture-review): (a) `_run_probe` :1085 is a second,
unwrapped `execute_batch` call site — `BatchLockContention` escapes it
into main's generic handler → `build_failed_receipt(stage="runner")`,
same as `BatchDurationExceeded` today; not a regression, out of scope;
tests 4.3/4.4 must drive `process_chunk`/`_run_one_batch_with_retry`,
not probe mode. (b) clean "no halving" assertion: count of "UPDATE ONLY"
statements == 1 plus connection.rollbacks — `batches_run` reads 0 on a
lock stop (exception propagates before the :1140 increment), mirroring
duration_wall.

## Test plan (maps to acceptance)

1. Shortfall double-zero → reason contains the concurrent-DELETE
   signature sentence; stop stage still `shortfall`; rollback/cursor
   behavior unchanged.
2. Shortfall with unmatched>0 (or unmappable>0) → signature sentence
   ABSENT; existing message shape intact.
3. Fake cursor raising pgcode 55P03 during UPDATE → stop stage
   `lock_contention`, reason mentions lock and the idle-window/
   final-sweep advice, NOT the duration_wall advice; no halving attempt
   (assert batch not retried); receipt schema-validates.
4. Same for 40P01 (deadlock) — today's unclassified escape becomes
   `lock_contention`.
5. Existing 57014 tests untouched and green (per issue: :266, :356,
   :375).
6. Receipt shape: all eligible chunks zeroed + one skipped chunk →
   `totals.pending_rows == 0` AND skipped chunk's `pending_rows` null
   AND `chunks_skipped_*` non-zero — pinning that this combination is
   legal and documented (test name/docstring cites the schema
   description).
7. Schema: receipt with stage `lock_contention` validates; unknown stage
   still rejected.

## Risks to watch

- Fake cursor fidelity: psycopg2 raises `errors.LockNotAvailable` /
  `DeadlockDetected` with `.pgcode`; fakes must set `.pgcode` the same
  way the existing 57014 fake does (inspect before writing).
- `BackfillStop("lock_contention")` must flow through
  `_finalize_stopped_chunk` and receipt assembly identically to other
  stops (grep for stage-specific handling; expected: none).
- Runbook markdownlint (docs/** gate) after the table edit.
