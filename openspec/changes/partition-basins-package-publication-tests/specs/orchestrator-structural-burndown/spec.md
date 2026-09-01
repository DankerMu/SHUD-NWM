## ADDED Requirements

### Requirement: Basins package publication tests remain complete under physical partitioning

The repository SHALL partition the Basins package publication pytest corpus into exactly six collectible modules and one non-collectible helper whose individual line counts are below 1,000, without changing `.large-file-guard.json` or retaining a collectible compatibility shim. Every baseline case SHALL be collected exactly once after partitioning: module prefixes may change, but all 88 unique `::test_name[param-id]` suffixes and all 80 normalized test bodies, decorators, fixture arguments, parameter values/IDs, assertions, skips and monkeypatch targets MUST remain equivalent.

#### Scenario: collection identity and test oracles are preserved one-to-one

- **WHEN** the baseline monolith and all six post-partition suites are collected and fingerprinted
- **THEN** each side yields exactly 88 unique identical sorted node suffixes and 80 one-to-one normalized test definitions
- **AND** executing the six files produces the same pass/skip semantics without dropped, duplicated, renamed or weakened cases
- **AND** `tests/basins_package_helpers.py` collects no tests.

#### Scenario: production-owner selection reaches every publication partition

- **WHEN** targeted selection runs for a changed file under `workers/model_registry/**`
- **THEN** all six publication partitions remain in the existing model-registry owner set alongside its prior consumers
- **AND** removing any one of the six partition edges—including retained core, which is not same-name-derived from the production owner—makes the selector contract RED before the edge is restored.

#### Scenario: helper-only selection reaches every consumer

- **WHEN** only `tests/basins_package_helpers.py` changes
- **THEN** targeted selection includes exactly all six publication partitions and `tests/test_basins_package.py`, plus the selector's existing meta-guard rider
- **AND** each collectible partition imports the helper at module scope and the historical sibling helper import remains valid
- **AND** deleting any required helper-consumer edge makes the selector contract RED.

#### Scenario: structural guard and current validation commands match the new layout

- **WHEN** the seven Python outputs, root/child current validation matrices, guard configuration and documentation authority are evaluated
- **THEN** every changed/new text source is strictly below 1,000 lines, the guard threshold and exclusion list are byte-identical, and an ordinary commit passes the hook
- **AND** the heading-bounded baseline M10 #147–#152 family is preserved under `docs/validation/production-closure.md`, with changes limited to the six-file publication commands and moved self-lint paths
- **AND** all six original root heading texts and anchor slugs remain byte-identical as links resolving to the matching child headings, both post-split files are below 1,000 lines, and both paths are current validation authority
- **AND** the live M9 closeout, #148 regression and opt-in Basins smoke commands execute all six suites, the moved real-smoke node still runs, and historical M9 result bullets plus archived evidence remain unchanged
- **AND** the structural diff contains no production, registry-corpus, database-filter or #1903 mapping behavior.
