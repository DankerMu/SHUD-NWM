## REMOVED Requirements

### Requirement: Retention tests remain complete under physical partitioning

**Reason**: Replaced by "Retention tests SHALL remain complete under physical partitioning": the selector scenarios of this requirement are cut (#3219), and OpenSpec refuses a MODIFIED block that drops a scenario, so the surviving text moves to a new header.
**Migration**: See the ADDED requirement "Retention tests SHALL remain complete under physical partitioning"; apart from the selector cuts its body and scenarios are unchanged.

### Requirement: Basins package publication tests remain complete under physical partitioning

**Reason**: Replaced by "Basins package publication tests SHALL remain complete under physical partitioning": the selector scenarios of this requirement are cut (#3219), and OpenSpec refuses a MODIFIED block that drops a scenario, so the surviving text moves to a new header.
**Migration**: See the ADDED requirement "Basins package publication tests SHALL remain complete under physical partitioning"; apart from the selector cuts its body and scenarios are unchanged.

### Requirement: QHH production-bootstrap tests remain complete under physical partitioning

**Reason**: The frozen partition-oracle fixtures and ledgers that enforced this requirement are deleted with the selector suite, their only reader (#3219, D4); the requirement is removed, not ported.
**Migration**: None; the partitioned test files stay and run in the full suite on every non-frontend pull request.

### Requirement: Basins registry-import tests remain complete under physical partitioning

**Reason**: The frozen partition-oracle fixtures and ledgers that enforced this requirement are deleted with the selector suite, their only reader (#3219, D4); the requirement is removed, not ported.
**Migration**: None; the partitioned test files stay and run in the full suite on every non-frontend pull request.

### Requirement: Copyback replay and retention mutex corpora partition without drift

**Reason**: Replaced by "Copyback replay and retention mutex corpora SHALL partition without drift": the selector scenarios of this requirement are cut (#3219), and OpenSpec refuses a MODIFIED block that drops a scenario, so the surviving text moves to a new header.
**Migration**: See the ADDED requirement "Copyback replay and retention mutex corpora SHALL partition without drift"; apart from the selector cuts its body and scenarios are unchanged.

### Requirement: Refresh-timer and cutover-flip corpora partition without drift

**Reason**: Replaced by "Refresh-timer and cutover-flip corpora SHALL partition without drift": the selector scenarios of this requirement are cut (#3219), and OpenSpec refuses a MODIFIED block that drops a scenario, so the surviving text moves to a new header.
**Migration**: See the ADDED requirement "Refresh-timer and cutover-flip corpora SHALL partition without drift"; apart from the selector cuts its body and scenarios are unchanged.

## MODIFIED Requirements

### Requirement: Scheduler registry refresh surfaces split behind stable CLI facades

The repository SHALL split the scheduler file-provider refresh script, the registry
publish script, and their two pytest corpora below 1,000 lines. Both historical script paths
SHALL remain executable facades: `python -m` entrypoints, argparse surface, help
output, environment variables, flags, exit codes, `schema_version` and audit field
semantics SHALL be equivalent to the pre-split owners.

#### Scenario: the CLI contract is byte-stable across the split

- **WHEN** `python -m scripts.scheduler_file_provider_refresh --help` and
  `python -m scripts.publish_scheduler_file_registry --help` run after the split
- **THEN** both exit zero and emit stdout byte-identical to the pre-split baseline.

#### Scenario: both refresh corpora collect identically

- **WHEN** the refresh and publish suites are collected after partitioning
- **THEN** the sorted suffix sets equal the pre-split baselines of 315 and 59.

### Requirement: Production ops runbook becomes an index over sub-runbooks

`docs/runbooks/current-production-ops.md` SHALL be split into
`docs/runbooks/production-ops/` sub-runbooks each below 1,000 lines, with the
historical path retained as an index landing page below 1,000 lines. Commands, jq
expressions and reproduction steps SHALL be preserved verbatim, and every existing
inbound reference and anchor SHALL still resolve.

#### Scenario: inbound links and anchors survive the split

- **WHEN** the references to `current-production-ops.md` are resolved after the split
- **THEN** every referencing file still resolves, the anchor
  `#311-pipeline-job-provenance-sidecar-and-recovery-2420` still resolves, and
  markdown lint passes.

## ADDED Requirements

### Requirement: Retention tests SHALL remain complete under physical partitioning

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

#### Scenario: the final exception is removed without replacement

- **WHEN** line counts and `.large-file-guard.json` are evaluated after partitioning
- **THEN** every collectible/helper file is below 1,000 lines,
  `tests/test_retention.py` is absent from exclusions, no replacement exclusion is
  added, and every unrelated exclusion and the threshold remain unchanged.

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

### Requirement: Copyback replay and retention mutex corpora SHALL partition without drift

The repository SHALL partition the scheduler state-index copyback replay suite and
the retention copyback-mutex corpus into collectible modules below 1,000 lines, with
shared definitions confined to non-collectible helpers and no collectible
compatibility shim. The retention copyback-mutex lane SHALL move to its own owner
module while `services/orchestrator/retention.py` retains the planner. Copyback
mutex semantics, budget semantics, and deletion selection SHALL be unchanged.

#### Scenario: collection identity survives partitioning

- **WHEN** the replay and mutex corpora are collected after partitioning
- **THEN** the sorted `::test_name[param-id]` suffix sets equal the pre-split
  baselines of 32 and 25 exactly, with no additions, losses, duplicates or skips
- **AND** assertions, decorators, fixtures and parameter IDs are unchanged.

### Requirement: Refresh-timer and cutover-flip corpora SHALL partition without drift

The repository SHALL partition the node-22 refresh-timer health suite into collectible
modules below 1,000 lines with shared definitions in non-collectible helpers.
The direct-grid display cutover-flip corpus that this requirement
also partitioned was deleted together with the station-flag flip hook by #3164, so its
partitions, its helper, its routing and its tracked-tree guard are retired with it.

#### Scenario: collection identity survives partitioning

- **WHEN** the refresh-timer corpus is collected after partitioning
- **THEN** the sorted `::test_name[param-id]` suffix set equals the pre-split
  baseline exactly, with no additions, losses, duplicates or skips.
