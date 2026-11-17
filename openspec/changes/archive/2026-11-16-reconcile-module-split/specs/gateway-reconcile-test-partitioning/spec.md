## MODIFIED Requirements

### Requirement: Gateway reconcile tests remain complete under physical partitioning

The repository SHALL partition the gateway-reconcile test corpus into collectible modules whose
individual line counts do not exceed the configured 1,000-line guard, without adding a guard
exemption or retaining a collectible compatibility shim. Every pre-partition pytest case SHALL be
collected exactly once after partitioning: the module prefix may change, but the complete
`::test_name[param-id]` suffix set, test bodies, decorators, fixtures, and assertion oracles MUST
remain equivalent.

When the gateway-reconcile corpus was partitioned (#1809), every partition module was within the guard and no exemption
was added. Several suites have since grown past 1,000 lines. Re-partitioning them under the same collection-identity
rules is tracked by #3177:
- `tests/test_gateway_reconcile_comment_accounting.py`
- `tests/test_gateway_reconcile_binding_provenance.py`
- `tests/test_gateway_reconcile_claimant_exclusivity.py`
- `tests/test_gateway_reconcile_file_cohort_identity.py`
- `tests/test_gateway_reconcile_inventory.py`

The only gateway-reconcile suites that `.large-file-guard.json` exempts SHALL be `comment_accounting` (exempted by #2674)
and the temporary exemptions for `binding_provenance`, `claimant_exclusivity` and `file_cohort_identity`. No other
gateway-reconcile exemption SHALL be added.

#### Scenario: Collection identity is preserved one-to-one

- **WHEN** the pre-partition and post-partition suites are collected with pytest
- **THEN** both yield 538 unique case suffixes after the first `::`, their sorted suffix sets are byte-identical, and every old suffix maps to exactly one new full node ID

#### Scenario: Every new module satisfies the guard

- **WHEN** the gateway-reconcile entries of `.large-file-guard.json` are listed
- **THEN** they are a subset of the four suites named above
- **AND** every gateway-reconcile module over 1,000 lines is one of the five suites named above

#### Scenario: The complete split suite preserves all oracles

- **WHEN** pytest runs all `tests/test_gateway_reconcile_*.py` modules together
- **THEN** all 538 cases pass without changed assertions, skipped cases, duplicate collection, or production-source modifications
