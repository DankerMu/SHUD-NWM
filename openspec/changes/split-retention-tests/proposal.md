## Why

`tests/test_retention.py` is 2,666 lines and remains the last temporary #1872
large-file exception. Its active retention, frontier, extra-root, root-admission,
overlap, and no-follow regression corpus must be partitioned without losing a
test or making targeted CI blind to moved cases.

## What Changes

- Partition the retention corpus into four collectible files below 1,000 lines,
  retaining `tests/test_retention.py` as the same-name production-owner suite.
- Move shared test-only constants/helpers into one non-collectible local module;
  preserve every test function body, decorator, parameter ID, fixture binding,
  assertion and 120-case node-ID suffix exactly once.
- Update the existing `services/orchestrator/**` selector owner set and its
  independent frozen/floor tests so a retention production change selects all
  four partitions plus its prior consumers.
- Remove only `tests/test_retention.py` from the large-file exclusions, add no
  replacement exclusion, and update the active scheduler compatibility inventory.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `orchestrator-structural-burndown`: require retention-test physical partitioning
  to preserve collection/oracle identity, targeted-CI ownership, and the 1,000-line
  structural guard without a compatibility shim.

## Impact

This change touches retention test layout, a test-only helper, CI test-selection
metadata/tests, one exact guard exclusion, and active governance documentation.
It changes no production source, runtime behavior, public API, CLI/env value,
database, frontend/display, or Slurm contract. Parent #1872 closes only after this
second child and its post-merge closure land.
