# Tasks: river-backfill-receipt-observability

## 1. Implementation (scripts/node27_river_identity_backfill.py)

- [ ] 1.1 Shortfall reason text: append the concurrent-DELETE
      double-snapshot signature sentence when `unmatched_rows == 0 and
      unmappable_rows == 0` (predicate, rollback, cursor rewind, stop
      fields all untouched). Check existing tests for full-string
      message anchors first; substring assertions preferred.
- [ ] 1.2 New `_is_lock_contention` (pgcode in {"55P03","40P01"}, no
      message fallback) + `BatchLockContention` exception raised from
      `execute_batch`'s except arm (57014 check byte-unchanged).
- [ ] 1.3 `_run_one_batch_with_retry`: catch `BatchLockContention` at
      both attempt sites → rollback → `BackfillStop("lock_contention",
      ...)`; the SQLSTATE/pgcode and the idle-window/final-sweep advice
      go in the reason STRING only — `stop` is schema-closed
      (`additionalProperties: false`, splat at :1468), so detail kwargs
      stay within chunk_schema/chunk_name/first_page/last_page. NO
      halving.
- [ ] 1.4 Do NOT add `SET LOCAL lock_timeout` (carve-out — follow-up
      issue labeled node-27; see design decision 6).

## 2. Schema (schemas/river_identity_backfill_receipt.schema.json)

- [ ] 2.1 stop.stage enum += `lock_contention`.
- [ ] 2.2 totals.pending_rows gains the measured-chunks-only
      description (design decision 5); type unchanged.

## 3. Runbook (docs/runbooks/tier-node27-timeseries-storage.md §4.6.2)

- [ ] 3.1 Stop table: "Four causes" → "Five causes"; new
      `lock_contention` bullet with distinct remediation + the "until
      lock_timeout is adopted, pure lock waits still report
      duration_wall" caveat.
- [ ] 3.2 `shortfall` bullet: document the double-zero signature and
      the check-parser-re-parse-window-first step.

## 4. Tests

- [ ] 4.1 Shortfall double-zero → signature sentence present; stage
      `shortfall`; behavior unchanged (rollback + next_page rewind).
- [ ] 4.2 Shortfall with non-zero diagnostics → signature ABSENT.
- [ ] 4.3 Fake cursor pgcode 55P03 → stage `lock_contention`, distinct
      advice, no halving (batch not re-attempted), receipt validates.
- [ ] 4.4 Fake cursor pgcode 40P01 → same classification.
- [ ] 4.5 Existing 57014 tests green unmodified
      (tests/test_node27_river_identity_backfill.py :266/:356/:375).
- [ ] 4.6 Extend the EXISTING receipt-shape test at
      tests/test_node27_river_identity_backfill_receipt.py:86-102
      (already builds compressed+active+terminal-zero-pending) with the
      `totals["pending_rows"] == 0` and non-zero `chunks_skipped_*`
      assertions. Naturally green (the totals change is
      description-only) — label honestly, no red evidence exists for it.
- [ ] 4.7 Schema test: stage `lock_contention` validates; unknown stage
      rejected.
- [ ] 4.8 AUTHORIZED existing-test modification (fixture-review P1
      enumeration):
      tests/test_node27_river_identity_backfill_receipt.py:105-122
      `test_stop_stage_enum_contains_only_stages_the_runner_can_emit` —
      the expected exact stage set gains `lock_contention`; the
      source-reachability leg (:119-122) needs no change (the runner
      emits the literal). Together with 4.6's extension, these are the
      ONLY authorized existing-test changes; the exactness assertion
      must NOT be loosened.

## 5. Spec delta

- [x] 5.1 MODIFIED requirement in
      `specs/river-identity-normalization/spec.md` (byte-faithful +
      appended observability sentence + 3 scenarios).

## 6. Evidence Floor

- [ ] 6.1 `uv run pytest -q tests/test_node27_river_identity_backfill.py
      tests/test_node27_river_identity_backfill_receipt.py` green.
- [ ] 6.2 Red evidence: new lock-contention and signature tests fail
      against unmodified source (honest labels for any naturally-green
      pins).
- [ ] 6.3 `uv run ruff check .` passes (per issue Verification field).
- [ ] 6.4 `openspec validate river-backfill-receipt-observability
      --strict --no-interactive` passes.
- [ ] 6.5 Zero modifications to existing test assertions beyond the two
      enumerated authorizations (4.6 extension, 4.8 stage-set update);
      57014 path byte-unchanged (diff inspection).
- [ ] 6.6 Follow-up issue for `SET LOCAL lock_timeout` adoption +
      node-27 dry-run filed and referenced in the PR body.
