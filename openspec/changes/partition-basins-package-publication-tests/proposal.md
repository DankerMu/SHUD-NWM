## Why

Issue #1903 must add requirement-driven Basins package publication tests, but `tests/test_basins_package_publication.py` is 3,582 lines on current `master` and is not grandfathered by the 1,000-line commit guard. Issue #1912 is the pure-structure prerequisite: partition the existing corpus without changing one test oracle or carrying any #1903 behavior.

## What Changes

- Partition the publication corpus into six responsibility-focused collectible suites below 1,000 lines, retaining `tests/test_basins_package_publication.py` as the core publication/forcing suite.
- Move shared test-only imports, constants, builders and assertions into one non-collectible `tests/basins_package_helpers.py` owner; update the sole sibling importer.
- Update the `workers/model_registry/**` targeted-CI owner route and support-module route so production changes select all six partitions and helper-only changes select all six plus `tests/test_basins_package.py`.
- Add collection/AST/selector mutation guards and update current validation commands to execute the full partition set.
- Move the complete heading-bounded current M10 #147–#152 production-closure validation family into `docs/validation/production-closure.md`, allowing only the required six-file command and moved self-lint path edits; leave the six original heading texts/slugs as linked stubs in the root matrix so both current documents satisfy the same structural guard.
- Keep `.large-file-guard.json` byte-identical; add no exclusion or hook bypass.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `orchestrator-structural-burndown`: require Basins publication-test partitioning to preserve all collection/oracle identities, helper consumers and targeted-CI ownership under the structural limit.

## Impact

This change affects only publication-test physical ownership, a non-collectible helper, `tests/test_basins_package.py`'s helper import, selector metadata/meta-tests, current validation-matrix ownership and OpenSpec. It changes no production code, Basins fixture bytes, package/source identity, schema, forcing/calibration behavior, registry tests, database path filter, frontend, Slurm or SHUD runtime.
