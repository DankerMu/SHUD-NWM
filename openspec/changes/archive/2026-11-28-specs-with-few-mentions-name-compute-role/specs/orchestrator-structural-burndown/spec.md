## MODIFIED Requirements

### Requirement: Refresh-timer and cutover-flip corpora SHALL partition without drift

The repository SHALL partition the refresh-timer health suite of the compute role into collectible
modules below 1,000 lines with shared definitions in non-collectible helpers.
The direct-grid display cutover-flip corpus that this requirement
also partitioned was deleted together with the station-flag flip hook by #3164, so its
partitions, its helper, its routing and its tracked-tree guard are retired with it.

#### Scenario: collection identity survives partitioning

- **WHEN** the refresh-timer corpus is collected after partitioning
- **THEN** the sorted `::test_name[param-id]` suffix set equals the pre-split
  baseline exactly, with no additions, losses, duplicates or skips.
