## MODIFIED Requirements

### Requirement: Refresh-timer and cutover-flip corpora partition without drift

The repository SHALL partition the node-22 refresh-timer health suite into collectible
modules below 1,000 lines with shared definitions in non-collectible helpers, SHALL route
every partition through an explicit `PathTestRule`, and SHALL guard the corpus's exact
tracked partition set. The direct-grid display cutover-flip corpus that this requirement
also partitioned was deleted together with the station-flag flip hook by #3164, so its
partitions, its helper, its routing and its tracked-tree guard are retired with it.

#### Scenario: collection identity survives partitioning

- **WHEN** the refresh-timer corpus is collected after partitioning
- **THEN** the sorted `::test_name[param-id]` suffix set equals the pre-split
  baseline exactly, with no additions, losses, duplicates or skips.

#### Scenario: a partition disappears from the tracked tree

- **WHEN** one partition file is removed or a compatibility shim is added
- **THEN** the tracked-tree guard in `tests/test_select_ci_tests.py` fails.
