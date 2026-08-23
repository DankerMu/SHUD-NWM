## 0. Risk and evidence contract

Fixture level: **expanded**. Repair intensity: **high**. Project profile: NHMS. Upstream suggested level and minimal mergeable slice: absent.

Selected packs: Config/project setup; Concurrency/shared state/ordering; Resource/time bounds; Legacy/oracle integrity; Error handling/partial outcomes; Release/dependency compatibility; narrow contract documentation. Must preserve local harness bounds, marker expressions, unrelated warning behavior, CI job limits and zero production changes.

## 1. Contract tests first

- [x] 1.1 Add a tracked shipping-config subprocess test: one joined worker raises a unique RuntimeError and repository pytest exits nonzero naming the cause plus `PytestUnhandledThreadExceptionWarning`.
- [x] 1.2 Add a source-derived mutant removing only the exact filter; the same subprocess exits zero with `1 passed` and the thread warning, proving the policy test bites semantically.
- [x] 1.3 Add an unrelated `UserWarning` control that remains a passing warning; reject broad all-warning escalation.
- [x] 1.4 Parse repository TOML/lock state to pin the exact category, no `pytest-timeout` dependency/package, and no timeout keys/method/addopts.
- [x] 1.5 Pin selector ownership for both `pyproject.toml` and `uv.lock`; removal of either policy/meta-guard route must red the selector tests while existing core-smoke ownership remains.
- [x] 1.6 Capture current-config RED/GREEN evidence: baseline `1 passed, 1 warning`; explicit exact `-W error` exits 1 with the unique worker cause. Exclude unrelated `/tmp/pyproject.toml` discovery by using repository `-c`.
- [x] 1.7 Audit final-head tracked tests and `conftest.py` files for explicit warning-filter overrides; none exist outside the policy proof. Record that ordinary pytest/Python code can intentionally override ini warning policy and reject an incomplete source-language analyzer as a false security boundary.

## 2. Warning policy implementation

- [x] 2.1 Add exactly `error::pytest.PytestUnhandledThreadExceptionWarning` under repository `filterwarnings`; do not add broad `error`, custom hooks, or warning suppression.
- [x] 2.2 Add `tests/test_pytest_thread_exception_policy.py` with explicit config/environment isolation and bounded subprocesses; leave no child/thread/temp residue.
- [x] 2.3 Route both pytest config and dependency lock changes to the policy suite and selector meta-guard plus their existing core smoke; do not weaken changed-test self-selection.
- [x] 2.4 Update the stale #1633 spin-wait explanation and test docstring: global escalation is defense in depth, while local capture/ordering/cleanup/terminability remains mandatory.
- [x] 2.5 Keep production, marker expressions, CI job timeouts, unrelated warning policy and all existing substantive concurrency assertions unchanged.

## 3. Timeout decision

- [x] 3.1 Do not add `pytest-timeout` or global `timeout`/`timeout_method`/timeout addopts: #1632 is not duration evidence and #1671 is whole-job, not per-test/marker distribution evidence.
- [x] 3.2 Reject the issue's marker-only `@pytest.mark.timeout(N)` path for this change: it still adds the plugin, lacks calibrated per-test values/methods, and would duplicate known local bounds while leaving an incomplete subset falsely reassuring.
- [x] 3.3 Record method/lifecycle limits: signal mode does not force process exit; thread mode hard-exits and can lose teardown/reporting; a timer canceled after a passing test does not cover interpreter-shutdown strand.
- [x] 3.4 Preserve issue-owned Barrier/spin/poll/join/subprocess bounds and 35/45-minute CI job bounds as separate controls; do not claim warning escalation catches blocked workers.
- [x] 3.5 A future global or marker-only timeout requires concrete uncovered tests, per-test marker-lane distributions, explicit method choice, teardown/report acceptance, and post-test non-daemon/child-process coverage.

## 4. Verification

- [x] 4.1 Pre-change master-lane expression under exact warning escalation reached natural terminal state with zero unhandled-thread failures (`13382 passed`, `12 skipped`, `182 deselected`); the sole unrelated entropy test failure reproduces on clean master and remains routed to #1662/#1707 without suppression or scope creep.
- [x] 4.2 Policy suite and selector suite pass (`190 passed`); removed-filter mutant is green only for its expected false-green control and makes the tracked policy assertion red.
- [x] 4.3 All 21 directly threaded test files pass under exact warning escalation (`4703 passed`, `23 skipped`); collection records 23 integration tests across the two threaded integration suites and no `e2e`/`grib` tests in the 21-file set today.
- [x] 4.4 Node-27 explicit-opt-in real-DB integration at `2419026420b685bbd84fbc0346bd02cb66a5ed39` completed with `163 passed`, one expected real-Basins-data skip, `13617 deselected`, zero thread-warning failures, and zero leftover throwaway databases; node-22 is not applicable because Slurm/SHUD scheduling is unchanged.
- [x] 4.5 The full master-lane expression reached a natural terminal result with zero unhandled-thread failures (`13381 passed`, `12 skipped`, `182 deselected`, one unrelated `UserWarning`). Its 13 failures are baseline defects, not #1646 regressions: entropy remains linked to #1662/#1707, and 12 state-clone tests also reproduce under the unmodified baseline pytest config (`12 failed`, `15 passed`) because a fixed index timestamp crossed its 168-hour freshness window; #1743 owns that date-driven failure. Policy/selector suites, Ruff, strict OpenSpec and diff check pass.
- [x] 4.6 Final-head PR CI executed policy + selector assertions (`2838 passed`, not collect-only), Governance succeeded on the same SHA, and branch-tip/evidence gates passed before merge.

## 5. Non-goals and sibling audit

- [x] 5.1 Audit all current `PytestUnhandledThreadExceptionWarning` prose/tests and update only statements made false by the new global policy; the active requirement and one test docstring are updated, while archived pre-policy evidence remains historical.
- [x] 5.2 Do not fix #1671 full-job duration, #1632 umask marker coverage, or unrelated pre-existing thread harnesses that already satisfy local bounds.
- [x] 5.3 Report newly surfaced non-policy debt out of scope: #1662/#1707 retain the entropy failure and #1743 owns the date-driven state-clone failures; no unhandled-thread warning debt or timeout-policy prerequisite surfaced, and no governance/production/domain fix was added.
