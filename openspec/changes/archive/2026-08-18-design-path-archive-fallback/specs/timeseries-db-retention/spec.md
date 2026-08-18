## ADDED Requirements

### Requirement: Byte-identity guard tests MUST survive openspec change archival

The H4/H6 byte-identity tests that read the tiering change's design.md SHALL
resolve the document from the pending change location first and fall back to
the archived change location, failing with an explicit dual-location message
when neither exists, so that archiving the change cannot turn the guards red.

#### Scenario: pending location preferred

WHEN the design.md exists in both the pending and archived locations
THEN the tests read the pending copy

#### Scenario: archived change still resolvable

WHEN the change has been archived and only
`openspec/changes/archive/<date>-tier-node27-timeseries-storage/design.md` exists
THEN the tests resolve the latest archived copy and keep running their assertions

#### Scenario: both locations missing fails loudly

WHEN neither location exists
THEN the tests fail with a message naming both searched locations instead of a bare FileNotFoundError
