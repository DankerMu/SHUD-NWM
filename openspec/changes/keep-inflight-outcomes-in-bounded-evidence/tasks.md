# Tasks: keep-inflight-outcomes-in-bounded-evidence

## Risk triage

- Fixture level: **compact**. One pure function, no I/O, no schema migration; but the
  surface is an **observability floor already relied on by a P0 investigation**
  (#1748) and by #1749's deployment receipt, so a silent regression here is
  expensive and invisible.
- Risk packs selected: `correctness-silent-miss` (a dropped lane reads as "zero
  events", indistinguishable from "no events"), `test-oracle-integrity` (the existing
  suite passed while half the requirement was unimplemented — the oracle must now
  bite on the absent lane).
- Not selected: security, performance, migration — no external input, no persistence
  change, payload strictly grows by <7 KB.

## 1. Implementation + regression

- [ ] 1.1 Make `_compact_bounded_restart_reconcile` symmetric across `inflight` and
  `reserved_unbound`. Both lanes' `outcomes` filtered through
  `_BOUNDED_RESTART_RECONCILE_OUTCOME_KEYS`; a lane absent from the source stays
  absent from the output.
- [ ] 1.2 Red-first regression: a bounded payload carrying `inflight.outcomes` with an
  `identity_mismatch_blocked` row and a non-zero `identity_blocked_streak`. Must fail
  on current code (key absent) and pass after.
- [ ] 1.3 Pin the absence direction: a source payload with **no** `inflight` key must
  not grow one; a source with `inflight` but no `outcomes` must not fabricate an empty
  list where the old shape had none.
- [ ] 1.4 Confirm no existing bounded-evidence test asserted the old (asymmetric)
  shape. If one did, it pinned the defect — record it as a deviation rather than
  quietly editing it.

## 2. Verification floor

- [ ] 2.1 `uv run pytest -q tests/test_scheduler_evidence*.py tests/test_production_scheduler.py` PASS
- [ ] 2.2 `uv run ruff check .` PASS
- [ ] 2.3 `openspec validate keep-inflight-outcomes-in-bounded-evidence --strict --no-interactive` PASS
- [ ] 2.4 Size sanity: the bounded artifact still fits `max_evidence_bytes`; the added
  lane is bounded by the same per-outcome key filter as the existing one.
