## MODIFIED Requirements

### Requirement: Basins registry-import tests remain complete under physical partitioning

The repository SHALL keep the Basins registry-import pytest corpus at exactly seven
collectible modules and one non-collectible helper whose individual line counts are below
1,000, without changing `.large-file-guard.json` or retaining a collectible compatibility
shim. Every case in the frozen `3c29698f…` baseline SHALL remain collected exactly once: all
96 unique `::test_name[param-id]` suffixes and all 94 test definitions, decorators, fixture
arguments, parameter values/IDs, assertions, skips, markers, and monkeypatch targets remain
equivalent, except definitions retired or rewritten through the tracked retirements ledger
`tests/fixtures/basins_registry_partition_retirements.json` (#2907). That ledger SHALL be the only
channel for removing or rewriting a frozen definition: the oracle file stays byte-identical, and every
guard compares the tree against the frozen oracle with the retirements ledger applied (a retired
definition and all of its frozen nodes removed, a rewritten definition compared against its ledgered
row with its frozen nodes and markers unchanged) and then united with the #2183 additions ledger.

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
- **THEN** the baseline yields exactly 96 unique sorted node suffixes and 94 normalized test
  definitions, with the retained core, parser, CLI, security, auth, DB, and QHH owners holding 18,
  13, 5, 20, 11, 5, and 22 test functions, and the post-partition side yields exactly that frozen set
  minus the ledgered retirements and united with the additions ledger, one-to-one (after #2907: 92
  suffixes, 90 definitions, QHH 18 functions, every other owner unchanged)
- **AND** default execution yields the frozen 78 passed and 18 skipped, and non-integration
  execution the frozen 78 passed, one skipped, and 17 deselected, each adjusted by the ledgered
  retirements (a retired non-integration node leaves `passed`, a retired integration node leaves
  `skipped` or `deselected`) and by the additions ledger (after #2907: 77 passed / 15 skipped and
  77 passed / 1 skipped / 14 deselected); the helper collects no tests
- **AND** the retained-core BUG-008 command passes exactly the two `output_segment_count`
  cases, and no retirement may name a retained-core definition
- **AND** effective counts are derived from the frozen oracle and the two ledgers, never restated
  as free literals, and frozen digests are compared on the reconstructed frozen set (observed minus
  additions, with ledgered retired entries restored).

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

- **WHEN** the frozen 17 registry integration suffixes, minus the ledgered retired integration
  nodes, and the CI `database:` block are evaluated
- **THEN** the frozen map of five auth cases to `tests/test_basins_registry_import_auth.py`, five
  DB cases to `tests/test_basins_registry_import_db.py`, and seven QHH/crosswalk cases to
  `tests/test_basins_registry_import_qhh.py` holds after subtracting the ledgered retirements
  (after #2907: 5 / 5 / 4)
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
  exactly 28 nodes minus the ledgered retired integration nodes
- **AND** every remaining node except the real-Basins smoke reports PASSED, including every
  non-retired registry-QHH node, the ledgered rewrite
  `test_registry_import_creates_idempotent_inactive_rows` and all eleven QHH-bootstrap nodes, while only the explicitly disabled real-Basins import smoke reports
  SKIPPED
- **AND** the temporary database and role are removed, production DB/display identity is
  unchanged, no real-Basins ingest gate is enabled for this combined selection, and no credential
  enters public evidence
- **AND** a ledgered rewrite of the real-Basins smoke is proven by a separate run of that single
  test on the same SHA and isolated database with the real-Basins gate enabled and `data/Basins`
  provided read-only, which reports it PASSED
- **AND** node-22 is not accessed for this database proof and remains DB-free.

#### Scenario: Structural guard and current commands match the maintained layout

- **WHEN** the eight registry test/helper outputs, selector routes, database filter, current
  validation commands, controlled helper transition and guard configuration are evaluated
- **THEN** every registry test/helper file is strictly below 1,000 lines, the guard remains
  enabled at 1,000 lines with no registry exclusion, and the issue change set contains no
  guard edit
- **AND** an ordinary commit passes the wired hook, live full-registry commands execute all
  seven suites, and the BUG-008 retained-core command remains valid
- **AND** historical and archived evidence remains unchanged; partition changes are limited to
  the controlled `_make_valid_model` fixture transition, oracle evidence, and ledgered
  retirements/rewrites, with no registry import SQL/schema/geometry/auth behaviour or Basins
  real-fixture byte change; deleting caller-less store read paths and id producers that no import
  path reaches (#2907) is not such a change.

#### Scenario: A ledgered retirement or rewrite passes every partition guard

- **WHEN** a frozen definition is deleted (or rewritten) and one retirements-ledger record names its
  issue, 40-hex base commit, owner, name, action, reason, frozen nodes and integration nodes (and, for
  a rewrite, the new 7-field row whose first field is the owner)
- **THEN** the collection, integration, owner-count, definitions/digests, execution and database-proof
  guards all pass, and the oracle file is byte-identical to its tracked content.

#### Scenario: An unledgered or malformed retirement stays RED by name

- **WHEN** a frozen definition is deleted or rewritten without a ledger record
- **THEN** the definitions guard fails and names `<partition>::<name>`
- **WHEN** a ledger record names a definition that is not a frozen oracle row or that belongs to the
  retained core, changes a rewrite row field other than source, AST or monkeypatch digest, lists nodes
  that differ from the frozen nodes, duplicates a name, overlaps an additions record, or carries a base
  commit that does not resolve, is not an ancestor of HEAD, or whose owner blob does not define the name
- **THEN** the ledger checker, which reads only the oracle, the two ledgers and git history,
  reports one named `ledger.<rule>` violation for that record
- **WHEN** a `retire` record names a definition the tree still defines, or a `rewrite` record's row
  differs from the tree's observed row
- **THEN** the definitions guard fails and names `<partition>::<name>`, printing the observed row
- **AND** the retirements ledger path routes targeted selection to the partition meta-suite, and
  removing that route makes the routing contract RED.
