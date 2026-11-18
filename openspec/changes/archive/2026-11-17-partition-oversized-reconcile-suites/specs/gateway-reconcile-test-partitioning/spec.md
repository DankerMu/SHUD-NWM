## REMOVED Requirements

### Requirement: Gateway reconcile tests remain complete under physical partitioning

**Reason**: The requirement carried a fixed 538-case count from #1809 and temporary exemptions for suites that grew
past the guard. #3177 re-partitions those suites and removes the exemptions, so both are obsolete.

**Migration**: Replaced by "Gateway reconcile tests stay within the line guard without exemptions", which keeps the
collection-identity rules without a hard count.

## ADDED Requirements

### Requirement: Gateway reconcile tests stay within the line guard without exemptions

The repository SHALL keep every collectible `tests/test_gateway_reconcile_*.py` module, and every gateway-reconcile
support module, at or below the configured 1,000-line guard. `.large-file-guard.json` SHALL contain no
gateway-reconcile entry, and no collectible compatibility shim SHALL remain. When a suite is re-partitioned, every
pre-partition pytest case SHALL be collected exactly once afterwards. The module prefix may change, but the complete
`::test_name[param-id]` suffix set, test bodies, decorators, fixtures and assertion oracles MUST stay equivalent. A
shared private helper SHALL move to a non-collectible support module, and no partition SHALL import another partition.

#### Scenario: Collection identity is preserved one-to-one

- **WHEN** a suite and its partitions are collected with pytest before and after a re-partition
- **THEN** the sorted unique case suffixes after the first `::` are byte-identical, and every old suffix maps to exactly
  one new full node ID

#### Scenario: Every gateway-reconcile module satisfies the guard

- **WHEN** the line counts of `tests/test_gateway_reconcile_*.py` and the gateway-reconcile support modules are listed
- **THEN** each is at most 1,000
- **AND** `.large-file-guard.json` lists none of them

#### Scenario: The complete split suite preserves all oracles

- **WHEN** pytest runs all `tests/test_gateway_reconcile_*.py` modules together
- **THEN** every case passes without changed assertions, skipped cases, duplicate collection or production-source
  modifications
