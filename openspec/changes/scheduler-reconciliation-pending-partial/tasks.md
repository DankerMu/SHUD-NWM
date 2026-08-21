## 1. Fixture and Invariant Gate

- [x] 1.1 Pass one read-only fixture review and strict validation before implementation.
- [x] 1.2 Preserve the governing invariant across producer, quality, pass counts, readiness recount, nested defer, durable no-op, and downstream stop surfaces.
- [x] 1.3 Keep Part A and Part B in one mergeable PR; record any implementation deviation.

## 2. Part A — Evidence and Readiness

- [x] 2.1 Add one governed reconciliation-status family consumed by candidate quality so all three tokens are non-success while failed classification remains false.
- [x] 2.2 Make producer partial counting include cycle/stage reconciliation statuses for attempted cycle-derived evidence.
- [x] 2.3 Add `reconciling` to the review-blocked pass vocabulary without adding it to passed or live-submitted compatibility.
- [x] 2.4 Make readiness partial and producer-partial predicates recognize all three tokens while blocked/failed/submitted inference remain false.
- [x] 2.5 Add direct truth-table tests for candidate quality and readiness outcome flags.
- [x] 2.6 Add a produced-artifact/readiness test: zero-submission reconciling row yields partial count one, public readiness blocked, and no status/cardinality errors.

## 3. Part B — Nested Defer State Machine

- [x] 3.1 Extend the nested defer family with `submit_result_ambiguous` and `reconcile_unverified` only.
- [x] 3.2 Add an explicit governed status-to-cycle-terminal mapping; duplicate skip maps to its skip terminal and reconciliation statuses map to `reconciling`.
- [x] 3.3 Return from the retry helper before pending-task mutation or aggregation synthesis, preserving full-cohort restoration.
- [x] 3.4 Flip the two existing collapse tests into positive defer oracles: no failed task stamp, no downstream stage, no attempt N+1, cycle `reconciling`.
- [x] 3.5 For nested `reconcile_unverified`, prove no second partial/failed cycle-status write lands beyond the nested producer's existing write/event.
- [x] 3.6 Preserve duplicate-skip, `submission_failed`, ordinary partial retry success/failure, and unknown fail-closed behavior.

## 4. Red Proof and Evidence Floor

- [x] 4.1 Produce one batched pre-change red run for the Part A truth table/artifact tests and Part B nested defer tests; leave no `red-proof` stash.
- [x] 4.2 `uv run pytest -q tests/test_orchestration_chain.py tests/test_production_readiness_validation.py tests/test_production_scheduler.py` passes.
- [x] 4.3 `uv run ruff check .` passes.
- [x] 4.4 `openspec validate scheduler-reconciliation-pending-partial --strict --no-interactive` passes.

## 5. Scope and Oracle Integrity

- [x] 5.1 Confirm no production-status alias, DB schema, Slurm gateway, reserve-gate, forcing-ready-partial, or public API change entered the diff.
- [x] 5.2 Confirm no existing test/spec/CI oracle was weakened; former collapse tests are replaced only because the governed contract explicitly changes.
- [x] 5.3 Record inspected unchanged sibling surfaces and state `No deviations` when applicable.
