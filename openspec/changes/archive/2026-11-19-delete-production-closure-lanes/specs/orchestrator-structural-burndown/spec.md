## REMOVED Requirements

### Requirement: Object-store validation facade split preserves runtime and evidence contracts

**Reason**: The production object-store (`validate-object-store`) validation lane is deleted (#3221): it is no longer an acceptance mechanism and nothing in CI or on either node runs it.
**Migration**: None.

## MODIFIED Requirements

### Requirement: Basins package publication tests SHALL remain complete under physical partitioning

The repository SHALL retain every case from the frozen Basins package publication baseline
while extending the current corpus to exactly seven collectible modules and one
non-collectible helper, each below 1,000 lines, without changing
`.large-file-guard.json` or retaining a collectible compatibility shim. The six original
partitions SHALL still contain all 88 unique baseline `::test_name[param-id]` suffixes and
all 80 normalized baseline test bodies, decorators, fixture arguments, parameter values/IDs,
assertions, skips and monkeypatch targets exactly once. The seventh owner,
`tests/test_basins_package_publication_rivseg.py`, SHALL contain only additive issue #1903
mapping tests and SHALL import `tests.basins_package_helpers` at module scope.

#### Scenario: frozen collection identity and additive tests are both preserved

- **WHEN** the baseline monolith, the six baseline partitions, and the seven current suites
  are collected and fingerprinted
- **THEN** the six baseline owners yield exactly the frozen 88 unique suffixes and 80
  one-to-one normalized definitions without dropped, duplicated, renamed or weakened cases
- **AND** the seventh owner collects the finite issue #1903 scenario matrix exactly once
- **AND** executing all seven files preserves the baseline pass/skip semantics and adds real
  mapping assertions
- **AND** `tests/basins_package_helpers.py` collects no tests.

#### Scenario: structural guard and current validation commands match the extended layout

- **WHEN** the eight Python outputs, root/child current validation matrices, guard
  configuration and documentation authority are evaluated
- **THEN** every changed/new text source is strictly below 1,000 lines, the guard threshold
  and exclusion list are byte-identical, and an ordinary commit passes the hook
- **AND** the live M9 closeout, #148 regression and opt-in Basins smoke commands execute all
  seven suites, the moved real-smoke node still runs, and historical M9 result bullets plus
  archived evidence remain unchanged
- **AND** the structural diff changes no frozen baseline test definition, production
  database filter, or unrelated #1912 owner contract.
