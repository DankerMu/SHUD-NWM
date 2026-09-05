## Why

Issue #1903 needs a minimal synthetic registry-fixture update, but the current
`tests/test_basins_registry_import.py` is 3,931 lines and is not exempt from the 1,000-line
commit guard. Issue #1913 is the final pure-structure prerequisite: split this test corpus
without losing registry or QHH-bootstrap coverage after #1948 changed the latter from one
suite into three suites plus a helper.

## What Changes

- Partition the registry-import corpus into exactly seven responsibility-focused collectible
  suites below 1,000 lines, retaining the historical path as the real core and BUG-008 suite.
- Move all 19 support functions, one helper class and four private constants into one
  non-collectible `tests/basins_registry_import_helpers.py` authority.
- Retarget eight collectible direct importers and the QHH-bootstrap support helper to the new
  helper; route the three QHH-bootstrap suites explicitly because selector support rules are
  not transitively expanded.
- Add one seven-partition selector authority. Registry-helper-only changes select eleven
  collectible consumers plus the existing selector meta-guard rider.
- Expand the CI `database:` filter to the exact eight-path union of the six registry paths
  and #1948's QHH scheduler/helper paths.
- Update current validation commands while preserving the historical BUG-008 command and
  archived evidence.
- Keep `.large-file-guard.json` outside this change and add no exclusion or bypass.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `orchestrator-structural-burndown`: require registry-import test partitioning to preserve
  collection and oracle identity, direct/support helper boundaries, targeted-CI ownership,
  #1948 QHH routing, and node-27 integration execution under the structural limit.

## Impact

This change affects only registry-import test ownership, shared test support, two cross-test
consumer modules, selector metadata/meta-tests, the CI database path filter, current
validation commands, a frozen oracle, and OpenSpec. It changes no production registry code,
SQL, schema, geometry/auth semantics, Basins fixture bytes, frontend, Slurm, SHUD runtime, or
issue #1903 mapping behavior. Real-DB verification runs only on node-27 against its local
PostgreSQL `:55432`; node-22 remains DB-free and is not used.
