## ADDED Requirements

### Requirement: Retention tests remain complete under physical partitioning

The repository SHALL partition the retention pytest corpus into four collectible
modules and at most one non-collectible local helper whose individual line counts
are below 1,000, without adding a guard exclusion or retaining a collectible
compatibility shim. Every pre-partition case SHALL be collected exactly once after
partitioning: the module prefix may change, but all 120 unique
`::test_name[param-id]` suffixes, test bodies, decorators, fixtures, parameter
values/IDs, monkeypatch targets, and assertion oracles MUST remain equivalent.
`tests/test_retention.py` SHALL remain the same-name suite for the production
retention owner.

#### Scenario: collection identity and oracles are preserved one-to-one

- **WHEN** the original monolith and all four post-partition files are collected
- **THEN** each side yields exactly 120 unique suffixes after the first `::`, their
  sorted suffix files are byte-identical, and every original test body/decorator
  fingerprint maps to exactly one post-partition definition
- **AND** running the four files passes all 120 cases without changed assertions,
  skips, duplicates, production-source modifications, or helper collection.

#### Scenario: targeted CI owns every retention partition

- **WHEN** targeted selection runs for `services/orchestrator/retention.py`
- **THEN** all four retention partitions and the previously selected independent
  retention-frontier suite are present
- **AND** removing any partition from the owner route makes the selector contract
  test fail.

#### Scenario: the final exception is removed without replacement

- **WHEN** line counts and `.large-file-guard.json` are evaluated after partitioning
- **THEN** every collectible/helper file is below 1,000 lines,
  `tests/test_retention.py` is absent from exclusions, no replacement exclusion is
  added, and every unrelated exclusion and the threshold remain unchanged.
