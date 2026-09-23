## MODIFIED Requirements

### Requirement: Basins registry-import tests remain complete under physical partitioning

The repository SHALL keep the Basins registry-import pytest corpus at exactly seven
collectible modules and one non-collectible helper whose individual line counts are below
1,000, without changing `.large-file-guard.json` or retaining a collectible compatibility
shim. Every case in the frozen `3c29698f…` baseline SHALL remain collected exactly once: all
96 unique `::test_name[param-id]` suffixes and all 94 test definitions, decorators, fixture
arguments, parameter values/IDs, assertions, skips, markers, and monkeypatch targets remain
equivalent.

The helper SHALL retain its exact 19-function, one-class, four-constant member inventory.
Issue #1903 may change only `_make_valid_model`'s `.sp.rivseg` synthetic row construction so
the declared segment block is complete and distributes rows deterministically over actual
reach IDs. That transition SHALL be independently bound to the merge-base
`27dc6aab…` helper blob. The tracked partition oracle may update only the
`_make_valid_model` source/AST row, helper aggregate/self digests, and explicit transition
metadata; every test-definition, owner, marker, consumer-route, database-authority, count,
and execution row remains frozen. A self-consistent whole-helper recapture without the
before-blob allowlist SHALL fail.

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

#### Scenario: Registry helper transition is independently bounded

- **WHEN** the current helper is compared to its merge-base `27dc6aab…` blob member by member
- **THEN** the same 19 functions, one class and four constants exist
- **AND** only `_make_valid_model` differs, only its `.sp.rivseg` row construction changes,
  and its signature, `.sp.riv` construction, shapefile construction, forcing fixture, and
  all other statements remain equivalent
- **AND** deleting the transition allowlist, changing another helper member, or regenerating
  every oracle digest around an unrelated helper mutation makes the proof RED.

#### Scenario: Helper ownership and two-level QHH consumption remain explicit

- **WHEN** imports of `tests/basins_registry_import_helpers.py` are derived from tracked
  module ASTs
- **THEN** the seven registry owners and the four publish-registry partitions
  `tests/test_publish_registry_{calibration_overrides,package_contexts,radiation_repair,refresh_lane}.py`
  are its eleven direct collectible importers
- **AND** `tests/qhh_production_bootstrap_helpers.py` and `tests/publish_registry_helpers.py` are its
  only non-collectible support importers, while QHH-bootstrap A, B, and C import that QHH helper rather than the registry
  helper directly
- **AND** the two D-to-registry imports remain the only QHH-helper transition, with all QHH
  rows, 66 nodes, owners, markers, and execution summaries unchanged and the QHH oracle
  self-digest valid
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
  auth, DB, QHH, and reingest united with the QHH helper and scheduler-owner paths
- **AND** parser, CLI, security, and QHH-bootstrap A/B paths are absent, and no broad registry
  glob substitutes for an exact path
- **AND** removing any one of the eight exact paths leaves that path unmatched while the
  other seven remain matched and unrelated future database patterns remain permitted.

#### Scenario: Node-27 executes all affected integration cases rather than skipping them

- **WHEN** the frozen final SHA is checked on node-27 with integration enabled against an
  isolated temporary database derived from node-27's local PostgreSQL `:55432`
- **THEN** the combined registry auth/DB/QHH and QHH-bootstrap scheduler selection contains
  exactly 28 nodes
- **AND** 27 nodes report PASSED, including all seven registry-QHH and all eleven
  QHH-bootstrap nodes, while only the explicitly disabled real-Basins import smoke reports
  SKIPPED
- **AND** the temporary database and role are removed, production DB/display identity is
  unchanged, no real-Basins ingest gate is enabled, and no credential enters public evidence
- **AND** node-22 is not accessed for this database proof and remains DB-free.

#### Scenario: Structural guard and current commands match the maintained layout

- **WHEN** the eight registry test/helper outputs, selector routes, database filter, current
  validation commands, controlled helper transition and guard configuration are evaluated
- **THEN** every registry test/helper file is strictly below 1,000 lines, the guard remains
  enabled at 1,000 lines with no registry exclusion, and the issue change set contains no
  guard edit
- **AND** an ordinary commit passes the wired hook, live full-registry commands execute all
  seven suites, and the BUG-008 retained-core command remains valid
- **AND** historical and archived evidence remains unchanged; current changes are limited
  to the controlled `_make_valid_model` fixture transition and oracle evidence, with no
  production registry SQL/schema/geometry/auth behavior or Basins real-fixture byte change.

### Requirement: Entropy audit enforcement and its corpus split without report drift

The repository SHALL split `scripts/governance/audit_repo_entropy.py` and
its former single test module (now the fifteen `tests/test_entropy_audit_*.py` partitions of
`ENTROPY_AUDIT_TESTS`) below 1,000 lines with shared definitions in
support modules. The audit's check identifiers, module heatmap keys, public function
set, exit codes, and the stable subset of `metadata` SHALL be unchanged. The stable
subset is exactly `schema_version`, `mode`, `check_family_count`,
`executed_check_families`, `skipped_path_families`, `max_scanned_text_file_bytes`,
`max_artifact_fingerprint_bytes` and `baseline_path`; the remaining `metadata` keys
are excluded because they embed wall-clock time, the absolute repository root, the
resolved comparison base ref, or counts of the audit's own tracked lines, all of
which drift between any two invocations. The split SHALL also land the two deferred #1809
items: five expected-command literals migrated to the `tests/test_gateway_reconcile_*.py`
glob and the two frozen pre-#1809 provenance lines removed from the compatibility
inventories. Verification-command literals in `_ScopedAgentContextConfig`, the scoped
`AGENTS.md` files, the governance inventories and the triage document SHALL name the
new partitions.

#### Scenario: the audit report is unchanged by its own split

- **WHEN** `--format json` runs immediately before and after the enforcement split,
  with no other repository change between the two runs
- **THEN** the eight stable `metadata` keys are key-for-key equal, the check-id set,
  the `module_heatmap` and `high_spread_patterns` key sets, and the public function
  set are equal, and the exit code is unchanged
- **AND** the full `findings` diff is empty, or every residual entry is attributed to
  a path this split created or removed and is enumerated in the pull request body;
  an unattributable residual is behavior drift and the split is reverted.

#### Scenario: the corpus collects identically and the deferred items land

- **WHEN** the entropy audit suites are collected after partitioning
- **THEN** the sorted suffix set equals the pre-split baseline of 410
- **AND** no frozen pre-#1809 guard literal remains in either compatibility
  inventory, and the five expected commands name the gateway-reconcile glob.
