## MODIFIED Requirements

### Requirement: Gateway reconcile tests remain complete under physical partitioning

The repository SHALL partition the gateway-reconcile test corpus into collectible modules whose
individual line counts do not exceed the configured 1,000-line guard, without adding a guard
exemption or retaining a collectible compatibility shim. Every pre-partition pytest case SHALL be
collected exactly once after partitioning: the module prefix may change, but the complete
`::test_name[param-id]` suffix set, test bodies, decorators, fixtures, and assertion oracles MUST
remain equivalent.

Three gateway-reconcile suites have since grown past the guard:
- `tests/test_gateway_reconcile_binding_provenance.py`
- `tests/test_gateway_reconcile_claimant_exclusivity.py`
- `tests/test_gateway_reconcile_file_cohort_identity.py`

They carry a temporary `.large-file-guard.json` exemption until they are re-partitioned under the same
collection-identity rules (#3177). No other gateway-reconcile module SHALL be exempted.

#### Scenario: Collection identity is preserved one-to-one

- **WHEN** the pre-partition and post-partition suites are collected with pytest
- **THEN** both yield 538 unique case suffixes after the first `::`, their sorted suffix sets are byte-identical, and every old suffix maps to exactly one new full node ID

#### Scenario: Every new module satisfies the guard

- **WHEN** line counts are measured for every new collectible and non-collectible gateway-reconcile module
- **THEN** each count is at most 1,000 and `.large-file-guard.json` contains no gateway-reconcile exemption beyond the three
  grown suites named above

#### Scenario: The complete split suite preserves all oracles

- **WHEN** pytest runs all `tests/test_gateway_reconcile_*.py` modules together
- **THEN** all 538 cases pass without changed assertions, skipped cases, duplicate collection, or production-source modifications
