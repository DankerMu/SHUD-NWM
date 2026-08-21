## 0. Risk and evidence contract

Fixture level: **expanded**. Repair intensity: **high**. Project profile: NHMS. Upstream suggested level and minimal mergeable slice: absent.

Selected packs: Concurrency/shared state/ordering; Resource limits/time bounds; Legacy compatibility/oracle integrity; Error handling/partial outcomes. All other core and NHMS domain packs are explicitly non-selected in `design.md`; no production/domain behavior changes.

Must preserve: the four named tests, Barrier participant counts (8, 2, 20, 40), real repository/provider operations, race release point, winner/loser/serialization/latest-history assertions, CI selector behavior, and zero production changes.

## 1. Contract tests first

- [x] 1.1 Add tracked failure-injection coverage for the `test_gateway_reconcile.py` explicit-thread harness: one worker raises before Barrier arrival; the original exception and bounded peer `BrokenBarrierError` path are observable; all started threads terminate.
- [x] 1.2 Add tracked failure-injection coverage for the `ThreadPoolExecutor` file-submit harness: pre-arrival constructor failure propagates through consumed futures/map, peers break bounded, and executor shutdown cannot strand the run.
- [x] 1.3 Add tracked failure-injection coverage for both `test_scheduler_file_provider_refresh.py` harness families, proving their existing error lists/bounded parent joins are insufficient without a Barrier bound and proving no peer remains alive after the repair.
- [x] 1.4 Add one bounded subprocess whole-run terminability proof against a real shipped harness seam. With the same pre-arrival failure still injected, the repaired subprocess exits after reporting the failure; an isolated mutant with the Barrier timeout removed reaches the parent's external timeout and is killed/reaped.
- [x] 1.5 Prove anti-vacuity for each injection seam: injected callable executes, mutation differs from source, and each removed timeout/error-propagation/liveness leg has a distinct red observable.
- [x] 1.6 Capture bounded pre-fix red evidence for all four issue-owned sites/families without leaving probe processes or threads behind.

## 2. Harness repair

- [x] 2.1 Bound the 8-party idempotency reservation Barrier, capture `BaseException`, use bounded joins, and assert worker errors/liveness before inspecting created/loser rows; close every session and verification session.
- [x] 2.2 Bound the 2-party file-submit Barrier and consume futures/results so repository-constructor, broken-barrier, and commit failures propagate; preserve `applied/collision`, job-id and inflight/unbound assertions.
- [x] 2.3 Bound the 20-party provider-lock Barrier while preserving explicit error capture, 20 real contenders, process-scoped-flock double, serialization and lock-cleanup assertions.
- [x] 2.4 Bound the 40-party receipt-publisher Barrier while preserving explicit error capture, 40 real publishes, exact newest-32 history, latest receipt and liveness assertions.
- [x] 2.5 Keep all four worker populations and substantive race assertions intact; do not replace real operations with mocks, reduce participants, serialize callers, weaken error assertions, or use daemon status as a cleanup substitute.
- [x] 2.6 Keep the diff to the two target test modules and this OpenSpec fixture; do not edit production, selector, CI, dependencies, `pyproject.toml`, #1646 policy, or #1648 tests.

## 3. Verification

- [x] 3.1 Focused original tests pass: the four exact node IDs retain their substantive outcomes and participant counts.
- [x] 3.2 New failure-injection/terminability/mutation tests pass under their external bounds and leave no process/thread residue.
- [x] 3.3 `uv run pytest -q tests/test_gateway_reconcile.py` passes within its existing runtime budget.
- [x] 3.4 `uv run pytest -q tests/test_scheduler_file_provider_refresh.py` passes within its existing runtime budget.
- [x] 3.5 `uv run ruff check .`, `git diff --check`, and `openspec validate fix-barrier-worker-strand-terminability --strict --no-interactive` pass.
- [ ] 3.6 Selector probes for changes to each test file include that assertion suite plus `tests/test_select_ci_tests.py` (local half complete); final-head PR CI must execute assertions (not collect-only) and Governance Audit must succeed on the same SHA.

## 4. Non-goals and routing

- [x] 4.1 Do not adopt global warning-as-error or pytest-timeout policy; #1646 remains the owner.
- [x] 4.2 Do not modify production-state polling diagnostics; #1648 remains the owner.
- [x] 4.3 Re-scan current Barrier sites after implementation and report newly discovered matching hazards; do not silently expand this PR beyond the four issue-owned sites.
