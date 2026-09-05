## ADDED Requirements

### Requirement: Basins registry-import tests remain complete under physical partitioning

The repository SHALL partition the Basins registry-import pytest corpus into exactly seven
collectible modules and one non-collectible helper whose individual line counts are below
1,000, without changing `.large-file-guard.json` or retaining a collectible compatibility
shim. Every case in the frozen `3c29698f…` baseline SHALL be collected exactly once after
partitioning: module prefixes may change, but all 96 unique `::test_name[param-id]` suffixes
and all 94 test definitions, decorators, fixture arguments, parameter values/IDs,
assertions, skips, markers, and monkeypatch targets MUST remain equivalent.

#### Scenario: Collection identity and test oracles are preserved one-to-one

- **WHEN** the baseline monolith and all seven post-partition suites are collected,
  fingerprinted, and executed without integration opt-in
- **THEN** each side yields exactly 96 unique identical sorted node suffixes and 94
  one-to-one normalized test definitions
- **AND** the retained core, parser, CLI, security, auth, DB, and QHH owners contain exactly
  18, 13, 5, 20, 11, 5, and 22 test functions respectively
- **AND** default execution yields 78 passed and 18 skipped, non-integration execution yields
  78 passed, one skipped, and 17 deselected, and the helper collects no tests
- **AND** the retained-core BUG-008 command passes exactly the two `output_segment_count`
  cases.

#### Scenario: Helper ownership and two-level QHH consumption remain explicit

- **WHEN** imports of `tests/basins_registry_import_helpers.py` are derived from tracked
  module ASTs
- **THEN** the seven registry owners and
  `tests/test_publish_scheduler_file_registry.py` are its eight direct collectible importers
- **AND** `tests/qhh_production_bootstrap_helpers.py` is its sole non-collectible support
  importer, while QHH-bootstrap A, B, and C import that QHH helper rather than the registry
  helper directly
- **AND** the two D-to-registry imports are the only controlled QHH-helper fingerprint
  transition, with all other QHH rows, 66 nodes, owners, markers, and execution summaries
  unchanged and the QHH oracle self-digest valid
- **AND** no collectible registry suite remains imported as a support module.

#### Scenario: Helper-only and production-owner selection reach every required suite

- **WHEN** only `tests/basins_registry_import_helpers.py` changes
- **THEN** targeted selection includes exactly the eight direct collectible importers plus
  all three QHH-bootstrap suites and the selector's existing meta-guard rider
- **AND** deleting any one of the eleven routed suite edges makes that suite absent before
  the edge is restored
- **WHEN** targeted selection runs for a changed `workers/model_registry/**` production file
  whose same-name derivation cannot supply a registry partition
- **THEN** all seven registry partitions remain in the existing model-registry owner set
  alongside all unrelated baseline targets
- **AND** removing any one of the seven registry-owner edges makes the contract RED without
  being rescued by same-name derivation.

#### Scenario: Integration markers remain bound to the exact combined database authority

- **WHEN** the 17 registry integration suffixes and the CI `database:` block are evaluated
- **THEN** exactly five auth cases map to `tests/test_basins_registry_import_auth.py`, five
  DB cases map to `tests/test_basins_registry_import_db.py`, and seven QHH/crosswalk cases
  map to `tests/test_basins_registry_import_qhh.py`
- **AND** the exact relevant database authority is the six registry paths for helper, core,
  auth, DB, QHH, and reingest united with #1948's QHH helper and scheduler-owner paths
- **AND** parser, CLI, security, and QHH-bootstrap A/B paths are absent, and no broad registry
  glob substitutes for an exact path
- **AND** removing any one of the eight exact paths leaves that path unmatched while the
  other seven remain matched and unrelated future database patterns remain permitted.

#### Scenario: Node-27 executes all ungated integration cases rather than skipping them

- **WHEN** the frozen final SHA is checked on node-27 with integration enabled against an
  isolated temporary database derived from node-27's local PostgreSQL `:55432`
- **THEN** the combined registry auth/DB/QHH and QHH-bootstrap scheduler selection contains
  exactly 28 nodes
- **AND** 27 nodes report PASSED, including all seven registry-QHH and all eleven
  QHH-bootstrap nodes, while only the explicitly disabled real-Basins import smoke reports
  SKIPPED
- **AND** the temporary database and role are removed, production DB/display identity is
  unchanged, no real-Basins ingest gate is enabled, and no credential enters public evidence
- **AND** node-22 is not accessed and remains DB-free.

#### Scenario: Structural guard and current commands match the new layout

- **WHEN** the eight registry test/helper outputs, selector routes, database filter, current
  validation commands, and guard configuration are evaluated
- **THEN** every replacement or new test/helper file is strictly below 1,000 lines, the
  current guard remains enabled at 1,000 lines with no registry exclusion, and the issue
  change set contains no guard edit
- **AND** an ordinary commit passes the wired hook, live full-registry commands execute all
  seven suites, and the BUG-008 retained-core command remains valid
- **AND** historical evidence remains unchanged and the structural diff contains no
  production SQL/schema/geometry/auth behavior, Basins fixture-byte change, or #1903 mapping
  behavior.
