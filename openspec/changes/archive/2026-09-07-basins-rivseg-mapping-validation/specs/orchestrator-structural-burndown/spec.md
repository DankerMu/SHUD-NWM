## MODIFIED Requirements

### Requirement: Object-store validation facade split preserves runtime and evidence contracts

The repository SHALL keep
`services.production_closure.object_store_validation` split into the eight responsibility
owners defined by its structural change. The historical facade and every owner MUST remain
below 1,000 lines while `.large-file-guard.json` remains byte-identical. Every pre-split
facade attribute, callable signature and dataclass shape MUST remain available: eight
callable seams use runtime facade forwarding, other helpers use plain re-export, and
module/class attribute patches observe the same authority objects. Standalone and packaged
CLI behavior, result/blocker/redaction, path safety, runtime staging and cleanup remain
equivalent.

Issue #1903 SHALL make one controlled synthetic-fixture transition: only
`alias-a.sp.riv` and `alias-a.sp.rivseg` change from their merge-base placeholder/truncated
bytes to a complete valid two-reach/two-segment mapping. Every other fixture path, text byte,
shapefile schema/record/geometry byte, function signature, and facade identity SHALL remain
unchanged from merge-base `27dc6aab…`. Package/manifest/checksum identity changes only as a
deterministic consequence of those two replacement payloads; it SHALL NOT be hand-pinned to
unrelated new material. Tests SHALL retain the old two mapping hashes as before-blob
provenance and pin the two exact replacement hashes plus all unchanged hashes.

#### Scenario: existing callers import and validate through the facade

- **WHEN** `slurm_validation`, readiness consumers or an existing test imports the
  historical module and validates the deterministic object-store fixture
- **THEN** all baseline names, signatures, dataclasses and class identities resolve
  without an import cycle
- **AND** evidence files, staged receipts, blocker ordering, redacted summary and object
  effects retain baseline behavior
- **AND** package/manifest/checksum movement is fully explained by the two controlled
  mapping payloads and no other fixture byte.

#### Scenario: synthetic fixture mapping transition is exact

- **WHEN** the fixture generated at merge-base `27dc6aab…` is compared to the issue #1903
  fixture
- **THEN** the old `.sp.riv` hash
  `debf08491b0e22a39c06502d0354b9ae14c169fbd581d2941b78ac61f7907863` and old
  `.sp.rivseg` hash
  `4eaccab2a297cdd5d09f13f193cb73c9048996dab9423340ce4092383188cb0f` are the only
  replaced stable-text hashes
- **AND** the replacement declares two reach rows with actual Index values `{1, 2}` and two
  segment rows whose `iRiv` values are `{1, 2}`
- **AND** an unrelated fixture-byte mutation makes the controlled-transition proof RED.

#### Scenario: a historical dynamic dependency is patched

- **WHEN** a test patches one of the eight governed facade callables and invokes the
  existing high-level writer, package, verification, registry, staging or CLI path
- **THEN** the direct/transitive coordinator forwards the facade's current runtime
  binding rather than a stale leaf-local callable
- **AND** shared module/class patches retain identity and the existing biting failure
  or output oracle proves the real call path observed the patch.

#### Scenario: unsafe or stale validation input retains fail-closed behavior

- **WHEN** existing symlink, ancestor-swap, non-regular, oversized, stale-workspace,
  tampered-after-verify, collision or pre-existing-object fixtures are validated
- **THEN** the same typed error or blocker and redacted evidence boundary occurs
- **AND** no external path, prior object or cleanup target not created by this run is
  written, replaced or deleted.

#### Scenario: standalone and packaged CLI contracts remain stable

- **WHEN** operators invoke either
  `python -m services.production_closure.object_store_validation` or
  `nhms-production validate-object-store`
- **THEN** click/argparse option, exit-code, stdout/stderr and redaction behavior remains
  identical, including usage failures without a traceback
- **AND** the existing production importer still resolves the historical public types
  and `validate_object_store` entrypoint.

#### Scenario: structural guard evaluates the finite owner set

- **WHEN** the eight production files and guard configuration are evaluated
- **THEN** each file is strictly below 1,000 lines, no ninth owner or replacement
  exclusion exists, the guard digest is unchanged, and a normal verified commit passes
  the hook.

### Requirement: Basins package publication tests remain complete under physical partitioning

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

#### Scenario: production-owner selection reaches every publication partition

- **WHEN** targeted selection runs for a changed file under `workers/model_registry/**`
- **THEN** all seven publication partitions remain in the existing model-registry owner set
  alongside its prior consumers
- **AND** removing any one of the seven partition edges—including retained core, which is
  not same-name-derived from the production owner—makes the selector contract RED before
  the edge is restored.

#### Scenario: helper-only selection reaches every consumer

- **WHEN** only `tests/basins_package_helpers.py` changes
- **THEN** targeted selection includes exactly all seven publication partitions and
  `tests/test_basins_package.py`, plus the selector's existing meta-guard rider
- **AND** each collectible partition imports the helper at module scope and the historical
  sibling helper import remains valid
- **AND** deleting any of the eight required helper-consumer edges makes the selector
  contract RED.

#### Scenario: structural guard and current validation commands match the extended layout

- **WHEN** the eight Python outputs, root/child current validation matrices, guard
  configuration and documentation authority are evaluated
- **THEN** every changed/new text source is strictly below 1,000 lines, the guard threshold
  and exclusion list are byte-identical, and an ordinary commit passes the hook
- **AND** the heading-bounded baseline M10 #147–#152 family remains under
  `docs/validation/production-closure.md`, with documentation changes limited to retargeting
  current publication commands to seven suites and the existing moved self-lint paths
- **AND** all six original root heading texts and anchor slugs remain byte-identical as links
  resolving to matching child headings, both post-split documentation files remain below
  1,000 lines, and both paths remain current validation authority
- **AND** the live M9 closeout, #148 regression and opt-in Basins smoke commands execute all
  seven suites, the moved real-smoke node still runs, and historical M9 result bullets plus
  archived evidence remain unchanged
- **AND** the structural diff changes no frozen baseline test definition, production
  database filter, or unrelated #1912 owner contract.

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
- **THEN** the seven registry owners and
  `tests/test_publish_scheduler_file_registry.py` are its eight direct collectible importers
- **AND** `tests/qhh_production_bootstrap_helpers.py` is its sole non-collectible support
  importer, while QHH-bootstrap A, B, and C import that QHH helper rather than the registry
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
