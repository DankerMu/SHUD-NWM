## 0. Risk and evidence contract

Fixture level: **expanded**. Repair intensity: **high**. Project profile: NHMS. Upstream suggested level and minimal mergeable slice: absent.

Selected packs: Concurrency/shared state/ordering; Resource limits/time bounds; Legacy compatibility/oracle integrity; Error handling/partial outcomes; test-owned File IO/lock cleanup; terminal receipt identity. Must preserve production `_LeaseHeartbeat`, real lease renewal/token takeover, expected `TerminalStateError`, terminal receipt publication, existing bounds, and zero production/config/dependency changes.

## 1. Contract tests first

- [x] 1.1 Add tracked direct-call injection against the shipping heartbeat test for an exception before the first successful renewal; prove the injected callable runs and the repaired parent names the cause before `heartbeat_seq`.
- [x] 1.2 Add tracked direct-call injection after the real first renewal and token replacement; prove a raised exception cannot false-green as the expected takeover-driven `lost=True` outcome.
- [x] 1.3 Add a control that injects `Exception` into `renew()` and proves unchanged production `_LeaseHeartbeat._run` maps it to `lost=True` without changing production code.
- [x] 1.4 Add tracked direct-call injections against the shipping supervisor test for finalizer and unexpected reader failures; prove each cause reaches the parent before empty/missing result symptoms.
- [x] 1.5 Keep expected `TerminalStateError` in `reader_result`, and prove all injections are non-vacuous by unique exception identity/call evidence while the normal shipping tests remain green.
- [x] 1.6 Capture bounded pre-fix RED for the four shipping paths: first-renew cause becomes sequence symptom; post-takeover cause false-greens; finalizer and reader causes become non-fatal thread warnings plus result symptoms.

## 2. Test-harness repair

- [x] 2.1 At the exact bound `lease.renew` seam used by the real heartbeat, record `BaseException` and re-raise so production retains its `Exception -> lost=True` mapping; inspect captured causes after each poll and before `heartbeat_seq`/`lost` assertions.
- [x] 2.2 Preserve the real normal renewal and stolen-token false-return takeover oracle; do not modify `_LeaseHeartbeat`, `FileSchedulerLease`, scheduler runtime, state fields, or polling bounds.
- [x] 2.3 Capture unexpected `BaseException` separately in the supervisor finalizer and reader workers, catching expected `TerminalStateError` first as the substantive reader result.
- [x] 2.4 Join/liveness-check both supervisor workers and release/close the owned file lock before surfacing captured failures; assert liveness/errors before finalizer/reader result and final-publication assertions.
- [x] 2.5 Keep the diff to the two target test modules and this OpenSpec fixture; do not edit production, selector, CI, dependencies, pytest warning/timeout config, DB, Slurm, or deployment behavior.

## 3. Verification

- [x] 3.1 The two original shipping tests and every new failure-injection/control node pass with the original substantive state/result assertions intact.
- [x] 3.2 `uv run pytest -q tests/test_production_scheduler.py` and `uv run pytest -q tests/test_node27_timeseries_compression_supervisor.py` pass.
- [ ] 3.3 Selector probes for both changed test files include their assertion suites plus `tests/test_select_ci_tests.py`; local selector half passes, while final-head PR CI must execute assertions rather than collect-only.
- [x] 3.4 `uv run ruff check .`, `git diff --check`, and `openspec validate fix-worker-poll-error-attribution --strict --no-interactive` pass.
- [ ] 3.5 Review and final-head evidence prove no warning-only unexpected worker path, no changed production mapping/domain outcome, no stale SHA, and same-SHA CI/Governance success.
- [x] 3.6 Node-27/node-22 live receipts are explicitly not applicable because this is test-only and changes no DB/display/Slurm/SHUD production behavior.

## 4. Non-goals and routing

- [x] 4.1 Do not reclassify these production-state polls under #1633's dedicated completion-sentinel contract or mechanically apply the stale issue-body `errors` prescription to production `_LeaseHeartbeat`.
- [x] 4.2 Do not adopt repository-wide warning-as-error, pytest-timeout, or arbitrary-thread cancellation policy; #1646 remains the owner.
- [x] 4.3 Audit sibling test-owned production-state polls for the same cause-first invariant and report matching out-of-scope hazards; no matching out-of-scope hazard was found, and implementation stayed within the two issue-owned tests.
