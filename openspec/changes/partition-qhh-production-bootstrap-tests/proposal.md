## Why

Issue #1913 must retarget registry-helper imports in
`tests/test_qhh_production_bootstrap.py`, but the file is 2,278 lines on current
`master`, is not excluded from the 1,000-line commit guard, and the wired hook rejects
that required edit. Issue #1948 is the pure-structure prerequisite: partition the existing
QHH bootstrap corpus without changing a test oracle, production behavior, the guard or
any #1913 registry-import implementation.

## What Changes

- Partition the QHH production-bootstrap corpus into exactly three responsibility-focused
  collectible suites below 1,000 lines, retaining the historical path for preflight/parser
  cases and BUG-008's five QHH output-segment-count nodes.
- Move twelve support functions and four scheduler-readiness constants into one
  non-collectible `tests/qhh_production_bootstrap_helpers.py` owner.
- Keep all nine integration definitions and eleven integration nodes together in
  `tests/test_qhh_production_bootstrap_scheduler.py`; the retained and state suites own no
  integration node.
- Update targeted-CI owner and helper routing, replace the historical CI database literal
  with the scheduler owner, add the DB-support helper as a second exact trigger, and add
  collection/source/AST/route/database mutation guards.
- Update current scheduler compatibility commands to name the real owner while preserving
  BUG-008 and archived historical evidence byte-for-byte.
- Keep `.large-file-guard.json` outside the #1948 PR-visible change set; retain the
  frozen baseline blob digest as provenance, and add no QHH exclusion or hook bypass.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `orchestrator-structural-burndown`: require QHH production-bootstrap test partitioning to
  preserve all collection/oracle identities, helper consumers, targeted-CI ownership and
  real-database routing under the structural limit.

## Impact

This change affects only QHH bootstrap test physical ownership, one non-collectible helper,
selector metadata/meta-tests, the exact C/D CI database paths, current scheduler compatibility
commands and this OpenSpec fixture. It changes no production code, registry monolith,
SQL/schema/geometry/auth behavior, Basins fixture bytes, BUG-008 historical command,
frontend, Slurm or SHUD runtime. Node-22 is not applicable and remains DB-free.
