# Close the last two unbound recovery-target copies (#1244)

## Why

After #1242/PR #1243 the recovery-target six-field contract has one
Python source (`contract.RECOVERY_TARGET_FIELDS`) with a widened drift
guard, but two copies remain outside the bound set (enumerated as known
remainders in that PR's narrowed claims):

1. capture's `_capture_catalog_post` SQL inlines all six values
   (scripts/node27_timeseries_compression_capture.py:445-452 at
   current master) — the truly silent copy: no test asserts the SQL
   text (the psql stub dispatches on the `capture:catalog_post` marker
   and returns a canned body), so a one-sided mutation ships green
   through every suite and only dies on the armed replay lane at
   live_evidence:3209-3211 / :3447-3452.
2. the verifier's inline expected decompress argv tail
   (scripts/node27_timeseries_compression_live_evidence.py:661-678,
   `_validate_exact_command_argv`, kind `decompress`) — the third
   live_evidence copy beyond the guard-bound `RECOVERY_TARGET` /
   `RECOVERY_RETURN_RELATION` constants (:201-209). Not silent (a
   one-sided flip is caught by the bundle fixtures, per PR #1243's
   final-review flip tests), but not single-sourced: a retarget must
   hand-edit it and its test fixture
   (tests/test_node27_timeseries_compression_live_evidence.py
   :1009-1020) in lock-step.

## What Changes

Issue's recommended route — derive + freeze both, zero behavior change:

1. Capture: interpolate the six fields from the already-derived
   `RECOVERY_TARGET` dict into the catalog_post SQL, extracted to a
   module-level `CATALOG_POST_SQL` constant (same derivation pattern
   as `RECOVERY_PREFLIGHT_SQL`, placed after `_CATALOG_BODY_SQL` which
   it references), with a byte-freeze test proving the rendered string
   is byte-identical to today's literal and that
   `/* capture:catalog_post */` remains the verbatim leading token
   (the test psql stub matches by substring containment, so the hard
   constraint is marker-verbatim + uniqueness among stub responses;
   the leading-token assertion is a byte-freeze detail).
2. Verifier: build the expected decompress argv tail from the
   verifier's OWN `RECOVERY_TARGET` constant (:201-208) — no new
   import of the contract module (the issue pins this; the verifier's
   constant is already guard-bound to the contract, so the binding
   chain closes transitively). The `len(argv) != 20` cardinality check
   and every other kind's contract stay untouched.
3. Rebuild the live_evidence decompress argv test fixture from the
   same source (no independent literal copy).
4. Guard/test additions: catalog_post byte-freeze (capture tests);
   argv-tail derivation case (live_evidence tests — feed a
   RECOVERY_TARGET-derived argv, assert accepted; deviation-rejection
   PARAMETRIZED over all six fields, each raising `EvidenceError`);
   drift-guard docstring updated (the "KNOWN UNBOUND" paragraph
   becomes false and is replaced by the new closure statement). The
   pre-existing e2e `test_real_state_machine_bundle_verifies_task_4_5_pass`
   stays untouched as the independent non-derived oracle (plan_author
   literals through the real `verify_bundle` path). Red proof: a
   contract-side single-field flip turns the widened drift guard red
   AND the catalog_post freeze red; an evidence-side flip turns the
   drift guard AND that e2e red — restored after.
5. Contract source-of-truth comment ledger updated (comment text
   only): both copies move into the bound list; the one remaining
   named copy becomes replay.py's `TARGET`/`TARGET_RELATION`
   (:24-32, not fully inert — `TARGET` is the default parameter at
   :138), with a tracked follow-up issue filed before merge.

Values unchanged everywhere; `packages/` does not import `scripts/`;
the verifier gains no new imports; no packages logic change.

## Non-goals

- Changing any recovery-target value; runtime target selection; schema
  edits (consts stay).
- Touching #1242/#1243 deliverables (`RECOVERY_TARGET_FIELDS`, schema
  consts, supervisor `target_args`, `RECOVERY_PREFLIGHT_SQL`).
- Other kinds in `_validate_exact_command_argv` (pg_dump/pg_restore_*/
  migration_apply/compression_*/benchmark_*).
- #1240 (INVOCATION_ARGV island) and #1090 (RECORD/EXEC docker argv
  split).
- Binding replay.py's `TARGET`/`TARGET_RELATION`
  (scripts/node27_timeseries_decompression_replay.py:24-32, NOT fully
  inert — `TARGET` is the default parameter at :138): named in the SoT
  ledger with a tracked follow-up instead of bound here.
