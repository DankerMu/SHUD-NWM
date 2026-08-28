## 1. Baseline Contract

- [x] 1.1 Capture the original explicit-file pytest collection as exactly 120
  unique sorted node suffixes and record all 87 test function AST/decorator/
  parameter fingerprints plus fixture/helper bindings.
- [x] 1.2 Run the original `tests/test_retention.py`; expect 120 passing cases, and
  capture the production-retention selector's prior target set.

## 2. Physical Test Partition

- [x] 2.1 Retain core tests in `tests/test_retention.py`; move pipeline-frontier,
  canonical/extra-root, and primary/overlap/no-follow sections into three named
  collectible modules, each below 1,000 lines.
- [x] 2.2 Move only genuinely shared constants/helpers into
  `tests/retention_test_helpers.py`; keep it non-collectible and import it at
  module scope from every consuming partition.
- [x] 2.3 Preserve all test function names, bodies, decorators, fixtures, parameter
  values/IDs, assertions and monkeypatch targets; pre/post suffixes and normalized
  per-test fingerprints SHALL be exactly equal with no duplicate.

## 3. Selector, Guard and Governance

- [x] 3.1 Extend the existing production retention owner route and independent
  selector pins/floor so `services/orchestrator/retention.py` selects all four
  partitions plus all previously selected targets.
- [x] 3.2 Prove deleting each new partition from the route makes the focused
  selector contract test red, then restore it and rerun green.
- [x] 3.3 Remove only `tests/test_retention.py` from `.large-file-guard.json`, add no
  replacement exclusion, keep threshold/unrelated entries unchanged, and prove
  every changed/new file is below 1,000 lines.
- [x] 3.4 Update the active scheduler compatibility inventory to name all current
  retention test consumers; leave archived historical paths unchanged.

## 4. Evidence Floor

- [x] 4.1 Run explicit four-file collect-only and pytest commands; expect 120
  unique identical suffixes and 120 passes, with the helper contributing no test.
- [x] 4.2 Run `tests/test_select_ci_tests.py` and entropy/large-file guard tests;
  expect all selector and structural assertions to pass.
- [x] 4.3 Run `uv run pytest -q tests/`; expect the complete repository suite to
  pass with no production source or test-oracle drift.
- [x] 4.4 Run ruff for every current tracked/new Python file, entropy audit,
  changed Markdown lint and `git diff --check`; expect zero new violations.
- [x] 4.5 Run `openspec validate split-retention-tests --strict
  --no-interactive`; expect strict validation success.
- [x] 4.6 Confirm the final PR changes only test layout/helper, selector metadata/
  tests, exact guard exclusion, active governance docs and OpenSpec; report every
  deviation explicitly.
